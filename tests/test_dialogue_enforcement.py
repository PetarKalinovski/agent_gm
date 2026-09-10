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
