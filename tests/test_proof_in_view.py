"""Proof in view (round 4, fleet5-tamsin T13-T20).

At Wolf's Hollow the quest's proof — the Cracked bone whistle — lay in plain
sight for six turns. The DM never mentioned it, the lead sat on a custom
step only the DM can tick ("Find out what is organizing the wolves"), and
she walked home without it to a Coldiron asking "Did you see proof?".

Pins: the brief's PROOF HERE order and LEFT BEHIND line, the lead pointing
at the Thing itself, and a custom step giving way to the proof step whose
item is within reach.
"""

import pytest

from src.models import (
    Connection, DMState, Location, LocationType, NPC, Player, Quest, QuestStatus, WorldClock, get_session,
)
from src.services import lead as lead_mod
from src.services import memory_files
from src.services import objectives as obj
from src.services.scene_brief import build_scene_brief

WHISTLE = {"id": "cracked_bone_whistle_1", "kind": "take", "label": "Cracked bone whistle", "x": 60, "y": 74,
           "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                                "description": "Split down the barrel — silent now.", "value": 0}}}
CRATE = {"id": "split_supply_crate_2", "kind": "search", "label": "Split supply crate", "x": 80, "y": 56,
         "payload": {"item": {"name": "Greywater Silver Piece", "type": "misc",
                              "description": "Freshly minted.", "value": 12}}}
SATCHEL = {"id": "satchel_0", "kind": "open", "label": "Lost traveler's satchel", "x": 50, "y": 60,
           "payload": {"item": {"name": "Water-Stained Letter", "type": "quest_item", "description": "A letter."},
                       "lock": "ask the keeper first"}}


@pytest.fixture()
def hollow(db, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    with get_session() as session:
        session.add(WorldClock(day=1, hour=21, minute=0))
        session.add(DMState(tension="low"))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True, visited=True)
        place = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True, visited=True,
                         objects=[WHISTLE, CRATE])
        session.add_all([square, place])
        session.flush()
        session.add(Connection(from_location_id=square.id, to_location_id=place.id, travel_type="walk",
                               travel_time_hours=0.75, bidirectional=True, discovered=True))
        brann = NPC(name="Captain Brann Coldiron", profession="watch captain", current_location_id=square.id)
        session.add(brann)
        player = Player(name="Tamsin Rook", current_location_id=place.id, currency=25)
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "hollow": place.id, "brann": brann.id}


def _quest(w, steps, title="Teeth on the North Road"):
    with get_session() as session:
        quest = Quest(title=title, description="", status=QuestStatus.ACTIVE,
                      objectives=obj.normalize_objectives(steps, session=session))
        session.add(quest)
        session.flush()
        player = session.get(Player, w["player"])
        player.active_quests = list(player.active_quests or []) + [quest.id]
        session.commit()
        return quest.id


def _steps(w, custom_done=False):
    return [
        {"text": "Reach Wolf's Hollow", "kind": "go_to", "target_id": w["hollow"], "done": True},
        {"text": "Find out what is organizing the wolves", "kind": "custom", "done": custom_done},
        {"text": "Bring the cracked bone whistle back to Captain Coldiron as proof", "kind": "deliver",
         "item_name": "Cracked Bone Whistle", "target_id": w["brann"]},
    ]


def _brief(w):
    with get_session() as session:
        return build_scene_brief(session, session.get(Player, w["player"]))["text"]


def _lead(w):
    with get_session() as session:
        return lead_mod.compute_lead(session, session.get(Player, w["player"]))


def _place_player(w, where):
    with get_session() as session:
        session.get(Player, w["player"]).current_location_id = w[where]
        session.commit()


def _carry(w, name):
    with get_session() as session:
        player = session.get(Player, w["player"])
        player.inventory = list(player.inventory or []) + [{"id": "x", "name": name, "type": "quest_item"}]
        session.commit()


# ----------------------------------------------------------------------
# The brief
# ----------------------------------------------------------------------

def test_the_brief_orders_the_proof_into_view_where_it_lies(hollow):
    _quest(hollow, _steps(hollow))
    text = _brief(hollow)
    line = next(l for l in text.splitlines() if l.startswith("PROOF HERE:"))
    # The whistle IS the proof — no "holds itself", no "not what it holds" riddle
    assert "Cracked bone whistle — the Cracked Bone Whistle itself" in line
    assert "holds" not in line
    assert '"Teeth on the North Road"' in line and "put the cracked bone whistle in plain view in this reply" in line
    assert "@interact Cracked bone whistle" in line
    # The crate's silver is nobody's proof: no order for it
    assert "Split supply crate" not in "\n".join(l for l in text.splitlines() if l.startswith("PROOF"))


def test_a_container_proof_is_the_container_in_view_not_its_contents(hollow):
    _quest(hollow, [{"text": "Find the minted silver", "kind": "obtain", "item_name": "Greywater Silver Piece"}],
           title="Silver")
    line = next(l for l in _brief(hollow).splitlines() if l.startswith("PROOF HERE:"))
    assert "Split supply crate holds Greywater Silver Piece" in line
    assert "put the split supply crate in plain view" in line and "not what it holds" in line


def test_no_proof_line_once_they_carry_it_or_without_a_quest(hollow):
    text = _brief(hollow)
    assert "PROOF HERE" not in text, "no quest, no proof"
    _quest(hollow, _steps(hollow))
    _carry(hollow, "Cracked Bone Whistle")
    assert "PROOF HERE" not in _brief(hollow)


def test_a_locked_proof_says_how_it_opens(hollow):
    with get_session() as session:
        place = session.get(Location, hollow["hollow"])
        place.objects = [SATCHEL]
        session.commit()
    _quest(hollow, [{"text": "Take the letter from the satchel", "kind": "obtain",
                     "item_name": "Water-Stained Letter"}], title="A Letter")
    line = next(l for l in _brief(hollow).splitlines() if l.startswith("PROOF HERE:"))
    assert "locked: ask the keeper first" in line and "@interact Lost traveler's satchel" in line


def test_walking_away_from_the_proof_is_named_in_the_next_brief(hollow):
    _quest(hollow, _steps(hollow))
    _place_player(hollow, "square")
    text = _brief(hollow)
    line = next(l for l in text.splitlines() if l.startswith("LEFT BEHIND:"))
    assert "they left the cracked bone whistle behind at Wolf's Hollow" in line
    assert "do NOT carry it" in line
    assert "PROOF HERE" not in text
    # Taken after all: nothing left behind
    _carry(hollow, "Cracked Bone Whistle")
    assert "LEFT BEHIND" not in _brief(hollow)


def test_a_place_never_stood_in_left_nothing_behind(hollow):
    _quest(hollow, _steps(hollow))
    with get_session() as session:
        session.get(Location, hollow["hollow"]).visited = False
        session.commit()
    _place_player(hollow, "square")
    assert "LEFT BEHIND" not in _brief(hollow)


# ----------------------------------------------------------------------
# The lead
# ----------------------------------------------------------------------

def test_the_lead_points_at_the_thing_holding_the_proof(hollow):
    _quest(hollow, [{"text": "Bring the cracked bone whistle back to Captain Coldiron", "kind": "deliver",
                     "item_name": "Cracked Bone Whistle", "target_id": hollow["brann"]}])
    lead = _lead(hollow)
    assert lead["text"] == "Take the cracked bone whistle here"
    assert lead["target"] == {"type": "object", "id": "cracked_bone_whistle_1", "name": "Cracked bone whistle",
                              "location_id": hollow["hollow"], "location_name": "Wolf's Hollow", "here": True}
    assert lead["route_next_hop_location_id"] is None and lead["kind"] == "deliver"
    context = lead_mod.format_lead_context(lead)
    assert "Cracked bone whistle is here" in context and "@interact Cracked bone whistle" in context


def test_a_container_lead_names_the_container_never_what_it_holds(hollow):
    _quest(hollow, [{"text": "Find the minted silver", "kind": "obtain", "item_name": "Greywater Silver Piece"}],
           title="Silver")
    lead = _lead(hollow)
    assert lead["text"] == "Search the split supply crate here"
    assert "silver" not in lead["text"].lower()


def test_a_custom_step_gives_way_to_the_proof_within_reach(hollow):
    """fleet5-tamsin: the villain answered "what is organizing the wolves"
    at T16, nobody wrote @step-done, and the tracker never moved."""
    _quest(hollow, _steps(hollow))
    lead = _lead(hollow)
    assert lead["objective_id"] == "o3" and lead["target"]["type"] == "object"
    assert lead["text"] == "Take the cracked bone whistle here"


def test_past_a_custom_step_the_proof_in_hand_goes_to_who_asked(hollow):
    _quest(hollow, _steps(hollow))
    _carry(hollow, "Cracked Bone Whistle")
    lead = _lead(hollow)
    assert lead["objective_id"] == "o3" and lead["kind"] == "deliver"
    assert lead["target"]["type"] == "npc" and lead["target"]["id"] == hollow["brann"]
    assert lead["route_next_hop_location_id"] == hollow["square"]


def test_away_from_the_proof_the_custom_step_still_leads(hollow):
    _quest(hollow, _steps(hollow))
    _place_player(hollow, "square")
    lead = _lead(hollow)
    assert lead["objective_id"] == "o2" and lead["text"] == "Find out what is organizing the wolves"


def test_a_locked_thing_never_leads(hollow):
    with get_session() as session:
        session.get(Location, hollow["hollow"]).objects = [SATCHEL]
        session.commit()
    _quest(hollow, [{"text": "Take the letter from the satchel, with the keeper's leave", "kind": "obtain",
                     "item_name": "Water-Stained Letter"}], title="A Letter")
    assert _lead(hollow)["text"] == "Take the letter from the satchel, with the keeper's leave"


def test_item_matching_reads_like_the_tick_does():
    assert lead_mod.same_item("Cracked Bone Whistle", "Cracked Bone Whistle")
    assert lead_mod.same_item("the whistle", "Cracked Bone Whistle")
    assert lead_mod.same_item("Silver Coin", "Greywater Silver Piece"), "money nouns are one noun"
    assert not lead_mod.same_item("Gate Pass", "Cracked Bone Whistle")
    assert not lead_mod.same_item("", "Cracked Bone Whistle")


def test_the_proof_lead_chip_is_the_canvas_touch_not_a_typed_turn(hollow):
    """The lead on a Thing here is the canvas's own [E] (no DM turn), and
    the things-here chip for the same Thing folds into it."""
    from src.services.suggestions import suggest_actions

    _quest(hollow, _steps(hollow))
    with get_session() as session:
        player = session.get(Player, hollow["player"])
        chips = suggest_actions(session, player, lead_mod.compute_lead(session, player),
                                player.current_location_id)
    assert chips[0] == {"kind": "interact", "object_id": "cracked_bone_whistle_1",
                        "label": "Take the cracked bone whistle here",
                        "text": "I take the cracked bone whistle here."}
    assert [c.get("object_id") for c in chips].count("cracked_bone_whistle_1") == 1


def test_the_proof_handed_over_closes_the_step_the_lead_skipped(hollow):
    """The lead skips the custom step to "Bring the whistle to Coldiron";
    the hand-over must then tick — a giver's return step used to wait for
    the custom step only the DM can tick. The question that step asks stays
    the story's to answer (round 7, fleet8-bram T7: the hand-over ticked
    "Find out what is organizing the wolves" while Coldiron said "Go back",
    and the job was paid in full a reply later): the tracker goes back to
    it, and the DM rules it with @step-done."""
    from src.services.commands import CommandRunner

    quest_id = _quest(hollow, _steps(hollow))
    with get_session() as session:
        session.get(Quest, quest_id).assigned_by_npc_id = hollow["brann"]
        session.commit()
    _carry(hollow, "Cracked Bone Whistle")
    _place_player(hollow, "square")
    assert _lead(hollow)["objective_id"] == "o3"
    assert CommandRunner(hollow["player"], present_ids=[hollow["brann"]]).run(
        "@hand Cracked Bone Whistle | to: Coldiron").ok
    with get_session() as session:
        steps = session.get(Quest, quest_id).objectives
    assert [s["done"] for s in steps] == [True, False, True]
    assert _lead(hollow)["text"] == "Find out what is organizing the wolves", "the open question, not the proof"


def test_the_proof_handed_over_still_settles_a_step_that_asks_nothing(hollow):
    """A custom step that is no question ("Talk your way past the toll
    post") is settled by the proof in the giver's hand, as before."""
    from src.services.commands import CommandRunner

    steps = _steps(hollow)
    steps[1] = {"text": "Talk your way past the toll post", "kind": "custom"}
    quest_id = _quest(hollow, steps)
    with get_session() as session:
        session.get(Quest, quest_id).assigned_by_npc_id = hollow["brann"]
        session.commit()
    _carry(hollow, "Cracked Bone Whistle")
    _place_player(hollow, "square")
    assert CommandRunner(hollow["player"], present_ids=[hollow["brann"]]).run(
        "@hand Cracked Bone Whistle | to: Coldiron").ok
    with get_session() as session:
        assert [s["done"] for s in session.get(Quest, quest_id).objectives] == [True, True, True]
    assert _lead(hollow) is None, "every step met: nothing frozen on the tracker"


def test_a_talk_back_to_the_giver_still_waits_for_the_custom_step(hollow):
    """Only a real item closes the loop early: "Report back to Coldiron"
    after an undone custom step still waits (the turn-0 payout bug)."""
    quest_id = _quest(hollow, [
        {"text": "Find out what is organizing the wolves", "kind": "custom"},
        {"text": "Report back to Captain Coldiron", "kind": "talk_to", "target_id": hollow["brann"]},
    ])
    with get_session() as session:
        session.get(Quest, quest_id).assigned_by_npc_id = hollow["brann"]
        session.commit()
    assert obj.note_conversation(hollow["player"], hollow["brann"]) == []


def test_a_hand_over_mid_quest_settles_no_custom_step(hollow):
    """Only the loop closing (the giver's own delivery) settles the custom
    steps before it; a letter dropped with someone else on the way does not
    answer "Find who wrote it"."""
    with get_session() as session:
        piper = NPC(name="Piper Wren", profession="hawker", current_location_id=hollow["hollow"])
        session.add(piper)
        session.commit()
        piper_id = piper.id
    quest_id = _quest(hollow, [
        {"text": "Find who wrote the letter", "kind": "custom"},
        {"text": "Leave the letter with Piper", "kind": "deliver", "item_name": "Water-Stained Letter",
         "target_id": piper_id},
        {"text": "Report back to Captain Coldiron", "kind": "talk_to", "target_id": hollow["brann"]},
    ])
    with get_session() as session:
        session.get(Quest, quest_id).assigned_by_npc_id = hollow["brann"]
        session.commit()
    assert obj.note_item_given(hollow["player"], piper_id, "Water-Stained Letter")
    with get_session() as session:
        steps = session.get(Quest, quest_id).objectives
    assert [s["done"] for s in steps] == [False, True, False]


def test_past_a_custom_step_a_find_already_carried_is_no_lead(hollow):
    """An obtain step whose item rode in before the quest did never leads
    ("Get the gate pass" while it is in the pack): the hand-over after it does."""
    _quest(hollow, [
        {"text": "Find out what is organizing the wolves", "kind": "custom"},
        {"text": "Get the gate pass", "kind": "obtain", "item_name": "Gate Pass"},
        {"text": "Show the gate pass to Captain Coldiron", "kind": "deliver", "item_name": "Gate Pass",
         "target_id": hollow["brann"]},
    ])
    _carry(hollow, "Gate Pass")
    lead = _lead(hollow)
    assert lead["objective_id"] == "o3" and lead["target"]["id"] == hollow["brann"]
