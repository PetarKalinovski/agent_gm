"""Markdown memory: NPC memories, the chronicle, the DM's notes.

Memory left the NPCRelationship columns for small markdown documents, kept
as rows of the world's own database (the .db is the whole save) and
exported as files a person can read and fix. These tests pin the format,
the export layout and the import of hand fixes, that a copied .db carries
its memory, the one-time move of a folder from before the database, the
fold that keeps Recent bounded without losing anything, the one-time
migration of a legacy relationship row, that nothing here ever raises into
a turn, and that `record_turn_dialogue` files the turn's words there (row
created, `talk_to` ticked).
"""

import shutil

import pytest
from sqlalchemy import text

from src.models import (
    Location, LocationType, MemoryDoc, NPC, NPCRelationship, Player, Quest, QuestStatus, WorldClock,
    get_engine, get_session,
)
from src.services import dialogue, memory_files
from src.services import objectives as obj


@pytest.fixture()
def mem(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    monkeypatch.setattr(memory_files, "memory_root", lambda: root)
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    return root


@pytest.fixture()
def world(db, mem):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=17, minute=40))
        lantern = Location(name="The Drowned Lantern", type=LocationType.POI, depth=0, discovered=True)
        session.add(lantern)
        session.flush()
        brann = NPC(name="Captain Brann Coldiron", current_location_id=lantern.id)
        piper = NPC(name="Piper Vell", current_location_id=lantern.id)
        session.add_all([brann, piper])
        player = Player(name="Ilse Várna", current_location_id=lantern.id)
        session.add(player)
        session.commit()
        return {"player": player.id, "brann": brann.id, "piper": piper.id, "lantern": lantern.id}


def _doc(world, npc="brann"):
    """The NPC's memory document as stored, None when there is none."""
    return memory_files._read(memory_files.npc_doc(world["player"], world[npc]))


def _exported(mem, world, npc="brann"):
    pid, nid = world["player"], world[npc]
    return mem / "players" / f"ilse-varna-{pid[:8]}" / "npcs" / f"captain-brann-coldiron-{nid[:8]}.md"


# ----------------------------------------------------------------------
# One save: the memory is inside the .db; files are an export
# ----------------------------------------------------------------------

def test_memory_is_written_into_the_database_not_beside_it(world, mem):
    pid, nid = world["player"], world["brann"]
    memory_files.append_exchange(pid, nid, "Any news?", ["The docks burned."], "Day 1 17:40")
    memory_files.add_journal_entry(pid, "The docks burned.")
    memory_files.write_chronicle(pid, "You came to Ashfall.")
    memory_files.add_dm_note(pid, "pay off the docks")
    dialogue.remember_heard(pid, ["Brann"])
    assert not mem.exists()
    with get_session() as session:
        docs = {row.doc for row in session.query(MemoryDoc).filter(MemoryDoc.player_id == pid)}
    assert docs == {f"npc:{nid}", "journal", "chronicle", "dm_notes", "heard_names"}


def test_a_copied_database_carries_its_memory(world, tmp_path):
    from src.models import init_db, reset_engine

    pid, nid = world["player"], world["brann"]
    memory_files.add_npc_note(pid, nid, "Owes him a favour")
    memory_files.add_journal_entry(pid, "The captain lied about the roster.")
    reset_engine()
    shutil.copy(tmp_path / "test.db", tmp_path / "backup.db")
    init_db(tmp_path / "backup.db")
    assert "Owes him a favour" in memory_files.read_npc_memory(pid, nid)
    assert memory_files.read_journal(pid)[-1].endswith("The captain lied about the roster.")


def test_export_layout_is_slugged_and_carries_the_id(world, mem):
    pid, nid = world["player"], world["brann"]
    memory_files.add_npc_note(pid, nid, "Owes him a favour")
    memory_files.write_chronicle(pid, "You came to Ashfall.")
    dialogue.remember_heard(pid, ["Brann"])
    assert memory_files.export_memory(mem) == 3
    folder = mem / "players" / f"ilse-varna-{pid[:8]}"
    assert memory_files.player_dir(pid) == folder
    assert "Owes him a favour" in _exported(mem, world).read_text(encoding="utf-8")
    assert (folder / "chronicle.md").read_text(encoding="utf-8").endswith("You came to Ashfall.\n")
    assert "- Brann" in (folder / "heard_names.md").read_text(encoding="utf-8")
    assert (folder / memory_files.IMPORTED_MARKER).exists()      # a copy, never moved in again
    assert memory_files.slug("  Ël  Ñame!! ") == "el-name"
    assert memory_files.slug("") == "unnamed"


def test_a_hand_fix_comes_back_through_import(world, mem):
    pid, nid = world["player"], world["brann"]
    memory_files.add_npc_note(pid, nid, "Owes him a favour")
    memory_files.export_memory(mem)
    path = _exported(mem, world)
    path.write_text(path.read_text(encoding="utf-8").replace("Owes him a favour", "Owes him nothing"),
                    encoding="utf-8")
    assert memory_files.import_memory(mem) == 1
    assert memory_files.memory_parts(pid, nid)["notes"] == ["(Day 1) Owes him nothing"]


def test_a_folder_from_before_the_database_moves_in_once(world, mem):
    pid, nid = world["player"], world["brann"]
    folder = mem / "players" / f"ilse-varna-{pid[:8]}"
    (folder / "npcs").mkdir(parents=True)
    _exported(mem, world).write_text(
        "# Brann — what they remember of Ilse\n## Notes\n- Saved his life\n## Recent\n", encoding="utf-8")
    (folder / "journal.md").write_text("# Journal — Ilse\n- (Day 1 08:00) The mill burned.\n", encoding="utf-8")
    (folder / "heard_names.md").write_text("# Names the player has heard said\n- Brann\n", encoding="utf-8")
    (folder / "npcs" / "nobody-zzzzzzzz.md").write_text("# Nobody\n", encoding="utf-8")   # no such NPC
    memory_files._imported.clear()
    assert memory_files.memory_parts(pid, nid)["notes"] == ["Saved his life"]
    assert memory_files.read_journal(pid) == ["(Day 1 08:00) The mill burned."]
    assert "Brann" in dialogue.heard_names(pid)
    assert (folder / memory_files.IMPORTED_MARKER).exists()
    # Marked: a later process never reads it again, and the database wins
    memory_files.add_npc_note(pid, nid, "Paid back")
    _exported(mem, world).write_text("# Brann\n## Notes\n- Stale\n", encoding="utf-8")
    memory_files._imported.clear()
    assert memory_files.memory_parts(pid, nid)["notes"] == ["Saved his life", "(Day 1) Paid back"]


def test_a_deleted_character_takes_their_memory_along(world):
    pid, nid = world["player"], world["brann"]
    memory_files.add_npc_note(pid, nid, "Owes him a favour")
    dialogue.remember_heard(pid, ["Brann"])
    memory_files.forget_player(pid)
    assert memory_files.read_npc_memory(pid, nid) == ""
    assert dialogue.heard_names(pid) == frozenset()


def test_a_renamed_character_keeps_their_memory(world):
    memory_files.add_npc_note(world["player"], world["brann"], "Owes him a favour")
    with get_session() as session:
        session.get(Player, world["player"]).name = "Ilse the Grey"
        session.get(NPC, world["brann"]).name = "Brann Coldiron"
        session.commit()
    assert "Owes him a favour" in memory_files.read_npc_memory(world["player"], world["brann"])


@pytest.mark.real_memory_root
def test_memory_root_follows_the_active_world(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr("src.config.get_active_db_path", lambda: "data/ashfall.db")
    assert memory_files.memory_root() == Path("data") / "memory" / "ashfall"


# ----------------------------------------------------------------------
# NPC memory format
# ----------------------------------------------------------------------

def test_exchange_and_note_format(world):
    pid, nid = world["player"], world["brann"]
    memory_files.add_npc_note(pid, nid, "She took the north road job for 60 coin.")
    memory_files.append_exchange(pid, nid, "What's really going on with the wolves?",
                                 ["Wolves are smarter than they ought to be."], "Day 1 17:40")
    text = _doc(world)
    assert text.splitlines() == [
        "# Captain Brann Coldiron — what they remember of Ilse Várna",
        "## Notes",
        "- (Day 1) She took the north road job for 60 coin.",
        "## Recent",
        '- Day 1 17:40 · Ilse: "What\'s really going on with the wolves?"',
        '- Day 1 17:40 · Brann Coldiron: "Wolves are smarter than they ought to be."',
        "## Earlier",
    ]
    assert memory_files.read_npc_memory(pid, nid) == text


def test_empty_stamp_reads_the_clock_and_quotes_are_not_doubled(world):
    memory_files.append_exchange(world["player"], world["brann"], "", ['"Get out."'], "")
    assert '- Day 1 17:40 · Brann Coldiron: "Get out."' in _doc(world)


def test_the_same_note_twice_is_one_note(world):
    for _ in range(3):
        memory_files.add_npc_note(world["player"], world["brann"], "First met at the Lantern.")
    assert memory_files.memory_parts(world["player"], world["brann"])["notes"] == [
        "(Day 1) First met at the Lantern."
    ]


def test_recent_keeps_only_the_last_n_lines_but_all_notes(world):
    pid, nid = world["player"], world["brann"]
    memory_files.add_npc_note(pid, nid, "Knows her brother")
    for i in range(5):
        memory_files.append_exchange(pid, nid, f"question {i}", [f"answer {i}"], "Day 1 18:00")
    text = memory_files.read_npc_memory(pid, nid, recent=3)
    assert "Knows her brother" in text
    assert "answer 4" in text and "question 4" in text and "answer 3" in text
    assert "question 3" not in text and "answer 0" not in text
    assert "## Earlier" in text


def test_recent_folds_into_earlier_without_losing_anything(world):
    pid, nid = world["player"], world["brann"]
    for i in range(16):
        memory_files.append_exchange(pid, nid, f"question {i}", [f"answer {i}"], "Day 2 09:00")
    parts = memory_files.memory_parts(pid, nid)
    # 32 lines went in: past RECENT_MAX the oldest FOLD_COUNT folded at once
    assert len(parts["recent"]) == 32 - memory_files.FOLD_COUNT
    assert len(parts["recent"]) <= memory_files.RECENT_MAX
    assert 'Ilse: "question 0"' in parts["earlier"]
    assert 'Brann Coldiron: "answer 7"' in parts["earlier"]
    assert "answer 15" in parts["recent"][-1]


def test_folded_earlier_stays_bounded(world, monkeypatch):
    monkeypatch.setattr(memory_files, "EARLIER_MAX_CHARS", 400)
    pid, nid = world["player"], world["brann"]
    for i in range(60):
        memory_files.append_exchange(pid, nid, f"question {i} " + "x" * 60, [], "Day 2 09:00")
    parts = memory_files.memory_parts(pid, nid)
    assert len(parts["earlier"]) <= 400
    assert "question 0 " not in parts["earlier"]          # the oldest fell off the tail


def test_condensed_summary_replaces_earlier_only_if_unchanged(world):
    pid, nid = world["player"], world["brann"]
    for i in range(16):
        memory_files.append_exchange(pid, nid, f"question {i}", [f"answer {i}"], "Day 2 09:00")
    doc = memory_files.npc_doc(pid, nid)
    snapshot = memory_files.memory_parts(pid, nid)["earlier"]
    assert not memory_files._apply_summary(doc, "something older", "Stale summary.")
    assert memory_files._apply_summary(doc, snapshot, "Ilse asked sixteen questions; I answered.")
    parts = memory_files.memory_parts(pid, nid)
    assert parts["earlier"] == "Ilse asked sixteen questions; I answered."
    assert len(parts["recent"]) == 16


def test_condense_job_uses_the_model_seam(world, monkeypatch):
    pid, nid = world["player"], world["brann"]
    memory_files.append_exchange(pid, nid, "hello", ["well met"], "Day 1 08:00")
    doc = memory_files.npc_doc(pid, nid)
    calls = []
    monkeypatch.setattr(memory_files, "_condense", lambda npc, pc, txt: calls.append(txt) or "Condensed.")
    memory_files._condense_job(doc, "", "Brann", "Ilse")         # Earlier is empty: matches
    assert calls == [""]
    assert memory_files.memory_parts(pid, nid)["earlier"] == "Condensed."


def test_a_summary_cannot_open_a_new_section(world):
    # A model reply that starts "## Summary" must stay inside Earlier, not
    # re-parse as a heading that leaves Earlier empty
    pid, nid = world["player"], world["brann"]
    memory_files.append_exchange(pid, nid, "hello", ["well met"], "Day 1 08:00")
    assert memory_files._apply_summary(memory_files.npc_doc(pid, nid), "", "## Summary\n# Ilse greeted me.")
    parts = memory_files.memory_parts(pid, nid)
    assert parts["earlier"] == "Summary\nIlse greeted me."
    assert "## Summary" not in _doc(world)


def test_a_bom_from_a_hand_edit_keeps_the_title(world, mem):
    # Notepad may save "UTF-8 with BOM"; the title line must still parse
    pid, nid = world["player"], world["brann"]
    memory_files.add_npc_note(pid, nid, "x")
    memory_files.write_chronicle(pid, "x")
    memory_files.export_memory(mem)
    path = _exported(mem, world)
    path.write_text("﻿# Brann — what they remember of Ilse\r\n## Notes\r\n- Owes him\r\n## Recent\r\n",
                    encoding="utf-8")
    chronicle = path.parent.parent / "chronicle.md"
    chronicle.write_text("\ufeff# The story so far — Ilse\n\nYou arrived.\n", encoding="utf-8")
    memory_files.import_memory(mem)
    memory_files.append_exchange(pid, nid, "hi", [], "Day 1 08:00")
    lines = _doc(world).splitlines()
    assert lines[0] == "# Brann — what they remember of Ilse"
    assert "- Owes him" in lines and '- Day 1 08:00 · Ilse: "hi"' in lines
    assert memory_files.read_chronicle(pid) == "You arrived."


def test_hand_edited_sections_survive_a_rewrite(world, mem):
    pid, nid = world["player"], world["brann"]
    memory_files.append_exchange(pid, nid, "hello", [], "Day 1 08:00")
    memory_files.export_memory(mem)
    path = _exported(mem, world)
    path.write_text(path.read_text(encoding="utf-8") + "## Grudges\nThe dog incident.\n", encoding="utf-8")
    memory_files.import_memory(mem)
    memory_files.append_exchange(pid, nid, "again", [], "Day 1 09:00")
    text = _doc(world)
    assert "## Grudges\nThe dog incident." in text and 'Ilse: "again"' in text


# ----------------------------------------------------------------------
# Legacy migration
# ----------------------------------------------------------------------

def test_first_read_migrates_a_legacy_relationship_row(world):
    pid, nid = world["player"], world["brann"]
    with get_session() as session:
        session.add(NPCRelationship(
            npc_id=nid, player_id=pid, trust_level=55,
            summary="(Day 1) player: asked about the docks",
            key_moments=["First met on Day 1", "Saved my life"],
            recent_messages=[{"role": "player", "content": "Any news?", "day": 2},
                             {"role": "npc", "content": "The docks burned last night.", "day": 2}],
        ))
        session.commit()
    assert _doc(world) is None
    text = memory_files.read_npc_memory(pid, nid)
    assert _doc(world) == text
    assert "- First met on Day 1" in text and "- Saved my life" in text
    assert '- Day 2 · Ilse: "Any news?"' in text
    assert '- Day 2 · Brann Coldiron: "The docks burned last night."' in text
    assert "(Day 1) player: asked about the docks" in text
    assert memory_files.has_history(pid, nid)


def test_a_write_before_any_read_still_migrates_first(world):
    pid, nid = world["player"], world["brann"]
    with get_session() as session:
        session.add(NPCRelationship(npc_id=nid, player_id=pid, key_moments=["Old friend"]))
        session.commit()
    memory_files.append_exchange(pid, nid, "Remember me?", [], "Day 3 10:00")
    parts = memory_files.memory_parts(pid, nid)
    assert parts["notes"] == ["Old friend"]
    assert 'Ilse: "Remember me?"' in parts["recent"][-1]


def test_a_row_without_memory_makes_no_document(world):
    with get_session() as session:
        session.add(NPCRelationship(npc_id=world["brann"], player_id=world["player"]))
        session.commit()
    assert memory_files.read_npc_memory(world["player"], world["brann"]) == ""
    assert _doc(world) is None
    assert not memory_files.has_history(world["player"], world["brann"])


# ----------------------------------------------------------------------
# Never raise
# ----------------------------------------------------------------------

def test_bad_input_never_raises(world, mem):
    pid, nid = world["player"], world["brann"]
    assert memory_files.read_npc_memory(None, nid) == ""
    assert memory_files.read_npc_memory(pid, "") == ""
    assert memory_files.read_npc_memory(pid, nid, recent="lots") == ""
    memory_files.append_exchange(None, None, "hi", ["hey"], "Day 1")
    memory_files.append_exchange(pid, nid, None, "a bare string, not a list", None)
    memory_files.append_exchange(pid, nid, "", [None, "", 42], "Day 1 08:00")
    memory_files.add_npc_note(pid, nid, None)
    memory_files.add_npc_note(pid, None, "x")
    memory_files.add_dm_note(None, "x")
    memory_files.write_chronicle(None, "x")
    assert memory_files.read_chronicle(None) == ""
    assert memory_files.read_dm_notes(pid, limit="x") == ""
    recent = memory_files.memory_parts(pid, nid)["recent"]
    assert recent[0].endswith('Brann Coldiron: "a bare string, not a list"')
    assert recent[1].endswith('Brann Coldiron: "42"')
    # A path traversal in an id stays inside the export folder
    memory_files.append_exchange(pid, "../../etc", "hi", [], "Day 1 08:00")
    memory_files.export_memory(mem)
    written = [p.resolve() for p in mem.rglob("*.md")]
    assert written and all(mem.resolve() in p.parents for p in written)
    assert not (mem.parent / "etc").exists() and not (mem.parent.parent / "etc").exists()


def test_a_broken_export_folder_never_touches_play(world, monkeypatch):
    def boom():
        raise OSError("disk on fire")

    monkeypatch.setattr(memory_files, "memory_root", boom)
    pid, nid = world["player"], world["brann"]
    memory_files.append_exchange(pid, nid, "hi", ["hey"], "Day 1 08:00")
    memory_files.add_npc_note(pid, nid, "x")
    memory_files.add_dm_note(pid, "x")
    memory_files.write_chronicle(pid, "x")
    assert 'Brann Coldiron: "hey"' in memory_files.read_npc_memory(pid, nid)
    assert memory_files.read_chronicle(pid) == "x"
    assert memory_files.read_dm_notes(pid).endswith(") x")


def test_no_database_is_no_memory_and_no_crash(mem):
    # get_session raising (no DB) must not take a turn down with it
    from src.models import reset_engine

    reset_engine()
    memory_files.append_exchange("p-123456789", "n-123456789", "hello", ["hi"], "Day 1 08:00")
    memory_files.add_journal_entry("p-123456789", "x")
    dialogue.remember_heard("p-123456789", ["Vex"])
    assert memory_files.read_npc_memory("p-123456789", "n-123456789") == ""
    assert memory_files.read_journal("p-123456789") == []
    assert dialogue.heard_names("p-123456789") == frozenset()


# ----------------------------------------------------------------------
# Chronicle and DM notes
# ----------------------------------------------------------------------

def test_chronicle_round_trip(world, mem):
    pid = world["player"]
    assert memory_files.read_chronicle(pid) == ""
    memory_files.write_chronicle(pid, "You came to Ashfall owing money.\n\nYou left owing more.")
    stored = memory_files._read(memory_files.chronicle_doc(pid))
    assert stored.startswith("# The story so far — Ilse Várna")
    assert memory_files.read_chronicle(pid) == "You came to Ashfall owing money.\n\nYou left owing more."


def test_chronicle_migrates_the_day_old_player_column(world):
    pid = world["player"]
    with get_engine().begin() as conn:
        conn.execute(text('ALTER TABLE "players" ADD COLUMN "chronicle" TEXT DEFAULT NULL'))
        conn.execute(text("UPDATE players SET chronicle = 'You burned the mill.' WHERE id = :id"), {"id": pid})
    assert memory_files.read_chronicle(pid) == "You burned the mill."
    assert memory_files._read(memory_files.chronicle_doc(pid)) is not None


def test_turn_window_writes_the_chronicle_file(world, monkeypatch):
    from src.agents import turn_window

    class _Writer:
        def __call__(self, prompt):
            assert "CHRONICLE SO FAR" in prompt
            return "You asked the captain about wolves."

    monkeypatch.setattr("src.agents.base.create_agent", lambda *a, **k: _Writer())
    dropped = [{"role": "user", "content": [{"text": "ctx Player says: what about the wolves?"}]},
               {"role": "assistant", "content": [{"text": "He scowls."}]}]
    turn_window.write_chronicle(world["player"], dropped)
    assert memory_files.read_chronicle(world["player"]) == "You asked the captain about wolves."


def test_dm_notes_append_dedupe_and_limit(world):
    pid = world["player"]
    for i in range(15):
        memory_files.add_dm_note(pid, f"thread {i}")
    memory_files.add_dm_note(pid, "thread 14")
    notes = memory_files.read_dm_notes(pid).splitlines()
    assert len(notes) == 12
    assert notes[-1] == "- (Day 1 17:40) thread 14"
    assert notes[0] == "- (Day 1 17:40) thread 3"
    assert memory_files.read_dm_notes(pid, limit=2).splitlines() == notes[-2:]
    assert memory_files._read(memory_files.dm_notes_doc(pid)).startswith("# DM notes — Ilse Várna")


# ----------------------------------------------------------------------
# record_turn_dialogue files the turn
# ----------------------------------------------------------------------

def _rel(npc_id, player_id):
    with get_session() as session:
        rel = session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id, NPCRelationship.player_id == player_id
        ).first()
        return None if rel is None else {"trust": rel.trust_level, "disposition": rel.current_disposition,
                                         "day": rel.last_interaction_day, "messages": rel.recent_messages}


def test_record_turn_dialogue_writes_memory_and_the_row(world, monkeypatch):
    # publish() reaches into src.web (the whole server); the tick is what matters here
    monkeypatch.setattr(obj, "publish", lambda player_id, records: None)
    pid, brann, piper = world["player"], world["brann"], world["piper"]
    with get_session() as session:
        quest = Quest(title="Teeth on the North Road", description="", status=QuestStatus.ACTIVE,
                      # An id needs no resolution (no session: kept as given)
                      objectives=obj.normalize_objectives(
                          [{"text": "Talk to Captain Brann Coldiron", "kind": "talk_to", "target_id": brann}]))
        session.add(quest)
        session.flush()
        session.get(Player, pid).active_quests = [quest.id]
        session.commit()
        quest_id = quest.id

    done = dialogue.record_turn_dialogue(pid, "What's really going on with the wolves?", [
        {"npc_id": brann, "npc_name": "Captain Brann Coldiron", "text": "Wolves are smarter than they ought to be."},
        {"npc_id": piper, "npc_name": "Piper Vell", "text": "Don't listen to him."},
        {"npc_name": "Gate Guard", "text": "Move along."},                 # ambient: no memory
    ], addressed_npc_id=brann)

    assert any("Talk to Captain Brann Coldiron" in line for line in done)
    with get_session() as session:
        assert obj.normalize_objectives(session.get(Quest, quest_id).objectives)[0]["done"]

    brann_text = memory_files.read_npc_memory(pid, brann)
    assert "- (Day 1) First met at The Drowned Lantern." in brann_text
    assert '- Day 1 17:40 · Ilse: "What\'s really going on with the wolves?"' in brann_text
    assert '- Day 1 17:40 · Brann Coldiron: "Wolves are smarter than they ought to be."' in brann_text
    # The player's words go to the one addressed, not to every bystander
    piper_parts = memory_files.memory_parts(pid, piper)
    assert piper_parts["recent"] == ['Day 1 17:40 · Piper Vell: "Don\'t listen to him."']

    for npc_id in (brann, piper):
        row = _rel(npc_id, pid)
        assert row["trust"] == 50 and row["disposition"] == "neutral" and row["day"] == 1
        assert not row["messages"]                                     # memory lives in the file now

    # A second conversation is not a second first meeting
    dialogue.record_turn_dialogue(pid, "And the mill?", [
        {"npc_id": brann, "text": "Burned."}], addressed_npc_id=brann)
    notes = memory_files.memory_parts(pid, brann)["notes"]
    assert sum("First met" in n for n in notes) == 1


def test_record_turn_dialogue_skips_engine_directives_and_silence(world):
    pid, brann = world["player"], world["brann"]
    assert dialogue.record_turn_dialogue(pid, "[COMBAT OUTCOME] you won", [], addressed_npc_id=brann) == []
    assert _rel(brann, pid) is None
    assert memory_files.read_npc_memory(pid, brann) == ""
    # Spoken to and silent: the player's words are still remembered, and the
    # relationship has begun (fleet6: the row is no introduction) — but
    # nobody gave a name, so neither knows the other's
    dialogue.record_turn_dialogue(pid, "Hello?", [], addressed_npc_id=brann)
    assert 'Ilse: "Hello?"' in memory_files.read_npc_memory(pid, brann)
    assert _rel(brann, pid)["trust"] == 50
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=brann, player_id=pid).one()
        assert rel.name_known is False and rel.knows_player_name is False
        assert not dialogue.player_has_met(session, pid, brann)


def test_record_turn_dialogue_ignores_unknown_npc_ids(world):
    pid = world["player"]
    dialogue.record_turn_dialogue(pid, "hi", [{"npc_id": "no-such-npc", "text": "boo"}])
    assert _rel("no-such-npc", pid) is None


def test_a_bare_role_names_nobody_outside_the_room(world):
    # An ambient "Guard" must not be filed as (or tick talk_to for) a named
    # NPC elsewhere whose name only carries the role as an epithet
    with get_session() as session:
        far = Location(name="Saltreach Docks", type=LocationType.DISTRICT, depth=0)
        session.add(far)
        session.flush()
        hilda = NPC(name="Hilda the Guard", current_location_id=far.id)
        session.add(hilda)
        session.commit()
        here = [world["brann"], world["piper"]]
        assert dialogue.resolve_speaker(session, "Guard", here) is None
        assert dialogue.resolve_speaker(session, "Hilda", here).id == hilda.id
        assert dialogue.resolve_speaker(session, "Hilda the Guard", here).id == hilda.id
        # Standing in the room, the role is enough
        assert dialogue.resolve_speaker(session, "Guard", here + [hilda.id]).id == hilda.id
        # Present people still resolve by surname, absent ones by name
        assert dialogue.resolve_speaker(session, "Coldiron", here).id == world["brann"]
        assert dialogue.resolve_speaker(session, "Coldiron", []).id == world["brann"]


def test_record_turn_dialogue_survives_malformed_speeches(world):
    # Anything shaped wrong is skipped, never raised into the turn
    pid, brann = world["player"], world["brann"]
    dialogue.record_turn_dialogue(pid, None, [None, "Brann: hi", {"npc_id": 7, "text": "x"},
                                              {"npc_id": brann, "text": 42}], addressed_npc_id=brann)
    assert memory_files.memory_parts(pid, brann)["recent"] == ['Day 1 17:40 · Brann Coldiron: "42"']


def test_a_name_the_player_gave_is_remembered(world):
    # fleet6-wenna T14: "I'm Wenna Cole," to a stranger; at T15 she said "Cole"
    pid, nid = world["player"], world["piper"]
    assert not memory_files.player_named_themself(pid, nid, "Ilse Várna")
    memory_files.append_exchange(pid, nid, "Just passing through.", ["Ilse, is it? No — you never said."],
                                 "Day 1 17:40")
    assert not memory_files.player_named_themself(pid, nid, "Ilse Várna")   # only the NPC said it
    memory_files.append_exchange(pid, nid, "I'm Várna, and I collect stories.", ["Várna. I'll remember."],
                                 "Day 1 17:45")
    assert memory_files.player_named_themself(pid, nid, "Ilse Várna")
    assert not memory_files.player_named_themself(pid, world["brann"], "Ilse Várna")
