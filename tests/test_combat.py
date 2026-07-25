"""Real-time combat integration: roster building + code-enforced outcomes.

The tier tables themselves are pinned by `test_enemy_tiers.py`; what is pinned
here is the wiring — that the payload the client fights with is resolved from
the DB row, that the fight's consequences (health, deaths, XP) are applied in
code BEFORE the DM narrates them, and that nothing mechanical leaks back out
as a number.
"""

from datetime import datetime, timedelta

import pytest

from src.models import (
    Cinematic,
    DMState,
    Event,
    Location,
    LocationType,
    NPC,
    Player,
    WorldClock,
    get_session,
)
from src.services import combat as combat_module
from src.services import cinematics as cine
from src.services.combat import (
    TIER_ROWS,
    apply_combat_result,
    build_combat_start,
    combat_outcome_message,
    combat_params,
    infer_archetype,
)


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
        # A clock is what makes the cinematic ledger's cooldowns computable
        session.add_all([player, bandit, faraway, DMState(tension="low"),
                         WorldClock(day=1, hour=8, minute=0)])
        session.commit()
        return {"player": player.id, "bandit": bandit.id,
                "faraway": faraway.id, "loc": loc.id}


def _add_npc(arena, name: str, **fields) -> str:
    """Another combatant standing in the square."""
    with get_session() as session:
        npc = NPC(name=name, current_location_id=arena["loc"], **fields)
        session.add(npc)
        session.commit()
        return npc.id


def _tension():
    with get_session() as session:
        return session.query(DMState).first().tension


def _player(arena) -> Player:
    with get_session() as session:
        return session.get(Player, arena["player"])


class FakeWallClock:
    """The cinematic ledger's real-time floor, under test control."""

    def __init__(self):
        self.t = datetime(2026, 7, 25, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += timedelta(minutes=minutes)


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


def test_start_combat_resolves_names(arena):
    # LLMs pass names instead of UUIDs — the tool copes (live playtest bug)
    result = build_combat_start(["Vex"], arena["player"])
    assert result.get("event") == "combat_start"
    assert result["enemies"][0]["npc_id"] == arena["bandit"]


def test_start_combat_rejects_absent_npc(arena):
    result = build_combat_start([arena["faraway"]], arena["player"])
    assert "error" in result and "not at the player's location" in result["error"]


# --- The payload the client fights with -----------------------------------


def test_combat_start_payload_carries_tier_and_params(arena):
    npc_id = _add_npc(arena, "Coldiron", danger_tier="dangerous",
                      combat_archetype="brute")
    enemy = build_combat_start([npc_id], arena["player"])["enemies"][0]
    assert enemy["tier"] == "dangerous"
    assert enemy["params"] == combat_params("dangerous", "brute")
    assert (enemy["params"]["hp"], enemy["params"]["damage"]) == (11, 2)
    assert enemy["boss"] is False and enemy["boss_title"] is None


def test_combat_start_tier_comes_from_the_row_not_the_caller(arena):
    """The DM has no difficulty dial: the tier is whatever the row says."""
    npc_id = _add_npc(arena, "Beggar Tom", danger_tier="harmless",
                      profession="dragon-slayer, allegedly")
    enemy = build_combat_start([npc_id], arena["player"])["enemies"][0]
    assert enemy["tier"] == "harmless"
    assert enemy["params"]["damage"] == 0 and enemy["params"]["flees"] is True


def test_combat_start_roster_names_tiers_in_words(arena):
    npc_id = _add_npc(arena, "Coldiron", danger_tier="deadly",
                      combat_archetype="stalker")
    result = build_combat_start([npc_id], arena["player"])
    assert result["roster"] == "Coldiron (deadly stalker)"
    assert "Coldiron (deadly stalker)" in result["instruction"]
    assert not any(ch.isdigit() for ch in result["instruction"])


def test_combat_start_without_a_boss_has_no_boss_key(arena):
    result = build_combat_start([arena["bandit"]], arena["player"])
    assert result["boss"] is None
    assert "cinematic" not in result
    assert _tension() == "high"


def test_combat_start_boss_sets_boss_key_and_climax_tension(arena):
    npc_id = _add_npc(arena, "Vharath", danger_tier="boss",
                      boss_title="the Ash-Wyrm", profession="wyrm")
    result = build_combat_start([npc_id], arena["player"])
    assert result["boss"] == {"npc_id": npc_id, "name": "Vharath",
                              "title": "the Ash-Wyrm"}
    assert result["enemies"][0]["boss"] is True
    assert result["enemies"][0]["params"]["hp"] == 24
    assert "bossPattern" in result["enemies"][0]["params"]
    assert _tension() == "climax"


def test_boss_title_falls_back_to_profession(arena):
    npc_id = _add_npc(arena, "Vharath", danger_tier="boss", profession="Ash-Wyrm")
    assert build_combat_start([npc_id], arena["player"])["boss"]["title"] == "Ash-Wyrm"


def test_boss_instruction_warns_about_the_entrance(arena):
    npc_id = _add_npc(arena, "Vharath", danger_tier="boss")
    instruction = build_combat_start([npc_id], arena["player"])["instruction"]
    assert "BOSS encounter" in instruction and "Vharath" in instruction
    assert "FREEZE-FRAMES" in instruction


# --- The entrance rides on the tool result (integration doc R7) -----------


def test_combat_start_attaches_the_boss_entrance(arena):
    npc_id = _add_npc(arena, "Vharath", danger_tier="boss",
                      boss_title="the Ash-Wyrm")
    result = build_combat_start([npc_id], arena["player"])
    assert result["cinematic"]["kind"] == "boss_intro"
    assert result["cinematic"]["title"] == "Vharath"
    assert result["cinematic"]["subtitle"] == "the Ash-Wyrm"
    assert result["cinematic"]["npc_id"] == npc_id
    with get_session() as session:
        row = session.query(Cinematic).filter(Cinematic.kind == "boss_intro").one()
        assert (row.status, row.source) == ("played", "auto")


def test_boss_rematch_starts_the_fight_without_a_second_entrance(arena, monkeypatch):
    """One entrance per boss, ever — the fight itself still starts normally."""
    clock = FakeWallClock()
    monkeypatch.setattr(cine, "_now", clock)
    npc_id = _add_npc(arena, "Vharath", danger_tier="boss")

    assert "cinematic" in build_combat_start([npc_id], arena["player"])
    clock.advance(minutes=120)      # far past every real-time budget

    again = build_combat_start([npc_id], arena["player"])
    assert "cinematic" not in again
    assert again["event"] == "combat_start" and again["boss"]["name"] == "Vharath"


# --- Health severity (integration doc R8) ---------------------------------


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


def test_hearts_lost_maps_health_more_severely_than_hits(arena):
    """One boss slam is three hearts — "one hit" understates a mauling."""
    changes = apply_combat_result(arena["player"], "victory", hits_taken=1, hearts_lost=3)
    assert changes["health"] == "badly_hurt"


def test_hearts_lost_absent_falls_back_to_hits(arena):
    changes = apply_combat_result(arena["player"], "victory", hits_taken=2, hearts_lost=0)
    assert changes["health"] == "hurt"


def test_hearts_healed_softens_the_floor(arena):
    changes = apply_combat_result(arena["player"], "victory", hits_taken=3, hearts_healed=2)
    assert changes["health"] == "winded"


def test_hearts_healed_can_erase_the_wound_entirely(arena):
    changes = apply_combat_result(arena["player"], "victory", hits_taken=2,
                                  hearts_lost=2, hearts_healed=5)
    assert changes["health"] is None
    with get_session() as session:
        assert session.get(Player, arena["player"]).health_status == "healthy"


def test_defeat_means_critical_not_dead(arena):
    changes = apply_combat_result(arena["player"], "defeat", hits_taken=5)
    assert changes["health"] == "critical"
    assert _tension() == "climax"
    with get_session() as session:
        assert session.get(Player, arena["player"]).health_status == "critical"


def test_hearts_healed_cannot_rescue_a_defeat(arena):
    changes = apply_combat_result(arena["player"], "defeat", hits_taken=1, hearts_healed=9)
    assert changes["health"] == "critical"


def test_defeated_npcs_die_with_world_event(arena):
    changes = apply_combat_result(
        arena["player"], "victory", hits_taken=0, defeated_npc_ids=[arena["bandit"]]
    )
    assert changes["npc_deaths"][0]["npc_name"] == "Vex"
    with get_session() as session:
        assert session.get(NPC, arena["bandit"]).status == "dead"
        deaths = session.query(Event).filter(Event.name == "Death of Vex").all()
        assert len(deaths) == 1 and deaths[0].player_witnessed


# --- XP: re-read from the DB, never from the client -----------------------


def test_xp_awarded_uses_db_tier(arena):
    npc_id = _add_npc(arena, "Coldiron", danger_tier="deadly")
    changes = apply_combat_result(arena["player"], "victory", defeated_npc_ids=[npc_id])
    assert changes["xp_awarded"] == TIER_ROWS["deadly"]["xp"] == 100
    assert changes["xp_breakdown"] == [
        {"npc_name": "Coldiron", "tier": "deadly", "xp": 100}
    ]
    assert _player(arena).xp == 100


def test_xp_ignores_the_inference_when_the_row_is_authored(arena):
    """A "dragon" the author called weak is worth exactly weak."""
    npc_id = _add_npc(arena, "Old Scale", danger_tier="weak",
                      description_physical="an enormous dragon, a titan of legend")
    changes = apply_combat_result(arena["player"], "victory", defeated_npc_ids=[npc_id])
    assert changes["xp_awarded"] == TIER_ROWS["weak"]["xp"] == 5


def test_xp_for_harmless_is_zero(arena):
    npc_id = _add_npc(arena, "Beggar Tom", danger_tier="harmless")
    changes = apply_combat_result(arena["player"], "victory", defeated_npc_ids=[npc_id])
    assert changes["xp_awarded"] == 0
    assert changes["xp_breakdown"] == [
        {"npc_name": "Beggar Tom", "tier": "harmless", "xp": 0}
    ]
    assert changes["level_up"] is False and "progression" not in changes
    assert _player(arena).xp in (0, None)


def test_xp_not_awarded_twice_for_same_corpse(arena):
    npc_id = _add_npc(arena, "Coldiron", danger_tier="dangerous")
    first = apply_combat_result(arena["player"], "victory", defeated_npc_ids=[npc_id])
    second = apply_combat_result(arena["player"], "victory", defeated_npc_ids=[npc_id])
    assert first["xp_awarded"] == 40
    assert second["xp_awarded"] == 0 and second["xp_breakdown"] == []
    assert second["npc_deaths"] == []
    assert _player(arena).xp == 40


def test_grant_xp_is_called_once_with_the_summed_total(arena, monkeypatch):
    calls = []

    def fake_grant_xp(session, player, amount, source):
        calls.append((amount, source))
        return {"leveled_up": False, "level": 1}

    monkeypatch.setattr(combat_module, "grant_xp", fake_grant_xp)
    ids = [_add_npc(arena, "Coldiron", danger_tier="dangerous"),
           _add_npc(arena, "Watchman", danger_tier="standard")]
    changes = apply_combat_result(arena["player"], "victory", defeated_npc_ids=ids)
    assert calls == [(55, "combat")]
    assert changes["xp_awarded"] == 55


def test_level_up_rides_back_on_the_result(arena):
    npc_id = _add_npc(arena, "Vharath", danger_tier="boss")
    changes = apply_combat_result(arena["player"], "victory", defeated_npc_ids=[npc_id])
    assert changes["xp_awarded"] == 300
    assert changes["level_up"] is True
    assert changes["new_level"] == changes["progression"]["level"] > 1
    assert changes["progression"]["source"] == "combat"
    assert _player(arena).level == changes["new_level"]


def test_a_dead_player_earns_nothing(arena):
    with get_session() as session:
        session.get(Player, arena["player"]).health_status = "dead"
        session.commit()
    npc_id = _add_npc(arena, "Coldiron", danger_tier="deadly")
    changes = apply_combat_result(arena["player"], "defeat", defeated_npc_ids=[npc_id])
    assert changes["xp_awarded"] == 100        # reported...
    assert "progression" not in changes        # ...but never banked
    assert _player(arena).xp in (0, None)


def test_kills_still_pay_when_the_fight_was_lost(arena):
    npc_id = _add_npc(arena, "Coldiron", danger_tier="standard")
    changes = apply_combat_result(arena["player"], "defeat", defeated_npc_ids=[npc_id])
    assert changes["xp_awarded"] == 15 and _player(arena).xp == 15


# --- The engine tells the next turn what it already did -------------------


def test_combat_result_records_its_mutations_in_the_ledger(arena):
    from src.web import pending_actions

    npc_id = _add_npc(arena, "Coldiron", danger_tier="dangerous")
    pending_actions.clear(arena["player"])
    apply_combat_result(arena["player"], "victory", hits_taken=2,
                        defeated_npc_ids=[npc_id])
    drained = pending_actions.drain(arena["player"])
    assert [a["kind"] for a in drained] == ["combat"]
    assert "Coldiron" in drained[0]["summary"]
    # Fed to audit_turn(code_actions=...) so the aftermath turn isn't flagged
    assert pending_actions.tool_equivalents(drained) >= {"update_player_health", "kill_npc"}


# --- What the DM is told: labels, never numbers ---------------------------


def test_combat_outcome_message_names_tiers_without_numbers(arena):
    npc_id = _add_npc(arena, "Coldiron", danger_tier="dangerous")
    changes = apply_combat_result(arena["player"], "victory", hits_taken=2,
                                  defeated_npc_ids=[npc_id])
    message = combat_outcome_message(changes)
    assert "Coldiron (dangerous)" in message
    assert "do NOT call kill_npc" in message
    assert "'hurt'" in message
    assert not any(ch.isdigit() for ch in message), "engine numbers must never reach the DM"


def test_combat_outcome_message_asks_for_oblique_growth(arena):
    npc_id = _add_npc(arena, "Vharath", danger_tier="boss")
    changes = apply_combat_result(arena["player"], "victory", defeated_npc_ids=[npc_id])
    message = combat_outcome_message(changes)
    assert "grown" in message and "Acknowledge it ONCE, obliquely" in message
    assert not any(ch.isdigit() for ch in message)


def test_combat_outcome_message_stays_quiet_about_growth_that_did_not_happen(arena):
    changes = apply_combat_result(arena["player"], "fled", hits_taken=1)
    message = combat_outcome_message(changes)
    assert "grown" not in message and "Killed:" not in message
