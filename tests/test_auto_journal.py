"""A journal that fills itself (round 4: 0, 1 and 1 entries in three
21-turn sessions of clue-hunting — the DM drops @journal first).

The engine journals what the character read, what they found (with its own
description), a clue that came by @give, and the words a delivered story
beat was told in — never a directive or a sealed statement, never a name the
player has not heard, never twice.
"""

import pytest

from src.models import (
    DMState, Location, LocationType, NPC, NPCTier, Player, StoryBeat, WorldClock, get_session,
)
from src.services import campaign as camp
from src.services import memory_files
from src.services.commands import CommandRunner
from src.services.dialogue import NameGate

NOTICE = {"id": "notice_0", "kind": "read", "label": "Notice board", "x": 60, "y": 30,
          "payload": {"text": "TAKEN BY WOLVES — third cart this month. — Capt. B. Coldiron."}}
WHISTLE = {"id": "whistle_1", "kind": "take", "label": "Cracked bone whistle", "x": 60, "y": 74,
           "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                                "description": "The same make as the one Captain Coldiron never mentions."}}}
CRATE = {"id": "crate_2", "kind": "search", "label": "Split supply crate", "x": 80, "y": 56,
         "payload": {"item": {"name": "Greywater Silver Piece", "type": "misc",
                              "description": "Freshly minted. The counting-house stamp is still sharp."}}}
BUNDLE = {"id": "bundle_3", "kind": "take", "label": "Dropped sweetroot bundle", "x": 30, "y": 66,
          "payload": {"item": {"name": "Sweetroot Bundle", "type": "consumable", "description": "Chewy."}}}


@pytest.fixture()
def town(db, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    monkeypatch.setattr(camp, "_spawn_generation", lambda player_id: None)
    camp.reset_state()
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=30))
        session.add(DMState())
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True,
                          visited=True, objects=[NOTICE, WHISTLE, CRATE, BUNDLE])
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True)
        session.add_all([square, hollow])
        session.flush()
        brann = NPC(name="Captain Brann Coldiron", tier=NPCTier.MAJOR, profession="Captain of the Watch",
                    current_location_id=square.id)
        sorrel = NPC(name="Sorrel Tack", tier=NPCTier.MINOR, profession="stable hand", current_location_id=square.id)
        vane = NPC(name="Aldric Vane", tier=NPCTier.MAJOR, profession="wolf-caller", current_location_id=hollow.id)
        session.add_all([brann, sorrel, vane])
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=25)
        session.add(player)
        session.commit()
        ids = {"player": player.id, "square": square.id, "hollow": hollow.id, "brann": brann.id,
               "sorrel": sorrel.id, "vane": vane.id}
    yield ids
    camp.reset_state()


def _journal(w):
    return memory_files.read_journal(w["player"])


# ----------------------------------------------------------------------
# What a touch taught them
# ----------------------------------------------------------------------

def test_interaction_entries_are_the_words_and_the_finds():
    read = {"success": True, "kind": "read", "label": "Notice board",
            "result": {"panel_text": "TAKEN BY WOLVES", "items_gained": []}}
    assert memory_files.interaction_journal_entries(read) == ['Notice board: "TAKEN BY WOLVES"']
    found = {"success": True, "kind": "search", "label": "Split supply crate",
             "result": {"panel_text": None, "items_gained": [{"name": "Greywater Silver Piece", "quantity": 1}]}}
    assert memory_files.interaction_journal_entries(found, CRATE["payload"]) == [
        "Found Greywater Silver Piece in the split supply crate: Freshly minted. The counting-house stamp is still sharp."]
    took = {"success": True, "kind": "take", "label": "Cracked bone whistle",
            "result": {"items_gained": [{"name": "Cracked Bone Whistle", "quantity": 1}]}}
    assert memory_files.interaction_journal_entries(took, WHISTLE["payload"])[0].startswith(
        "Found Cracked Bone Whistle: The same make"), "the thing IS the find: said once"


def test_supplies_beds_and_the_dead_are_not_clues():
    snack = {"success": True, "kind": "take", "label": "Dropped sweetroot bundle",
             "result": {"items_gained": [{"name": "Sweetroot Bundle", "quantity": 1}]}}
    assert memory_files.interaction_journal_entries(snack, BUNDLE["payload"]) == []
    body = {"success": True, "kind": "body", "label": "Wolf's body",
            "result": {"panel_text": "You find: 3 coin.", "items_gained": []}}
    assert memory_files.interaction_journal_entries(body) == []
    assert memory_files.interaction_journal_entries({"success": False, "locked": True}) == []


def test_engine_entries_skip_what_the_journal_already_says(town):
    memory_files.add_journal_entry(town["player"], 'Sorrel said: "Three nights ago I heard one howl answer another."')
    written = memory_files.add_journal_entries(town["player"], [
        'Sorrel Tack: "Three nights ago I heard one howl answer another."',
        "Found Cracked Bone Whistle: Split down the barrel.",
        "Found Cracked Bone Whistle: Split down the barrel.",
    ])
    assert written == ["Found Cracked Bone Whistle: Split down the barrel."]
    assert len(_journal(town)) == 2


def test_an_interact_in_a_reply_is_journaled_after_the_reply_masked(town):
    """The web path holds engine entries for the reply's end, like @journal,
    and masks a name the player never heard — here only the find's own
    description holds "Coldiron"."""
    runner = CommandRunner(town["player"])
    gate = NameGate.for_turn(town["player"], "", present_ids=[town["brann"]])
    runner.journal_mask = gate.mask
    assert runner.run("@interact Cracked bone whistle").ok
    assert runner.run("@interact Notice board").ok
    assert _journal(town) == [], "nothing before the reply ends"
    runner.ledger()
    entries = _journal(town)
    assert any("Found Cracked Bone Whistle: The same make as the one the Watch Captain never mentions" in e
               for e in entries), entries
    assert any('Notice board: "TAKEN BY WOLVES' in e for e in entries), "the words they read are verbatim"
    assert not any("Coldiron never mentions" in e for e in entries)


def test_an_interact_without_a_mask_journals_at_once(town):
    assert CommandRunner(town["player"]).run("@interact Split supply crate").ok
    assert any(e.endswith("Found Greywater Silver Piece in the split supply crate: Freshly minted. "
                          "The counting-house stamp is still sharp.") for e in _journal(town))


def test_a_canvas_touch_is_journaled_and_what_was_read_counts_as_heard(town):
    from src.services.scene_objects import resolve_interaction

    result = resolve_interaction(town["player"], town["square"], "notice_0")
    memory_files.journal_interaction(town["player"], result, town["square"])
    result = resolve_interaction(town["player"], town["square"], "whistle_1")
    memory_files.journal_interaction(town["player"], result, town["square"])
    entries = _journal(town)
    assert any("Capt. B. Coldiron" in e for e in entries), "read on the panel: their own words"
    # Read on the notice, so "Coldiron" is a name they have now seen
    assert any("the one Captain Coldiron never mentions" in e for e in entries), entries


def test_a_clue_given_is_journaled_gear_is_not(town):
    runner = CommandRunner(town["player"])
    assert runner.run("@give Ferry Ledger Page | kind: clue | about: One barge is logged twice.").ok
    assert runner.run("@give Ashwood Club | kind: weapon | tier: common").ok
    entries = _journal(town)
    assert any(e.endswith("Ferry Ledger Page — One barge is logged twice.") for e in entries)
    assert not any("Ashwood" in e for e in entries)


# ----------------------------------------------------------------------
# A delivered beat, in the words that told it
# ----------------------------------------------------------------------

def _beat(key, act, sequence, **kwargs):
    beat = {
        "key": key, "act": act, "sequence": sequence, "beat_type": "revelation", "title": f"Beat {key}",
        "directive": f"Deliver {key}: SEALED DIRECTIVE TEXT.", "fixed_truths": [], "reveals_truth_ids": [],
        "delivery_keywords": [key], "trigger_type": "location", "trigger_data": {"location_id": "nowhere"},
        "cinematic": "none", "consequence_met": [], "consequence_missed": [], "clock_label": None,
        "directive_missed": None, "met_condition": None,
    }
    beat.update(kwargs)
    return beat


def _spine(town, beat):
    payload = {
        "title": "The Toll Road", "premise": "Someone drills the wolves.", "stakes_personal": "Your road.",
        "villain_npc_id": town["vane"], "villain_name": "Aldric Vane",
        "acts": [{"act": 1, "title": "Teeth", "premise": "The cart is late."},
                 {"act": 2, "title": "Whistles", "premise": "The pack answers."},
                 {"act": 3, "title": "The Howl", "premise": "It ends at the hollow."}],
        "fixed_truths": [{"key": "truth_villain", "kind": "villain_identity",
                          "statement": "Aldric Vane was a Watch sergeant.", "spoiler_keywords": ["sergeant"]}],
        "beats": [beat] + [_beat(f"f{i}", 3, 90 + i) for i in range(max(0, camp.MIN_BEATS - 1))],
    }
    assert camp.persist_campaign(town["player"], payload)["status"] == "active"
    with get_session() as session:
        row = session.query(StoryBeat).filter(StoryBeat.title == beat["title"]).one()
        row.status = "fired"
        session.commit()


def test_a_delivered_beat_journals_the_lines_that_told_it(town):
    """fleet5-wenna T19-20: the hooded man burying something by the north
    wall was delivered, and never reached her journal."""
    _spine(town, _beat("b1", 1, 1, title="A Cloak by the Wall",
                       delivery_keywords=["Sorrel", "hooded", "north wall", "buried"]))
    spoken = [
        {"npc_id": town["sorrel"], "npc_name": "Sorrel", "text": "Evening. Cold one."},
        {"npc_id": town["sorrel"], "npc_name": "Sorrel",
         "text": "Saw a hooded man by the north wall last night. He buried something. Then he was gone."},
        {"npc_id": town["sorrel"], "npc_name": "Sorrel", "text": "Sorrel's my name, if you need it."},
    ]
    text = "\n".join(s["text"] for s in spoken)
    result = camp.mark_beat_delivery(town["player"], text, speakers=["Sorrel"], spoken=spoken)
    assert result["delivered"]
    entries = _journal(town)
    assert len(entries) == 1, entries
    assert entries[0].endswith('Sorrel: "Saw a hooded man by the north wall last night. He buried something."')
    assert "Day 1" in entries[0], "stamped with the day it was heard"
    assert "SEALED" not in entries[0] and "Cloak" not in entries[0]


def test_beat_lines_are_at_most_two_and_never_from_the_prose(town):
    _spine(town, _beat("b1", 1, 1, title="Howls on a Count",
                       delivery_keywords=["howl", "count", "eight"]))
    spoken = [
        {"npc_id": town["sorrel"], "npc_name": "Sorrel", "text": "Every howl comes on a count."},
        {"npc_id": town["sorrel"], "npc_name": "Sorrel", "text": "Eight of them. Always eight, then a count of four."},
        {"npc_id": town["brann"], "npc_name": "Watch Captain", "text": "Eight howls, and the gate shuts."},
    ]
    prose = "The howl rises on the count of eight."
    camp.mark_beat_delivery(town["player"], prose + "\n" + "\n".join(s["text"] for s in spoken),
                            speakers=["Sorrel", "Watch Captain"], spoken=spoken)
    entries = _journal(town)
    assert len(entries) == 2, entries
    assert all("rises on the count" not in e for e in entries), "prose is not what anyone said"
    assert any(e.endswith('Sorrel: "Eight of them. Always eight, then a count of four."') for e in entries)


def test_no_speech_no_beat_journal(town):
    _spine(town, _beat("b1", 1, 1, title="Smoke", delivery_keywords=["smoke"]))
    assert camp.mark_beat_delivery(town["player"], "Smoke rises over the hollow.")["delivered"]
    assert _journal(town) == []


def test_a_canvas_touch_masks_a_name_from_outside_the_scene(town):
    """The Hollow whistle's description names "Captain Coldiron", who stands
    in the square: a gate scoped to the Hollow's people let it through, and
    a journal line counts as heard ever after."""
    from src.services.scene_objects import resolve_interaction

    with get_session() as session:
        session.get(Location, town["hollow"]).objects = [WHISTLE]
        session.get(Player, town["player"]).current_location_id = town["hollow"]
        session.commit()
    result = resolve_interaction(town["player"], town["hollow"], "whistle_1")
    assert memory_files.journal_interaction(town["player"], result, town["hollow"])
    entries = _journal(town)
    assert any("the one the Watch Captain never mentions" in e for e in entries), entries
    assert not any("Coldiron" in e for e in entries)
