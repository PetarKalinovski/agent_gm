"""Round 9's progress and kindness through /api/play with a scripted DM.

The unit tests (test_progress_round9.py) call the pieces; these drive a
whole reply, so the frames the client reads are checked as it reads them:
a met step's toast carries its XP (fleet10: 21 turns of investigation moved
the bar 0), and a gift's toast names the person only as far as the player
has heard them, never a number (fleet10-wenna T12).
"""

import asyncio
import json

import pytest

from src.models import (
    NPC, Location, LocationType, NPCRelationship, Player, Quest, QuestStatus, WorldClock, get_session,
)
from src.services import job_offers, memory_files, standing


@pytest.fixture()
def world(db, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    job_offers.reset()
    with get_session() as session:
        session.add(WorldClock(day=1, hour=15, minute=0))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True, visited=True)
        yard = Location(name="Stable Yard", type=LocationType.POI, depth=0, discovered=True)
        session.add_all([square, yard])
        session.flush()
        piper = NPC(name="Piper Quillow", profession="Herbalist's apprentice", current_location_id=square.id,
                    description_physical="A small, quick girl with ink on her fingers.")
        sorrel = NPC(name="Sorrel Tack", profession="Stable hand", current_location_id=yard.id,
                     description_physical="A friendly young man all elbows and straw-colored hair.")
        session.add_all([piper, sorrel])
        session.flush()
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=25)
        session.add(player)
        session.flush()
        howls = Quest(
            title="Counting the Howls", status=QuestStatus.ACTIVE, assigned_by_npc_id=piper.id,
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
        session.flush()
        player.active_quests = [howls.id]
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
    return frames, complete


def test_a_gift_in_the_stream_toasts_the_role_never_the_unheard_name(world, monkeypatch):
    frames, complete = _play(
        world, monkeypatch,
        "@coin -2 | pressed into the apprentice's palm\nYou press the coins into her ink-stained palm.\n",
        'I press two coins into her hand. "For your trouble — you have done careful work."', ("piper",))
    assert [c["ok"] for c in complete["commands"]] == [True]
    toast = next(f for f in frames if f.get("type") == "kindness")
    assert toast["text"].endswith("won't forget that.") and "Piper" not in toast["text"]
    assert not any(ch.isdigit() for ch in toast["text"])
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=world["piper"], player_id=world["player"]).one()
        assert rel.trust_level == 50 + standing.GIFT_TRUST


def test_a_met_step_s_toast_carries_its_xp_in_the_stream(world, monkeypatch):
    frames, complete = _play(
        world, monkeypatch,
        '@move Stable Yard\nSorrel Tack (friendly): "Three calls a night, always the same gaps."\n',
        'I head to the stable yard. "Sorrel? What does the yard hear after dark?"', ())
    assert [c["ok"] for c in complete["commands"]] == [True]
    done = [f for f in frames if f.get("type") == "objective_done"]
    assert done and done[0]["progression"]["xp_gained"] == 20      # job: 60 by the steps, three steps
    assert done[0]["progression"]["source"] == "quest_step"
    with get_session() as session:
        assert session.get(Player, world["player"]).xp == 20
        assert session.get(Quest, world["howls"]).step_xp_paid == 20
