"""Round 6's job fixes through /api/play with a scripted DM.

fleet7-wenna: Piper pitched "Counting the Howls" at T2, the player went and
asked Sorrel at T4 — never started. fleet7-tamsin T11: the reply pocketed
sweetroot she never reached for. fleet7-tamsin T14: a refused @hand and a
payout narrated anyway.
"""

import asyncio
import json

import pytest

from src.models import (
    NPC, Location, LocationType, Player, Quest, QuestStatus, WorldClock, get_session,
)
from src.services import job_offers, memory_files, objectives
from src.services.scene_objects import normalize_objects

BUNDLE = {"kind": "take", "label": "Dropped sweetroot bundle", "x": 34, "y": 66,
          "payload": {"item": {"name": "Sweetroot Bundle", "type": "consumable", "description": "Steadies hands."}}}


@pytest.fixture()
def world(db, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    job_offers.reset()
    with get_session() as session:
        session.add(WorldClock(day=1, hour=18, minute=0))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True, visited=True,
                          objects=normalize_objects([BUNDLE]))
        session.add(square)
        session.flush()
        yard = Location(name="Stable Yard", type=LocationType.POI, depth=0, discovered=True)
        session.add(yard)
        session.flush()
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id,
                    description_physical="A small, quick girl with ink on her fingers.")
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=yard.id,
                     description_physical="A friendly young man all elbows and straw-colored hair.")
        session.add_all([piper, sorrel])
        session.flush()
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=30)
        session.add(player)
        session.flush()
        howls = Quest(
            title="Counting the Howls", status=QuestStatus.NOT_STARTED, assigned_by_npc_id=piper.id,
            description="Find who else is awake when the howls start.",
            objectives=[
                {"text": "Ask Sorrel Tack what the stable yard hears after dark", "kind": "talk_to",
                 "target_id": sorrel.id},
                {"text": "Stand out the next howl and mark where it rises", "kind": "custom"},
                {"text": "Bring what you heard back to Piper", "kind": "talk_to", "target_id": piper.id},
            ],
            rewards={"currency": 10},
        )
        session.add(howls)
        session.commit()
        ids = {"player": player.id, "square": square.id, "yard": yard.id, "piper": piper.id,
               "sorrel": sorrel.id, "howls": howls.id}
    yield ids
    job_offers.reset()


class _FakeAgent:
    def __init__(self, events):
        self.events = events
        self.messages = []

    async def stream_async(self, context):
        for event in self.events:
            yield event


class _FakeDM:
    def __init__(self, events, present):
        self.agent = _FakeAgent(events)
        self.last_brief = {"present": present}
        self._player_name = "Wenna Cole"

    def _build_context(self, player_input, **_kwargs):
        return "THE SCENE\nPlayer says: " + player_input


def _play(world, monkeypatch, reply, player_input, present):
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    with get_session() as session:
        names = {world[k]: session.get(NPC, world[k]).name for k in present}
    dm = _FakeDM([{"data": reply}], names)
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    server._pending_audit_feedback.pop(world["player"], None)
    server._pending_engine_notes.pop(world["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=player_input, player_id=world["player"]))
        return [chunk async for chunk in response.body_iterator]

    frames = []
    for chunk in asyncio.run(run()):
        for line in str(chunk).splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                frames.append(json.loads(line[6:]))
    complete = next(f for f in frames if f.get("type") == "complete")
    return frames, complete, server._pending_engine_notes.get(world["player"]) or []


def test_a_pitched_job_play_worked_is_started_by_the_engine(world, monkeypatch):
    _play(world, monkeypatch,
          'Piper Quillow (eager): "I\'ve got a job in it, if you want it — ten coin. Ask Sorrel at the stable '
          'yard what he hears after dark."\n',
          '"Have you noticed anything strange about the howls?"', ("piper",))
    assert world["howls"] in job_offers.open_offers(world["player"])
    frames, complete, notes = _play(
        world, monkeypatch,
        '@move Stable Yard\nSorrel Tack (friendly): "Three calls, always the same gaps."\n',
        'I head to the stable yard. "Sorrel? What have the howls sounded like to you?"', ("piper",))
    assert [c["ok"] for c in complete["commands"]] == [True]
    with get_session() as session:
        quest = session.get(Quest, world["howls"])
        assert quest.status == QuestStatus.ACTIVE
        assert objectives.normalize_objectives(quest.objectives)[0]["done"] is True
    types = [f.get("type") for f in frames]
    assert "quest_activated" in types and "objective_done" in types
    assert any('the engine started the job' in n and "Counting the Howls" in n for n in notes)


def test_a_take_nobody_asked_for_is_refused_and_the_pocketing_is_named(world, monkeypatch):
    frames, complete, _ = _play(
        world, monkeypatch,
        "@interact Dropped sweetroot bundle\nYou notice a loose bundle of sweetroot by the crate, and you pocket "
        "it without breaking stride.\n",
        'I walk over to the apprentice. "What aren\'t you telling me about those howls?"', ("piper",))
    assert [c["ok"] for c in complete["commands"]] == [False]
    assert complete["commands"][0]["message"].startswith("the player did not reach for it")
    checks = [w["check"] for w in complete["audit"]]
    assert "refused_but_narrated" in checks and "item_without_tool" not in checks
    with get_session() as session:
        assert not session.get(Player, world["player"]).inventory
