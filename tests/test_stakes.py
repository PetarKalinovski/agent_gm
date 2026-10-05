"""Stakes: a failed roll costs something, and so does waiting.

fleet6-bram T7 failed the howl count, T8 charged nothing, T18 ticked the very
step that failed and T19 paid in full; fleet6-wenna T8 -> T9 the same, one
reply apart. fleet6-tamsin held the gate pass from T2 to T20 with the
Hollow open to her and waiting cost nothing (src/services/stakes.py).
"""

import asyncio
import json

import pytest

from src.models import (
    NPC, DMState, Event, Location, LocationType, NPCRelationship, Player, Quest, QuestStatus, WorldClock,
    get_session, reset_engine,
)
from src.services import stakes
from src.services.commands import CommandRunner

STEP = "Stand out the next howl and mark where it rises"
REASON = "marking where the drilled howls rise and the gaps between them"     # fleet6-bram T7's roll


@pytest.fixture()
def yard(db):
    stakes.reset_state()
    with get_session() as session:
        session.add_all([WorldClock(day=1, hour=19, minute=30), DMState(tension="low")])
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True)
        session.add(square)
        session.flush()
        sorrel = NPC(name="Sorrel Tack", current_location_id=square.id, danger_tier="harmless")
        session.add(sorrel)
        session.flush()
        quest = Quest(title="Counting the Howls", description="Mark the howls for Piper.",
                      status=QuestStatus.ACTIVE,
                      objectives=[{"text": "Ask Sorrel Tack what the stable yard hears", "kind": "custom"},
                                  {"text": STEP, "kind": "custom"}])
        session.add(quest)
        session.flush()
        player = Player(name="Bram Holloway", current_location_id=square.id, active_quests=[quest.id])
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "quest": quest.id, "sorrel": sorrel.id}


def _step_done(pid, step=STEP):
    return CommandRunner(pid).run(f"@step-done Counting the Howls | {step}")


def _done(quest_id):
    from src.services.objectives import normalize_objectives

    with get_session() as session:
        return {o["text"]: o["done"] for o in normalize_objectives(session.get(Quest, quest_id).objectives)}


# ---------------------------------------------------------------------------
# A failed roll binds its step
# ---------------------------------------------------------------------------

def test_a_failed_roll_binds_the_step_it_attempted_for_this_reply_and_the_next(yard):
    pid = yard["player"]
    stakes.begin_turn(pid)
    assert stakes.note_roll(pid, {"outcome": "failure", "skill": "perception", "reason": REASON}) == [STEP]
    refused = _step_done(pid)
    assert not refused.ok
    assert "that attempt failed — it needs a new attempt (roll_check) or another route first" in refused.message
    assert _done(yard["quest"])[STEP] is False
    stakes.begin_turn(pid)                                   # the next reply: still bound
    assert not _step_done(pid).ok
    stakes.begin_turn(pid)                                   # the one after: free
    assert _step_done(pid).ok and _done(yard["quest"])[STEP] is True


def test_a_fresh_attempt_that_works_frees_the_step(yard):
    pid = yard["player"]
    stakes.begin_turn(pid)
    stakes.note_roll(pid, {"outcome": "critical_failure", "reason": REASON})
    stakes.note_roll(pid, {"outcome": "success", "reason": "marking where the next howl rises, ear to the wind"})
    assert _step_done(pid).ok


def test_only_the_step_the_reason_names_is_bound(yard):
    pid = yard["player"]
    stakes.begin_turn(pid)
    # One shared word ("howl") is no attempt on the step; other steps stay free
    assert stakes.note_roll(pid, {"outcome": "failure", "reason": "calming the horses before the howl"}) == []
    assert stakes.note_roll(pid, {"outcome": "failure", "reason": REASON}) == [STEP]
    assert _step_done(pid, "Ask Sorrel Tack what the stable yard hears").ok
    assert stakes.note_roll(pid, {"outcome": "failure", "reason": ""}) == []


# ---------------------------------------------------------------------------
# An unpaid price is charged
# ---------------------------------------------------------------------------

def _clock_minutes():
    with get_session() as session:
        clock = session.query(WorldClock).first()
        return clock.hour * 60 + (clock.minute or 0)


def _tension():
    with get_session() as session:
        return session.query(DMState).first().tension


def test_a_paid_or_fought_debt_costs_nothing_more(yard):
    pid = yard["player"]
    stakes.owe(pid, [{"outcome": "failure", "skill": "perception", "reason": REASON}])
    assert stakes.settle_owed_cost(pid, paid=True) is None and stakes.owed(pid) is None
    stakes.owe(pid, [{"outcome": "partial", "skill": "stealth", "reason": "creeping"}])
    assert stakes.settle_owed_cost(pid, paid=False, fought=True) is None
    assert _tension() == "low"


def test_a_missed_watch_costs_the_time_it_ate(yard):
    pid = yard["player"]
    before = _clock_minutes()
    stakes.owe(pid, [{"outcome": "failure", "skill": "perception", "reason": REASON}])
    charged = stakes.settle_owed_cost(pid, paid=False)
    assert charged["line"].startswith(f"@time {stakes.TIME_COST_MINUTES}m")
    assert _clock_minutes() - before == stakes.TIME_COST_MINUTES
    assert "the engine did: 30 minutes lost to it" in charged["note"]
    assert "ALREADY applied — show it in this reply" in charged["note"]
    assert stakes.owed(pid) is None                         # charged once


def test_a_failed_ask_costs_the_trust_of_the_person_it_was_about(yard):
    pid = yard["player"]
    with get_session() as session:
        session.add(NPCRelationship(npc_id=yard["sorrel"], player_id=pid, trust_level=50, name_known=True))
        session.commit()
    stakes.owe(pid, [{"outcome": "failure", "skill": "persuasion",
                      "reason": "pressing Sorrel for more than he has said"}], present_ids=[yard["sorrel"]])
    charged = stakes.settle_owed_cost(pid, paid=False, present_ids=[yard["sorrel"]])
    assert charged["line"].startswith("@trust Sorrel Tack -5")
    with get_session() as session:
        row = session.query(NPCRelationship).filter_by(npc_id=yard["sorrel"], player_id=pid).one()
        assert row.trust_level == 45


def test_a_stranger_loses_trust_too(yard):
    """fleet6-tamsin T4 failed an ask of Sorrel before he gave his name; @trust
    lands on a stranger's row now, so the price is his trust, not the danger.
    The player still does not know his name."""
    from src.services.dialogue import player_has_met

    pid = yard["player"]
    stakes.owe(pid, [{"outcome": "partial", "skill": "persuasion", "reason": "pressing Sorrel"}],
               present_ids=[yard["sorrel"]])
    charged = stakes.settle_owed_cost(pid, paid=False, present_ids=[yard["sorrel"]])
    assert charged["line"].startswith("@trust Sorrel Tack -5") and _tension() == "low"
    with get_session() as session:
        row = session.query(NPCRelationship).filter_by(npc_id=yard["sorrel"], player_id=pid).one()
        assert row.trust_level == 45
        assert player_has_met(session, pid, yard["sorrel"]) is False


def test_a_social_roll_about_nobody_raises_the_danger(yard):
    pid = yard["player"]
    stakes.owe(pid, [{"outcome": "partial", "skill": "persuasion", "reason": "talking the crowd down"}])
    charged = stakes.settle_owed_cost(pid, paid=False)
    assert charged["line"] == "@tension rising" and _tension() == "rising"


# ---------------------------------------------------------------------------
# The whole loop through play_sse (fleet6-bram T7 -> T8, fleet6-wenna T8 -> T9)
# ---------------------------------------------------------------------------

def _roll_events(pid, outcome, reason, tool_id):
    args = {"player_id": pid, "skill": "perception", "difficulty": "tricky", "reason": reason}
    result = {"success": True, "outcome": outcome, "skill": "perception", "difficulty": "tricky",
              "event": "check_result", "instruction": "Narrate it."}
    return [
        {"current_tool_use": {"toolUseId": tool_id, "name": "roll_check", "input": json.dumps(args)[:25]}},
        {"current_tool_use": {"toolUseId": tool_id, "name": "roll_check", "input": json.dumps(args)}},
        {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": tool_id, "name": "roll_check",
                                                                   "input": args}}]}},
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": tool_id, "status": "success", "content": [{"text": json.dumps(result)}]}}]}},
    ]


def test_a_refused_roll_never_lends_its_reason_to_the_next_one():
    """roll_check refused an unknown skill (no outcome, no check_result):
    the retry's result pairs with the retry's own reason."""
    calls: dict = {}
    taken: set = set()
    first = {"player_id": "p", "skill": "insight", "difficulty": "tricky", "reason": REASON}
    for event in _roll_events("p", "failure", REASON, "t1")[:3]:
        stakes.capture_roll_input(event, calls)
    stakes.capture_roll_input({"message": {"role": "user", "content": [{"toolResult": {
        "toolUseId": "t1", "status": "success",
        "content": [{"text": json.dumps({"error": f"Unknown skill '{first['skill']}'"})}]}}]}}, calls)
    for event in _roll_events("p", "success", "asking the stable hand about the paddock", "t2"):
        stakes.capture_roll_input(event, calls)
    assert stakes.take_roll_call(calls, taken)["reason"] == "asking the stable hand about the paddock"


def test_play_sse_binds_the_failed_step_then_charges_the_unpaid_price(yard, tmp_path, monkeypatch):
    from src.services import memory_files
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    pid = yard["player"]
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    tick = f"@step-done Counting the Howls | {STEP}\n"
    script = [
        _roll_events(pid, "failure", REASON, "t1")
        + [{"data": "The third howl bends off the roofs and the count slips away from you.\n"}, {"data": tick}],
        [{"data": "Sorrel shrugs and goes back to the horses.\n"}, {"data": tick}],
        [{"data": "The next night the count holds.\n"}, {"data": tick}],
    ]

    class Agent:
        messages: list = []

        async def stream_async(self, context):
            for event in script.pop(0):
                yield event

    class DM:
        agent = Agent()
        last_brief = {"present": {yard["sorrel"]: "Sorrel Tack"}}
        _player_name = "Bram Holloway"

        def _build_context(self, player_input, **_kwargs):
            return f"Player says: {player_input}"

    session = {"dm": DM(), "tool_tracker": ToolUsageTracker()}
    monkeypatch.setattr(server, "get_or_create_session", lambda _pid: session)
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    for store in (server._pending_audit_feedback, server._pending_engine_notes, server._pending_command_ledger):
        store.pop(pid, None)

    def play(words):
        async def run():
            response = await server.play_sse(server.GameRequest(player_input=words, player_id=pid))
            return [chunk async for chunk in response.body_iterator]
        frames = []
        for chunk in asyncio.run(run()):
            for line in str(chunk).splitlines():
                if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                    frames.append(json.loads(line[6:]))
        return next(f for f in frames if f.get("type") == "complete")

    first = play("I hold the count and mark where each howl rises.")
    assert first["commands"][0]["ok"] is False
    assert "that attempt failed" in first["commands"][0]["message"]
    assert "partial_without_cost" in [w["check"] for w in first["audit"]]
    assert "roll_without_attempt" not in [w["check"] for w in first["audit"]]   # marking IS an attempt
    assert stakes.owed(pid)["reason"] == REASON

    second = play("I curse and fold the parchment.")
    assert second["commands"][0]["ok"] is False                                 # still the next reply
    assert second["engine_costs"][0]["line"].startswith("@time 30m")
    notes = server._pending_engine_notes.get(pid) or []
    assert any("the engine did" in note for note in notes)

    third = play("Tomorrow night, at the fence again.")
    assert third["commands"][0]["ok"] is True
    assert "engine_costs" not in third
    assert _done(yard["quest"])[STEP] is True


# ---------------------------------------------------------------------------
# Waiting has a clock (the Emberfall seed, fleet6-tamsin's T2 -> T20)
# ---------------------------------------------------------------------------

@pytest.fixture()
def emberfall(tmp_path):
    from src.data.playtest_seed import create_playtest_world
    from src.models.campaign import Campaign, StoryBeat
    from src.services.objectives import normalize_objectives

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    stakes.reset_state()
    with get_session() as session:
        square = session.query(Location).filter_by(name="Market Square").one()
        hollow = session.query(Location).filter_by(name="Wolf's Hollow").one()
        teeth = session.query(Quest).filter_by(title="Teeth on the North Road").one()
        objectives = normalize_objectives(teeth.objectives)
        objectives[0]["done"] = True                            # the gate pass is hers
        teeth.objectives, teeth.status = objectives, QuestStatus.ACTIVE
        player = Player(name="Tamsin Rook", current_location_id=square.id, active_quests=[teeth.id],
                        inventory=[{"id": "gate_pass", "name": "Gate Pass", "type": "quest_item"}])
        session.add(player)
        session.flush()
        campaign = Campaign(player_id=player.id, title="The Toll Road", status="active", current_act=1,
                            premise="Someone is using trained wolves to strangle the north road.")
        session.add(campaign)
        session.flush()
        session.add_all([
            StoryBeat(campaign_id=campaign.id, act=1, sequence=1, title="The Gate Pass", status="delivered",
                      consequence_met=[{"op": "activate_quest", "quest_id": teeth.id}]),
            StoryBeat(campaign_id=campaign.id, act=1, sequence=3, title="The Evening Howl",
                      beat_type="deadline_event", status="armed",
                      clock_label="Before the second howl finds the gap in the Watch",
                      consequence_missed=[{"op": "world_event", "name": "A Second Watchman Missing",
                                           "description": "A Lantern Watchman sent to check the north wall "
                                                          "doesn't report back by dawn."}]),
        ])
        session.commit()
        ids = {"player": player.id, "square": square.id, "hollow": hollow.id, "quest": teeth.id}
    yield ids
    reset_engine()


def _events_named(name):
    with get_session() as session:
        return session.query(Event).filter(Event.name == name).all()


def test_six_replies_with_the_road_open_land_a_consequence(emberfall):
    pid = emberfall["player"]
    lines = [stakes.waited_line(pid, player_input="I order another drink and watch the room.")
             for _ in range(stakes.WAIT_TURNS + 1)]
    assert lines[:-1] == [None] * stakes.WAIT_TURNS
    line = lines[-1]
    assert line.startswith("[WHILE YOU WAITED — the world kept its own pace: the road to Wolf's Hollow has "
                           "stood open to the player for 6 replies")
    # The world moving on, never the player's fault (fleet7-bram T13: "that's on me")
    assert "nobody says they were slow, late or to blame" in line
    assert "while they waited" not in line.lower().split("[while you waited")[1]
    # The act's own vocabulary (the Watch, its watchman) picks the patrol
    assert "A patrol sent out toward Wolf's Hollow" in line
    rows = _events_named("A Patrol Overdue")
    assert len(rows) == 1 and rows[0].narrated_to_player is True
    with get_session() as session:
        assert session.query(DMState).first().tension == "rising"
    # The count starts over (and a step stops pressing after MAX_WAIT_CONSEQUENCES)
    assert stakes.waited_line(pid) is None
    for _ in range(stakes.WAIT_TURNS * (stakes.MAX_WAIT_CONSEQUENCES + 1)):
        stakes.waited_line(pid)
    assert stakes._waiting[pid]["fired"] == stakes.MAX_WAIT_CONSEQUENCES


def _clock_at(day, hour, minute=0):
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.day, clock.hour, clock.minute = day, hour, minute
        session.commit()


def test_six_open_hours_of_waiting_count_as_much_as_six_replies(emberfall):
    """fleet6-tamsin carried the pass from T2 and sat in two rooms with the
    Hollow open to her: hours of open road count, not just replies."""
    pid = emberfall["player"]
    _clock_at(2, 7)
    assert stakes.waited_line(pid) is None
    _clock_at(2, 13, 30)
    line = stakes.waited_line(pid, player_input="I finish my stew.")
    assert line and "for 6 open hours" in line


def test_a_night_behind_the_barred_gate_is_not_waiting(emberfall):
    """fleet7-bram T13-T19: the gate barred till dawn, and six replies in town
    were charged as "Another Cart Taken". The seed's road says the gate is
    barred after the howl: the night's replies and hours do not count, and the
    count picks up at first light where it stood."""
    pid = emberfall["player"]
    _clock_at(1, 19, 30)
    assert stakes.waited_line(pid) is None                        # the count starts before dark
    _clock_at(1, 21)
    for _ in range(stakes.WAIT_TURNS + 3):
        assert stakes.waited_line(pid, player_input="I order another drink.") is None
    assert stakes._waiting[pid]["turns"] == 0
    _clock_at(2, 7)                                               # she slept till first light
    assert stakes.waited_line(pid, player_input="First light. I'm up.") is None
    assert stakes._waiting[pid]["turns"] == 1
    assert _events_named("A Patrol Overdue") == []


def test_no_clock_while_the_way_in_is_shut_to_them(emberfall):
    """The pass step ticked, but nothing in the pack opens the gate: the road
    is the world's wall, not one the player is leaving untaken."""
    pid = emberfall["player"]
    with get_session() as session:
        session.get(Player, pid).inventory = []
        session.commit()
    for _ in range(stakes.WAIT_TURNS + 2):
        assert stakes.waited_line(pid, player_input="I order another drink.") is None
    assert stakes._waiting.get(pid) is None
    # The pass in hand: the road is theirs, and the count starts from there
    with get_session() as session:
        session.get(Player, pid).inventory = [{"id": "gate_pass", "name": "Gate Pass", "type": "quest_item"}]
        session.commit()
    assert stakes.waited_line(pid) is None
    assert stakes._waiting[pid]["turns"] == 0


def test_setting_out_or_being_there_is_not_waiting(emberfall):
    pid = emberfall["player"]
    for _ in range(stakes.WAIT_TURNS + 2):
        assert stakes.waited_line(pid, player_input="I head for Wolf's Hollow now.") is None
    with get_session() as session:
        session.get(Player, pid).current_location_id = emberfall["hollow"]
        session.commit()
    assert stakes.waited_line(pid) is None
    assert stakes._waiting.get(pid) is None


def test_a_plan_for_first_light_and_talk_of_wolves_are_still_waiting(emberfall):
    """fleet6-wenna said "I'm going up to Wolf's Hollow at first light" at
    T10-T18 of one evening, and in a wolf story every other line names the
    wolves: neither is setting out, so neither holds the clock off."""
    pid = emberfall["player"]
    said = [
        "I'm going up to Wolf's Hollow at first light like we planned, but did the rider say anything?",
        "This isn't a wolf problem, Captain. Someone is running those wolves.",
        "\"I need sleep before we walk into whatever's waiting at Wolf's Hollow.\" I stand.",
        "I'll come find you with my answer on whether I'm heading north tonight or waiting for daylight.",
        "But I want the pass *before* I walk into Wolf's Hollow, not after.",
    ]
    lines = [stakes.waited_line(pid, player_input=said[i % len(said)]) for i in range(stakes.WAIT_TURNS + 1)]
    assert lines[-1] and lines[-1].startswith("[WHILE YOU WAITED")
    # Leaving now still holds it: a road out, or the place with a verb of going
    for words in ("But right now I'm heading through that gate before it bars.",
                  "I make for the Hollow, knife loose in its sheath.", "Setting out. No more talk."):
        assert stakes._heading_there(words, "Wolf's Hollow"), words


def test_no_clock_until_the_road_is_theirs(emberfall):
    """The pass not yet in hand: the first undone step is the pass, not the road."""
    from src.services.objectives import normalize_objectives

    pid = emberfall["player"]
    with get_session() as session:
        teeth = session.get(Quest, emberfall["quest"])
        objectives = normalize_objectives(teeth.objectives)
        objectives[0]["done"] = False
        teeth.objectives = objectives
        session.commit()
    for _ in range(stakes.WAIT_TURNS + 2):
        assert stakes.waited_line(pid) is None


def test_engine_turns_and_owned_replies_keep_the_count_but_land_nothing(emberfall):
    pid = emberfall["player"]
    for _ in range(stakes.WAIT_TURNS + 1):
        assert stakes.waited_line(pid, owned=True) is None
    assert stakes.waited_line(pid, engine_turn=True) is None
    assert stakes.waited_line(pid) is not None                 # the first free reply gets it


def test_a_complication_this_reply_holds_the_waiting_clock_to_the_next(monkeypatch):
    """One push from the world per reply: a [COMPLICATION] (or its UNANSWERED
    press) and a [WHILE YOU WAITED] never land together."""
    from types import SimpleNamespace

    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import DMOrchestrator
    from src.services import complications

    monkeypatch.setattr(dm_orchestrator, "get_player", lambda pid: {"name": "Tamsin Rook", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [])
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {"fired_events": []})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "### WORLD STATE")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)
    monkeypatch.setattr(complications, "reply_owned", lambda *a, **k: False)
    pushes = {"directive_line": None, "unanswered_line": None}
    for name in pushes:
        monkeypatch.setattr(complications, name, lambda *a, _n=name, **k: pushes[_n])
    owned = []
    monkeypatch.setattr(stakes, "waited_line", lambda *a, **k: owned.append(k["owned"]) or None)
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Tamsin Rook")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- Teeth [q1]"]
    DMOrchestrator._build_context(fake, "I order another drink.")
    pushes["directive_line"] = "[COMPLICATION — test]"
    DMOrchestrator._build_context(fake, "I order another drink.")
    pushes["directive_line"], pushes["unanswered_line"] = None, "[COMPLICATION — UNANSWERED test]"
    DMOrchestrator._build_context(fake, "I order another drink.")
    assert owned == [False, True, True]
