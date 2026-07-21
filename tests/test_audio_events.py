"""The audio layer's structured SSE events and voice assignment.

speak()/describe_location() surface metadata through their tool-result
dicts (the npc_death mechanism); the ToolUsageTracker must turn those
into speech/scene notifications or the frontend never hears anything.
"""

from src.services.voice_generator import VoiceGenerator, compute_audio_id
from src.tools.narration import describe_location, speak
from src.web.streaming import ToolUsageTracker


def _message_with_tool_result(result_dict) -> dict:
    """Simulate a Strands toolResult message carrying a tool's return dict."""
    return {
        "message": {
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "status": "success",
                        # Tool results arrive as Python-repr strings
                        "content": [{"text": str(result_dict)}],
                    }
                }
            ]
        }
    }


def _notifications_of_type(tracker: ToolUsageTracker, type_: str) -> list[dict]:
    return [n for n in tracker.drain_notifications() if n.get("type") == type_]


def test_speak_result_carries_speech_event():
    result = speak(npc_name="Mira", text="You should not have come here.", tone="whispered", action="steps back")
    assert result["event"] == "speech"

    tracker = ToolUsageTracker()
    tracker.process_stream_payload(_message_with_tool_result(result))
    events = _notifications_of_type(tracker, "speech")
    assert len(events) == 1
    assert events[0]["npc_name"] == "Mira"
    assert events[0]["text"] == "You should not have come here."
    assert events[0]["tone"] == "whispered"
    assert events[0]["action"] == "steps back"


def test_describe_location_result_carries_scene_event():
    result = describe_location(
        name="The Sunken Vault", description="Water drips.", atmosphere=["damp", "echoing"], time_of_day="night"
    )
    assert result["event"] == "scene"

    tracker = ToolUsageTracker()
    tracker.process_stream_payload(_message_with_tool_result(result))
    events = _notifications_of_type(tracker, "scene")
    assert len(events) == 1
    assert events[0]["location"] == "The Sunken Vault"
    assert events[0]["time_of_day"] == "night"
    assert events[0]["atmosphere"] == ["damp", "echoing"]


def test_npc_death_event_still_parses():
    tracker = ToolUsageTracker()
    tracker.process_stream_payload(_message_with_tool_result(
        {"event": "npc_death", "npc_id": "n1", "npc_name": "Bandit", "cause_of_death": "stabbed"}
    ))
    events = _notifications_of_type(tracker, "npc_death")
    assert len(events) == 1
    assert events[0]["npc_id"] == "n1"


def test_audio_id_is_stable_and_tone_sensitive():
    a = compute_audio_id("npc-1", "Hello there", "normal")
    assert a == compute_audio_id("npc-1", "Hello there", "normal")
    assert a != compute_audio_id("npc-1", "Hello there", "angry")
    assert a != compute_audio_id("npc-2", "Hello there", "normal")


def test_voice_pool_assignment_matches_gender_and_is_stable(db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # voice dirs are created under cwd/data
    from src.models import NPC, get_session

    with get_session() as session:
        npc = NPC(
            name="Sera",
            description_physical="An old woman with silver hair and a weathered face",
            voice_pattern="Speaks slowly, raspy",
        )
        session.add(npc)
        session.commit()
        npc_id = npc.id

    gen = VoiceGenerator("testworld")
    first = gen.ensure_voice_assigned(npc_id)
    assert first is not None

    from src.config import load_settings
    pool = {e.voice_id: e.tags for e in load_settings().audio.voice_pool}
    assert "female" in pool[first]

    # Persisted: the same voice comes back without re-picking
    assert gen.ensure_voice_assigned(npc_id) == first
