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
