"""DialogueDeduper: DM re-quotes of speech-event lines get stripped."""

from src.web.turn_audit import audit_turn
from src.web.turn_filters import DialogueDeduper


SPOKEN = "The howls ain't natural, I'm telling you. Drilled-wrong, like something taught them."


def test_exact_requote_stripped():
    d = DialogueDeduper()
    d.register_speech(SPOKEN)
    text = f'Piper leans in. "{SPOKEN}" Her hands are shaking.'
    out = d.filter_block(text)
    assert SPOKEN not in out
    assert "Her hands are shaking" in out


def test_fragment_requote_stripped():
    d = DialogueDeduper()
    d.register_speech(SPOKEN)
    text = 'She repeats that the howls "ain\'t natural, I\'m telling you" and looks away.'
    out = d.filter_block(text)
    assert "ain't natural" not in out


def test_unrelated_quote_passes():
    d = DialogueDeduper()
    d.register_speech(SPOKEN)
    text = 'A sign above the door reads "The Drowned Lantern — rooms and ale".'
    assert d.filter_block(text) == text


def test_inactive_deduper_is_transparent():
    d = DialogueDeduper()
    text = f'"{SPOKEN}" she says.'
    assert d.filter_block(text) == text
    assert d.feed("chunk one ") == "chunk one "  # zero latency before speech


def test_streaming_feed_releases_at_boundaries():
    d = DialogueDeduper()
    d.register_speech(SPOKEN)
    # Feed a re-quote split across chunks, then clean text
    out = d.feed('Piper repeats, "The howls ain\'t natural, ')
    out += d.feed("I'm telling you. Drilled-wrong, like something taught them.\" ")
    out += d.feed("The fire cracks. ")
    out += d.flush()
    assert "howls" not in out
    assert "The fire cracks." in out


def test_flush_returns_remainder():
    d = DialogueDeduper()
    d.register_speech(SPOKEN)
    d.feed("An unfinished sentence with no boundary")
    assert "unfinished sentence" in d.flush()


# ---------------------------------------------------------------------------
# Turn audit
# ---------------------------------------------------------------------------

STATE_A = {"health": "healthy", "inventory": [], "location_id": "loc1", "currency": 10}


def _tools(*names):
    return [{"name": n, "status": "success"} for n in names]


def test_audit_quest_banner_without_tool():
    warnings = audit_turn("QUEST ACCEPTED: Teeth on the North Road", [], STATE_A, STATE_A)
    assert any(w["check"] == "quest_banner_without_tool" for w in warnings)


def test_audit_quest_banner_with_tool_clean():
    warnings = audit_turn(
        "Quest accepted: Teeth on the North Road", _tools("activate_quest"), STATE_A, STATE_A
    )
    assert warnings == []


def test_audit_damage_without_tool():
    warnings = audit_turn("The wolf rakes your arm — you are bleeding badly.", [], STATE_A, STATE_A)
    assert any(w["check"] == "damage_without_tool" for w in warnings)


def test_audit_damage_with_health_change_clean():
    after = dict(STATE_A, health="hurt")
    warnings = audit_turn(
        "The wolf rakes your arm — you are bleeding badly.",
        _tools("update_player_health"), STATE_A, after,
    )
    assert warnings == []


def test_audit_arrival_without_move():
    warnings = audit_turn("You arrive at Wolf's Hollow as dusk falls.", [], STATE_A, STATE_A)
    assert any(w["check"] == "arrival_without_move" for w in warnings)


def test_audit_clean_narration():
    warnings = audit_turn(
        "The market hums around you. Piper watches from her stall.", [], STATE_A, STATE_A
    )
    assert warnings == []


def test_audit_first_person_injury():
    # Live playtest: DM narrated "I've pulled something in my shoulder" toolless
    warnings = audit_turn("I've pulled something in my shoulder.", [], STATE_A, STATE_A)
    assert any(w["check"] == "damage_without_tool" for w in warnings)


def test_audit_fake_consequence_ledger():
    # Live playtest: DM printed "Consequences applied: ... disposition is now
    # hostile ... an event 'Brawl' is created" with zero tool calls
    text = ("Consequences applied: Captain Coldiron's disposition is now hostile. "
            "An event 'Brawl in Market Square' is created.")
    warnings = audit_turn(text, [], STATE_A, STATE_A)
    assert any(w["check"] == "consequences_without_tools" for w in warnings)
    # Same text WITH the tools succeeding is clean
    warnings = audit_turn(text, _tools("update_npc_relationship", "create_event"), STATE_A, STATE_A)
    assert not any(w["check"] == "consequences_without_tools" for w in warnings)


def test_audit_fabricated_named_dialogue():
    # Watchlist item: DM writes NEW quoted lines for named NPCs inline —
    # never delivered through speak/prompt_npc_agent, so the player hears nothing
    text = 'Captain Coldiron growls, "You picked the wrong gate, stranger." His hand finds the axe.'
    warnings = audit_turn(text, [], STATE_A, STATE_A)
    assert any(w["check"] == "dialogue_without_speak" for w in warnings)


def test_audit_fabricated_dialogue_postfix_attribution():
    text = '"The Hollow takes what it wants," Piper says, not looking up.'
    warnings = audit_turn(text, [], STATE_A, STATE_A)
    assert any(w["check"] == "dialogue_without_speak" for w in warnings)


def test_audit_ambient_dialogue_inline_flagged():
    # The old prompt sanctioned exactly this via narrate — now it must go
    # through speak() so it gets voice treatment
    text = "A guard calls out: 'Halt! State your business!'"
    warnings = audit_turn(text, [], STATE_A, STATE_A)
    assert any(w["check"] == "dialogue_without_speak" for w in warnings)


def test_audit_speak_format_mimicry_flagged():
    text = 'Coldiron: "Get out of my square before I lose patience."'
    warnings = audit_turn(text, [], STATE_A, STATE_A)
    assert any(w["check"] == "dialogue_without_speak" for w in warnings)


def test_audit_signage_quote_clean():
    text = 'The sign says "No entry after dark" in faded paint.'
    warnings = audit_turn(text, [], STATE_A, STATE_A)
    assert not any(w["check"] == "dialogue_without_speak" for w in warnings)


def test_audit_player_own_words_clean():
    text = '"Let me pass," you say, keeping your voice level.'
    warnings = audit_turn(text, [], STATE_A, STATE_A)
    assert not any(w["check"] == "dialogue_without_speak" for w in warnings)


def test_audit_short_exclamation_clean():
    # One- or two-word quotes are texture, not fabricated dialogue
    text = 'He mutters "damn" under his breath and turns away.'
    warnings = audit_turn(text, [], STATE_A, STATE_A)
    assert not any(w["check"] == "dialogue_without_speak" for w in warnings)


def test_audit_feedback_formatting():
    from src.web.turn_audit import format_audit_feedback
    assert format_audit_feedback([]) == ""
    out = format_audit_feedback([{"check": "x", "detail": "Fake quest banner."}])
    assert "Fake quest banner." in out and "REALITY AUDIT" in out
