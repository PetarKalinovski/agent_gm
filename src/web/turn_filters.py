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
