"""Structured objectives: the engine ticks them, the DM never un-ticks them.

The old objective was a sentence nobody could check. These tests pin the
contract of `src/services/objectives.py`: legacy strings still load, done
is sticky, and every engine path (arrival, talk, pickup, hand-over, kill)
completes exactly the objective aimed at it — without ever completing the
quest on its own.
"""

from src.models import (
    Connection, Location, LocationType, NPC, Player, Quest, QuestStatus, get_session,
)
from src.services import objectives as obj
from src.services.progression import QUEST_XP, xp_for_quest
from src.tools.world_write.items import transfer_item
from src.tools.world_write.npcs import kill_npc
from src.tools.world_write.player import add_to_inventory, move_player
from src.tools.world_write.quests import activate_quest, create_quest, update_quest_objectives
from src.web import pending_actions


# ----------------------------------------------------------------------
# World
# ----------------------------------------------------------------------

def _world(session):
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True)
    hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
    session.add_all([square, hollow])
    session.flush()
    session.add(Connection(
        from_location_id=square.id, to_location_id=hollow.id,
        travel_type="walk", travel_time_hours=0.5, bidirectional=True, discovered=True,
    ))
    brann = NPC(name="Captain Brann Coldiron", current_location_id=square.id)
    vex = NPC(name="Vex Marrow", current_location_id=hollow.id)
    session.add_all([brann, vex])
    player = Player(name="Rook", current_location_id=square.id, currency=50)
    session.add(player)
    session.commit()
    return {"player": player.id, "square": square.id, "hollow": hollow.id,
            "brann": brann.id, "vex": vex.id}


def _quest(session, world, objectives, status=QuestStatus.ACTIVE, title="Teeth on the North Road"):
    quest = Quest(title=title, description="", status=status,
                  objectives=obj.normalize_objectives(objectives, session=session))
    session.add(quest)
    session.flush()
    if status == QuestStatus.ACTIVE:
        player = session.get(Player, world["player"])
        player.active_quests = list(player.active_quests or []) + [quest.id]
    session.commit()
    return quest.id


def _objectives(quest_id):
    with get_session() as session:
        return obj.normalize_objectives(session.get(Quest, quest_id).objectives)


def _drain(world):
    events = obj.pop_pending_events(world["player"])
    tools = obj.pop_audit_tools(world["player"])
    pending_actions.clear(world["player"])
    return events, tools


# ----------------------------------------------------------------------
# Shape
# ----------------------------------------------------------------------

def test_legacy_strings_normalize_to_custom():
    out = obj.normalize_objectives(["Find the key", "[DONE] Talk to the guard", "~~Sleep~~"])
    assert [o["kind"] for o in out] == ["custom", "custom", "custom"]
    assert [o["done"] for o in out] == [False, True, True]
    assert out[1]["text"] == "Talk to the guard"
    assert out[2]["text"] == "Sleep"
    assert [o["id"] for o in out] == ["o1", "o2", "o3"]


def test_unknown_kind_and_garbage_degrade_gracefully():
    out = obj.normalize_objectives([{"text": "Do a thing", "kind": "teleport"}, {"kind": "go_to"}, 42, None])
    assert len(out) == 1
    assert out[0]["kind"] == "custom"


def test_unresolvable_target_is_dropped_but_text_survives(db):
    with get_session() as session:
        world = _world(session)
        out = obj.normalize_objectives(
            [{"text": "Find the ghost", "kind": "talk_to", "target_id": "no-such-npc"}], session=session
        )
    assert out[0]["text"] == "Find the ghost"
    assert out[0]["target_id"] is None


def test_target_names_resolve_to_ids(db):
    with get_session() as session:
        world = _world(session)
        out = obj.normalize_objectives([
            {"text": "Find Coldiron", "kind": "talk_to", "target_id": "Brann Coldiron"},
            {"text": "Reach the hollow", "kind": "go_to", "target_id": "wolf's hollow"},
        ], session=session)
    assert out[0]["target_id"] == world["brann"] and out[0]["target_type"] == "npc"
    assert out[0]["target_name"] == "Captain Brann Coldiron"
    assert out[1]["target_id"] == world["hollow"] and out[1]["target_type"] == "location"


def test_done_is_sticky_across_a_rewrite_that_omits_it(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, [
            {"text": "Get the gate pass", "kind": "obtain", "item_name": "gate pass", "done": True},
            {"text": "Reach the hollow", "kind": "go_to", "target_id": world["hollow"]},
        ])
    # The DM rewrites the list from memory: no done flag, no target id
    result = update_quest_objectives(quest_id, [
        {"text": "Get the gate pass", "kind": "obtain", "item_name": "gate pass"},
        {"text": "Reach the hollow", "kind": "go_to"},
        "Bring proof back",
    ])
    out = _objectives(quest_id)
    assert out[0]["done"] is True
    assert out[1]["target_id"] == world["hollow"], "a marker the rewrite dropped is carried over"
    assert out[2]["kind"] == "custom" and out[2]["done"] is False
    assert result["all_done"] is False


def test_public_view_has_a_fixed_key_set(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, ["Look around"])
        quest = session.get(Quest, quest_id)
        public = obj.objectives_public(quest)
    assert set(public[0]) == {"id", "text", "kind", "target_id", "target_type", "target_name", "item_name", "done"}


# ----------------------------------------------------------------------
# Engine completion
# ----------------------------------------------------------------------

def test_arrival_completes_go_to(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, [{"text": "Reach the hollow", "kind": "go_to", "target_id": world["hollow"]}])
    _drain(world)
    result = move_player(world["player"], world["hollow"])
    assert result.get("success")
    assert result.get("objectives_done") and "Reach the hollow" in result["objectives_done"][0]
    out = _objectives(quest_id)
    assert out[0]["done"] is True and out[0]["done_day"] == 1
    events, tools = _drain(world)
    assert len(events) == 1 and events[0]["type"] == "objective_done" and events[0]["all_done"] is True
    assert tools == {"update_quest_objectives"}
    with get_session() as session:
        assert session.get(Quest, quest_id).status == QuestStatus.ACTIVE, "all done never completes the quest"


def test_pickup_completes_obtain_case_insensitively(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, [{"text": "Get the pass", "kind": "obtain", "item_name": "Gate Pass"}])
    _drain(world)
    add_to_inventory(world["player"], "gate pass", item_type="quest_item")
    assert _objectives(quest_id)[0]["done"] is True


def test_unrelated_pickup_does_not_tick(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, [{"text": "Get the pass", "kind": "obtain", "item_name": "gate pass"}])
    add_to_inventory(world["player"], "bread")
    assert _objectives(quest_id)[0]["done"] is False


def test_kill_completes_kill_via_tool_and_via_combat(db):
    from src.services.combat import apply_combat_result
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, [
            {"text": "Kill Vex", "kind": "kill", "target_id": world["vex"]},
            {"text": "Deal with Brann", "kind": "kill", "target_id": world["brann"]},
        ])
    _drain(world)
    kill_npc(world["vex"], "a knife in the dark")
    out = _objectives(quest_id)
    assert out[0]["done"] is True and out[1]["done"] is False
    changes = apply_combat_result(world["player"], "victory", defeated_npc_ids=[world["brann"]])
    assert changes.get("objectives_done")
    assert all(o["done"] for o in _objectives(quest_id))


def test_handover_completes_deliver_only_to_the_named_npc(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, [
            {"text": "Bring the letter to Coldiron", "kind": "deliver",
             "target_id": world["brann"], "item_name": "letter"},
        ])
    add_to_inventory(world["player"], "Water-Stained Letter", item_type="quest_item", quantity=2)
    _drain(world)
    transfer_item(world["player"], world["vex"], "water-stained_letter", from_type="player", to_type="npc")
    assert _objectives(quest_id)[0]["done"] is False
    transfer_item(world["player"], world["brann"], "water-stained_letter", from_type="player", to_type="npc")
    assert _objectives(quest_id)[0]["done"] is True


def test_conversation_hook_completes_talk_to(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, [{"text": "Find Coldiron", "kind": "talk_to", "target_id": world["brann"]}])
    assert obj.note_conversation(world["player"], world["vex"]) == []
    ticked = obj.note_conversation(world["player"], world["brann"])
    assert len(ticked) == 1 and ticked[0]["all_done"] is True
    assert _objectives(quest_id)[0]["done"] is True


def test_inactive_quests_never_tick(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, [{"text": "Reach the hollow", "kind": "go_to", "target_id": world["hollow"]}],
                          status=QuestStatus.NOT_STARTED)
    move_player(world["player"], world["hollow"])
    assert _objectives(quest_id)[0]["done"] is False


def test_custom_objective_is_the_dms_to_mark(db):
    with get_session() as session:
        world = _world(session)
        quest_id = _quest(session, world, ["Earn Maera's trust", "Reach the hollow"])
    move_player(world["player"], world["hollow"])
    assert not any(o["done"] for o in _objectives(quest_id)), "custom objectives never engine-match"
    ticked = obj.note_custom_done(world["player"], quest_id, "o1")
    assert len(ticked) == 1 and ticked[0]["all_done"] is False
    assert _objectives(quest_id)[0]["done"] is True


# ----------------------------------------------------------------------
# Ledger + tools
# ----------------------------------------------------------------------

def test_ledger_renders_objective_met_and_the_ready_note(db):
    with get_session() as session:
        world = _world(session)
        _quest(session, world, [{"text": "Reach the hollow", "kind": "go_to", "target_id": world["hollow"]}])
    pending_actions.clear(world["player"])
    move_player(world["player"], world["hollow"])
    block = pending_actions.format_pending_actions(pending_actions.drain(world["player"]))
    assert 'Objective met: "Reach the hollow" (Teeth on the North Road)' in block
    assert 'Every objective on "Teeth on the North Road" is now met' in block
    assert "update_quest_objectives" in pending_actions.tool_equivalents(
        [{"kind": "objective", "summary": "x", "tools": ["update_quest_objectives"]}]
    )


def test_create_quest_takes_a_scale_and_structured_objectives(db):
    with get_session() as session:
        world = _world(session)
    result = create_quest(
        "Errand for Brann", "Fetch the pass",
        [{"text": "Find Coldiron", "kind": "talk_to", "target_id": "Captain Brann Coldiron"}, "Come back"],
        start_active=True, scale="mission",
    )
    assert result["objectives"][0]["target_id"] == world["brann"]
    assert result["objectives"][1]["kind"] == "custom"
    assert "warning" not in result
    with get_session() as session:
        quest = session.get(Quest, result["id"])
        assert quest.scale == "mission"
        assert xp_for_quest(quest) == QUEST_XP["mission"]
        player = session.get(Player, world["player"])
        assert player.active_quests == [result["id"]]


def test_create_quest_warns_on_untargeted_typed_objectives(db):
    with get_session() as session:
        _world(session)
    result = create_quest("Vague", "", [{"text": "Find the ghost", "kind": "talk_to"}])
    assert "warning" in result and "Find the ghost" in result["warning"]


def test_activate_quest_repairs_a_legacy_seed(db):
    with get_session() as session:
        world = _world(session)
        quest = Quest(title="Seeded", description="", status=QuestStatus.NOT_STARTED,
                      objectives=["Reach Wolf's Hollow", "[DONE] Hear the rumour"])
        session.add(quest)
        session.commit()
        quest_id = quest.id
    result = activate_quest(quest_id)
    assert result["objectives"][0] == {
        "id": "o1", "text": "Reach Wolf's Hollow", "kind": "custom", "target_id": None,
        "target_type": None, "target_name": None, "item_name": None, "done": False,
    }
    assert result["objectives"][1]["done"] is True
