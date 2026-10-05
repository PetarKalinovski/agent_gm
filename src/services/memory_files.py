"""Markdown memory: what each NPC remembers of a character, the character's
chronicle, and the DM's own notes.

The database stays the single source of truth for MECHANICS — health, coin,
trust, disposition, who stands where. Memory is not mechanics: it is prose
the DM reads back — what was said, what an NPC now believes about the
player, the story so far. It used to live in NPCRelationship columns
(recent_messages, key_moments, summary) that only code could read and
nobody could correct by hand. Now it is small markdown documents, one row
each in the world's own database (`MemoryDoc`), so the .db is the whole
save — a backup, a copy or a restore carries every memory with the world
it belongs to (2026-10-05: with the files beside the .db, a restored world
kept memories of things that had not happened yet, and a copied .db had
none). Per player character:

    chronicle      the story so far (turn_window folds it)
    dm_notes       the DM's scratchpad (@plan)
    journal        what the character has learned (@journal, the engine)
    heard_names    the name words the player has heard said aloud
    npc:<npc id>   one NPC's memory of this character

`export_memory` writes them out as files a person can open, read and fix,
in the layout they used to live in, and `import_memory` reads them back
(`main.py memory-export` / `memory-import`):

    data/memory/<world>/players/<player-slug>-<id8>/
        chronicle.md  dm_notes.md  journal.md  heard_names.md
        npcs/<npc-slug>-<id8>.md

A folder in that layout from before the move is copied into the database
the first time its character is touched (never over a document the
database already holds), then marked so it is read only once.

An NPC file has three sections: Notes (facts they know or believe — @note,
"First met"), Recent (one line per utterance, newest last) and Earlier (what
fell out of Recent). Recent is bounded: past RECENT_MAX lines the oldest
FOLD_COUNT are folded into Earlier at once as clipped one-liners — a
deterministic fold, so nothing is lost if no model is around — and a
background fast-model pass may later condense Earlier into prose.

Documents are keyed by ids, never names, so a renamed character keeps
their memory and two NPCs who share a name never share one. Exported
folder and file names carry the id's first 8 characters for the same
reason (import matches on that suffix).

Every function here runs inside a turn in flight: none of them raises. A
memory write that fails is a warning in the log, never a broken turn.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RECENT_MAX = 30            # exchange lines kept verbatim in "## Recent"
FOLD_COUNT = 16            # ...the oldest this many fold into "## Earlier" at once
NOTES_MAX = 40             # oldest notes drop past this
EARLIER_MAX_CHARS = 3000   # "## Earlier" keeps its newest tail
SUMMARY_MAX_CHARS = 1500   # a condensed Earlier from the background pass
LINE_MAX_CHARS = 400       # one utterance in Recent
FOLDED_LINE_CHARS = 140    # one utterance once folded into Earlier
NOTE_MAX_CHARS = 300
DM_NOTES_MAX = 120
CHRONICLE_FILE_MAX_CHARS = 8000

# Off switch for the background condensing pass (tests, offline tools)
BACKGROUND_SUMMARY = True

_NOTES, _RECENT, _EARLIER = "notes", "recent", "earlier"
_HEADINGS = {_NOTES: "Notes", _RECENT: "Recent", _EARLIER: "Earlier"}

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


# ----------------------------------------------------------------------
# Names and paths
# ----------------------------------------------------------------------

def slug(text: Any, fallback: str = "unnamed") -> str:
    """'Captain Brann Coldiron' -> 'captain-brann-coldiron' (ascii, hyphens)."""
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    out = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return out[:48].rstrip("-") or fallback


def _id8(value: Any) -> str:
    """The first 8 characters of an id, made safe to sit in a file name."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(value or "")[:8]) or "x"


def memory_root() -> Path:
    """data/memory/<world>, the world being the active database's stem: where
    `export_memory` writes and memory from before the database lived."""
    from src.config import get_active_db_path

    return Path("data") / "memory" / Path(get_active_db_path()).stem


def _player_name(player_id: str) -> str:
    try:
        from src.models import Player, get_session

        with get_session() as session:
            row = session.get(Player, player_id)
            return (row.name or "") if row is not None else ""
    except Exception:
        return ""


def _npc_name(npc_id: str) -> str:
    try:
        from src.models import NPC, get_session

        with get_session() as session:
            row = session.get(NPC, npc_id)
            return (row.name or "") if row is not None else ""
    except Exception:
        return ""


def _existing(parent: Path, suffix: str, want_dir: bool) -> Path | None:
    """An entry under `parent` whose name ends in `-<suffix>` (a rename)."""
    try:
        hits = sorted(p for p in parent.glob(f"*-{suffix}") if p.is_dir() == want_dir)
    except OSError:
        return None
    return hits[0] if hits else None


def player_dir(player_id: str, root: Path | None = None) -> Path:
    """<root>/players/<slug(player name)>-<player_id[:8]>: a character's
    exported folder (root defaults to memory_root())."""
    base = (root if root is not None else memory_root()) / "players"
    id8 = _id8(player_id)
    wanted = base / f"{slug(_player_name(player_id), 'player')}-{id8}"
    if wanted.is_dir():
        return wanted
    return _existing(base, id8, want_dir=True) or wanted


def _npc_file(folder: Path, npc_id: str) -> Path:
    """<player folder>/npcs/<slug(npc name)>-<npc_id[:8]>.md."""
    base = folder / "npcs"
    id8 = _id8(npc_id)
    wanted = base / f"{slug(_npc_name(npc_id), 'npc')}-{id8}.md"
    if wanted.is_file():
        return wanted
    return _existing(base, f"{id8}.md", want_dir=False) or wanted


CHRONICLE, DM_NOTES, JOURNAL, HEARD_NAMES = "chronicle", "dm_notes", "journal", "heard_names"
_PLAYER_DOCS = (CHRONICLE, DM_NOTES, JOURNAL, HEARD_NAMES)
_NPC_PREFIX = "npc:"


@dataclass(frozen=True)
class Doc:
    """One memory document: whose, and which ("journal", "npc:<id>")."""
    player_id: str
    name: str


def npc_doc(player_id: str, npc_id: str) -> Doc:
    return Doc(player_id, f"{_NPC_PREFIX}{npc_id}")


def chronicle_doc(player_id: str) -> Doc:
    return Doc(player_id, CHRONICLE)


def dm_notes_doc(player_id: str) -> Doc:
    return Doc(player_id, DM_NOTES)


def speaker_label(name: str, *, first_word: bool = False) -> str:
    """How a line names its speaker: 'Brann Coldiron' (titles dropped), or
    just the first word for the player ('Ilse')."""
    words = str(name or "").split()
    if not words:
        return "Someone"
    if first_word:
        return words[0]
    from src.services.dialogue import _TITLE_WORDS

    kept = list(words)
    while len(kept) > 1 and kept[0].lower().strip(".") in _TITLE_WORDS:
        kept.pop(0)
    return " ".join(kept)


def format_stamp(clock: Any) -> str:
    """'Day 3 17:40' from a WorldClock row, '' without one."""
    if clock is None:
        return ""
    try:
        return f"Day {int(clock.day)} {int(clock.hour):02d}:{int(clock.minute or 0):02d}"
    except (TypeError, ValueError):
        return ""


def stamp_now() -> str:
    """The world clock as an exchange stamp ('Day 3 17:40'), '' if unreadable."""
    try:
        from src.models import WorldClock, get_session

        with get_session() as session:
            return format_stamp(session.query(WorldClock).first())
    except Exception:
        return ""


def _day_now() -> int | None:
    try:
        from src.models import WorldClock, get_session

        with get_session() as session:
            clock = session.query(WorldClock).first()
            return int(clock.day) if clock is not None else None
    except Exception:
        return None


# ----------------------------------------------------------------------
# Storage: one MemoryDoc row per document
# ----------------------------------------------------------------------
# Every caller writes after its own session has committed (a memory line is
# a nicety recorded once the world changed), so a write here never waits on
# a lock its own thread holds. A background thread (the chronicle fold, the
# condensing pass) can meet a turn's transaction mid-commit: retry briefly.

WRITE_ATTEMPTS = 4


def _lock_for(doc: Doc) -> threading.RLock:
    key = f"{doc.player_id}|{doc.name}"
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def _read(doc: Doc) -> str | None:
    """The document's markdown, None when there is none."""
    from src.models import MemoryDoc, get_session

    _import_legacy(doc.player_id)
    with get_session() as session:
        row = session.get(MemoryDoc, (doc.player_id, doc.name))
        return row.text if row is not None else None


def _write(doc: Doc, text: str) -> None:
    """Insert or replace the document (the caller logs a failure)."""
    from sqlalchemy.dialects.sqlite import insert
    from sqlalchemy.exc import OperationalError

    from src.models import MemoryDoc, get_session

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    upsert = insert(MemoryDoc).values(player_id=doc.player_id, doc=doc.name, text=text, updated_at=now)
    upsert = upsert.on_conflict_do_update(
        index_elements=[MemoryDoc.player_id, MemoryDoc.doc], set_={"text": text, "updated_at": now})
    for attempt in range(WRITE_ATTEMPTS):
        try:
            with get_session() as session:
                session.execute(upsert)
                session.commit()
            return
        except OperationalError as e:
            if "locked" not in str(e).lower() or attempt == WRITE_ATTEMPTS - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


def _one_line(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _no_headings(text: str) -> str:
    """Prose bound for "## Earlier" with any markdown heading marks removed:
    a model reply opening "## Summary" would otherwise re-parse as a new
    section and leave Earlier empty."""
    return "\n".join(re.sub(r"^\s*#+\s*", "", line) for line in str(text or "").splitlines()).strip()


def _unquote(text: str) -> str:
    """Spoken words arrive bare; strip stray wrapping quotes so a line is
    never double-quoted."""
    s = text.strip()
    while len(s) >= 2 and s[0] in "\"“'" and s[-1] in "\"”'":
        s = s[1:-1].strip()
    return s


# ----------------------------------------------------------------------
# NPC memory files
# ----------------------------------------------------------------------

@dataclass
class _Memory:
    title: str = ""
    notes: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    earlier: str = ""
    # Sections a person added by hand survive every rewrite
    extra: list[tuple[str, list[str]]] = field(default_factory=list)

    def has_history(self) -> bool:
        return bool(self.recent or self.earlier.strip())


def _parse(text: str) -> _Memory:
    mem = _Memory()
    current: str | None = None
    buckets: dict[str, list[str]] = {_NOTES: [], _RECENT: [], _EARLIER: []}
    extra: dict[str, list[str]] = {}
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            heading = line[3:].strip()
            key = heading.lower()
            if key in buckets:
                current = key
            else:
                current = heading
                extra.setdefault(heading, [])
            continue
        if line.startswith("# ") and current is None and not mem.title:
            mem.title = line[2:].strip()
            continue
        if current is None:
            continue
        (buckets[current] if current in buckets else extra[current]).append(line)

    def bullets(lines: list[str]) -> list[str]:
        out = []
        for line in lines:
            s = line.strip()
            if s.startswith("- ") or s.startswith("* "):
                s = s[2:].strip()
            if s:
                out.append(s)
        return out

    mem.notes = bullets(buckets[_NOTES])
    mem.recent = bullets(buckets[_RECENT])
    mem.earlier = "\n".join(buckets[_EARLIER]).strip()
    mem.extra = [(h, lines) for h, lines in extra.items()]
    return mem


def _render(mem: _Memory, recent: int | None = None, *, earlier: bool = True) -> str:
    lines = [f"# {mem.title}" if mem.title else "# Memory", f"## {_HEADINGS[_NOTES]}"]
    lines.extend(f"- {n}" for n in mem.notes)
    lines.append(f"## {_HEADINGS[_RECENT]}")
    shown = mem.recent if recent is None else (mem.recent[-recent:] if recent > 0 else [])
    lines.extend(f"- {r}" for r in shown)
    if earlier:
        lines.append(f"## {_HEADINGS[_EARLIER]}")
        if mem.earlier.strip():
            lines.append(mem.earlier.strip())
    for heading, body in mem.extra:
        lines.append(f"## {heading}")
        lines.extend(body)
    return "\n".join(lines).rstrip() + "\n"


def _title(npc_name: str, player_name: str) -> str:
    return f"{npc_name or 'Someone'} — what they remember of {player_name or 'the player'}"


def _exchange_line(stamp: str, label: str, words: str) -> str:
    said = f'{label}: "{words}"'
    return f"{stamp} · {said}" if stamp else said


def _bound_earlier(text: str) -> str:
    """Keep the newest tail; drop whole lines first, clip only a giant one."""
    text = text.strip()
    if len(text) <= EARLIER_MAX_CHARS:
        return text
    lines = text.splitlines()
    while len(lines) > 1 and len("\n".join(lines)) > EARLIER_MAX_CHARS:
        lines.pop(0)
    text = "\n".join(lines).strip()
    if len(text) > EARLIER_MAX_CHARS:
        text = "…" + text[-(EARLIER_MAX_CHARS - 1):]
    return text


def _fold(mem: _Memory) -> bool:
    """Move the oldest Recent lines into Earlier. True when anything moved."""
    moved = False
    while len(mem.recent) > RECENT_MAX:
        old, mem.recent = mem.recent[:FOLD_COUNT], mem.recent[FOLD_COUNT:]
        clipped = "\n".join(f"- {_one_line(line, FOLDED_LINE_CHARS)}" for line in old)
        mem.earlier = _bound_earlier(f"{mem.earlier}\n{clipped}" if mem.earlier.strip() else clipped)
        moved = True
    return moved


def _legacy_memory(player_id: str, npc_id: str, npc_name: str, player_name: str) -> _Memory | None:
    """An NPCRelationship row's old memory columns, as a memory file."""
    from src.models import NPCRelationship, get_session

    with get_session() as session:
        rel = session.query(NPCRelationship).filter(
            NPCRelationship.npc_id == npc_id, NPCRelationship.player_id == player_id
        ).first()
        if rel is None:
            return None
        summary = str(rel.summary or "")
        moments = [m for m in (rel.key_moments or []) if m]
        messages = [m for m in (rel.recent_messages or []) if isinstance(m, dict)]
    if not (summary.strip() or moments or messages):
        return None
    mem = _Memory(title=_title(npc_name, player_name))
    mem.notes = [_one_line(m, NOTE_MAX_CHARS) for m in moments][-NOTES_MAX:]
    you = speaker_label(player_name, first_word=True) if player_name else "Player"
    them = speaker_label(npc_name) if npc_name else "NPC"
    for m in messages:
        words = _unquote(_one_line(m.get("content"), LINE_MAX_CHARS))
        if not words:
            continue
        day = m.get("day")
        stamp = f"Day {day}" if day not in (None, "") else ""
        mem.recent.append(_exchange_line(stamp, you if m.get("role") == "player" else them, words))
    mem.earlier = _bound_earlier(_no_headings(summary))
    _fold(mem)
    return mem


def _load(doc: Doc, player_id: str, npc_id: str) -> _Memory | None:
    """The file's memory; on first touch, the legacy row migrated into it.
    Call with the document's lock held."""
    text = _read(doc)
    if text is not None:
        return _parse(text)
    try:
        mem = _legacy_memory(player_id, npc_id, _npc_name(npc_id), _player_name(player_id))
    except Exception as e:
        logger.warning(f"legacy NPC memory unreadable for {npc_id}: {e}")
        mem = None
    if mem is not None:
        _write(doc, _render(mem))
        logger.info(f"Migrated NPC memory {npc_id} -> {doc.name}")
    return mem


def _fresh(player_id: str, npc_id: str) -> _Memory:
    return _Memory(title=_title(_npc_name(npc_id), _player_name(player_id)))


def _valid_ids(*ids: Any) -> bool:
    return all(isinstance(i, str) and i.strip() for i in ids)


def memory_parts(player_id: str, npc_id: str) -> dict[str, Any]:
    """{"notes": [...], "recent": [...], "earlier": str} — the parsed file,
    for callers that lay memory out themselves (the scene brief)."""
    empty = {"notes": [], "recent": [], "earlier": ""}
    if not _valid_ids(player_id, npc_id):
        return empty
    try:
        doc = npc_doc(player_id, npc_id)
        with _lock_for(doc):
            mem = _load(doc, player_id, npc_id)
        if mem is None:
            return empty
        return {"notes": list(mem.notes), "recent": list(mem.recent), "earlier": mem.earlier}
    except Exception as e:
        logger.warning(f"NPC memory read failed ({npc_id}): {e}")
        return empty


def has_history(player_id: str, npc_id: str) -> bool:
    """Have these two ever exchanged a word (now or in the legacy row)?"""
    parts = memory_parts(player_id, npc_id)
    return bool(parts["recent"] or parts["earlier"].strip())


def player_named_themself(player_id: str, npc_id: str, player_name: str) -> bool:
    """Did the player say their own name to this NPC in an earlier exchange?

    fleet6-wenna T14: "I'm Wenna Cole," she told the Keeper, who was still a
    stranger to HER, so the Keeper saying "Cole" at T15 was flagged as reading
    the character sheet. The player's own words in the NPC's file settle it.
    Since the split (dialogue: who knows whom) the row's `knows_player_name`
    is the record; this seeds it on a new row and covers exchanges filed
    before the column existed.
    """
    tokens = [w for w in re.findall(r"[^\W\d_][\w'-]+", player_name or "") if len(w) >= 3]
    if not tokens:
        return False
    label = speaker_label(player_name, first_word=True)
    parts = memory_parts(player_id, npc_id)
    lines = list(parts["recent"]) + str(parts["earlier"] or "").splitlines()
    for line in lines:
        head, sep, words = str(line).partition(f"· {label}:")
        if not sep:
            continue
        if any(re.search(rf"\b{re.escape(t)}\b", words) for t in tokens):
            return True
    return False


def read_npc_memory(player_id: str, npc_id: str, recent: int | None = None) -> str:
    """The NPC's memory file as markdown ('' when there is none).

    `recent=N` keeps only the last N lines of "## Recent"; Notes and Earlier
    always come whole. The first read of an NPC who only has a legacy
    NPCRelationship memory migrates it into a new file.
    """
    if not _valid_ids(player_id, npc_id):
        return ""
    try:
        doc = npc_doc(player_id, npc_id)
        with _lock_for(doc):
            mem = _load(doc, player_id, npc_id)
        if mem is None:
            return ""
        if recent is not None:
            try:
                recent = max(0, int(recent))
            except (TypeError, ValueError):
                recent = None
        return _render(mem, recent)
    except Exception as e:
        logger.warning(f"NPC memory read failed ({npc_id}): {e}")
        return ""


def append_exchange(player_id: str, npc_id: str, player_words: str, npc_lines: list[str], when: str) -> None:
    """File one exchange: the player's words (if any) then each NPC line.

    `when` is the stamp ('Day 1 17:40'); empty means "read the world
    clock". Past RECENT_MAX lines the oldest fold into Earlier.
    """
    if not _valid_ids(player_id, npc_id):
        return
    try:
        if isinstance(npc_lines, str):
            npc_lines = [npc_lines]
        stamp = _one_line(when, 40) or stamp_now()
        you = speaker_label(_player_name(player_id) or "Player", first_word=True)
        them = speaker_label(_npc_name(npc_id) or "NPC")
        lines: list[str] = []
        said = _unquote(_one_line(player_words, LINE_MAX_CHARS))
        if said:
            lines.append(_exchange_line(stamp, you, said))
        for line in npc_lines or []:
            words = _unquote(_one_line(line, LINE_MAX_CHARS))
            if words:
                lines.append(_exchange_line(stamp, them, words))
        if not lines:
            return
        doc = npc_doc(player_id, npc_id)
        with _lock_for(doc):
            mem = _load(doc, player_id, npc_id) or _fresh(player_id, npc_id)
            mem.recent.extend(lines)
            folded = _fold(mem)
            _write(doc, _render(mem))
            snapshot = mem.earlier
        if folded:
            _condense_later(doc, snapshot, them, you)
    except Exception as e:
        logger.warning(f"NPC memory write failed ({npc_id}): {e}")


def add_npc_note(player_id: str, npc_id: str, text: str) -> None:
    """A fact the NPC now knows or believes about the player: '(Day 3) …'.

    The same fact twice is one note."""
    if not _valid_ids(player_id, npc_id):
        return
    try:
        note = _one_line(text, NOTE_MAX_CHARS).lstrip("- ").strip()
        if not note:
            return
        day = _day_now()
        entry = f"(Day {day}) {note}" if day is not None else note
        key = note.lower().rstrip(".")
        doc = npc_doc(player_id, npc_id)
        with _lock_for(doc):
            mem = _load(doc, player_id, npc_id) or _fresh(player_id, npc_id)
            if any(re.sub(r"^\(Day [^)]*\)\s*", "", n).lower().rstrip(".") == key for n in mem.notes):
                return
            mem.notes = (mem.notes + [entry])[-NOTES_MAX:]
            _write(doc, _render(mem))
    except Exception as e:
        logger.warning(f"NPC note write failed ({npc_id}): {e}")


# ----------------------------------------------------------------------
# The background condensing pass
# ----------------------------------------------------------------------

CONDENSE_PROMPT = """You keep one NPC's memory of a player character in a text RPG.
You get what the NPC remembers from older conversations (clipped lines and
earlier summaries). Rewrite it as one or two short paragraphs, past tense,
from the NPC's point of view ("Ilse asked about the wolves; I told her...").

Keep what the NPC would act on: promises, debts, sums agreed, insults,
favours, what the player asked for, what they learned, the tone between
them. Keep names and places exactly. Drop small talk.
Write ONLY the memory text."""


def _silent(**_kwargs: Any) -> None:
    """A background agent must never stream into a live turn's tracker
    (create_agent falls back to the global handler) or print."""


def _summarizer_enabled() -> bool:
    return BACKGROUND_SUMMARY and "PYTEST_CURRENT_TEST" not in os.environ


def _condense(npc_label: str, player_label: str, text: str) -> str:
    """The fast model's condensed Earlier ('' on any failure)."""
    try:
        from src.agents.base import create_agent

        agent = create_agent("recap", CONDENSE_PROMPT, tools=[], callback_handler=_silent)
        reply = str(agent(
            f"NPC: {npc_label}\nPLAYER CHARACTER: {player_label}\n\nOLDER MEMORY:\n{text}"
        )).strip()
    except Exception as e:
        logger.warning(f"NPC memory condense failed: {e}")
        return ""
    return _one_line(reply, SUMMARY_MAX_CHARS) if reply else ""


def _apply_summary(doc: Doc, snapshot: str, summary: str) -> bool:
    """Swap Earlier for its summary — only if Earlier is still exactly what
    was summarized (a newer fold means the next pass gets all of it)."""
    summary = _no_headings(summary)
    if not summary:
        return False
    try:
        with _lock_for(doc):
            text = _read(doc)
            if text is None:
                return False
            mem = _parse(text)
            if mem.earlier.strip() != snapshot.strip():
                return False
            mem.earlier = summary.strip()
            _write(doc, _render(mem))
            return True
    except Exception as e:
        logger.warning(f"NPC memory condense write failed ({doc.name}): {e}")
        return False


def _condense_job(doc: Doc, snapshot: str, npc_label: str, player_label: str) -> None:
    _apply_summary(doc, snapshot, _condense(npc_label, player_label, snapshot))


def _condense_later(doc: Doc, snapshot: str, npc_label: str, player_label: str) -> None:
    if not _summarizer_enabled() or not snapshot.strip():
        return
    try:
        threading.Thread(
            target=_condense_job, args=(doc, snapshot, npc_label, player_label),
            daemon=True, name="npc-memory-condense",
        ).start()
    except Exception as e:
        logger.warning(f"NPC memory condense not started: {e}")


# ----------------------------------------------------------------------
# The chronicle
# ----------------------------------------------------------------------

def _strip_heading(text: str) -> str:
    lines = (text or "").splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _legacy_chronicle(player_id: str) -> str:
    """Player.chronicle lived for a day as a column; read it if a DB has it."""
    try:
        from sqlalchemy import text

        from src.models import get_session

        with get_session() as session:
            value = session.execute(
                text("SELECT chronicle FROM players WHERE id = :id"), {"id": player_id}
            ).scalar()
        return str(value or "").strip()
    except Exception:
        return ""


def read_chronicle(player_id: str) -> str:
    """The story so far, as plain text ('' before anything was folded)."""
    if not _valid_ids(player_id):
        return ""
    try:
        doc = chronicle_doc(player_id)
        with _lock_for(doc):
            text = _read(doc)
            if text is not None:
                return _strip_heading(text)
            legacy = _legacy_chronicle(player_id)
            if legacy:
                write_chronicle(player_id, legacy)
            return legacy
    except Exception as e:
        logger.warning(f"chronicle read failed ({player_id}): {e}")
        return ""


def write_chronicle(player_id: str, text: str) -> None:
    """Replace the story so far."""
    if not _valid_ids(player_id):
        return
    try:
        body = str(text or "").strip()[-CHRONICLE_FILE_MAX_CHARS:]
        doc = chronicle_doc(player_id)
        heading = f"# The story so far — {_player_name(player_id) or 'the player'}"
        with _lock_for(doc):
            _write(doc, f"{heading}\n\n{body}\n" if body else f"{heading}\n")
    except Exception as e:
        logger.warning(f"chronicle write failed ({player_id}): {e}")


# ----------------------------------------------------------------------
# DM notes
# ----------------------------------------------------------------------

def _note_lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip().startswith("- ")]


def read_dm_notes(player_id: str, limit: int = 12) -> str:
    """The DM's newest `limit` notes, one '- (Day 3 17:40) …' line each."""
    if not _valid_ids(player_id):
        return ""
    try:
        doc = dm_notes_doc(player_id)
        with _lock_for(doc):
            lines = _note_lines(_read(doc))
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 12
        return "\n".join(lines[-limit:]) if limit > 0 else ""
    except Exception as e:
        logger.warning(f"DM notes read failed ({player_id}): {e}")
        return ""


def add_dm_note(player_id: str, text: str) -> None:
    """One line on the DM's scratchpad: threads to pay off, foreshadowing."""
    if not _valid_ids(player_id):
        return
    try:
        note = _one_line(text, 500).lstrip("- ").strip()
        if not note:
            return
        stamp = stamp_now()
        doc = dm_notes_doc(player_id)
        with _lock_for(doc):
            lines = _note_lines(_read(doc))
            if any(re.sub(r"^- (\([^)]*\)\s*)?", "", line) == note for line in lines):
                return
            lines = (lines + [f"- ({stamp}) {note}" if stamp else f"- {note}"])[-DM_NOTES_MAX:]
            heading = f"# DM notes — {_player_name(player_id) or 'the player'}"
            _write(doc, heading + "\n" + "\n".join(lines) + "\n")
    except Exception as e:
        logger.warning(f"DM note write failed ({player_id}): {e}")


# --- the player's journal: what the CHARACTER has learned -------------------
# The DM kept trying to `@note` facts on the player themself (2026-10-04
# fleet playtest: "the clerk confirmed Aldric Vane's family lost their land")
# — a mystery needs somewhere the clues the player earned are written down.
# One line per fact; the DM reads it in its brief, the player in the Journal
# panel (GET /api/journal/{player_id}).

JOURNAL_MAX = 60


def journal_doc(player_id: str) -> Doc:
    return Doc(player_id, JOURNAL)


def read_journal(player_id: str, limit: int | None = None) -> list[str]:
    """Journal entries, oldest first, without the leading '- '."""
    if not _valid_ids(player_id):
        return []
    try:
        doc = journal_doc(player_id)
        with _lock_for(doc):
            lines = [line[2:].strip() for line in _note_lines(_read(doc))]
        return lines[-limit:] if limit else lines
    except Exception as e:
        logger.warning(f"journal read failed ({player_id}): {e}")
        return []


def add_journal_entry(player_id: str, text: str) -> None:
    """One thing the character now knows. Duplicates are ignored."""
    if not _valid_ids(player_id):
        return
    try:
        entry = _one_line(text, 400).lstrip("- ").strip()
        if not entry:
            return
        stamp = stamp_now()
        doc = journal_doc(player_id)
        with _lock_for(doc):
            lines = _note_lines(_read(doc))
            if any(re.sub(r"^- (\([^)]*\)\s*)?", "", line) == entry for line in lines):
                return
            lines = (lines + [f"- ({stamp}) {entry}" if stamp else f"- {entry}"])[-JOURNAL_MAX:]
            heading = f"# Journal — {_player_name(player_id) or 'the player'}"
            _write(doc, heading + "\n" + "\n".join(lines) + "\n")
    except Exception as e:
        logger.warning(f"journal write failed ({player_id}): {e}")


# --- the journal fills itself ------------------------------------------------
# Round 4 (fleet5): 0, 1 and 1 journal entries in three 21-turn sessions of
# clue-hunting — the DM drops @journal first when a reply is short. What the
# character read, found, or heard a story beat turn on is written by the
# engine: the words on screen, never a directive or a sealed statement.

JOURNAL_ENTRY_CHARS = 300
_JOURNAL_SAME = 0.8          # share of a new entry's words an old one already says


def _journal_core(text: Any) -> str:
    """What an entry says, for duplicate checks: its quoted words when it has
    a quote, else the line — stamp, case and punctuation dropped."""
    s = re.sub(r"^\s*-?\s*\([^)]*\)\s*", "", str(text or ""))
    m = re.search(r"[\"“](.{12,}?)[\"”]", s)
    return " ".join(re.findall(r"[a-z0-9]+", (m.group(1) if m else s).lower()))


def _journal_says(old: str, new: str) -> bool:
    """Does the old entry already say the new one? Same words, or the new
    one's words inside it, or nearly all its content words."""
    if not new or new == old:
        return True
    if len(new) >= 20 and new in old:
        return True
    words = {w for w in new.split() if len(w) >= 4}
    return bool(words) and len(words & set(old.split())) >= _JOURNAL_SAME * len(words)


def add_journal_entries(player_id: str, entries: list[str]) -> list[str]:
    """Engine-written entries (`add_journal_entry` for each), minus any the
    journal — or an earlier entry of this batch — already says. Returns the
    entries written. Never raises."""
    written: list[str] = []
    try:
        known = [_journal_core(e) for e in read_journal(player_id)]
        for entry in entries or []:
            text = _one_line(entry, JOURNAL_ENTRY_CHARS + 60).strip()
            core = _journal_core(text)
            if not core or any(_journal_says(old, core) for old in known):
                continue
            add_journal_entry(player_id, text)
            known.append(core)
            written.append(text)
    except Exception as e:
        logger.warning(f"journal entries not kept ({player_id}): {e}")
    return written


# A @journal line restating an entry the engine just wrote (fleet6-tamsin:
# the notice and the satchel letter twice each — the engine's words, then the
# DM's paraphrase: 75-100% of its content words). Looser than `_journal_says`:
# 60% of the new line's content words, stems compared ("carts" is "cart",
# "paying" is "pay"), three at least — "the howls repeat every third night"
# after Piper's three-directions quote shares half and is a new fact
_JOURNAL_REPEAT = 0.6
_JOURNAL_STOP = frozenset({
    "that", "this", "with", "from", "into", "they", "them", "their", "there", "then", "than",
    "were", "been", "have", "said", "says", "what", "when", "where", "which", "while", "just",
    "only", "also", "some", "more", "most", "very", "about", "over", "under", "beneath",
})


def _journal_stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _journal_words(text: Any) -> set[str]:
    core = re.sub(r"^\s*-?\s*\([^)]*\)\s*", "", str(text or "")).lower()
    return {_journal_stem(w) for w in re.findall(r"[a-z0-9]+", core) if len(w) >= 4 and w not in _JOURNAL_STOP}


def journal_repeats(new: str, entries: list[str]) -> str | None:
    """The entry among `entries` that already says `new` (most of its
    content words, three at least), else None."""
    words = _journal_words(new)
    if len(words) < 3:
        return None
    for old in entries or []:
        shared = words & _journal_words(old)
        if len(shared) >= 3 and len(shared) >= _JOURNAL_REPEAT * len(words):
            return old
    return None


def _same_thing(label: str, item: str) -> bool:
    """"Cracked bone whistle" holds the Cracked Bone Whistle — say it once."""
    a = set(re.findall(r"[a-z]+", label.lower())) - {"the", "a", "an", "of"}
    b = set(re.findall(r"[a-z]+", item.lower())) - {"the", "a", "an", "of"}
    return bool(a) and (a <= b or b <= a)


def interaction_journal_entries(result: dict[str, Any], payload: dict[str, Any] | None = None) -> list[str]:
    """What touching a Thing taught the character, as journal lines: the
    words it showed (`Scratches on the hut wall: "…"`) and each find
    with its own description (`Found Cracked Bone Whistle: Split down the
    barrel…`). `result` is `scene_objects.resolve_interaction`'s success
    body; `payload` the object's (for each find's description). Beds, the
    dead's pockets, and supplies (potions, gear) are not clues."""
    if not isinstance(result, dict) or not result.get("success"):
        return []
    kind, label = result.get("kind"), str(result.get("label") or "it")
    if kind in ("body", "rest"):
        return []
    inner = result.get("result") or {}
    entries: list[str] = []
    # A document found (a letter's `reads`) rides the panel too; its words
    # get their own entry below, word for word (fleet6-wenna T17)
    document = inner.get("document") if isinstance(inner.get("document"), dict) else {}
    text = (payload or {}).get("text") if document else inner.get("panel_text")
    if text and kind in ("read", "open", "search"):
        entries.append(f'{label}: "{_one_line(text, JOURNAL_ENTRY_CHARS)}"')
    held = (payload or {}).get("item") or {}
    for found in inner.get("items_gained") or []:
        name = str((found or {}).get("name") or "").strip()
        if not name:
            continue
        if held.get("name") == name and held.get("type") in ("consumable", "weapon", "armor"):
            continue
        about = str(held.get("description") or "").strip() if held.get("name") == name else ""
        where = "" if _same_thing(label, name) else f" in the {label[:1].lower() + label[1:]}"
        entries.append(f"Found {name}{where}" + (f": {_one_line(about, JOURNAL_ENTRY_CHARS)}" if about else ""))
    if document.get("text"):
        entries.append(f'{document.get("item") or label} reads: "{_one_line(document["text"], JOURNAL_ENTRY_CHARS)}"')
    return entries


def journal_interaction(player_id: str, result: dict[str, Any], location_id: str | None = None) -> list[str]:
    """Journal a canvas [E] (`interaction_journal_entries`), names masked as
    the reply stream masks them: the words the player just read on the panel
    are heard; a name only an item's description holds is not (it shows as
    what they know the person by). Returns the entries written. Never raises."""
    payload: dict[str, Any] = {}
    try:
        if location_id and isinstance(result, dict) and result.get("object_id"):
            from src.models import Location, get_session
            from src.services.scene_objects import normalize_objects

            with get_session() as session:
                location = session.get(Location, location_id)
                objects = normalize_objects(location.objects) if location is not None else []
            payload = next((o["payload"] for o in objects if o["id"] == result["object_id"]), {})
    except Exception as e:
        logger.debug(f"interaction payload unreadable ({location_id}): {e}")
    try:        # the touch already happened: an odd result body never fails it
        entries = interaction_journal_entries(result, payload)
    except Exception as e:
        logger.warning(f"interaction not journaled ({player_id}): {e}")
        return []
    if not entries:
        return []
    try:
        from src.services.dialogue import NameGate

        # Everyone the entries name is in the mask's scope: a gate scoped to
        # this turn's people let "Captain Coldiron" through from the Hollow
        # whistle's description, and a journal line counts as heard ever after
        gate = NameGate.for_turn(player_id, "", context="\n".join(entries))
        gate.hear((result.get("result") or {}).get("panel_text"))
        entries = [gate.mask(e) for e in entries]
    except Exception as e:
        logger.warning(f"journal masking unavailable ({player_id}): {e}")
        return []
    return add_journal_entries(player_id, entries)


# --- what people told the character -------------------------------------------
# fleet10-wenna: 21 clue-hunting turns, two journal entries. The cart
# driver's account (wolves "in a line… something whistled, high and
# cracked"), Sorrel's "past the ridge", and the step "Stand out the next howl
# and mark where it rises" ticked at T19 never reached it: the engine
# journaled reads, finds and gives — never what people SAID, nor what a
# ruled step showed. A speaker line carrying an active quest's evidence (two
# of its words, one of them its item's or a long one — replayed over the
# fleet7-10 speech: 21 lines in 12 sessions, nearly all clues) or a fired
# beat's keywords is kept, as said, under the name on screen; a custom step
# @step-done ticks is kept with what answered it.

TESTIMONY_LINES = 2          # per reply
# Words any errand has: they prove nothing about what was seen
_TESTIMONY_COMMON = frozenset("""
get got gets find found finds bring brought back go goes going went come comes came walk walked walks
want wants wanted need needs make made take took give gave keep kept tell told ask asked say said
know knew see saw seen look looked hear heard hears reach reached help eyes eye hand hands spare due sure
what where when there here then than this that with from your their about over before after again
thing things something nothing anything someone anyone everyone people way time times long little
proof work job word words town road gate pass place right left good well listen listening listens nobody
stand standing start starts started else awake close dark next will would could should please believe
believes believed counted count counting night nights three running sort apprentice badge
""".split())


def _evidence_forms(text: str, names: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """The word forms that would show a line is about this text: its
    content words, no names, no errand words (`_TESTIMONY_COMMON`)."""
    from src.services.campaign import _GENERIC_WORDS, _word_forms, _words

    forms: set[str] = set()
    for w in _words(text):
        if len(w) < 4 or w in _GENERIC_WORDS or w in _TESTIMONY_COMMON or w in names:
            continue
        forms |= {f for f in _word_forms(w) if len(f) >= 4 and f not in _TESTIMONY_COMMON}
    return forms


def _evidence_hits(text: str, forms: set[str]) -> set[str]:
    from src.services.campaign import _word_forms, _words

    hits: set[str] = set()
    for w in _words(text):
        if len(w) < 4 or w in _TESTIMONY_COMMON:
            continue
        got = _word_forms(w) & forms
        if got:
            hits.add(min(got, key=len))
    return hits


def testimony_entries(spoken: list[dict[str, Any]] | None, quests: list[dict[str, Any]],
                      beat_keywords: list[list[str]], steps_done: list[dict[str, Any]], prose: str = "",
                      names: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Journal lines for one reply (see above). `quests`: [{"text", "items"}]
    (an active quest's description and steps, the words of its items);
    `beat_keywords`: each fired beat's delivery keywords, names taken out;
    `steps_done`: [{"text", "quest_text"}] for the custom steps ticked."""
    from src.services.campaign import _word_forms, mentioned_keywords

    lines = [n for n in spoken or [] if isinstance(n, dict) and str(n.get("text") or "").strip()]

    def quoted(note: dict[str, Any]) -> str:
        who = str(note.get("npc_name") or note.get("label") or "").strip()
        words = _one_line(note.get("text"), JOURNAL_ENTRY_CHARS)
        return f'{who}: "{words}"' if who else f'"{words}"'

    entries: list[str] = []
    for step in steps_done:
        forms = _evidence_forms(step["text"], names) | _evidence_forms(step.get("quest_text", ""), names)
        # on a tie the narration's act answers it, not someone's aside
        candidates = [(len(_evidence_hits(str(n["text"]), forms)), 0, quoted(n)) for n in lines]
        candidates += [(len(_evidence_hits(s, forms)), 1, f'"{_one_line(s, JOURNAL_ENTRY_CHARS)}"')
                       for s in re.split(r"(?<=[.!?…])\s+|\n+", prose or "") if s.strip()]
        best = max(candidates, default=(0, 0, ""))
        entries.append(f"{step['text']} — " + (best[2] if best[0] else "done"))
    scored: list[tuple[int, int, str]] = []
    # a quest's items are its evidence too ("Bring proof back" asks for the
    # Cracked Bone Whistle)
    quest_forms = [(_evidence_forms(" ".join([q["text"], *(q.get("items") or ())]), names),
                    {f for w in q.get("items") or () for f in _word_forms(w)}) for q in quests]
    for index, note in enumerate(lines):
        text = str(note["text"])
        best = 0
        for forms, items in quest_forms:
            hits = _evidence_hits(text, forms)
            if len(hits) >= 2 and any(h in items or len(h) >= 6 for h in hits):
                best = max(best, len(hits))
        for keywords in beat_keywords:
            best = max(best, 2 * len(mentioned_keywords(keywords, text)))
        if best:
            scored.append((best, index, quoted(note)))
    kept = sorted(sorted(scored, key=lambda s: (-s[0], s[1]))[:TESTIMONY_LINES], key=lambda s: s[1])
    return entries + [entry for _, _, entry in kept]


def journal_testimony(player_id: str | None, spoken: list[dict[str, Any]] | None, results: Any = (),
                      prose: str = "", mask: Any = None) -> list[str]:
    """After a reply: journal what people told the character and what
    answered a custom step this reply ruled (`testimony_entries`), names
    masked by the turn's NameGate (`mask`), never twice. Returns the
    entries written. Never raises."""
    if not _valid_ids(player_id):
        return []
    try:
        from src.models import Location, NPC, Player, get_session
        from src.services import objectives as objectives_mod
        from src.services.campaign import _campaign_beats, _distinctive, _text_forms, _word_forms, _words
        from src.services.campaign import get_campaign_row

        steps_done: list[dict[str, Any]] = []
        for r in results or ():
            if not getattr(r, "ok", False) or getattr(r, "verb", "") != "step-done" or not isinstance(r.result, dict):
                continue
            for rec in r.result.get("records") or []:
                if isinstance(rec, dict) and rec.get("kind") == "custom" and rec.get("text"):
                    steps_done.append({"text": str(rec["text"]), "quest_id": rec.get("quest_id")})
        with get_session() as session:
            player = session.get(Player, player_id)
            if player is None:
                return []
            names: set[str] = set(_words(player.name or ""))
            for (n,) in session.query(NPC.name).all() + session.query(Location.name).all():
                names |= set(_words(n or ""))
            quests = []
            texts: dict[str, str] = {}
            for quest in objectives_mod.active_quests_for(session, player):
                objectives = objectives_mod.normalize_objectives(quest.objectives)
                text = " ".join([quest.description or ""] + [o["text"] for o in objectives
                                                             if o.get("kind") in ("custom", "obtain", "deliver")])
                texts[quest.id] = text
                items = [w for o in objectives for w in _words(o.get("item_name") or "")]
                quests.append({"text": text, "items": items})
            for step in steps_done:
                step["quest_text"] = texts.get(step.get("quest_id") or "", "")
            beat_keywords: list[list[str]] = []
            campaign = get_campaign_row(session, player_id)
            if campaign is not None:
                name_forms = _text_forms(" ".join(names))
                for beat in _campaign_beats(session, campaign):
                    if beat.status in ("fired", "stalled"):
                        # a keyword that is only a name proves nothing
                        beat_keywords.append([
                            str(k) for k in beat.delivery_keywords or []
                            if any(not (_word_forms(w) & name_forms) for w in _distinctive(str(k)))])
        entries = testimony_entries(spoken, quests, [k for k in beat_keywords if k], steps_done, prose, names)
        if mask is not None:
            entries = [mask(e) for e in entries]
        return add_journal_entries(player_id, entries) if entries else []
    except Exception as e:
        logger.warning(f"testimony not journaled ({player_id}): {e}")
        return []


# ----------------------------------------------------------------------
# Every NPC's memory at once; the names the player has heard
# ----------------------------------------------------------------------

def npc_memory_texts(player_id: str) -> dict[str, str]:
    """{npc_id: markdown} for every NPC with a memory of this character."""
    if not _valid_ids(player_id):
        return {}
    try:
        from src.models import MemoryDoc, get_session

        _import_legacy(player_id)
        with get_session() as session:
            rows = session.query(MemoryDoc.doc, MemoryDoc.text).filter(
                MemoryDoc.player_id == player_id, MemoryDoc.doc.like(f"{_NPC_PREFIX}%")).all()
        return {name[len(_NPC_PREFIX):]: text or "" for name, text in rows}
    except Exception as e:
        logger.warning(f"NPC memories unreadable ({player_id}): {e}")
        return {}


# Read on every name the client shows, written a few times a turn: cached
# per world and character, dropped on every write through this module
_heard_cache: dict[tuple[str, str], frozenset[str]] = {}


def _world_key() -> str:
    try:
        from src.models import get_engine

        return str(get_engine().url)
    except Exception:
        return ""


def read_heard_names(player_id: str) -> frozenset[str]:
    """The name words the player has heard said aloud. Empty on any trouble."""
    if not _valid_ids(player_id):
        return frozenset()
    key = (_world_key(), player_id)
    cached = _heard_cache.get(key)
    if cached is not None:
        return cached
    try:
        doc = Doc(player_id, HEARD_NAMES)
        with _lock_for(doc):
            words = frozenset(line[2:].strip() for line in (_read(doc) or "").splitlines()
                              if line.startswith("- ") and line[2:].strip())
    except Exception as e:
        logger.debug(f"heard names unreadable for {player_id}: {e}")
        return frozenset()
    _heard_cache[key] = words
    return words


def add_heard_names(player_id: str, words: list[str]) -> None:
    """Keep these name words as heard (the new ones, after the old)."""
    words = [w for w in dict.fromkeys(words or []) if w]
    if not _valid_ids(player_id) or not words:
        return
    try:
        doc = Doc(player_id, HEARD_NAMES)
        with _lock_for(doc):
            known = [line[2:].strip() for line in (_read(doc) or "").splitlines()
                     if line.startswith("- ") and line[2:].strip()]
            new = [w for w in words if w not in set(known)]
            if not new:
                return
            lines = ["# Names the player has heard said"] + [f"- {w}" for w in sorted(known)] + [f"- {w}" for w in new]
            _write(doc, "\n".join(lines) + "\n")
            _heard_cache.pop((_world_key(), player_id), None)
    except Exception as e:
        logger.warning(f"heard names not kept for {player_id}: {e}")


def forget_player(player_id: str) -> None:
    """Drop every memory document of a deleted character."""
    try:
        from src.models import MemoryDoc, get_session

        with get_session() as session:
            session.query(MemoryDoc).filter(MemoryDoc.player_id == player_id).delete()
            session.commit()
        _heard_cache.pop((_world_key(), player_id), None)
    except Exception as e:
        logger.warning(f"memory of {player_id} not dropped: {e}")


# ----------------------------------------------------------------------
# Files: export, import, and the folders from before the database
# ----------------------------------------------------------------------

IMPORTED_MARKER = ".in-database"
_MARKER_TEXT = ("This folder is a copy. The memory lives in the world's database; edits here take\n"
                "effect only through `uv run main.py memory-import --db <world>.db`.\n")
_imported: set[tuple[str, str]] = set()


def _resolve_id8(model: Any, id8: str) -> str | None:
    """The one id of `model` that starts with these 8 characters."""
    from src.models import get_session

    with get_session() as session:
        hits = [row[0] for row in session.query(model.id).filter(model.id.like(f"{id8}%")).limit(2).all()]
    return hits[0] if len(hits) == 1 else None


def _folder_docs(folder: Path) -> dict[str, str]:
    """{doc name: markdown} for one character's folder. utf-8-sig: a file
    fixed by hand in Notepad may come back with a BOM, which would hide its
    "# " title line from the parser."""
    from src.models import NPC

    found: dict[str, str] = {}
    for name in _PLAYER_DOCS:
        path = folder / f"{name}.md"
        if path.is_file():
            found[name] = path.read_text(encoding="utf-8-sig", errors="replace")
    npcs = folder / "npcs"
    if npcs.is_dir():
        for path in sorted(npcs.glob("*.md")):
            npc_id = _resolve_id8(NPC, path.stem.rsplit("-", 1)[-1])
            if npc_id is None:
                logger.warning(f"memory file names no NPC in this world, skipped: {path}")
                continue
            found[f"{_NPC_PREFIX}{npc_id}"] = path.read_text(encoding="utf-8-sig", errors="replace")
    return found


def _store_folder(player_id: str, folder: Path, *, overwrite: bool) -> int:
    """Copy a character folder's documents into the database; without
    `overwrite`, never over one it already holds. Returns how many."""
    from src.models import MemoryDoc, get_session

    docs = _folder_docs(folder)
    if not overwrite and docs:
        with get_session() as session:
            held = {row[0] for row in session.query(MemoryDoc.doc).filter(MemoryDoc.player_id == player_id).all()}
        docs = {name: text for name, text in docs.items() if name not in held}
    for name, text in docs.items():
        doc = Doc(player_id, name)
        with _lock_for(doc):
            _write(doc, text)
    if docs:
        _heard_cache.pop((_world_key(), player_id), None)
    return len(docs)


def _import_legacy(player_id: str) -> None:
    """Once per character and process: a folder from before memory moved
    into the database is copied in (never over what the database holds),
    then marked so it is read only once. Never raises."""
    key = (_world_key(), player_id)
    if key in _imported:
        return
    _imported.add(key)
    try:
        folder = player_dir(player_id)
        if not folder.is_dir() or (folder / IMPORTED_MARKER).exists():
            return
        count = _store_folder(player_id, folder, overwrite=False)
        (folder / IMPORTED_MARKER).write_text(_MARKER_TEXT, encoding="utf-8")
        logger.info(f"Moved {count} memory file(s) of {player_id} into the database from {folder}")
    except Exception as e:
        logger.warning(f"memory folder of {player_id} not moved into the database: {e}")


def export_memory(root: Path | None = None) -> int:
    """Write every character's memory out as markdown files, in the layout
    the module docstring shows (root defaults to memory_root()), for a
    person to read or fix. Returns the number of files written."""
    from src.models import MemoryDoc, get_session

    root = Path(root) if root is not None else memory_root()
    with get_session() as session:
        rows = session.query(MemoryDoc.player_id, MemoryDoc.doc, MemoryDoc.text).all()
    written = 0
    folders: dict[str, Path] = {}
    for player_id, name, text in rows:
        folder = folders.get(player_id)
        if folder is None:
            folder = folders[player_id] = player_dir(player_id, root)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / IMPORTED_MARKER).write_text(_MARKER_TEXT, encoding="utf-8")
        if name.startswith(_NPC_PREFIX):
            path = _npc_file(folder, name[len(_NPC_PREFIX):])
        else:
            path = folder / f"{slug(name, 'doc').replace('-', '_')}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or "", encoding="utf-8", newline="\n")
        written += 1
    return written


def import_memory(root: Path | None = None) -> int:
    """Read memory files (an export fixed by hand, or a folder from before
    the database) back in, replacing what the database holds for each
    document found. Returns the number of documents stored."""
    from src.models import Player

    root = Path(root) if root is not None else memory_root()
    stored = 0
    players = root / "players"
    for folder in sorted(players.iterdir()) if players.is_dir() else []:
        if not folder.is_dir():
            continue
        player_id = _resolve_id8(Player, folder.name.rsplit("-", 1)[-1])
        if player_id is None:
            logger.warning(f"memory folder names no character in this world, skipped: {folder}")
            continue
        stored += _store_folder(player_id, folder, overwrite=True)
        (folder / IMPORTED_MARKER).write_text(_MARKER_TEXT, encoding="utf-8")
    return stored
