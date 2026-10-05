"""Every spoken line reaches the player as speech — enforced in code.

Playtesting (2026-09-11) found a whole conversation delivered as action-beat
prose: `She wipes her hands on her apron. "I run this place..."` — no
said-tag, so the attribution-based audit saw nothing, and not one line was
voiced. These tests pin the three layers that now catch it:

- `unspoken_quotes` finds quotes with no speech tool behind them
- the audit's `dialogue_without_speak` fires on action-beat quotes
- `speech_events_for_unspoken` turns them into real speech events, and the
  tracker fans an `events` list out like standalone results
"""

import json

from src.web.streaming import ToolUsageTracker
from src.web.turn_audit import audit_turn, unspoken_quotes
from src.web.turn_filters import speech_events_for_unspoken

STATE = {"health": "healthy", "inventory": [], "location_id": "loc1", "currency": 10}

ACTION_BEATS = (
    'The amber liquid catches the firelight as Maera slides the glass toward you. '
    '"Halloran," she says, the name tasting of old ash. "He was a good man. A '
    'woodcutter, lived out past the north gate." She wipes her hands on her apron. '
    '"I run this place since my father passed. You are new to Emberfall." '
    'Outside, the evening deepens.'
)


def _checks(warnings):
    return {w["check"] for w in warnings}


# --------------------------------------------------------------------------
# unspoken_quotes
# --------------------------------------------------------------------------

def test_action_beat_quotes_are_found():
    quotes = unspoken_quotes(ACTION_BEATS)
    assert quotes == [
        "He was a good man. A woodcutter, lived out past the north gate.",
        "I run this place since my father passed. You are new to Emberfall.",
    ]  # "Halloran," is under four words and stays with the prose


def test_players_own_reported_words_are_not_dialogue():
    text = (
        'You lean on the bar and say, "I have a letter for a dead man, and I mean '
        'to deliver it." The room goes quiet. '
        '"Then you had better find his grave," you add, and nobody laughs.'
    )
    assert unspoken_quotes(text) == []


def test_written_text_is_not_dialogue():
    text = (
        'The notice reads: "All lanterns lit at dusk by order of the Watch." '
        'Below it, in a clerk\'s hand: "No carts after the howl." You move on.'
    )
    assert unspoken_quotes(text) == []


def test_curly_quotes_count_too():
    text = 'He shrugs. “The road pays or the road closes, courier.”'
    assert unspoken_quotes(text) == ["The road pays or the road closes, courier."]


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def test_audit_flags_action_beat_dialogue():
    warnings = audit_turn(ACTION_BEATS, [], STATE, STATE)
    assert "dialogue_without_speak" in _checks(warnings)


def test_audit_stays_clean_for_player_words_and_signage():
    text = (
        'You say, "I am only passing through." The sign above the door reads: '
        '"The Drowned Lantern — rooms by the night." Nobody answers you.'
    )
    warnings = audit_turn(text, [], STATE, STATE)
    assert "dialogue_without_speak" not in _checks(warnings)


# --------------------------------------------------------------------------
# speech_events_for_unspoken
# --------------------------------------------------------------------------

def test_unspoken_lines_become_speech_events():
    events = speech_events_for_unspoken(ACTION_BEATS, [], "Maera Duskwell")
    assert [e["event"] for e in events] == ["speech", "speech"]
    assert events[0]["npc"] == "Maera Duskwell"
    assert events[0]["text"].startswith("He was a good man")
    assert "synthesized" not in events[0]


def test_lines_already_spoken_are_skipped():
    spoken = ["He was a good man. A woodcutter, lived out past the north gate."]
    events = speech_events_for_unspoken(ACTION_BEATS, spoken, "Maera Duskwell")
    assert len(events) == 1
    assert events[0]["text"].startswith("I run this place")


def test_synthesized_flag_and_limit():
    text = " ".join(f'"Line number {i} of many many words here."' for i in range(10))
    events = speech_events_for_unspoken(text, [], "Piper", synthesized=True, limit=3)
    assert len(events) == 3
    assert all(e["synthesized"] is True for e in events)


# --------------------------------------------------------------------------
# tracker fan-out of an `events` list
# --------------------------------------------------------------------------

def _tool_result_message(result: dict) -> dict:
    return {
        "message": {
            "role": "user",
            "content": [{
                "toolResult": {
                    "toolUseId": "t1",
                    "name": "prompt_npc_agent",
                    "status": "success",
                    "content": [{"text": json.dumps(result)}],
                }
            }],
        }
    }


def test_tracker_fans_out_event_list_in_order():
    tracker = ToolUsageTracker()
    result = {
        "text_response": "prose",
        "events": [
            {"event": "speech", "npc": "Maera", "text": "First line spoken here.", "tone": "normal"},
            {"event": "speech", "npc": "Maera", "text": "Second line spoken here.", "tone": "normal",
             "synthesized": True},
        ],
    }
    tracker.process_stream_payload(_tool_result_message(result))
    speech = [n for n in tracker.drain_notifications() if n.get("type") == "speech"]
    assert [s["text"] for s in speech] == ["First line spoken here.", "Second line spoken here."]
    assert "synthesized" not in speech[0]
    assert speech[1]["synthesized"] is True


# --------------------------------------------------------------------------
# Code fences never reach the player; what is inside them is executed
# --------------------------------------------------------------------------

from src.web.tool_rescue import parse_call, resolve_npc
from src.web.turn_filters import CodeFenceFilter


def _stream(filter_, chunks):
    out = "".join(filter_.feed(c) for c in chunks)
    return out + filter_.flush()


def test_fence_is_held_back_and_captured():
    f = CodeFenceFilter()
    text = _stream(f, ["He turns as you approach. ", "```json\n{\"npc_id\": \"x\"}\n```", " A scout, then."])
    assert text == "He turns as you approach.  A scout, then."
    assert f.fences == ['json\n{"npc_id": "x"}\n']


def test_fence_split_across_tokens():
    f = CodeFenceFilter()
    text = _stream(f, ["Rain. `", "``json\n{\"a\": 1}", "\n``", "` Wind."])
    assert text == "Rain.  Wind."
    assert f.fences == ['json\n{"a": 1}\n']


def test_unterminated_fence_is_swallowed_at_flush():
    f = CodeFenceFilter()
    text = _stream(f, ["Before. ```json\n{\"npc_id\": \"y\", \"player_input\": \"hi\"}"])
    assert text == "Before. "
    assert len(f.fences) == 1 and '"npc_id": "y"' in f.fences[0]


def test_lone_backticks_in_prose_pass_through():
    f = CodeFenceFilter()
    assert _stream(f, ["a `word` and `` two ``", " done"]) == "a `word` and `` two `` done"
    assert f.fences == []


def test_parse_call_reads_bare_arguments_and_explicit_forms():
    bare = 'json\n{"player_id": "p", "npc_id": "brann_coldiron", "player_input": "Captain.", "is_first_interaction": true, "context": "c"}\n'
    assert parse_call(bare) == ("prompt_npc_agent", {
        "player_id": "p", "npc_id": "brann_coldiron", "player_input": "Captain.",
        "is_first_interaction": True, "context": "c",
    })
    explicit = '{"tool": "speak", "arguments": {"npc_name": "Guard", "text": "Halt there.", "tone": "shouted"}}'
    assert parse_call(explicit) == ("speak", {"npc_name": "Guard", "text": "Halt there.", "tone": "shouted"})
    assert parse_call("{\"tool\": \"move_player\", \"arguments\": {}}") is None
    assert parse_call("just prose in a fence") is None
    assert parse_call("[1, 2, 3]") is None


def test_resolve_npc_accepts_ids_names_and_slugs(db):
    from src.models import Location, LocationType, NPC, get_session

    with get_session() as session:
        square = Location(name="Square", type=LocationType.DISTRICT)
        hollow = Location(name="Hollow", type=LocationType.DISTRICT)
        session.add_all([square, hollow])
        session.flush()
        brann = NPC(name="Captain Brann Coldiron", current_location_id=square.id)
        vex = NPC(name="Vex Marrow", current_location_id=hollow.id)
        session.add_all([brann, vex])
        session.commit()
        brann_id, square_id = brann.id, square.id

    with get_session() as session:
        assert resolve_npc(session, brann_id, None).name == "Captain Brann Coldiron"
        assert resolve_npc(session, "brann_coldiron", square_id).name == "Captain Brann Coldiron"
        assert resolve_npc(session, "Vex", square_id).name == "Vex Marrow"
        assert resolve_npc(session, "nobody_here", square_id) is None
        assert resolve_npc(session, None, square_id) is None


# --------------------------------------------------------------------------
# Tool-result echoes are dropped line by line
# --------------------------------------------------------------------------

from src.web.turn_filters import JunkLineFilter


def test_junk_lines_are_dropped_and_prose_kept():
    f = JunkLineFilter()
    text = _stream(f, ["He nods.\n[]\n\n[{\"type\": \"text\", ", "\"text\": \"\\n[]\\n\"}]\nThe square empties."])
    assert text == "He nods.\n\nThe square empties."
    assert f.dropped == ["[]", '[{"type": "text", "text": "\\n[]\\n"}]']


def test_junk_filter_holds_partial_lines():
    f = JunkLineFilter()
    assert f.feed("Half a li") == ""
    assert f.feed("ne.\n[") == "Half a line.\n"
    assert f.feed("]") == ""
    assert f.flush() == ""          # the held "[]" was junk
    assert f.dropped == ["[]"]


def test_brackets_inside_prose_survive():
    f = JunkLineFilter()
    text = _stream(f, ["The ledger reads [illegible] here.\n", "A list: [] of nothing, he jokes."])
    assert text == "The ledger reads [illegible] here.\nA list: [] of nothing, he jokes."


def test_json_blob_pasted_into_prose_is_removed():
    f = JunkLineFilter()
    line = ('Quest Updated: Teeth on the North Road activated{"id": "e63f", "title": "Teeth", '
            '"rewards": {"currency": 60, "items": ["Wolf-Tooth Charm"]}} He nods.\n')
    assert _stream(f, [line]) == "Quest Updated: Teeth on the North Road activated He nods.\n"
    assert f.dropped and f.dropped[0].startswith('{"id": "e63f"')


def test_prose_brackets_are_not_json():
    f = JunkLineFilter()
    text = "A note reads {the whole square} and [illegible] and {\"unterminated\n"
    assert _stream(f, [text]) == text
    assert f.dropped == []


def test_same_speech_result_seen_twice_renders_once():
    tracker = ToolUsageTracker()
    result = {"success": True, "npc": "Brann", "tone": "grim", "event": "speech",
              "text": "You have got it. Brass, in your hand now.", "action": None}
    tracker.process_stream_payload(_tool_result_message(result))
    tracker.process_stream_payload(_tool_result_message(result))
    speech = [n for n in tracker.drain_notifications() if n.get("type") == "speech"]
    assert len(speech) == 1


# --------------------------------------------------------------------------
# A paragraph is read once per turn
# --------------------------------------------------------------------------

from src.web.turn_filters import SentenceDeduper

PARA = ("Maera's eyes stay on you a beat too long before they settle, polite and unhurried. "
        "The hearth pops behind her. A few of the trestle tables are empty this late.")


def test_repeated_paragraph_is_dropped_second_time():
    d = SentenceDeduper()
    assert d.filter(PARA) == PARA
    assert d.filter(PARA).strip() == ""
    # a fresh sentence after the repeat still gets through
    assert d.filter(PARA + " Then the door bangs open behind you.").strip() == "Then the door bangs open behind you."


def test_a_dropped_repeat_keeps_the_paragraph_break_after_it():
    # A model that repeats its pre-roll sentence after the tool: the break
    # went with it and the next paragraph glued on ("slow.The lamp")
    d = SentenceDeduper()
    d.filter("He weighs whether honesty costs him anything tonight.\n")
    assert d.filter("He weighs whether honesty costs him anything tonight. "
                    "Something shifts behind his eyes, slow.\n\n") == "Something shifts behind his eyes, slow.\n\n"
    assert d.filter("Something shifts behind his eyes, slow. The lamp gutters low, trailing \n") == (
        "The lamp gutters low, trailing \n")


def test_short_sentences_are_never_deduped():
    d = SentenceDeduper()
    assert d.filter("Yes. Yes. Yes.") == "Yes. Yes. Yes."
    assert d.filter("Yes.") == "Yes."


def test_narration_then_tokens_reads_once():
    d = SentenceDeduper()
    assert d.filter(PARA) == PARA                       # narrate() block
    tokens = d.filter("Maera's eyes stay on you a beat too long before they settle, polite and unhurried. ")
    assert tokens.strip() == ""                         # the echo
