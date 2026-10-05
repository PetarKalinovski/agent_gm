"""Dialogue as screenplay lines, narration as plain text.

The DM used to deliver the story through three channels: a `narrate` tool,
a `speak` tool, and a whole NPC sub-agent (`prompt_npc_agent`). Every tool
call costs a model round-trip, and the round-trip after the last tool is
where the model wrote its meta-summary ("The quest is live, pointing her
straight at Coldiron…") — straight into the player's chat. Measured on the
2026-10-04 playtest: a question to one NPC took 37.8 s and four hops.

Now the DM's streamed reply IS the narration, and every spoken line sits on
its own line in one fixed shape:

    Coldiron (grim): "Wolves are smarter than they ought to be."

Everything mechanical is a command line, also alone on its line:

    @give Gate Pass | kind: key

`DialogueMarkupFilter` turns speaker lines into structured speech (voice,
portrait, bubble) and command lines into ("command", line) segments for
`src/services/commands.py` as they stream, and lets everything else
through. A command line is never shown and never read as speech.
`MetaSentenceFilter` drops what the player must never read: the model's
planning and bookkeeping ("Let me move the player…", "The scene leaves
Ilse with…").
"""

from __future__ import annotations

import re
from typing import Any

# Lowercase words a speaker label may contain ("Watchman of the North Gate")
_NAME_CONNECTORS = {"of", "the", "de", "da", "del", "van", "von", "du", "la", "le", "al", "bin", "and"}
# "Coldiron says:" — the verb is narration, the name is the speaker
_SPEECH_VERBS = re.compile(
    r"\s+(?:says|said|asks|asked|replies|replied|mutters|muttered|whispers|whispered|"
    r"growls|growled|calls|called|shouts|shouted|snaps|snapped|adds|added|answers|answered)$",
    re.IGNORECASE,
)
_MAX_NAME_WORDS = 5
_MAX_NAME_CHARS = 48
_MAX_TONE_CHARS = 40
_QUOTED = re.compile(r'["“]([^"“”]+)["”]')
_STAGE = re.compile(r"\*([^*\n]{1,200})\*")
_OPEN_QUOTES = '"“'


def _strip_line_decoration(line: str) -> str:
    """Leading list/quote markers and bold markers around a speaker tag."""
    s = line.lstrip()
    s = re.sub(r"^(?:[-–—>•]+\s*)", "", s)
    return s.replace("**", "")


_SPEECH_VERB_WORDS = {
    "says", "said", "asks", "asked", "replies", "replied", "mutters", "muttered",
    "whispers", "whispered", "growls", "growled", "calls", "called", "shouts",
    "shouted", "snaps", "snapped", "adds", "added", "answers", "answered",
}


def _word_ok(word: str, index: int, last: bool) -> bool:
    """One finished word of a speaker label."""
    if not re.fullmatch(r"[A-Za-zÀ-ÿ'’.\-]+", word):
        return False
    w = word.strip(".'’-")
    if not w:
        return False
    if w[0].isupper():
        return True
    if index > 0 and w.lower() in _NAME_CONNECTORS:
        return True
    return index > 0 and last and w.lower() in _SPEECH_VERB_WORDS


def _name_ok(name: str) -> bool:
    words = name.split()
    if not words or len(words) > _MAX_NAME_WORDS or len(name) > _MAX_NAME_CHARS:
        return False
    return all(_word_ok(w, i, i == len(words) - 1) for i, w in enumerate(words))


def _partial_word_ok(word: str, index: int) -> bool:
    """The word still being typed: could it become an acceptable word?"""
    if not re.fullmatch(r"[A-Za-zÀ-ÿ'’.\-]*", word):
        return False
    w = word.lstrip(".'’-")
    if not w or w[0].isupper():
        return True
    if index == 0:
        return False
    low = w.lower()
    return any(c.startswith(low) for c in _NAME_CONNECTORS | _SPEECH_VERB_WORDS)


# A leading list bullet or code backtick before the "@" (kept in step with
# src/services/commands.py)
_COMMAND_LEAD = re.compile(r"^\s*(?:[-*•]\s+)?`?\s*(?=@)")
# What a line looks like before its "@" has arrived: hold it a moment longer
_COMMAND_LEAD_SO_FAR = re.compile(r"\s*(?:[-*•](?:\s+`?\s*)?|`\s*)?")


def is_command_text(line: str) -> bool:
    """A `@verb …` line (first non-space character "@").

    A leading backtick or list bullet ("- @give …") is tolerated — the model
    sometimes formats its commands as inline code or a list, and that must
    not put them on the player's screen.
    """
    return bool(_COMMAND_LEAD.match(line or ""))


def command_text(line: str) -> str:
    """The command as the runner wants it: no newline, bullet or code backticks."""
    return _COMMAND_LEAD.sub("", line or "", count=1).strip().strip("`").strip()


def could_be_dialogue(buffer: str) -> bool:
    """Can a line that starts with `buffer` still turn out to be a speaker line?

    Decides within a few dozen characters for ordinary narration, so a
    paragraph streams almost immediately; a real `Name (tone): "…` prefix is
    held until the line ends. A command line is held whole too — it must
    never leak a character to the player.
    """
    if is_command_text(buffer) or _COMMAND_LEAD_SO_FAR.fullmatch(buffer):
        return True
    s = _strip_line_decoration(buffer).lstrip("*")
    if not s:
        return True
    if not s[0].isupper():
        return False
    cut = min([i for i in (s.find("("), s.find(":")) if i != -1] or [len(s)])
    region, rest = s[:cut], s[cut:]
    name = region.replace("*", "")
    words = name.split()
    if len(words) > _MAX_NAME_WORDS or len(name.strip()) > _MAX_NAME_CHARS:
        return False
    closed = bool(rest) or name.endswith(" ")
    finished = words if closed else words[:-1]
    for i, w in enumerate(finished):
        if not _word_ok(w, i, closed and i == len(finished) - 1):
            return False
    if not closed and words and not _partial_word_ok(words[-1], len(words) - 1):
        return False
    if not rest:
        return True
    if rest.startswith("("):
        close = rest.find(")")
        if close == -1:
            return len(rest) <= _MAX_TONE_CHARS + 1
        if close > _MAX_TONE_CHARS + 1:
            return False
        rest = rest[close + 1:].lstrip()
        if not rest:
            return True
    if not rest.startswith(":"):
        return False
    after = rest[1:].lstrip()
    if not after:
        return True
    return after[0] in _OPEN_QUOTES or after[0] == "*" or after[0].isalpha()


def parse_dialogue_line(line: str) -> dict[str, Any] | None:
    """`Name (tone): "words"` -> {speaker, tone, text, action, remainder, quoted}.

    `remainder` is narration written after the closing quote on the same
    line ("…" He turns away.) — the caller releases it as prose. `quoted`
    says whether the words were in quotes; an unquoted line is only speech
    when the caller can resolve the speaker or a tone was given.
    """
    s = _strip_line_decoration(line).rstrip()
    m = re.match(r"^\*?([^:(\n*]{1,%d})\*?\s*(?:\(([^)\n]{1,%d})\))?\s*:\s*(.+)$"
                 % (_MAX_NAME_CHARS, _MAX_TONE_CHARS), s)
    if not m:
        return None
    name = _SPEECH_VERBS.sub("", m.group(1).strip())
    if not _name_ok(name):
        return None
    tone = (m.group(2) or "").strip().lower() or None
    body = m.group(3).strip()
    # "(warm, wiping a mug)" — the first word is the voice, the rest a gesture
    tone_action = None
    if tone and "," in tone:
        tone, tone_action = (part.strip() for part in tone.split(",", 1))
        tone = tone or None

    quoted = _QUOTED.findall(body)
    # *word* inside the quotes is emphasis ("there *are* a few"), not a
    # gesture — only asterisks outside the quotes are stage directions
    actions = [a.strip() for a in _STAGE.findall(_QUOTED.sub(" ", body))]
    remainder = ""
    if quoted:
        spoken = " ".join(_STAGE.sub(lambda m: m.group(1), q).strip() for q in quoted)
        last_close = max(body.rfind('"'), body.rfind("”"))
        tail = body[last_close + 1:].strip() if last_close != -1 else ""
        tail = _STAGE.sub("", tail).strip()
        if tail:
            remainder = tail
        lead = body[: min(i for i in (body.find('"'), body.find("“")) if i != -1)]
        lead = _STAGE.sub("", lead).strip(" -–—,:")
        if lead:
            actions.insert(0, lead)
    else:
        spoken = _STAGE.sub(" ", body)
        spoken = re.sub(r"\s{2,}", " ", spoken).strip()
    spoken = spoken.strip().strip('"“”').strip()
    if not spoken:
        return None
    if tone_action:
        actions.insert(0, tone_action)
    return {
        "speaker": name,
        "tone": tone,
        "text": spoken,
        "action": "; ".join(a for a in actions if a) or None,
        "remainder": remainder,
        "quoted": bool(quoted),
    }


# Labels that can only ever be the player character
_PLAYER_SELF = frozenset({"you", "me", "i", "myself", "yourself", "player", "the player"})
# Titles before a name are not the name: "Sergeant Holloway" is still Bram
_LABEL_TITLES = frozenset({
    "sir", "ser", "lady", "lord", "dame", "master", "mistress", "mister", "miss", "mr", "mrs", "ms",
    "captain", "sergeant", "commander", "lieutenant", "brother", "sister", "father", "mother",
    "doctor", "dr", "professor", "agent", "detective", "inspector", "officer", "the",
})


def is_player_label(label: str, player_name: str | None) -> bool:
    """Is this speaker label the player character? For Bram Holloway:
    "Bram Holloway", "Bram", "Holloway" (titles aside); for anyone, "You"
    or "Me". The DM never voices the player — the words are theirs to say."""
    words = re.findall(r"[a-zà-ÿ'’]+", (label or "").lower())
    if not words:
        return False
    if " ".join(words) in _PLAYER_SELF:
        return True
    name = [w for w in re.findall(r"[a-zà-ÿ'’]+", (player_name or "").lower()) if w not in _LABEL_TITLES]
    words = [w for w in words if w not in _LABEL_TITLES]
    if not name or not words:
        return False
    return words == name or (len(words) == 1 and len(words[0]) >= 3 and words[0] in (name[0], name[-1]))


# A speaker line past this many words goes out as two (or more), cut at a
# sentence end near its middle: a bubble and a voice clip stay short
# (fleet9-tamsin T0, a 76-word Captain line; T4, a 73-word pitch; fleet9-wenna
# T0 113 words). The parts after the first carry `continues`: the audit
# still weighs the one line the DM wrote (turn_audit too_talky)
SPLIT_SPEECH_WORDS = 60
_SPEECH_BREAK = re.compile(r"[.!?…]+[\"'’”)]*\s+(?=[\"'“‘(*]?[A-Z])")
# "Capt. B. Coldiron" is no sentence end
_ABBREVIATION = re.compile(r"(?:^|\s)(?:[A-Z]|Mr|Mrs|Ms|Dr|St|Capt|Sgt|Lt|Mt|No|Jr|Sr|Gen|Col)\.$")


def split_long_speech(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """`parsed` (a speaker line) as one or more lines of at most
    SPLIT_SPEECH_WORDS where it has sentence ends to cut at; the gesture
    stays on the first, the remainder (prose after the quote) on none."""
    text = str(parsed.get("text") or "")
    total = len(text.split())
    if total <= SPLIT_SPEECH_WORDS:
        return [parsed]
    cuts = [m.end() for m in _SPEECH_BREAK.finditer(text) if not _ABBREVIATION.search(text[:m.start() + 1])]
    cuts = [c for c in cuts if text[:c].strip() and text[c:].strip()]
    if not cuts:
        return [parsed]
    cut = min(cuts, key=lambda c: abs(len(text[:c].split()) - total / 2))
    # Every part carries the whole `line`: the name gate hears it at once, so
    # a name said in the second half shows on the first bubble too, as it did
    # on the one line
    whole = parsed.get("line") or text
    head = {**parsed, "text": text[:cut].strip(), "remainder": "", "line": whole}
    tail = {**parsed, "text": text[cut:].strip(), "action": None, "remainder": "", "continues": True,
            "line": whole}
    return split_long_speech(head) + split_long_speech(tail)


class DialogueMarkupFilter:
    """Stream filter: speaker lines become speech, command lines become
    commands, everything else streams.

    Each line is held only while it could still be a speaker tag or is a
    command. A line that turns out to be dialogue is withheld whole and
    parsed when it ends; its speech waits in `speech` for the caller to
    drain (and to resolve the speaker against the world).
    `accept_unquoted(speaker, tone)` decides whether an unquoted
    `Name: words` line is speech — it must name someone real, or carry a
    tone, to count. A command line is withheld until its newline and handed
    over whole, without the newline; the text-only `feed`/`flush` helpers
    queue it in `commands` instead.

    `player_name`: a speaker line in the player character's own voice
    (their name, first name or surname, or "You"/"Me") is dropped whole —
    neither speech nor prose — and kept in `player_lines` for the audit.
    fleet4-bram T15: the DM wrote `Bram Holloway: "Anyone been through here
    lately…"` and the player had to ask again in their own words.
    """

    def __init__(self, accept_unquoted=None, player_name: str | None = None) -> None:
        self._line = ""
        self._holding = True
        self.speech: list[dict[str, Any]] = []
        self.commands: list[str] = []
        self.player_lines: list[dict[str, Any]] = []
        self._accept_unquoted = accept_unquoted or (lambda speaker, tone: bool(tone))
        self._player_name = player_name or ""

    def feed_segments(self, text: str) -> list[tuple[str, Any]]:
        """Ordered output: ("text", str), ("speech", parsed) and
        ("command", line) segments."""
        segments: list[tuple[str, Any]] = []
        buf: list[str] = []

        def emit_text(t: str) -> None:
            if t:
                buf.append(t)

        def emit(kind: str, value: Any) -> None:
            if buf:
                segments.append(("text", "".join(buf)))
                buf.clear()
            segments.append((kind, value))

        for ch in text or "":
            if self._holding:
                self._line += ch
                if ch == "\n":
                    self._finish_line(emit_text, emit)
                elif not could_be_dialogue(self._line):
                    emit_text(self._line)
                    self._line = ""
                    self._holding = False
            else:
                emit_text(ch)
                if ch == "\n":
                    self._holding = True
        if buf:
            segments.append(("text", "".join(buf)))
        return segments

    def flush_segments(self) -> list[tuple[str, Any]]:
        segments: list[tuple[str, Any]] = []
        if self._holding and self._line:
            self._finish_line(
                lambda t: segments.append(("text", t)) if t else None,
                lambda kind, value: segments.append((kind, value)),
            )
        self._line = ""
        self._holding = True
        return segments

    def feed(self, text: str) -> str:
        """Text only; finished speaker lines queue in `speech`, command
        lines in `commands`."""
        return self._collect(self.feed_segments(text))

    def flush(self) -> str:
        return self._collect(self.flush_segments())

    def _collect(self, segments: list[tuple[str, Any]]) -> str:
        out = []
        for kind, value in segments:
            if kind == "speech":
                self.speech.append(value)
            elif kind == "command":
                self.commands.append(value)
            else:
                out.append(value)
        return "".join(out)

    def drain_speech(self) -> list[dict[str, Any]]:
        out, self.speech = self.speech, []
        return out

    def drain_commands(self) -> list[str]:
        out, self.commands = self.commands, []
        return out

    def _finish_line(self, emit_text, emit) -> None:
        line, self._line = self._line, ""
        self._holding = True
        if is_command_text(line):
            command = command_text(line)
            if command.strip("@").strip():
                emit("command", command)
            return
        parsed = parse_dialogue_line(line.rstrip("\n"))
        if parsed and is_player_label(parsed["speaker"], self._player_name):
            self.player_lines.append(parsed)
            # Their words go; the world's narration after the quote stays
            if parsed["remainder"]:
                emit_text(parsed["remainder"] + ("\n" if line.endswith("\n") else ""))
            return
        if parsed and (parsed["quoted"] or self._accept_unquoted(parsed["speaker"], parsed["tone"])):
            for part in split_long_speech(parsed):
                emit("speech", part)
            tail = parsed["remainder"]
            if tail:
                emit_text(tail + ("\n" if line.endswith("\n") else ""))
            return
        emit_text(line)


# --- the model's bookkeeping, which the player must never read ------------

_META_OPENERS = re.compile(
    r"^(?:(?:I'll|I will|I'm going to|I am going to|I'm|I've|I have|I need to|I should(?:n't)?|I can|"
    r"I check|I'd|I won't|I don't need|Let me|Let's|Now I|Now let|Now,? let|First,? I|Next,? I|"
    r"Both NPCs|The NPC|This reply|My reply|My (?:last )?response|This response|The audit|"
    r"The reality audit)\b"
    r"|(?:Good|Great|Perfect|Okay|OK|Alright|All right|Done|Excellent)[,.!—-])",
)
_META_TOKENS = re.compile(
    r"(?:\bthe player\b|\bplayer's\b|\b\w+_id\b|`|\btool (?:call|result)s?\b|"
    # "the lead" is the tracker's word, but a pack has a lead wolf and a
    # rider takes the lead: those are the story's (a wolf campaign)
    r"\bnarrat\w*|(?<!take )(?<!takes )(?<!took )(?<!taking )(?<!in )\bthe lead\b"
    r"(?![\w-]|\s+(?:wolf|wolves|horse|horses|mare|rider|riders|cart|carts|wagon|dog|dogs|hound|hounds|"
    r"scout|scouts|runner|beast|animal|pack|seal|weight|weights|pipe|shot|rope|reins?)\b)|"
    r"\bobjective\b|\bquest is (?:live|active|now active)\b|"
    r"\bnext step\b|\bthe scene leaves\b|\bset up the\b|\bhook\b.*\bquest\b|\bbeat\b.*\bdeliver|"
    # fleet8-tamsin T9/T13: "…a spurious result to discard", "This reply needs
    # to … matching their stated intent". Words the story never uses
    r"\bthis reply\b|\bstated intent\b|\bspurious\b|\b(?:result|roll|line|command)s? to discard\b|"
    r"\bmade (?:that|a|the|this) second call\b|\bsecond (?:tool|roll_check) call\b|"
    # "the brief" as a noun ("… per the brief."), never the adjective of a
    # list ("the brief, sharp bark of a fox")
    r"\b(?:per|according to) (?:the|my) (?:scene )?(?:brief|directive)\b|"
    r"\b(?:the|my) (?:scene )?brief\b(?=\s*(?:[.;:!?)]|$|(?:says|said|lists|listed|shows|showed|"
    r"notes|noted|has|had|gives|gave|puts|names|mentions|wants|asks)\b)))",
    re.IGNORECASE,
)
# "The reply/The engine/The directive…" opens fiction too ("The reply comes
# from the hills."), so it is meta only when it goes on to plan or talk tools
# and never addresses "you". Not on a fiction verb: "The response is a growl
# that has to be a warning", "the wolf refuses to give ground"
_META_SUBJECT = re.compile(
    r"^(?:The|That) (?:reply|response|engine|directive|brief)\b.*?"
    r"\b(?:needs? to|flag\w*|correct\w*|applied|appl(?:y|ies)|"
    r"commands?|roll_check|tool|context|wrote|writes?|I|my|me)\b"
    r"|^(?:The|That) (?:engine|directive) (?:has |had |just )?refus\w*",
)
# fleet8-tamsin T9: "I shouldn't have made that second call — … now I have a
# spurious result to discard." Narration is second person: a sentence whose
# subject is "I" and which never says "you" is the model talking to itself.
# Quotes and italics (a letter, a song) are gone before this test runs
_I_SUBJECT = re.compile(
    r"^(?:(?i:but|and|so|now|then|still|actually|honestly|okay|ok|wait|hmm|well|instead|also|again)"
    r"[,—–-]?\s+)*I(?:'(?:m|ll|ve|d)\b|\s+[A-Za-z])"
    r"|^(?i:since|because|if|when|rather than|instead of|given|after|before|now that|although|though|"
    r"while|once|until)\b[^,\"“”:]{0,100},\s+I(?:'(?:m|ll|ve|d)\b|\s+[A-Za-z])",
)
_SECOND_PERSON = re.compile(r"\byou(?:rs?|rself|rselves)?\b", re.IGNORECASE)
# The model summing up where things stand, or laying out the menu, in place
# of a scene (fleet9-bram T12: "The pass is in his coat, Sorrel's warning is
# filed away, and the square around him is still worth a look before he calls
# the night done."; T14: "Whatever's next — another word with the Captain, …
# — is his to choose."; fleet7-bram T17: "the night is yours to spend as you
# choose, whether that's resting up … or seeing what else this square still
# has to offer"). Replayed over every transcript's 2,780 narration sentences:
# those three, nothing else ("the rest is up to you" is a look, and stays)
_RECAP = re.compile(
    r"\b(?:is|are|was|were|'s|now|safely|neatly)\s+filed\s+away\b"
    r"(?!\s+(?:in|into|under|behind|inside|among|beneath|between|on|with)\b)"
    # "your to …" is what "Bram's to decide" comes out as in "you" (`_verdict`)
    r"|\b(?:his|hers|theirs|yours?)\s+to\s+(?:choose|decide|pick"
    # fleet10-wenna T9: "The yard is yours to use as you please — wait here…,
    # go back…, or use the time for something else entirely"
    r"|(?:use|spend|do\s+with)\s+as\s+(?:you|he|she|they)\s+(?:chooses?|likes?|will|pleases?|see\s+fit))\b"
    r"|\bor\s+(?:use\s+the\s+time\s+for|do)\s+something\s+else\s+entirely\b"
    r"|\bcall(?:s|ed|ing)?\s+(?:the|this)\s+night\s+(?:done|over|finished)\b"
    r"|^\W*Whatever(?:'s|\s+is|\s+comes)\s+next\b",
    re.IGNORECASE,
)
# ...and a paragraph closing on the narrator's option list: "you can wait it
# out here…, or go find out more about the cart" (fleet10-wenna T10), "You
# could try to hold out here till first light…, or force yourself up now"
# (fleet10-bram T20). Only the sentence that ENDS its paragraph (`_verdict`),
# never "you can see/hear/tell…" (a look), never a quote. Replayed over every
# transcript's 3,056 narration sentences: 9 hits, every one a closing menu
# (fleet6-tamsin T12, fleet7-bram T8, fleet8-wenna T18, fleet9-tamsin T6,
# fleet9-wenna T3, fleet10-bram T20, fleet10-wenna T9/T10), no fiction lost
_MENU_CLOSE = re.compile(
    r"(?:^|[,;:—–]\s*|\band\s+)(?:now\s+|still\s+|either\s+)?you\s+(?:can|could|might)\s+"
    r"(?:still\s+|either\s+|always\s+)?"
    r"(?!(?:see|hear|feel|smell|taste|tell(?!\s+(?:him|her|them|the|a|an|someone|anyone)\b)|make\s+out|almost"
    r"|just|barely|only|already|not|never|no\s+longer"
    # ...nor a stake or a feeling: "you could lose a foot, or worse", "you
    # might freeze, or starve, or both", "you could swear it came from the
    # ridge, or just beyond it" (review: fiction a paragraph can end on)
    r"|be|swear|lose|die|bleed|freeze|starve|drown|fall|break)\b)"
    r"[a-z][\w'’-]*\b[^.!?]{0,200}?(?:,\s*|\s+[—–]\s+|\s+)or\s+"
    r"(?!(?:just\s+|even\s+)?(?:worse|both|neither|less|more|so|beyond|perhaps|maybe|not|nothing)\b)"
    r"(?:else\s+)?(?:you\s+(?:can|could)\s+)?(?:simply\s+|just\s+|instead\s+)?[a-z]",
    re.IGNORECASE,
)
# A single-quoted line closing inside the sentence ("He mutters, 'I saw it. I
# swear I did.' Then…"): its second sentence opens on the speaker's "I", and
# is theirs. Only straight/curly double quotes are shielded upstream
_SPOKEN_CLOSE = re.compile(r"[.,!?…—–-]'(?=\s|$)")
# A closing italic/bold after the stop ends the sentence too: otherwise a
# planning sentence after "*…did this.*" took the italic line down with it
_SENTENCE_END = re.compile(r'[.!?…][*_"”’)]{0,3}(?=\s)|\n')


def _outside_quotes(sentence: str) -> str:
    return _QUOTED.sub(" ", sentence)


def is_meta_sentence(sentence: str, player_name: str | None = None) -> bool:
    """A sentence of planning or bookkeeping, not fiction.

    Narration is second person; quoted dialogue is excluded before any test.
    """
    s = sentence.strip().lstrip("*_#>-• ").strip()
    if not s:
        return False
    bare = _outside_quotes(s).strip().replace("’", "'")
    if not bare:
        return False
    if _META_OPENERS.match(bare):
        return True
    if _META_TOKENS.search(bare) or _RECAP.search(bare):
        return True
    if (_META_SUBJECT.match(bare) or (_I_SUBJECT.match(bare) and not _SPOKEN_CLOSE.search(bare))) \
            and not _SECOND_PERSON.search(bare):
        return True
    if names_the_player(bare, player_name):
        return True
    return False


def names_the_player(sentence: str, player_name: str | None) -> bool:
    """Narration is second person: a sentence calling the player character
    by name is the model's bookkeeping ("Bram can head north now") — or
    third-person fiction the filter turns back into "you" (`to_second_person`)."""
    if not player_name:
        return False
    first = player_name.split()[0]
    return len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", sentence) is not None


# The player character as the subject: "Bram reads it twice" → "You read it
# twice" (fleet9-bram wrote the whole session in third person, and every
# such sentence was dropped as bookkeeping — 17 replies lost lines)
_THIRD_TO_BASE = {"is": "are", "was": "were", "has": "have", "does": "do", "goes": "go",
                  "isn't": "aren't", "wasn't": "weren't", "hasn't": "haven't", "doesn't": "don't"}
_NOT_A_VERB = frozenset({"as", "us", "this", "thus", "plus", "across", "perhaps", "towards", "unless",
                         "besides", "its", "yes", "always", "sometimes", "nevertheless", "afterwards",
                         "less", "his", "hers", "ours", "theirs", "upwards", "backwards", "forwards"})
_ADVERB = (r"(?:\w+ly|always|never|still|just|already|also|only|even|then|now|finally|again|barely|almost"
           r"|nearly|simply|quietly)")


def _base_form(verb: str) -> str | None:
    """A third-person-singular present verb in its base form, else None."""
    v = verb.lower().replace("’", "'")
    if v in _THIRD_TO_BASE:
        return _THIRD_TO_BASE[v]
    if len(v) < 3 or v in _NOT_A_VERB or not v.endswith("s") or v.endswith(("ss", "us", "is", "'s")):
        return None
    if v.endswith("ies") and len(v) > 4:
        return v[:-3] + "y"
    if re.search(r"(?:ss|sh|ch|x|z|o)es$", v):
        return v[:-2]
    return v[:-1]


def to_second_person(sentence: str, player_name: str | None) -> str:
    """`sentence` with the player character's name made "you" (possessives
    "your", the verb after a subject name in its base form)."""
    from src.services.dialogue import without_player_name

    names = [w for w in (player_name or "").split() if len(w) >= 3]
    if not names:
        return sentence
    who = r"(?:" + r"\s+".join(re.escape(w) for w in names) + r"|" + "|".join(re.escape(w) for w in names) + r")"

    def agree(m: re.Match) -> str:
        base = _base_form(m.group("verb"))
        return m.group(0) if base is None else f"{m.group('pre')}{m.group('who')}{m.group('gap')}{base}"

    # Only where the name is the subject: the sentence's start, or after a
    # clause break ("…, and Bram reads") — never "hands Bram coins"
    fixed = re.sub(
        rf"(?P<pre>^[\s*_\"“(]*|(?:[,;:—–]|\b(?i:and|but|as|while|when|until|before|after|that|so|then|if"
        rf"|once|because|where|though|whether))\s+)"
        rf"(?P<who>{who})(?![\w'’-])(?P<gap>\s+(?:{_ADVERB}\s+)?)(?P<verb>[a-z][\w'’]*)\b",
        agree, sentence)
    return without_player_name(fixed, player_name)


class MetaSentenceFilter:
    """Sentence-buffered: releases each finished sentence unless it is meta.

    Holds at most one sentence, so narration still arrives as it is written;
    dropped sentences are kept in `dropped` for the audit/log.

    `also_drop(sentence)`: a sentence of fiction that no longer holds — the
    turn's `NameGate.comments_on_cut_name` (fleet7-tamsin T11: "She says your
    name like…" after the line lost the name). Kept apart in `retracted`:
    it is no bookkeeping, and the audit must not say so.
    """

    def __init__(self, player_name: str | None = None, also_drop=None) -> None:
        self.player_name = player_name
        self._also_drop = also_drop
        self._buf = ""
        self.dropped: list[str] = []
        self.retracted: list[str] = []
        # Third-person sentences turned back into "you" (the audit says so)
        self.rewritten: list[str] = []
        # Open *italic* / quoted spans, carried from one sentence to the next
        self._in_italic = False
        self._in_quote = False
        # What was released last, and whether a sentence was dropped since:
        # a paragraph that lost its first sentence must not open on a space
        self._tail = "\n"
        self._after_drop = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        out: list[str] = []
        while True:
            m = _SENTENCE_END.search(self._buf)
            if not m:
                break
            end = m.end()
            sentence, self._buf = self._buf[:end], self._buf[end:]
            out.append(self._judge(sentence))
        return "".join(out)

    def flush(self) -> str:
        rest, self._buf = self._buf, ""
        return self._judge(rest) if rest else ""

    def _judge(self, sentence: str) -> str:
        out = self._verdict(sentence)
        if out != sentence and not out.strip():
            self._after_drop = True
        elif self._after_drop and out.strip():
            if self._tail.endswith("\n"):
                out = out.lstrip(" \t")
            self._after_drop = False
        if out:
            self._tail = out
        return out

    def _verdict(self, sentence: str) -> str:
        italic: list[str] = []
        bare = self._outside_spans(sentence, italic)
        # Third-person fiction whose only fault is the player character's
        # name: turned back into "you", never dropped (fleet9-bram)
        if (bare.strip() and not _meta_in_italics("".join(italic)) and not is_meta_sentence(bare)
                and names_the_player(bare, self.player_name)):
            try:
                fixed = to_second_person(sentence, self.player_name)
            except Exception:              # the repair's trouble falls back to the drop
                fixed = None
            # ...unless "you" shows it was the menu all along ("What comes
            # next is Bram's to decide." -> "…your to decide."): dropped below
            if fixed and not names_the_player(_outside_quotes(fixed), self.player_name) \
                    and not is_meta_sentence(_outside_quotes(fixed)):
                self.rewritten.append(sentence.strip())
                return fixed
        if (bare.strip() and is_meta_sentence(bare, self.player_name)) or _meta_in_italics("".join(italic)):
            self.dropped.append(sentence.strip())
            # keep paragraph breaks so the prose around it still reads
            return "\n" if sentence.endswith("\n") else ""
        # A closing option list (`_MENU_CLOSE`): what is left of the buffer
        # says whether this sentence ends its paragraph — a line break next,
        # or nothing at all (flush)
        rest = self._buf.lstrip(" \t")
        if bare.strip() and (sentence.endswith("\n") or not rest or rest.startswith("\n")) \
                and _MENU_CLOSE.search(bare.strip()):
            self.dropped.append(sentence.strip())
            return "\n" if sentence.endswith("\n") else ""
        if self._also_drop is not None and bare.strip():
            try:
                retract = bool(self._also_drop(bare))
            except Exception:              # a hook's trouble never costs the prose
                retract = False
            if retract:
                self.retracted.append(sentence.strip())
                return "\n" if sentence.endswith("\n") else ""
        return sentence

    def _outside_spans(self, sentence: str, italic: list[str] | None = None) -> str:
        """The sentence's words outside *italics* and quotes — with the spans
        that were open when it began — and the spans left open after it.
        `italic` collects the words inside italics (and outside quotes).

        What is inside is the fiction's own text: a letter read, a line of
        song. fleet6-wenna T17: the satchel letter's last sentence ("I'll
        have proof by the time you read this…*") opened like a plan, was
        dropped, and took the closing italic with it. A line break closes
        every span, so a stray "*" can only shield the rest of its line."""
        out: list[str] = []
        i = 0
        while i < len(sentence):
            ch = sentence[i]
            if sentence.startswith("**", i):          # bold, not italics
                i += 2
                continue
            if ch == "\n":
                self._in_italic = self._in_quote = False
            elif ch == "*":
                self._in_italic = not self._in_italic
            elif ch == '"':
                self._in_quote = not self._in_quote
            elif ch == "“":
                self._in_quote = True
            elif ch == "”":
                self._in_quote = False
            elif not (self._in_italic or self._in_quote):
                out.append(ch)
            elif self._in_italic and not self._in_quote and italic is not None:
                italic.append(ch)
            i += 1
        return "".join(out)


# Italics shield the fiction's first person (a letter's "I'll have proof…"),
# never the model's bookkeeping set in italics: "*Let me move the player to
# the tavern first.*" was dropped before the shield and must still be. Inside
# italics a sentence is meta on its tokens ("the player", ids, backticks), or
# on a planning opener that talks mechanics (a roll, a quest, a command)
_MECHANICS_WORDS = re.compile(
    r"\b(?:rolls?|rolled|roll_check|quests?|NPCs?|players?|tiers?|tiered|DM)\b|@\w",
    re.IGNORECASE,
)


def _meta_in_italics(text: str) -> bool:
    s = text.strip().lstrip("_#>-• (").strip()
    if not s:
        return False
    return bool(_META_TOKENS.search(s) or (_META_OPENERS.match(s) and _MECHANICS_WORDS.search(s)))
