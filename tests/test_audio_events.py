"""The tracker's tool-result fan-out and voice assignment.

speak()/describe_location() surface metadata through their tool-result
dicts (the npc_death mechanism); the ToolUsageTracker must turn those
into speech/scene notifications or the frontend never hears anything.
The same mechanism now carries check results, cinematics, and the
progression block that rides ALONGSIDE another event (integration §2.2).
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


_PLUMBING = {"tool_update", "tool_summary"}


def _game_events(result_dict) -> list[dict]:
    """Feed one tool result through the tracker, in-order game events only."""
    tracker = ToolUsageTracker()
    tracker.process_stream_payload(_message_with_tool_result(result_dict))
    return [n for n in tracker.drain_notifications() if n.get("type") not in _PLUMBING]


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


# ---------------------------------------------------------------------------
# Skill checks — labels cross the wire, numbers never do
# ---------------------------------------------------------------------------


def test_check_result_event_becomes_notification():
    events = _game_events({
        "success": True,
        "event": "check_result",
        "outcome": "partial",
        "skill": "stealth",
        "difficulty": "tricky",
        "instruction": "They slip past, but something is wrong.",
    })
    assert [e["type"] for e in events] == ["check_result"]
    assert events[0]["outcome"] == "partial"
    assert events[0]["skill"] == "stealth"


def test_check_result_notification_leaks_no_numbers():
    """The roll, the DC and the total stay server-side — forever."""
    events = _game_events({
        "event": "check_result",
        "outcome": "critical_failure",
        "skill": "athletics",
        "difficulty": "desperate",
        "_debug": {"roll": 1, "dc": 19, "total": 4},
    })
    assert events[0] == {
        "type": "check_result", "outcome": "critical_failure", "skill": "athletics"
    }


# ---------------------------------------------------------------------------
# Progression fan-out — runs AFTER the elif chain, never instead of it
# ---------------------------------------------------------------------------


_PROGRESSION = {
    "xp_gained": 120, "source": "quest", "xp": 340, "level": 3,
    "previous_level": 2, "leveled_up": True, "xp_into_level": 90,
    "xp_for_level": 200, "unlocked": ["Superior gear no longer fights you."],
    "stats": {"max_hearts": 6, "damage": 1},
}


def test_progression_block_emits_xp_gain():
    events = _game_events({"success": True, "progression": dict(_PROGRESSION)})
    xp = [e for e in events if e["type"] == "xp_gain"]
    assert len(xp) == 1
    assert xp[0] == {
        "type": "xp_gain", "amount": 120, "source": "quest", "xp": 340,
        "level": 3, "xp_into_level": 90, "xp_for_level": 200,
    }


def test_level_up_carries_unlocks_and_stats():
    events = _game_events({"success": True, "progression": dict(_PROGRESSION)})
    ups = [e for e in events if e["type"] == "level_up"]
    assert len(ups) == 1
    assert ups[0]["level"] == 3
    assert ups[0]["previous_level"] == 2
    assert ups[0]["unlocked"] == ["Superior gear no longer fights you."]
    assert ups[0]["stats"] == {"max_hearts": 6, "damage": 1}


def test_no_level_up_without_leveled_up_flag():
    block = dict(_PROGRESSION, leveled_up=False)
    types = [e["type"] for e in _game_events({"progression": block})]
    assert types == ["xp_gain"]


def test_progression_without_gain_emits_nothing():
    """grant_xp returns {} when there is nothing to grant."""
    assert _game_events({"success": True, "progression": {}}) == []
    assert _game_events({"success": True, "progression": dict(_PROGRESSION, xp_gained=0)}) == []


def test_quest_completion_emits_quest_then_xp_then_level_up():
    """One tool result, three events, in the order the frontend expects."""
    events = _game_events({
        "success": True,
        "event": "quest_completed",
        "id": "q1",
        "title": "The Ashen Gate",
        "rewards_applied": ["50 coin"],
        "progression": dict(_PROGRESSION),
    })
    assert [e["type"] for e in events] == ["quest_completed", "xp_gain", "level_up"]
    assert events[0]["quest_id"] == "q1"
    assert events[0]["rewards_applied"] == ["50 coin"]


def test_real_grant_xp_block_fans_out(player):
    """Key agreement with progression.grant_xp — not a hand-written dict."""
    from src.models import Player, get_session
    from src.services.progression import grant_xp

    with get_session() as session:
        p = session.query(Player).filter_by(id=player).first()
        block = grant_xp(session, p, 120, "quest")
        session.commit()

    events = _game_events({"success": True, "progression": block})
    assert [e["type"] for e in events] == ["xp_gain", "level_up"]
    xp, level_up = events
    assert xp["amount"] == 120
    assert xp["source"] == "quest"
    assert xp["xp"] == block["xp"]
    assert xp["level"] == block["level"] == 2
    assert xp["xp_into_level"] == block["xp_into_level"]
    assert xp["xp_for_level"] == block["xp_for_level"]
    assert level_up["previous_level"] == 1
    assert level_up["stats"] == block["stats"]


# ---------------------------------------------------------------------------
# Cinematics — a nested key on ANY tool result, emitted first
# ---------------------------------------------------------------------------


def test_play_cinematic_result_emits_cinematic():
    events = _game_events({
        "success": True,
        "event": "cinematic",
        "kind": "title_card",
        "title": "THE ROAD NORTH",
        "subtitle": "Three days from Emberfall",
        "npc_id": None,
        "mood": None,
        "duration_ms": 4200,
    })
    assert events == [{
        "type": "cinematic", "kind": "title_card", "title": "THE ROAD NORTH",
        "subtitle": "Three days from Emberfall", "npc_id": None,
        "mood": None, "duration_ms": 4200,
    }]


def test_rejected_cinematic_emits_nothing():
    """A rejection carries no `event` key and no kind — nothing reaches the client."""
    assert _game_events({
        "ok": False,
        "reason": "cooldown",
        "detail": "Too soon since the last one.",
        "instruction": "Say nothing about it; narrate the moment in prose.",
    }) == []


def test_nested_cinematic_precedes_the_tools_own_event():
    """Boss intro rides on start_combat and MUST land before combat_start."""
    events = _game_events({
        "success": True,
        "event": "combat_start",
        "enemies": [{"npc_id": "n1", "name": "Coldiron", "archetype": "brute",
                     "tier": "boss", "params": {"hp": 24}}],
        "boss": {"npc_id": "n1", "name": "Coldiron", "title": "Warden of the Ashen Gate"},
        "cinematic": {
            "kind": "boss_intro", "title": "COLDIRON",
            "subtitle": "Warden of the Ashen Gate", "npc_id": "n1",
            "mood": None, "duration_ms": 3600,
        },
    })
    assert [e["type"] for e in events] == ["cinematic", "combat_start"]
    assert events[0]["kind"] == "boss_intro"
    assert events[0]["npc_id"] == "n1"


def test_nested_cinematic_rides_on_an_eventless_result():
    events = _game_events({
        "success": True,
        "cinematic": {"kind": "weather", "title": None, "subtitle": None,
                      "npc_id": None, "mood": "rain", "duration_ms": 0},
    })
    assert [e["type"] for e in events] == ["cinematic"]
    assert events[0]["mood"] == "rain"


def test_nested_cinematic_without_kind_is_ignored():
    assert _game_events({"success": True, "cinematic": {"title": "NOPE"}}) == []
    assert _game_events({"success": True, "cinematic": None}) == []


def test_cinematic_payload_matches_the_service_shape():
    """The six wire fields are exactly what cinematics.py builds."""
    from src.services.cinematics import DURATIONS_MS

    events = _game_events({
        "event": "cinematic", "kind": "vision", "title": None, "subtitle": None,
        "npc_id": None, "mood": None, "duration_ms": DURATIONS_MS["vision"],
    })
    assert set(events[0]) == {
        "type", "kind", "title", "subtitle", "npc_id", "mood", "duration_ms"
    }


# ---------------------------------------------------------------------------
# combat_start — the server resolved the numbers; the tracker only forwards
# ---------------------------------------------------------------------------


def test_combat_start_passes_tier_params_and_boss_through():
    params = {"hp": 11, "damage": 2, "speed": 5.28, "windup": 553, "boss": False,
              "tier": "dangerous", "archetype": "brute"}
    events = _game_events({
        "success": True,
        "event": "combat_start",
        "enemies": [{"npc_id": "9f1c", "name": "Coldiron", "archetype": "brute",
                     "tier": "dangerous", "params": params, "boss": False,
                     "boss_title": None}],
        "boss": None,
        "roster": "Coldiron (dangerous brute)",
    })
    assert [e["type"] for e in events] == ["combat_start"]
    enemy = events[0]["enemies"][0]
    assert enemy["tier"] == "dangerous"
    assert enemy["params"] == params
    assert enemy["boss"] is False
    assert enemy["boss_title"] is None
    assert events[0]["boss"] is None


def test_combat_start_boss_object_survives_the_wire():
    events = _game_events({
        "event": "combat_start",
        "enemies": [{"npc_id": "n1", "name": "Coldiron", "tier": "boss",
                     "params": {"hp": 24, "boss": True}, "boss": True,
                     "boss_title": "Warden of the Ashen Gate"}],
        "boss": {"npc_id": "n1", "name": "Coldiron", "title": "Warden of the Ashen Gate"},
    })
    assert events[0]["boss"] == {
        "npc_id": "n1", "name": "Coldiron", "title": "Warden of the Ashen Gate"
    }
    assert events[0]["enemies"][0]["params"]["hp"] == 24


def test_old_style_combat_start_still_works():
    """A payload without tiers (legacy shape) keeps streaming."""
    events = _game_events({
        "event": "combat_start",
        "enemies": [{"npc_id": "n1", "name": "Bandit", "archetype": "skirmisher"}],
    })
    assert events[0]["enemies"][0]["name"] == "Bandit"
    assert events[0]["boss"] is None


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
