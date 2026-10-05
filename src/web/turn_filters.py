"""Server-side turn text filters.

DialogueDeduper kills the double-dialogue bug: after an NPC line goes out
as a structured `speech` event (rendered with portrait + TTS), the DM often
re-quotes the same line in its own narration — the player reads it twice
and the second copy has no voice. The DM prompt forbids it, but prompts
leak; this filter guarantees it.

Token streams are filtered at sentence granularity: text is held back only
until a safe boundary (sentence end with no open quote), so streaming feel
is preserved. The filter is inert until the first speech line registers.
"""

import re
import string


_QUOTE_OPEN = "\"“"
_QUOTE_CLOSE = "\"”"
_QUOTE_RE = re.compile(r'["“]([^"“”]{8,600})["”]')
_SENT_END = ".!?\n"

_PUNCT_TABLE = str.maketrans("", "", string.punctuation + "’‘“”—–…")


def _normalize(text: str) -> str:
    return " ".join(text.translate(_PUNCT_TABLE).lower().split())


class SentenceDeduper:
    """Drops sentences the player already read this turn.

    The DM sometimes delivers a paragraph through `narrate` and then writes
    the same paragraph again as its reply (seen live with Sonnet); the two
    arrive by different paths (a narration block, then tokens). Every
    sentence of 40+ characters released to the client is remembered for
    the turn; a later copy — from either path — is dropped. Works on
    already sentence-bounded text (the DialogueDeduper's releases and the
    narration blocks), so it never holds tokens back.
    """

    MIN_CHARS = 24
    _SPLIT = re.compile(r"(?<=[.!?…])\s+")

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def filter(self, text: str) -> str:
        if not text:
            return text
        parts = self._SPLIT.split(text)
        kept: list[str] = []
        for part in parts:
            key = _normalize(part)
            if len(key) >= self.MIN_CHARS:
                if key in self.seen:
                    continue
                self.seen.add(key)
            kept.append(part)
        if len(kept) == len(parts):
            return text
        out = " ".join(p for p in kept if p)
        # keep paragraph breaks readable when a whole paragraph vanished
        if not out.strip():
            return ""
        # ...and the break after what is left: a model that repeats its
        # pre-roll sentence after the tool lost the newline with it, and the
        # next paragraph glued on ("eyes, slow.The lamp gutters")
        return out.rstrip() + text[len(text.rstrip()):]


class CodeFenceFilter:
    """Holds Markdown code fences back from the player.

    Narration never legitimately contains a ``` fence; when one appears it
    is the DM writing a tool call as text (seen live: a complete
    prompt_npc_agent call, JSON and all, printed into the chat). Everything
    between the fences is captured in `fences` for the rescue path and
    never released. Text outside fences streams through untouched, except
    that a trailing backtick or two is held until the next token settles
    whether a fence is opening.
    """

    FENCE = "```"

    def __init__(self) -> None:
        self._buf = ""
        self._in_fence = False
        self.fences: list[str] = []

    def feed(self, text: str) -> str:
        self._buf += text or ""
        out = ""
        while True:
            if self._in_fence:
                end = self._buf.find(self.FENCE)
                if end == -1:
                    return out
                self.fences.append(self._buf[:end])
                self._buf = self._buf[end + len(self.FENCE):]
                self._in_fence = False
                continue
            start = self._buf.find(self.FENCE)
            if start == -1:
                hold = 0
                for k in (2, 1):
                    if self._buf.endswith("`" * k):
                        hold = k
                        break
                cut = len(self._buf) - hold
                out += self._buf[:cut]
                self._buf = self._buf[cut:]
                return out
            out += self._buf[:start]
            self._buf = self._buf[start + len(self.FENCE):]
            self._in_fence = True

    def flush(self) -> str:
        if self._in_fence:
            if self._buf.strip():
                self.fences.append(self._buf)
            self._buf = ""
            self._in_fence = False
            return ""
        out = self._buf
        self._buf = ""
        return out


# Lines that are a tool result echoed as prose, not narration. Seen live:
# "[]" and '[{"type": "text", "text": "\n[]\n"}]' printed into the chat
# between an NPC's lines — the DM model repeating a content block verbatim.
_JUNK_LINE = re.compile(
    r"""^\s*(?:
        \[\s*\]                                   # []
        |\{\s*\}                                  # {}
        |\[\s*\{\s*"type"\s*:\s*"text".*\}\s*\]   # [{"type": "text", ...}]
        |\{\s*"type"\s*:\s*"text".*\}            # {"type": "text", ...}
        |\{\s*"(?:success|status|result|error)"\s*:.*\}   # {"success": true, ...}
    )\s*$""",
    re.VERBOSE,
)
# A line that only asks the player for their move, game-show style
# (fleet9-tamsin T11 ended the wolf's entrance on a bolded "**What do you
# do?**"): the scene already hands them the moment. Replayed over every
# transcript: that one line
_PROMPT_LINE = re.compile(
    r"^\s*[*_]{0,3}\s*(?:so,?\s+)?(?:what\s+(?:do|will|would)\s+you\s+do(?:\s+(?:now|next))?|what\s+now|your\s+move)"
    r"\s*[?.!…]*\s*[*_]{0,3}\s*$",
    re.IGNORECASE,
)


def _strip_json_blobs(line: str, dropped: list[str]) -> str:
    """Remove every JSON object/array embedded in a line of prose.

    Seen live: the DM wrote "Quest Updated: Teeth on the North Road
    activated{"id": "...", "rewards": {...}}" — a tool result pasted mid-
    sentence. Brackets are matched with a small stack that respects JSON
    strings; a candidate is removed only if it actually parses, so prose
    like "[illegible]" or "{the whole square}" is untouched.
    """
    import json

    out = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch not in "{[":
            out.append(ch)
            i += 1
            continue
        depth = 0
        in_str = False
        escape = False
        j = i
        while j < n:
            c = line[j]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        candidate = line[i:j + 1] if j < n and depth == 0 else None
        parsed = None
        if candidate and len(candidate) >= 2:
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        # An empty [] or {} mid-sentence is prose ("a list: [] of nothing");
        # a real tool echo carries keys or strings
        if isinstance(parsed, (dict, list)) and (len(candidate) >= 8 or '"' in candidate):
            dropped.append(candidate)
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class JunkLineFilter:
    """Drops tool-result echoes from the token stream: whole junk lines
    (`_JUNK_LINE`) and JSON objects pasted into prose — and a line that only
    asks "What do you do?" (`_PROMPT_LINE`).

    Works at line granularity: text is released up to the last newline,
    each complete line is cleaned, and the trailing partial line is held
    until it completes (or `flush`).
    """

    def __init__(self) -> None:
        self._buf = ""
        self.dropped: list[str] = []
        self.prompts: list[str] = []        # "**What do you do?**" lines, kept apart from tool echoes

    def _clean(self, line: str) -> str | None:
        if _JUNK_LINE.match(line):
            self.dropped.append(line)
            return None
        if _PROMPT_LINE.match(line):
            self.prompts.append(line)       # no tool echo: the audit's tool_result_in_prose never sees it
            return None
        return _strip_json_blobs(line, self.dropped) if ("{" in line or "[" in line) else line

    def feed(self, text: str) -> str:
        self._buf += text or ""
        if "\n" not in self._buf:
            return ""
        head, _, self._buf = self._buf.rpartition("\n")
        pieces = []
        for line in head.split("\n"):
            cleaned = self._clean(line)
            if cleaned is not None:
                pieces.append(cleaned + "\n")
        return "".join(pieces)

    def flush(self) -> str:
        line, self._buf = self._buf, ""
        cleaned = self._clean(line)
        return cleaned if cleaned is not None else ""


def speech_events_for_unspoken(
    text: str,
    spoken: list[str],
    npc_name: str,
    *,
    synthesized: bool = False,
    limit: int = 6,
) -> list[dict]:
    """Speech events for the quoted lines in `text` that nobody delivered.

    "Code enforces, LLM narrates": the prompts ask for every spoken line to
    go through a speech tool, but models write action beats with quotes.
    Each such line becomes a real `speech` event (voice, portrait, bubble)
    attributed to `npc_name`. `spoken` are lines already delivered, so a
    quote that repeats one is skipped. `synthesized=True` tells the client
    the prose already shows the words — voice and bubble only, no second
    chat line.
    """
    from src.web.turn_audit import unspoken_quotes

    spoken_norm = [_normalize(s) for s in spoken if s]
    spoken_norm = [s for s in spoken_norm if len(s) >= 8]
    events: list[dict] = []
    for quote in unspoken_quotes(text or ""):
        q = _normalize(quote)
        if any(q in s or s in q for s in spoken_norm):
            continue
        event = {"event": "speech", "npc": npc_name, "text": quote,
                 "tone": "normal", "action": None}
        if synthesized:
            event["synthesized"] = True
        events.append(event)
        if len(events) >= limit:
            break
    return events


class DialogueDeduper:
    """Strips DM re-quotes of lines already delivered as speech events."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self._buf = ""

    @property
    def active(self) -> bool:
        return bool(self.spoken)

    def register_speech(self, text: str | None) -> None:
        normalized = _normalize(text or "")
        if len(normalized) >= 8:
            self.spoken.append(normalized)

    def _matches_spoken(self, quote: str) -> bool:
        nq = _normalize(quote)
        if len(nq) < 8:
            return False
        for line in self.spoken:
            if nq in line or line in nq:
                return True
            # Fuzzy: heavy word overlap catches paraphrase. The speech event
            # is rendered in chat as the authoritative line, so stripping a
            # reworded DM copy loses nothing.
            qw, lw = set(nq.split()), set(line.split())
            if qw and lw:
                overlap = len(qw & lw) / min(len(qw), len(lw))
                if overlap >= 0.65 and min(len(qw), len(lw)) >= 5:
                    return True
        return False

    def filter_block(self, text: str) -> str:
        """Remove sentences containing quotes that match spoken lines."""
        if not self.active or not text:
            return text

        drop_spans: list[tuple[int, int]] = []
        for m in _QUOTE_RE.finditer(text):
            if not self._matches_spoken(m.group(1)):
                continue
            # Expand to the containing sentence: previous boundary -> next
            start = m.start()
            while start > 0 and text[start - 1] not in _SENT_END:
                start -= 1
            end = m.end()
            # A quote that ends its own sentence ("...them.") IS the sentence
            # end — expanding further would swallow the next sentence. Only
            # expand forward for mid-sentence quotes / attribution tails
            # ('"..." she says.').
            if not m.group(1).rstrip().endswith((".", "!", "?")):
                while end < len(text) and text[end] not in _SENT_END:
                    end += 1
                if end < len(text):
                    end += 1  # include the terminator
            drop_spans.append((start, end))

        if not drop_spans:
            return text

        out = []
        pos = 0
        for start, end in drop_spans:
            if start > pos:
                out.append(text[pos:start])
            pos = max(pos, end)
        out.append(text[pos:])
        result = "".join(out)
        # Collapse whitespace artifacts left by the removal
        result = re.sub(r"[ \t]{2,}", " ", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result

    # ------------------------------------------------------------------
    # Streaming (token channel)
    # ------------------------------------------------------------------

    def feed(self, chunk: str) -> str:
        """Buffer streamed tokens; release filtered text at safe boundaries."""
        if not self.active:
            # Nothing spoken yet — pass through untouched, zero latency.
            # (Any earlier buffered text is released too.)
            if self._buf:
                out = self._buf + chunk
                self._buf = ""
                return out
            return chunk

        self._buf += chunk
        release_at = self._release_point(self._buf)
        if release_at is None:
            if len(self._buf) > 1500:  # runaway quote/no punctuation — bail out
                out = self.filter_block(self._buf)
                self._buf = ""
                return out
            return ""
        out = self.filter_block(self._buf[:release_at])
        self._buf = self._buf[release_at:]
        return out

    def flush(self) -> str:
        out = self.filter_block(self._buf) if self.active else self._buf
        self._buf = ""
        return out

    @staticmethod
    def _release_point(buf: str) -> int | None:
        """Last sentence boundary not inside an open quote, else None."""
        depth = 0
        candidate = None
        for i, ch in enumerate(buf):
            if ch == '"':
                depth = 0 if depth else 1
            elif ch in "“":
                depth += 1
            elif ch in "”":
                depth = max(0, depth - 1)
            elif ch in _SENT_END and depth == 0:
                candidate = i + 1
        return candidate
