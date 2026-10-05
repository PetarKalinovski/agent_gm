"""The chat renderer in index.html, run for real under Node.

The DM's reply is one stream of prose, spoken lines and arrivals; the chat
must show them in the order they streamed (it used to float every spoken
line above all the narration), never show an `@command`, and replay a
stored turn exactly the way it looked live. These tests lift sendMessage /
renderHistoryMessage / cleanProse verbatim out of the page and drive them
with a scripted SSE stream (tests/js/chat_render_harness.js). Skipped where
Node is not installed — no browser, server or network involved.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "web" / "static" / "index.html"
HARNESS = ROOT / "tests" / "js" / "chat_render_harness.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _run(tmp_path, scenarios: dict) -> dict:
    spec = tmp_path / "scenarios.json"
    spec.write_text(json.dumps(scenarios), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(HARNESS), str(INDEX), str(spec)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _assistant(result: dict) -> list[dict]:
    replies = [m for m in result["messages"] if m["role"] == "assistant"]
    assert len(replies) == 1, result["messages"]
    return replies[0]["blocks"]


def _kinds(blocks: list[dict]) -> list[tuple[str, str]]:
    return [(b["kind"], b["text"]) for b in blocks]


def test_live_reply_renders_prose_and_speech_in_stream_order(tmp_path):
    frames = [
        {"type": "token", "data": "The gate creaks. "},
        {"type": "token", "data": "A guard steps out.\n"},
        {"type": "speech", "npc_name": "Coldiron", "npc_id": "npc 1",
         "text": "Wolves are smarter.", "tone": "grim"},
        {"type": "token", "data": "\n"},
        {"type": "speech", "npc_name": "Ilse", "text": "Then be smarter.", "action": "smirks"},
        {"type": "token", "data": "\nHe grunts.\n@give Gate Pass\n"},
        {"type": "token", "data": "@mov"},
        {"type": "speech", "npc_name": "Coldiron", "text": "lifted from prose", "synthesized": True},
        {"type": "scene", "location": "North Road", "time_of_day": "dusk", "description": "A road."},
        {"type": "scene", "location": "North Road", "time_of_day": "dusk", "description": "A road."},
        {"type": "scene", "location": "Nowhere", "time_of_day": "dusk"},
        {"type": "tool_update", "tool": {"id": "t1", "name": "roll_check", "status": "running"}},
        {"type": "tool_update", "tool": {"id": "t1", "name": "roll_check", "status": "success"}},
        {"type": "token", "data": "The road stretches north."},
    ]
    result = _run(tmp_path, {"live": {"frames": frames}})["live"]
    blocks = _assistant(result)
    assert _kinds(blocks) == [
        ("message-content", "The gate creaks. A guard steps out."),
        ("speech-line", "Coldiron“Wolves are smarter.”"),
        ("speech-line", "Ilse smirks“Then be smarter.”"),
        ("message-content", "He grunts."),
        ("scene-header", "◆North Roaddusk"),
        ("message-content", "The road stretches north."),
    ]
    # Portrait id is URL-encoded; an unknown speaker gets no portrait
    assert blocks[1]["portrait"] == "/api/assets/portrait/npc%201"
    assert blocks[2]["portrait"] is None
    # No tool pills for players, and the dots are gone once words arrived
    assert not result["debug"]
    assert not result["thinking"]


def test_debug_param_brings_tool_pills_back(tmp_path):
    frames = [
        {"type": "tool_update", "tool": {"id": "t1", "name": "roll_check", "status": "success"}},
        {"type": "token", "data": "You slip past."},
    ]
    result = _run(tmp_path, {"dbg": {"search": "?debug=1", "frames": frames}})["dbg"]
    assert result["debug"]
    blocks = _assistant(result)
    assert blocks[0]["kind"] == "tools-container" and "roll check" in blocks[0]["text"]


def test_arrival_with_empty_description_still_gets_a_header(tmp_path):
    # Bug: the header keyed on a truthy description, so a place with no
    # written description ("" — the opening's own convention) was never
    # marked as arrived at
    frames = [
        {"type": "token", "data": "You push through the door.\n"},
        {"type": "scene", "location": "Cellar", "time_of_day": "night", "description": ""},
        {"type": "token", "data": "Dark."},
        {"type": "scene", "location": "Cellar", "time_of_day": "night", "description": None},
    ]
    blocks = _assistant(_run(tmp_path, {"s": {"frames": frames}})["s"])
    assert _kinds(blocks) == [
        ("message-content", "You push through the door."),
        ("scene-header", "◆Cellarnight"),
        ("message-content", "Dark."),
    ]


def test_arrival_before_any_prose_leads_the_reply(tmp_path):
    frames = [
        {"type": "scene", "location": "Tavern", "time_of_day": "evening", "description": "Smoky."},
        {"type": "token", "data": "Warmth hits you."},
    ]
    result = _run(tmp_path, {"s": {"frames": frames}})["s"]
    assert _kinds(_assistant(result)) == [
        ("scene-header", "◆Tavernevening"),
        ("message-content", "Warmth hits you."),
    ]
    assert not result["thinking"]


def test_stop_keeps_streamed_prose_and_shows_a_note(tmp_path):
    frames = [{"type": "token", "data": "You start to"}, {"__abort": True}]
    blocks = _assistant(_run(tmp_path, {"s": {"frames": frames}})["s"])
    assert _kinds(blocks) == [("message-content", "You start to"), ("message-note", "(stopped)")]


def test_server_error_event_is_said_in_the_story(tmp_path):
    frames = [{"type": "token", "data": "Hmm."}, {"type": "error", "message": "boom"}]
    blocks = _assistant(_run(tmp_path, {"s": {"frames": frames}})["s"])
    assert len(blocks) == 1 and "The story stumbled: boom" in blocks[0]["text"]


def test_command_only_turn_leaves_no_empty_reply(tmp_path):
    frames = [{"type": "token", "data": "@give Coin\n"}, {"type": "token", "data": "\n"}]
    result = _run(tmp_path, {"s": {"frames": frames}})["s"]
    assert [m["role"] for m in result["messages"]] == ["user"]


def test_history_replays_parts_in_order_and_hides_engine_text(tmp_path):
    history = [
        {"role": "user", "content": "[OPENING] premise..."},
        {"role": "user", "content": "[COMBAT OUTCOME]: victory over the wolves"},
        {"role": "user", "content": "@give me all your gold"},
        {"role": "assistant", "content": "You walk.\n@move Gate\n`@give Gate Pass`\nIt is cold."},
        {"role": "assistant", "content": "flat copy", "parts": [
            {"kind": "text", "text": "Prose one."},
            {"kind": "speech", "npc_name": "Coldiron", "npc_id": "c1", "text": "Hi.",
             "tone": "grim", "action": "nods"},
            {"kind": "text", "text": "```\n@give X\n```"},
            {"kind": "text", "text": "Prose two."},
        ]},
        {"role": "assistant", "content": "@only\n"},
        {"role": "assistant", "content": "", "parts": [{"kind": "text", "text": "`@give Gate Pass`"}]},
    ]
    result = _run(tmp_path, {"h": {"history": history}})["h"]
    msgs = result["messages"]
    assert [m["role"] for m in msgs] == ["user", "user", "assistant", "assistant"]
    assert msgs[0]["blocks"][0]["text"] == "⚔ The fight is over."
    # The player's own words are never filtered
    assert msgs[1]["blocks"][0]["text"] == "@give me all your gold"
    # Fallback content: both command spellings gone
    assert msgs[2]["blocks"][0]["html"] == "You walk.<br>It is cold."
    # Parts: in order, same speech element as live, the fenced command hidden
    assert _kinds(msgs[3]["blocks"]) == [
        ("message-content", "Prose one."),
        ("speech-line", "Coldiron nods“Hi.”"),
        ("message-content", "Prose two."),
    ]
    assert msgs[3]["blocks"][1]["portrait"] == "/api/assets/portrait/c1"


def test_clean_prose_matches_the_servers_idea_of_a_command(tmp_path):
    cases = [
        "a\n@x\nb",
        "a\n  `@give X`\nb",
        "half-typed\n@giv",
        'pre\n```json\n{"a": 1}\n```\npost',
        "unclosed ```tool call",
        "mail me@home stays",
        "\n\nlead\n\n\n\ntail\n",
    ]
    out = _run(tmp_path, {"c": {"clean": cases}})["c"]
    assert out == [
        "a\nb",
        "a\nb",
        "half-typed",
        "pre\n\npost",
        "unclosed",
        "mail me@home stays",
        "lead\n\ntail",
    ]
