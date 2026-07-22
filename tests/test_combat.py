"""Real-time combat integration: roster building + code-enforced outcomes."""

import pytest

from src.models import DMState, Event, Location, LocationType, NPC, Player, get_session
from src.services.combat import apply_combat_result, build_combat_start, infer_archetype


@pytest.fixture()
def arena(db):
    with get_session() as session:
        loc = Location(name="Square", type=LocationType.DISTRICT)
        elsewhere = Location(name="Hollow", type=LocationType.POI)
        session.add_all([loc, elsewhere])
        session.flush()
        player = Player(name="Tester", current_location_id=loc.id)
        bandit = NPC(name="Vex", current_location_id=loc.id,
                     description_physical="a hulking massive brawler")
        faraway = NPC(name="Ghost", current_location_id=elsewhere.id)
        session.add_all([player, bandit, faraway, DMState(tension="low")])
        session.commit()
        return {"player": player.id, "bandit": bandit.id, "faraway": faraway.id}


def _tension():
    with get_session() as session:
        return session.query(DMState).first().tension


def test_infer_archetype_from_description(arena):
    with get_session() as session:
        assert infer_archetype(session.get(NPC, arena["bandit"])) == "brute"


def test_explicit_archetype_wins(arena):
    with get_session() as session:
        npc = session.get(NPC, arena["bandit"])
        npc.combat_archetype = "stalker"
        session.commit()
        assert infer_archetype(session.get(NPC, arena["bandit"])) == "stalker"


def test_start_combat_builds_roster_and_floors_tension(arena):
    result = build_combat_start([arena["bandit"]], arena["player"])
    assert result.get("event") == "combat_start"
    assert result["enemies"][0]["name"] == "Vex"
    assert result["enemies"][0]["archetype"] == "brute"
    assert _tension() == "high"


def test_start_combat_rejects_absent_npc(arena):
    result = build_combat_start([arena["faraway"]], arena["player"])
    assert "error" in result and "not at the player's location" in result["error"]


def test_victory_hits_map_to_health(arena):
    changes = apply_combat_result(arena["player"], "victory", hits_taken=2)
    assert changes["health"] == "hurt"
    with get_session() as session:
        assert session.get(Player, arena["player"]).health_status == "hurt"


def test_combat_never_heals(arena):
    with get_session() as session:
        session.get(Player, arena["player"]).health_status = "badly_hurt"
        session.commit()
    changes = apply_combat_result(arena["player"], "victory", hits_taken=1)
    assert changes["health"] is None
    with get_session() as session:
        assert session.get(Player, arena["player"]).health_status == "badly_hurt"


def test_defeat_means_critical_not_dead(arena):
    changes = apply_combat_result(arena["player"], "defeat", hits_taken=5)
    assert changes["health"] == "critical"
    assert _tension() == "climax"
    with get_session() as session:
        assert session.get(Player, arena["player"]).health_status == "critical"


def test_defeated_npcs_die_with_world_event(arena):
    changes = apply_combat_result(
        arena["player"], "victory", hits_taken=0, defeated_npc_ids=[arena["bandit"]]
    )
    assert changes["npc_deaths"][0]["npc_name"] == "Vex"
    with get_session() as session:
        assert session.get(NPC, arena["bandit"]).status == "dead"
        deaths = session.query(Event).filter(Event.name == "Death of Vex").all()
        assert len(deaths) == 1 and deaths[0].player_witnessed
