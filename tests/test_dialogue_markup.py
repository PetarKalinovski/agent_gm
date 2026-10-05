"""The DM's reply as a screenplay: prose streams, speaker lines become
speech, `@command` lines become commands and never reach the player.

Every stream test runs the same reply three ways — whole, in uneven chunks,
and one character at a time — because the model's tokens split lines
anywhere, and a filter that only works on whole lines leaks.
"""

import pytest

from src.web.dialogue_markup import (
    DialogueMarkupFilter,
    MetaSentenceFilter,
    could_be_dialogue,
    is_meta_sentence,
    parse_dialogue_line,
)


def _chunks(text: str, mode: str) -> list[str]:
    if mode == "whole":
        return [text]
    if mode == "chars":
        return list(text)
    sizes, out, i, k = (3, 7, 1, 11, 5, 2), [], 0, 0
    while i < len(text):
        out.append(text[i:i + sizes[k % len(sizes)]])
        i += sizes[k % len(sizes)]
        k += 1
    return out


def _segments(text: str, mode: str, **kwargs) -> list[tuple[str, object]]:
    """Every segment the filter emits, adjacent text runs merged."""
    f = DialogueMarkupFilter(**kwargs)
    raw = []
    for chunk in _chunks(text, mode):
        raw.extend(f.feed_segments(chunk))
    raw.extend(f.flush_segments())
    merged: list[tuple[str, object]] = []
    for kind, value in raw:
        if kind == "text" and merged and merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + value)
        else:
            merged.append((kind, value))
    return merged


MODES = ("whole", "chunks", "chars")


# ----------------------------------------------------------------------
# Speaker lines
# ----------------------------------------------------------------------

def test_parse_dialogue_line_shapes():
    p = parse_dialogue_line('Coldiron (grim): "Wolves are smarter than they ought to be."')
    assert p["speaker"] == "Coldiron" and p["tone"] == "grim"
    assert p["text"] == "Wolves are smarter than they ought to be." and p["quoted"]

    p = parse_dialogue_line('Mara Vell (warm, wiping a mug): "Sit. Eat." She turns away.')
    assert p["speaker"] == "Mara Vell" and p["tone"] == "warm"
    assert p["action"] == "wiping a mug" and p["remainder"] == "She turns away."

    p = parse_dialogue_line('**Gate Guard**: "Papers."')
    assert p["speaker"] == "Gate Guard" and p["text"] == "Papers."

    # Prose with a colon is not a speaker line
    assert parse_dialogue_line("You see three things: a knife, a key, a coin.") is None
    assert parse_dialogue_line("the rain falls: steady, cold") is None


@pytest.mark.parametrize("mode", MODES)
def test_speaker_lines_become_speech_in_order(mode):
    reply = (
        "The fire spits. Coldiron does not look up.\n"
        'Coldiron (grim): "Wolves are smarter than they ought to be."\n'
        "He pushes the map toward you.\n"
        'Piper: "I can show you the road."\n'
    )
    segs = _segments(reply, mode)
    kinds = [k for k, _ in segs]
    assert kinds == ["text", "speech", "text", "speech"]
    assert segs[0][1] == "The fire spits. Coldiron does not look up.\n"
    assert segs[1][1]["speaker"] == "Coldiron" and segs[1][1]["tone"] == "grim"
    assert segs[2][1] == "He pushes the map toward you.\n"
    assert segs[3][1]["speaker"] == "Piper" and segs[3][1]["text"] == "I can show you the road."


@pytest.mark.parametrize("mode", MODES)
def test_narration_passes_through_untouched(mode):
    reply = (
        "Rain needles the cobbles. You smell tar and wet wool.\n\n"
        "The square empties: hawkers drag their carts under the eaves, and "
        "someone laughs too loudly in the tavern.\n"
        "it is late."
    )
    segs = _segments(reply, mode)
    assert segs == [("text", reply)]


def test_unquoted_line_needs_a_tone_or_a_known_name():
    plain = DialogueMarkupFilter()
    assert plain.feed("Note: the gate is shut.\n") == "Note: the gate is shut.\n"
    toned = DialogueMarkupFilter()
    assert toned.feed("Coldiron (dry): Fine.\n") == ""
    assert toned.drain_speech()[0]["text"] == "Fine."
    known = DialogueMarkupFilter(accept_unquoted=lambda speaker, tone: speaker == "Coldiron")
    assert known.feed("Coldiron: Fine.\n") == ""
    assert known.drain_speech()[0]["speaker"] == "Coldiron"


def test_ordinary_prose_releases_early():
    """A narration line streams within a few characters — it is not held to the newline."""
    f = DialogueMarkupFilter()
    assert f.feed("You walk") == "You walk"
    assert f.feed(" north.") == " north."
    assert not could_be_dialogue("you walk")
    assert could_be_dialogue("Coldiron (")


# ----------------------------------------------------------------------
# Command lines
# ----------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_command_lines_become_command_segments(mode):
    reply = (
        "@give Gate Pass | kind: key | about: A brass-sealed pass.\n"
        "@trust Coldiron +5\n"
        "Coldiron slides the pass across the table.\n"
        'Coldiron (grim): "North gate. Show that and they\'ll open."\n'
        "@move North Gate | through\n"
        "You step out into the rain."
    )
    segs = _segments(reply, mode)
    assert [k for k, _ in segs] == ["command", "command", "text", "speech", "command", "text"]
    assert segs[0][1] == "@give Gate Pass | kind: key | about: A brass-sealed pass."
    assert segs[1][1] == "@trust Coldiron +5"
    assert segs[2][1] == "Coldiron slides the pass across the table.\n"
    assert segs[3][1]["speaker"] == "Coldiron"
    assert segs[4][1] == "@move North Gate | through"
    assert segs[5][1] == "You step out into the rain."
    # Not one character of a command reaches the player
    shown = "".join(v for k, v in segs if k == "text")
    assert "@" not in shown and "Gate Pass" not in shown


@pytest.mark.parametrize("mode", MODES)
def test_command_held_until_newline_and_flushed_at_end(mode):
    f = DialogueMarkupFilter()
    emitted = []
    for chunk in _chunks("The door groans.\n@time 20m", mode):
        emitted.extend(f.feed_segments(chunk))
    # Without a newline the command is still held — nothing of it is out
    assert all(k == "text" for k, _ in emitted)
    assert "".join(v for _, v in emitted) == "The door groans.\n"
    assert f.flush_segments() == [("command", "@time 20m")]
    assert f.flush_segments() == []


def test_indented_and_backticked_commands_are_still_commands():
    segs = _segments("   @coin -12\n`@take Torch x2`\nYou pay.\n", "chars")
    assert segs == [("command", "@coin -12"), ("command", "@take Torch x2"), ("text", "You pay.\n")]
    assert could_be_dialogue("@") and could_be_dialogue("  @giv") and could_be_dialogue("`")


@pytest.mark.parametrize("mode", MODES)
def test_bulleted_commands_never_reach_the_player(mode):
    reply = "You pocket the pass.\n- @give Gate Pass | kind: key\n* @trust Coldiron +5\n- a dented helm\n"
    segs = _segments(reply, mode)
    assert segs == [
        ("text", "You pocket the pass.\n"),
        ("command", "@give Gate Pass | kind: key"),
        ("command", "@trust Coldiron +5"),
        ("text", "- a dented helm\n"),
    ]
    assert could_be_dialogue("* ") and could_be_dialogue("- `")


def test_at_sign_inside_prose_is_prose():
    segs = _segments("You meet her @ the docks.\nCoins: 3 left.\n", "chars")
    assert segs == [("text", "You meet her @ the docks.\nCoins: 3 left.\n")]


def test_command_line_is_never_parsed_as_speech():
    segs = _segments('@note Coldiron | She said: "I lied about the letter."\n', "chunks")
    assert segs == [("command", '@note Coldiron | She said: "I lied about the letter."')]


def test_text_helpers_queue_commands_instead_of_leaking_them():
    f = DialogueMarkupFilter()
    out = f.feed("@give Rope\nYou coil it.\n")
    out += f.feed('Piper (wry): "Handy."\n@join Piper')
    out += f.flush()
    assert out == "You coil it.\n"
    assert f.drain_commands() == ["@give Rope", "@join Piper"]
    assert f.commands == []
    assert [s["speaker"] for s in f.drain_speech()] == ["Piper"]


def test_blank_command_marker_is_swallowed():
    assert _segments("@\nYou wait.\n", "whole") == [("text", "You wait.\n")]


# ----------------------------------------------------------------------
# Meta sentences
# ----------------------------------------------------------------------

def test_meta_sentences_are_dropped_fiction_kept():
    f = MetaSentenceFilter(player_name="Ilse Marr")
    out = f.feed("The lamp gutters. Let me move the player to the gate. ")
    out += f.feed("Rain hammers the shutters. The quest is now active. ")
    out += f.feed('Ilse has the lead now. "Mind the player," he says.')
    out += f.flush()
    assert "The lamp gutters." in out and "Rain hammers the shutters." in out
    assert "Let me" not in out and "quest is now active" not in out and "Ilse has" not in out
    assert len(f.dropped) == 3


def test_meta_detection_ignores_quoted_words():
    assert not is_meta_sentence('"The player? Never heard of them," she says.')
    assert is_meta_sentence("I'll have Coldiron answer next.")
    assert not is_meta_sentence("You feel the weight of the coin in your pocket.")


def test_planning_sentences_from_round_seven_are_dropped():
    # fleet8-tamsin T9 and T13 reached the player word for word
    assert is_meta_sentence("I shouldn't have made that second call — it was unnecessary "
                            "and now I have a spurious result to discard.")
    assert is_meta_sentence("This reply needs to correct that and actually send them through "
                            "the gate, matching their stated intent to move north and then cut east.")
    assert is_meta_sentence("I won't repeat the howl pattern here.")
    assert is_meta_sentence("I don't need another roll for this.")
    assert is_meta_sentence("The audit says the gate was never crossed.")
    assert is_meta_sentence("The reply needs to land the payment first.")
    assert is_meta_sentence("Since the stable hand isn't present, I need to move them first.")
    assert is_meta_sentence("But I already wrote the move above.")
    assert is_meta_sentence("Per the brief, nobody else is here.")
    # curly apostrophes plan just as well
    assert is_meta_sentence("I shouldn’t have rolled twice.")


def test_first_person_rule_spares_the_fiction():
    # Narration is second person; the story's own "I" lives in quotes/italics
    assert not is_meta_sentence('"I shouldn\'t be telling you this," she mutters.')
    assert not is_meta_sentence("I never thought you'd come back, the note says.")
    assert not is_meta_sentence("The reply comes from the hills: a single long howl.")
    assert not is_meta_sentence("The second call comes, closer than the first.")
    assert not is_meta_sentence("After a brief pause, the guard waves the cart on.")
    assert not is_meta_sentence("The brief flare of the match shows a scratched door.")
    assert not is_meta_sentence("In the loft, the horses shift and stamp.")
    f = MetaSentenceFilter()
    out = f.feed("The wind turns. *I will find the man who did this.* ")
    out += f.feed("I shouldn't have made that second call. Sorrel shivers.\n")
    out += f.flush()
    assert out == "The wind turns. *I will find the man who did this.* Sorrel shivers.\n"
    assert f.dropped == ["I shouldn't have made that second call."]


def test_round_seven_rules_keep_the_fiction_they_could_mistake():
    # "the brief" as an adjective, a reply that is a growl, a pack's lead
    # wolf, a single-quoted line's second sentence: all the story's own
    assert not is_meta_sentence("The brief, sharp bark of a fox cuts the dark.")
    assert not is_meta_sentence("The response is a growl that has to be a warning.")
    assert not is_meta_sentence("The reply is a low growl; the wolf refuses to give ground.")
    assert not is_meta_sentence("The lead wolf breaks from the birches.")
    assert not is_meta_sentence("Sorrel takes the lead, lantern high.")
    f = MetaSentenceFilter()
    out = f.feed("He mutters, 'I saw it. I swear I did.' Then he looks away.\n") + f.flush()
    assert out == "He mutters, 'I saw it. I swear I did.' Then he looks away.\n" and f.dropped == []
    # ...and the bookkeeping they share words with still goes
    assert is_meta_sentence("According to the brief, the stable hand is a young man.")
    assert is_meta_sentence("The gate stays shut, per the directive.")
    assert is_meta_sentence("The lead points at the gatehouse.")
    assert is_meta_sentence("The engine refused the move, so the gate stays shut.")
    assert is_meta_sentence("I swear I did.")


@pytest.mark.parametrize("written, shown", [
    # fleet9-bram: the DM wrote the session in third person and the filter
    # dropped every such sentence as bookkeeping (17 replies lost lines)
    ("Bram reads it twice — cargo gone, drivers left breathing.",
     "You read it twice — cargo gone, drivers left breathing."),
    ("He looks at Bram the way a quartermaster looks at a crate.",
     "He looks at you the way a quartermaster looks at a crate."),
    ("The sweetroot is whole, and Bram pockets it without breaking stride.",
     "The sweetroot is whole, and you pocket it without breaking stride."),
    ("Bram's jaw aches from the pressure of it.", "Your jaw aches from the pressure of it."),
    ("As Bram pushes back from the bar, a woman looks up.", "As you push back from the bar, a woman looks up."),
    ("Bram Holloway is not a patient man.", "You are not a patient man."),
    ("The Captain hands Bram coins and turns away.", "The Captain hands you coins and turns away."),
])
def test_third_person_fiction_is_turned_into_you(written, shown):
    from src.web.dialogue_markup import MetaSentenceFilter

    meta = MetaSentenceFilter("Bram Holloway")
    assert (meta.feed(written + " ") + meta.flush()).strip() == shown
    assert meta.rewritten and not meta.dropped


def test_planning_that_names_the_player_is_still_dropped():
    from src.web.dialogue_markup import MetaSentenceFilter

    meta = MetaSentenceFilter("Bram Holloway")
    out = meta.feed("Bram can head north now; the lead points to the gatehouse. ") + meta.flush()
    assert not out.strip() and meta.dropped and not meta.rewritten
