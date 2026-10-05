"""The party: companions the engine moves, fights, wounds and heals.

`Player.party_members` was a column nobody moved. These tests pin that
joining is gated in code, that companions walk through the same door in
the same transaction, that nothing else can quietly pull one away, that
they fight beside the player at their own tier, and that going down is a
wound rest clears — never a death the engine decides.
"""

import pytest

from src.models import (
    Connection, DMState, Event, Location, LocationType, NPC, NPCRelationship, Player,
    WorldClock, get_session,
)
from src.services import party, standing
from src.services.combat import apply_combat_result, build_combat_start, combat_outcome_message
from src.services.rest import rest_now
from src.services.scene_objects import normalize_objects
from src.services.suggestions import suggest_actions
from src.services.travel import travel_now
from src.services.world_simulation import sim_move_npc
from src.tools.world_read.locations import get_npcs_at_location
from src.tools.world_read.player import get_player
from src.tools.world_write.npcs import move_npc
from src.tools.world_write.party import join_party, leave_party
from src.tools.world_write.player import move_player


def _world(session, *, trust=None, disposition=None, health="hurt"):
    session.add(WorldClock(day=1, hour=20, minute=0))
    session.add(DMState(tension="low"))
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True, visited=True)
    tavern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, depth=0, discovered=True,
                      objects=normalize_objects([{"kind": "rest", "label": "Upstairs bed", "x": 86, "y": 30}]),
                      rest={"kind": "inn", "price": 8, "quality": "good"})
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
    session.add_all([square, tavern, hollow])
    session.flush()
    session.add(Connection(from_location_id=square.id, to_location_id=tavern.id, travel_type="walk",
                           travel_time_hours=0.1, bidirectional=True, discovered=True))
    session.add(Connection(from_location_id=square.id, to_location_id=hollow.id, travel_type="walk",
                           travel_time_hours=1.0, bidirectional=True, discovered=True))
    piper = NPC(name="Piper Vell", profession="courier", current_location_id=square.id,
                description_physical="a wiry, quick young courier", danger_tier="weak")
    sorrel = NPC(name="Sorrel Tack", profession="merchant", current_location_id=square.id)
    wolf = NPC(name="Hollow Wolf", current_location_id=square.id, danger_tier="standard",
               description_physical="a lean grey wolf")
    session.add_all([piper, sorrel, wolf])
    player = Player(name="Rook", current_location_id=square.id, currency=50, health_status=health,
                    position_x=50, position_y=50)
    session.add(player)
    session.flush()
    if trust is not None or disposition is not None:
        session.add(NPCRelationship(npc_id=piper.id, player_id=player.id,
                                    current_disposition=disposition or "neutral",
                                    trust_level=50 if trust is None else trust))
    session.commit()
    return {"player": player.id, "piper": piper.id, "sorrel": sorrel.id, "wolf": wolf.id,
            "square": square.id, "tavern": tavern.id, "hollow": hollow.id}


def _party_ids(player_id):
    with get_session() as session:
        return list(session.get(Player, player_id).party_members or [])


def _npc(npc_id):
    with get_session() as session:
        n = session.get(NPC, npc_id)
        return {"loc": n.current_location_id, "x": n.position_x, "y": n.position_y,
                "condition": n.condition, "status": n.status}


# ----------------------------------------------------------------------
# Joining is the engine's decision
# ----------------------------------------------------------------------

def test_join_refusal_thresholds_live_in_standing():
    npc = NPC(name="Piper")
    assert standing.join_refusal(npc, None)["error"] == "trust"
    assert standing.join_refusal(npc, NPCRelationship(trust_level=standing.JOIN_TRUST - 1))["error"] == "trust"
    assert standing.join_refusal(npc, NPCRelationship(trust_level=standing.JOIN_TRUST)) is None
    assert standing.join_refusal(npc, NPCRelationship(trust_level=5, current_disposition="warm")) is None
    assert standing.join_refusal(npc, NPCRelationship(trust_level=99, current_disposition="hostile"))["error"] == "hostile"


def test_join_gates(db):
    with get_session() as session:
        w = _world(session, trust=10)
    # A near-stranger says no — and the tool tells the DM to play the refusal
    result = join_party(w["player"], w["piper"])
    assert result["error"] == "trust" and "instruction" in result
    assert _party_ids(w["player"]) == []

    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=w["piper"]).first()
        rel.trust_level = standing.JOIN_TRUST
        session.commit()
    result = join_party(w["player"], "piper vell")           # a name is tolerated
    assert result["success"] and result["party"] == ["Piper Vell"]
    assert _party_ids(w["player"]) == [w["piper"]]
    again = join_party(w["player"], w["piper"])          # the DM often repeats the NPC agent's call
    assert again["error"] == "already" and "Nothing to play" in again["instruction"]

    # Not here / dead / full
    with get_session() as session:
        away = NPC(name="Ghost", current_location_id=w["hollow"])
        dead = NPC(name="Corpse", current_location_id=w["square"], status="dead")
        session.add_all([away, dead])
        session.commit()
        away_id, dead_id = away.id, dead.id
    assert join_party(w["player"], away_id)["error"] == "not_here"
    assert join_party(w["player"], dead_id)["error"] == "dead"
    with get_session() as session:
        p = session.get(Player, w["player"])
        extra = [NPC(name=f"Hand {i}", current_location_id=w["square"]) for i in range(party.MAX_PARTY)]
        session.add_all(extra)
        session.flush()
        p.party_members = [w["piper"]] + [n.id for n in extra[:party.MAX_PARTY - 1]]
        session.commit()
        last = extra[-1].id
        session.add(NPCRelationship(npc_id=last, player_id=p.id, trust_level=90))
        session.commit()
    assert join_party(w["player"], last)["error"] == "full"


def test_leave_records_an_event(db):
    with get_session() as session:
        w = _world(session, trust=80)
    assert join_party(w["player"], w["piper"])["success"]
    result = leave_party(w["player"], w["piper"], reason="she has her own road")
    assert result["success"] and result["party"] == []
    assert _party_ids(w["player"]) == []
    with get_session() as session:
        ev = session.query(Event).filter(Event.name.like("%parts ways%")).first()
        assert ev is not None and w["piper"] in ev.npcs_involved and "own road" in ev.description
    assert leave_party(w["player"], w["piper"])["error"] == "not_member"


# ----------------------------------------------------------------------
# They walk through the same door
# ----------------------------------------------------------------------

def test_move_player_carries_the_party_and_settles_them_beside_the_player(db):
    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])
    result = move_player(w["player"], w["tavern"])
    assert result["success"] and result["party_followed"] == ["Piper Vell"]
    assert "never @npc-move them" in result["party_note"]
    with get_session() as session:
        p = session.get(Player, w["player"])
        n = _npc(w["piper"])
        assert n["loc"] == w["tavern"]
        assert abs(n["x"] - p.position_x) <= 8 and abs(n["y"] - p.position_y) <= 8
        # The one who stayed home stayed home
        assert session.get(NPC, w["sorrel"]).current_location_id == w["square"]


def test_canvas_travel_carries_the_party_too(db):
    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])
    result = travel_now(w["player"], w["tavern"])
    assert result["success"] and result["party"] == ["Piper Vell"]
    assert _npc(w["piper"])["loc"] == w["tavern"]


def test_nothing_else_pulls_a_companion_away_quietly(db):
    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])

    # The simulator is refused outright
    assert "refused" in sim_move_npc(w["piper"], w["hollow"])["error"]
    assert _npc(w["piper"])["loc"] == w["square"]

    # The DM's move_npc to the player's own location keeps them
    assert move_npc(w["piper"], w["square"]).get("party_left") is None
    assert _party_ids(w["player"]) == [w["piper"]]

    # ...and sending them elsewhere takes them out of the party, with a note
    result = move_npc(w["piper"], w["hollow"])
    assert result["success"] and result["party_left"] and "out" in result["note"]
    assert _party_ids(w["player"]) == []


def test_campaign_op_that_moves_a_companion_parts_them(db):
    from src.models.campaign import Campaign
    from src.services.campaign import execute_ops

    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])
    with get_session() as session:
        player = session.get(Player, w["player"])
        campaign = Campaign(player_id=player.id, title="T", status="active")
        session.add(campaign)
        session.flush()
        applied, _ = execute_ops(session, [{"op": "move_npc", "npc_id": w["piper"], "to": w["hollow"]}],
                                 campaign, player)
        session.commit()
    assert any("left the player's party" in line for line in applied)
    assert _party_ids(w["player"]) == []


def test_sync_drops_the_dead_and_says_so(db):
    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])
    with get_session() as session:
        session.get(NPC, w["piper"]).status = "dead"
        session.commit()
    with get_session() as session:
        player = session.get(Player, w["player"])
        gone = party.sync(session, player)
        session.commit()
    assert gone == [{"npc_id": w["piper"], "name": "Piper Vell", "reason": "dead"}]
    assert party.departure_lines(gone) == ["Piper Vell is dead and is no longer in the party."]
    assert _party_ids(w["player"]) == []


# ----------------------------------------------------------------------
# They fight beside the player, at their own tier
# ----------------------------------------------------------------------

def test_combat_roster_carries_fit_allies_and_never_a_companion_as_enemy(db):
    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])

    roster = build_combat_start([w["wolf"]], w["player"])
    assert roster["event"] == "combat_start"
    assert [a["name"] for a in roster["allies"]] == ["Piper Vell"]
    ally = roster["allies"][0]
    assert ally["tier"] == "weak" and ally["params"]["hp"] >= 1 and "damage" in ally["params"]
    assert "Piper Vell" in roster["instruction"]

    refused = build_combat_start([w["piper"]], w["player"])
    assert "@leave" in refused["error"] and "@fight" in refused["error"]

    # A wounded companion sits the fight out
    with get_session() as session:
        session.get(NPC, w["piper"]).condition = party.WOUNDED
        session.commit()
    assert build_combat_start([w["wolf"]], w["player"])["allies"] == []


def test_going_down_wounds_never_kills_and_the_outcome_says_so(db):
    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])
    changes = apply_combat_result(
        w["player"], "victory", hits_taken=1, defeated_npc_ids=[w["wolf"]],
        ally_npc_ids=[w["piper"], w["sorrel"]], downed_allies=[w["piper"], w["sorrel"]],
    )
    assert changes["allies_fought"] == ["Piper Vell"]         # Sorrel is not in the party
    assert changes["allies_wounded"] == ["Piper Vell"]
    n = _npc(w["piper"])
    assert n["status"] == "alive" and n["condition"] == party.WOUNDED
    assert _npc(w["sorrel"])["condition"] is None
    message = combat_outcome_message(changes)
    assert "WOUNDED" in message and "Piper Vell" in message and "never dead" in message
    assert _party_ids(w["player"]) == [w["piper"]]              # still in the party


def test_rest_heals_the_party(db):
    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])
    with get_session() as session:
        session.get(NPC, w["piper"]).condition = party.WOUNDED
        session.commit()
    move_player(w["player"], w["tavern"])
    result = rest_now(w["player"])
    assert result["success"] and result["party_healed"] == ["Piper Vell"]
    assert "on their feet" in result["summary"]
    assert _npc(w["piper"])["condition"] is None


# ----------------------------------------------------------------------
# Everyone can see who travels with whom
# ----------------------------------------------------------------------

def test_readers_mark_companions(db):
    from src.services.asset_manager import AssetManager

    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])
    here = {n["name"]: n for n in get_npcs_at_location(w["square"])}
    assert here["Piper Vell"]["in_party"] is True and here["Sorrel Tack"]["in_party"] is False
    assert get_player(w["player"])["party"] == [
        {"id": w["piper"], "name": "Piper Vell", "profession": "courier", "condition": None}
    ]
    scene = AssetManager._load_scene_data(None, w["square"], w["player"])
    rows = {n["name"]: n for n in scene["npcs"]}
    assert rows["Piper Vell"]["companion"] is True and rows["Hollow Wolf"]["companion"] is False
    with get_session() as session:
        payload = party.party_payload(session, session.get(Player, w["player"]))
    assert payload[0]["name"] == "Piper Vell" and payload[0]["here"] is True


def test_come_along_chip_only_when_the_engine_would_say_yes(db):
    with get_session() as session:
        w = _world(session, trust=10)
        player = session.get(Player, w["player"])
        labels = [c["label"] for c in suggest_actions(session, player, None, w["square"])]
        assert not any("come along" in l for l in labels)
        session.query(NPCRelationship).filter_by(npc_id=w["piper"]).first().trust_level = standing.JOIN_TRUST
        session.commit()
        chips = suggest_actions(session, player, None, w["square"])
        chip = next(c for c in chips if c["kind"] == "party")
        assert chip["label"] == "Ask Piper to come along" and chip["npc_id"] == w["piper"]
        player.party_members = [w["piper"]]
        session.commit()
        assert not any(c["kind"] == "party" for c in suggest_actions(session, player, None, w["square"]))


def test_party_commands_are_in_the_dms_language():
    """The DM joins and parts companions with @join / @leave (no tools since
    2026-10-04); both must run through the engine's party gate."""
    from src.services.commands import COMMANDS

    assert {"join", "leave"} <= set(COMMANDS)


def test_combat_start_sse_event_carries_the_allies(db):
    """The client spawns allies from the SSE event, so the roster must ride it."""
    from tests.test_audio_events import _message_with_tool_result, _notifications_of_type
    from src.web.streaming import ToolUsageTracker

    with get_session() as session:
        w = _world(session, trust=80)
    join_party(w["player"], w["piper"])
    roster = build_combat_start([w["wolf"]], w["player"])
    tracker = ToolUsageTracker()
    tracker.process_stream_payload(_message_with_tool_result(roster))
    events = _notifications_of_type(tracker, "combat_start")
    assert len(events) == 1
    assert [a["name"] for a in events[0]["allies"]] == ["Piper Vell"]
    assert events[0]["allies"][0]["params"]["hp"] >= 1
