"""Round 7 (fleet8): stakes, dice and pacing.

  * fleet8-wenna T11 -> T12: she calmed the scout wolf on a real roll; the
    next reply's ambush roll sent that same wolf at her.
  * fleet8-tamsin T8: a quiet-streak summons pulled her out of the loft
    twenty minutes before the howl she had spent three turns waiting for;
    fleet8-bram T14 stood a night watch at the paddock fence and nothing came.
  * fleet8-tamsin T9: a second athletics roll "placeholder - not needed" came
    back critical_failure and was thrown away; a coin she tossed by choice
    stood as its price.
  * fleet8-bram: "winded" from T4 to T20 and nothing was ever harder for it.
  * fleet8-bram T12/T19: prose written before the roll and prose written
    after it, both on screen; T19 a failed persuasion roll on a willing
    stable boy deleted the beat's second-press clue.
  * fleet8-wenna T20: a bad-news complication voiced a "Yura" who exists
    nowhere and took the Captain's trust for the town's mood.
"""

import asyncio
import json
import random

import pytest

from src.models import (
    NPC, DMState, Event, Faction, Location, LocationType, NPCRelationship, Player, WorldClock, get_session,
    reset_engine,
)
from src.services import complications, encounters, roll_intent, stakes
from src.services import progression as prog
from src.services.commands import CommandResult
from src.tools.world_write.progression import _REPLY_ROLLS, roll_check


class _AlwaysStrikes(random.Random):
    def random(self):
        return 0.0


class _Dice:
    """A d20 that rolls exactly what the test says."""

    def __init__(self, *values: int):
        self._values = list(values)

    def randint(self, a: int, b: int) -> int:
        return self._values.pop(0)


def _force(monkeypatch, *rolls: int) -> None:
    monkeypatch.setattr(prog, "_RNG", _Dice(*rolls))


def _clear_directors():
    for state in (encounters._linger, encounters._since_ambush, encounters._parley, encounters._parley_skips,
                  encounters._talking, encounters._held, encounters._drawn, encounters._standoff,
                  encounters._standoff_held, encounters._answering):
        state.clear()
    complications.reset_state()
    stakes.reset_state()
    _REPLY_ROLLS.clear()


@pytest.fixture()
def yard(db):
    """The stable yard at night: a hungry scout wolf at the fence, the stable
    boy, and the player (fleet8-wenna T10)."""
    _clear_directors()
    with get_session() as session:
        place = Location(name="Stable Yard", type=LocationType.DISTRICT, discovered=True,
                         description="Stalls, a trough and the paddock fence on the north side.")
        fangs = Faction(name="The Hollow Pack")
        session.add_all([place, fangs, WorldClock(day=1, hour=21), DMState(tension="low")])
        session.flush()
        wolf = NPC(name="Torn-Ear Wolf", species="wolf", danger_tier="weak", faction_id=fangs.id,
                   current_location_id=place.id, current_mood="hostile")
        sorrel = NPC(name="Sorrel Tack", profession="stable hand", danger_tier="harmless",
                     current_location_id=place.id, current_mood="nervous")
        player = Player(name="Wenna Cole", current_location_id=place.id, currency=10)
        session.add_all([wolf, sorrel, player])
        session.commit()
        ids = {"player": player.id, "wolf": wolf.id, "sorrel": sorrel.id, "yard": place.id}
    yield ids
    _clear_directors()


def _lingering(pid, place, words="I hold out the scraps."):
    encounters.note_turn(pid, place)
    return encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input=words)


# Round 8: holding out scraps with a wolf in the yard is a stand-off the die
# decides (encounters.roll holds for it) — where only the ambush is under
# test, the player just looks around
LOOKING = "I look around the yard."


# ---------------------------------------------------------------------------
# 1. What the player won stays won
# ---------------------------------------------------------------------------

def test_a_won_roll_against_the_wolf_holds_it_off_the_next_rolls(yard):
    """fleet8-wenna T11 -> T12: 'approaching the hungry scout wolf calmly'
    came back success; the next lingering roll (p=0.7) sent that wolf."""
    pid, place = yard["player"], yard["yard"]
    encounters.note_turn(pid, place)
    held = encounters.note_check(pid, "success", "cunning", "approaching the hungry scout wolf calmly")
    assert held == [yard["wolf"]]
    for _ in range(encounters.CALM_HOLD_ROLLS):
        assert _lingering(pid, place) is None
    struck = _lingering(pid, place)
    assert struck and struck["names"] == ["Torn-Ear Wolf"]     # held, not tamed forever


@pytest.mark.parametrize("outcome,skill,reason", [
    ("failure", "cunning", "approaching the hungry scout wolf calmly"),
    ("critical_failure", "persuasion", "talking the wolf down"),
    ("success", "athletics", "vaulting the paddock rail"),           # names no danger, faces none down
])
def test_a_lost_roll_or_one_about_something_else_holds_nothing(yard, outcome, skill, reason):
    pid, place = yard["player"], yard["yard"]
    encounters.note_turn(pid, place)
    assert encounters.note_check(pid, outcome, skill, reason) == []
    assert _lingering(pid, place, LOOKING) is not None


def test_a_won_sneak_naming_nobody_was_against_every_danger_here(yard):
    pid, place = yard["player"], yard["yard"]
    encounters.note_turn(pid, place)
    assert encounters.note_check(pid, "partial", "stealth", "slipping along the stalls") == [yard["wolf"]]
    assert _lingering(pid, place) is None


def test_the_roll_check_tool_holds_the_wolf_and_answers_the_scout_complication(yard, monkeypatch):
    pid, place = yard["player"], yard["yard"]
    complications._open[pid] = {"kind": "scout", "entity_id": yard["wolf"], "entity_name": "Torn-Ear Wolf",
                                "stub": None, "what": "a scout", "ignored": "it comes", "replies": 0,
                                "where": place}
    _force(monkeypatch, 18)
    result = roll_check(pid, "cunning", "tricky", reason="approaching the hungry scout wolf calmly")
    assert result["outcome"] == "success"
    assert encounters._held.get(yard["wolf"]) == encounters.CALM_HOLD_ROLLS
    # Its "if they ignore it, it comes for them" press would undo the roll too
    assert complications.open_complication(pid) is None


STANDOFF = ("The torn-ear wolf stops at the fence, head low, ribs showing.\n\n"
            "You could back away toward the stalls, or hold out what you have and see what it does.")


def test_a_standoff_settled_peacefully_holds_the_wolf_on(yard):
    pid, place = yard["player"], yard["yard"]
    encounters.note_parley(pid, [], STANDOFF)
    assert _lingering(pid, place) is None                      # the standoff's own one hold
    encounters.note_parley(pid, [], "Your voice stays low. Its ribs heave once, and then it sits, "
                                    "haunches lowering into the mud, still watching you.")
    assert _lingering(pid, place) is None                      # settled: held on
    assert _lingering(pid, place) is None
    assert _lingering(pid, place) is not None


def test_a_standoff_that_ends_in_teeth_holds_nothing_more(yard):
    pid, place = yard["player"], yard["yard"]
    encounters.note_parley(pid, [], STANDOFF)
    assert _lingering(pid, place) is None
    encounters.note_parley(pid, [], "It sits for a heartbeat — then lunges at your outstretched hand.")
    assert _lingering(pid, place, LOOKING) is not None


# ---------------------------------------------------------------------------
# 2. A vigil the player keeps
# ---------------------------------------------------------------------------

TAMSIN_T7 = ('I nod, already calculating. "I\'ll stay. Eight o\'clock works." I move toward the paddock '
             'railing, keeping my voice steady.')
TAMSIN_T8 = ("I accept the leg up into the loft, settling into position where I can see the paddock and the "
             "hills beyond without being silhouetted against the sky.")
BRAM_T14 = ('I straighten up from the fence. "I\'m going to stand out here and wait for those horses to go '
            'still. When they do, I\'m marking where the howl comes from." I turn toward the open yard, '
            'positioning myself where I can watch both the horses and the dark beyond the fence.')
WENNA_T7 = ('"I\'m going to stand out here tonight and mark the next howl myself, if you\'ll have me."')
TAMSIN_T9 = ('I grip the edge of the loft, torn for a second, then curse quietly. "Piper doesn\'t panic." I '
             'swing back down, landing hard. "Sorrel, stay here tonight. Listen for when that answer-back howl '
             'comes, mark the time exact. I\'ll be back tomorrow to hear it." I toss him a coin as I follow the '
             'runner back toward the square.')


@pytest.mark.parametrize("words", [TAMSIN_T7, TAMSIN_T8, BRAM_T14, WENNA_T7,
                                   "I keep watch on the treeline until the howl comes."])
def test_a_watch_the_player_keeps_is_a_vigil(words):
    assert complications.keeping_vigil(words)


@pytest.mark.parametrize("words", [
    TAMSIN_T9,                                   # an order to Sorrel as she leaves is not her watch
    'I wait for his answer, arms folded.',
    'I lean on the post and wait for her to finish.',
    'I watch his face for any sign of a lie.',
    "[COMBAT OUTCOME — victory] I wait.",
])
def test_talk_and_engine_turns_are_no_vigil(words):
    assert complications.keeping_vigil(words) is None


@pytest.fixture()
def square(db):
    _clear_directors()
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True)
        session.add_all([square, WorldClock(day=1, hour=14), DMState(tension="low")])
        session.flush()
        player = Player(name="Tamsin Rook", current_location_id=square.id, currency=12)
        session.add(player)
        session.commit()
        ids = {"player": player.id, "square": square.id}
    yield ids
    _clear_directors()


def _quiet(pid, n):
    for _ in range(n):
        complications.note_turn(pid, player_input="I ask about the carter.")


def test_a_town_complication_waits_out_a_vigil_a_few_replies_at_most(square):
    """fleet8-tamsin T8: the summons came while she lay in the loft."""
    pid = square["player"]
    _quiet(pid, 10)
    for _ in range(complications.VIGIL_MAX_WAITS):
        assert complications.directive_line(pid, player_input=TAMSIN_T8) is None
    assert complications.directive_line(pid, player_input=TAMSIN_T8).startswith("[COMPLICATION")


def test_without_a_vigil_the_quiet_streak_still_bites(square):
    pid = square["player"]
    _quiet(pid, 10)
    assert complications.directive_line(pid, player_input="I order a drink.").startswith("[COMPLICATION")


def test_a_public_event_due_within_the_hour_owns_it(square):
    pid = square["player"]
    with get_session() as session:
        session.query(WorldClock).first().minute = 40
        session.add(Event(name="The Evening Howl", description="The howl rises.", event_type="meso",
                          scheduled_day=1, scheduled_hour=15, player_visible=True,
                          locations_involved=[square["square"]]))
        session.commit()
    _quiet(pid, 10)
    assert complications.directive_line(pid, player_input="I order a drink.") is None
    with get_session() as session:
        session.query(WorldClock).first().minute = 0
        session.query(WorldClock).first().hour = 13       # two hours off: not yet its hour
        session.commit()
    assert complications.directive_line(pid, player_input="I order a drink.").startswith("[COMPLICATION")


@pytest.fixture()
def emberfall_night(tmp_path):
    from src.data.playtest_seed import create_playtest_world

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    _clear_directors()
    with get_session() as session:
        places = {loc.name: loc.id for loc in session.query(Location).all()}
        clock = session.query(WorldClock).first()
        clock.hour, clock.minute = 22, 10
        session.query(DMState).first().tension = "low"        # fleet8-bram T14: calm all evening
        player = Player(name="Bram Holloway", current_location_id=places["Stable Yard"], currency=10)
        session.add(player)
        session.commit()
        ids = {"player": player.id, **places}
    yield ids
    _clear_directors()
    reset_engine()


def _scouts(location_id):
    with get_session() as session:
        return [n for n in session.query(NPC).filter(NPC.current_location_id == location_id).all()
                if n.profession and n.profession.endswith("scout")]


def test_a_night_watch_at_the_fence_brings_the_scout_whatever_the_tension(emberfall_night):
    """fleet8-bram T14: "I'm going to stand out here and wait" at the paddock
    fence at night, tension low — and nothing came."""
    pid, yard_id = emberfall_night["player"], emberfall_night["Stable Yard"]
    _quiet(pid, complications.VIGIL_SCOUT_QUIET)
    assert complications.directive_line(pid, player_input="I ask Sorrel about the horses.") is None
    line = complications.directive_line(pid, player_input=BRAM_T14)
    assert line.startswith("[COMPLICATION — night at the edge of town, and the player is keeping watch")
    assert len(_scouts(yard_id)) == 1


def test_the_watch_brings_its_scout_past_an_open_complication_and_keeps_it_open(emberfall_night):
    """fleet8-bram T13 brought a watcher in the loft; T14's night watch was
    still owed its scout."""
    pid = emberfall_night["player"]
    watcher = {"kind": "watcher", "entity_id": None, "entity_name": "Corran Pike", "stub": "@new-npc Corran",
               "what": "a watcher", "ignored": "follows", "replies": 1, "where": emberfall_night["Stable Yard"]}
    complications._open[pid] = dict(watcher)
    _quiet(pid, 1)
    assert complications.directive_line(pid, player_input=BRAM_T14)
    assert complications.open_complication(pid)["kind"] == "watcher"
    assert len(_scouts(emberfall_night["Stable Yard"])) == 1


# ---------------------------------------------------------------------------
# 6b. Bad news follows from what the player did, carried by nobody new
# ---------------------------------------------------------------------------

def _pin(monkeypatch, kind):
    menu = {k: (k, what, bite) for k, _, what, bite in complications._TOWN + complications._WILD}
    monkeypatch.setattr(complications, "_pick", lambda *a, **k: menu[kind])


def test_bad_news_is_about_someone_they_dealt_with_and_names_no_one_new(square, monkeypatch):
    pid = square["player"]
    with get_session() as session:
        piper = NPC(name="Piper Quillow", current_location_id=square["square"])
        session.add(piper)
        session.flush()
        session.add(NPCRelationship(npc_id=piper.id, player_id=pid, name_known=True, last_interaction_day=1))
        session.commit()
    _pin(monkeypatch, "bad_news")
    _quiet(pid, 10)
    line = complications.directive_line(pid)
    assert "It is about Piper Quillow, whom they have dealt with" in line
    assert "never a new name in the prose" in line
    assert "never for the town's mood" in line
    assert "the matching @trust" not in line
    assert "only for what the player themselves did" in complications.open_complication(pid)["ignored"]


def test_bad_news_with_nobody_dealt_with_takes_no_ones_trust(square, monkeypatch):
    pid = square["player"]
    _pin(monkeypatch, "bad_news")
    _quiet(pid, 10)
    assert "no @trust with anyone who had no part in it" in complications.directive_line(pid)


# ---------------------------------------------------------------------------
# 3. One act, one roll; a tip is no price; a critical failure costs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["placeholder - not needed", "test", "not really needed", "n/a"])
def test_a_placeholder_roll_is_refused(player, monkeypatch, reason):
    _force(monkeypatch, 1)
    result = roll_check(player, "athletics", "easy", reason=reason)
    assert "error" in result and "nothing was rolled" in result["error"]
    assert "event" not in result and "outcome" not in result


def test_testing_the_rope_is_a_real_reason(player, monkeypatch):
    _force(monkeypatch, 15)
    assert roll_check(player, "athletics", "easy", reason="testing the rope before the climb")["event"] \
        == "check_result"


def test_the_same_act_rolled_twice_in_a_reply_keeps_the_first_result(player, monkeypatch):
    """fleet8-tamsin T9: two athletics rolls for one drop from the loft."""
    stakes.begin_turn(player)
    _force(monkeypatch, 15, 1, 1)
    first = roll_check(player, "athletics", "easy", reason="dropping fast from the loft")
    again = roll_check(player, "athletics", "easy", reason="dropping from the loft again")
    assert first["outcome"] == again["outcome"] == "success"
    assert again["repeat"] is True and "event" not in again        # no second floater, nothing to pair
    assert "one act, one roll" in again["instruction"]
    assert "check_result" not in json.dumps(again)
    # Another act is its own roll; the next reply rolls the same act anew
    assert roll_check(player, "athletics", "easy", reason="vaulting the paddock rail")["outcome"] \
        == "critical_failure"
    stakes.begin_turn(player)
    assert roll_check(player, "athletics", "easy", reason="dropping fast from the loft")["outcome"] \
        == "critical_failure"


def test_without_a_counted_reply_every_roll_is_rolled(player, monkeypatch):
    """The CLI never begins a reply: nothing may assume one."""
    _force(monkeypatch, 15, 1)
    assert roll_check(player, "athletics", "easy", reason="dropping from the loft")["outcome"] == "success"
    assert roll_check(player, "athletics", "easy", reason="dropping from the loft")["outcome"] \
        == "critical_failure"


def _ok(line):
    return CommandResult(line=line, verb="", ok=True, tool=None, result={})


def test_a_tip_the_player_chose_is_not_the_price_of_a_critical_failure():
    from src.web.turn_audit import partial_without_cost

    tips = [_ok("@coin -1 | tossed to Sorrel for his watch tonight")] * 2
    assert partial_without_cost(tips, ["critical_failure"], "healthy") is None        # legacy callers
    hit = partial_without_cost(tips, ["critical_failure"], "healthy", player_input=TAMSIN_T9)
    assert hit and hit["check"] == "partial_without_cost" and "CRITICAL FAILURE" in hit["detail"]
    # ...a price the world sets still is one, whatever the player said
    assert partial_without_cost([_ok("@coin -2 | the gate toll")], ["failure"], "healthy",
                                player_input="I pay the toll and walk on.") is None
    # ...and a paid answer (fleet8-bram T13) is the player's own coin too
    assert stakes.chosen_payment("@coin -3 | paid Sorrel for a straight answer",
                                 'I pull out a couple coin and hold them out. "For the straight answer."')
    assert not stakes.chosen_payment("@coin -3 | the fall cost a purse", "I drop from the loft.")


def test_an_unpaid_critical_failure_on_a_drop_is_a_fall(player):
    stakes.owe(player, [{"outcome": "critical_failure", "skill": "athletics",
                         "reason": "dropping fast from the loft"}])
    charged = stakes.settle_owed_cost(player, paid=False)
    assert charged["line"].startswith("@health winded | cause: dropping fast from the loft")
    with get_session() as session:
        assert session.get(Player, player).health_status == "winded"


def test_a_fall_never_takes_a_hurt_player_further_than_time(player):
    with get_session() as session:
        session.get(Player, player).health_status = "hurt"
        session.add(WorldClock(day=1, hour=21))
        session.commit()
    stakes.owe(player, [{"outcome": "critical_failure", "skill": "athletics", "reason": "the drop"}])
    assert stakes.settle_owed_cost(player, paid=False)["line"].startswith("@time 30m")


# ---------------------------------------------------------------------------
# 4. Wounds that matter
# ---------------------------------------------------------------------------

def _hurt(player_id, health):
    with get_session() as session:
        session.get(Player, player_id).health_status = health
        session.commit()


def test_winded_a_climb_goes_a_step_harder_and_the_dm_reads_why(player, monkeypatch):
    _hurt(player, "winded")
    _force(monkeypatch, 9)               # 9 + level 1 clears "easy"; against "tricky" it is a partial
    result = roll_check(player, "athletics", "easy", reason="scrambling up the paddock rail")
    assert result["outcome"] == "partial" and result["difficulty"] == "tricky"
    assert result["wound"] == {"health": "winded", "because": "short of breath, their ribs slow them"}
    assert "Their body was against them: short of breath" in result["instruction"]
    assert not any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in result.values())


def test_badly_hurt_past_desperate_is_rolled_at_a_disadvantage(player, monkeypatch):
    _hurt(player, "badly_hurt")
    # (a seasoned hand: at level 1 desperate at a disadvantage is a ~1% roll,
    # and the round-10 odds floor drops the disadvantage — test_choices_round10)
    with get_session() as session:
        session.get(Player, player).level = 6
        session.commit()
    _force(monkeypatch, 19, 4)
    result = roll_check(player, "stealth", "hard", reason="slipping past the gate guard")
    assert result["difficulty"] == "desperate" and result["outcome"] == "failure"   # the lower die


@pytest.mark.parametrize("skill,about", [("persuasion", ""), ("stealth", "npc-1")])
def test_a_wound_never_touches_words_or_a_face(player, monkeypatch, skill, about):
    _hurt(player, "hurt")
    _force(monkeypatch, 9)
    result = roll_check(player, skill, "easy", reason="talking my way in", about_npc_id=about)
    assert result["difficulty"] == "easy" and "wound" not in result


def test_the_brief_says_the_wound_in_one_line(yard):
    from src.services.scene_brief import build_scene_brief

    _hurt(yard["player"], "winded")
    with get_session() as session:
        text = build_scene_brief(session, session.get(Player, yard["player"]))["text"]
    lines = [line for line in text.splitlines() if line.startswith("The player is winded")]
    assert len(lines) == 1 and "climbing, running and sneaking go harder" in lines[0]
    _hurt(yard["player"], "healthy")
    with get_session() as session:
        assert "The player is" not in build_scene_brief(session, session.get(Player, yard["player"]))["text"]


# ---------------------------------------------------------------------------
# 6. The dice never withhold a story beat's words
# ---------------------------------------------------------------------------

@pytest.fixture()
def sorrel_beat(yard):
    from src.models.campaign import Campaign, StoryBeat

    with get_session() as session:
        campaign = Campaign(player_id=yard["player"], title="The Toll Road", status="active", current_act=1)
        session.add(campaign)
        session.flush()
        session.add(StoryBeat(
            campaign_id=campaign.id, act=1, sequence=2, title="What Sorrel Saw", status="fired",
            directive="If Wenna presses Sorrel Tack, have the boy admit he saw a man in an ash-gray cloak; on "
                      "a second press he adds the man moved like an old Watchman would.",
            lead_target={"type": "npc", "id": yard["sorrel"]},
        ))
        session.commit()
    return yard


@pytest.mark.parametrize("reason", ["pressing Sorrel a second time", "pressing the stable hand", ""])
def test_a_social_roll_on_the_beats_willing_person_is_no_roll(sorrel_beat, monkeypatch, reason):
    """fleet8-bram T19: the roll failed and the second-press clue was lost."""
    _force(monkeypatch, 1)
    result = roll_check(sorrel_beat["player"], "persuasion", "tricky", reason=reason)
    assert result.get("no_roll") is True and "outcome" not in result and "event" not in result
    assert "Play it exactly as the beat writes it" in result["instruction"]


def test_a_hostile_beat_person_or_a_physical_roll_is_still_rolled(sorrel_beat, monkeypatch):
    pid = sorrel_beat["player"]
    _force(monkeypatch, 15, 15)
    assert roll_check(pid, "athletics", "easy", reason="vaulting the fence past Sorrel")["event"] == "check_result"
    with get_session() as session:
        session.get(NPC, sorrel_beat["sorrel"]).current_mood = "hostile"
        session.commit()
    assert roll_check(pid, "persuasion", "tricky", reason="pressing Sorrel")["event"] == "check_result"


# ---------------------------------------------------------------------------
# 5. The roll-first hold (a scripted DM through play_sse)
# ---------------------------------------------------------------------------

def _roll_events(pid, outcome, reason, tool_id):
    args = {"player_id": pid, "skill": "persuasion", "difficulty": "tricky", "reason": reason}
    result = {"success": True, "outcome": outcome, "skill": "persuasion", "difficulty": "tricky",
              "event": "check_result", "instruction": "Narrate it."}
    return [
        {"current_tool_use": {"toolUseId": tool_id, "name": "roll_check", "input": json.dumps(args)[:25]}},
        {"current_tool_use": {"toolUseId": tool_id, "name": "roll_check", "input": json.dumps(args)}},
        {"message": {"role": "assistant", "content": [{"toolUse": {"toolUseId": tool_id, "name": "roll_check",
                                                                   "input": args}}]}},
        {"message": {"role": "user", "content": [{"toolResult": {
            "toolUseId": tool_id, "status": "success", "content": [{"text": json.dumps(result)}]}}]}},
    ]


BEFORE = "Sorrel's eyes drop to the straw and he hunches smaller.\n"
AFTER = "Sorrel's shoulders drop, and the words come out of him in a rush.\n"
RISKY = "I lean on him hard. I threaten to tell the Captain he sleeps on watch."


@pytest.fixture()
def stream(yard, tmp_path, monkeypatch):
    from src.services import memory_files
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    pid = yard["player"]
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    script: list = []

    class Agent:
        messages: list = []

        async def stream_async(self, context):
            for event in script.pop(0):
                yield event

    class DM:
        agent = Agent()
        last_brief = {"present": {yard["sorrel"]: "Sorrel Tack"}}
        _player_name = "Wenna Cole"

        def _build_context(self, player_input, **_kwargs):
            return "\n\n".join(filter(None, [roll_intent.directive(player_input), f"Player says: {player_input}"]))

    session = {"dm": DM(), "tool_tracker": ToolUsageTracker()}
    monkeypatch.setattr(server, "get_or_create_session", lambda _pid: session)
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    for store in (server._pending_audit_feedback, server._pending_engine_notes, server._pending_command_ledger):
        store.pop(pid, None)

    def play(words, events):
        script.append(events)

        async def run():
            response = await server.play_sse(server.GameRequest(player_input=words, player_id=pid))
            return [chunk async for chunk in response.body_iterator]
        frames = []
        for chunk in asyncio.run(run()):
            for line in str(chunk).splitlines():
                if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                    frames.append(json.loads(line[6:]))
        shown = "".join(f.get("data") or "" for f in frames if f.get("type") == "token")
        complete = next(f for f in frames if f.get("type") == "complete")
        return shown, complete, frames

    return play


def test_prose_written_before_the_roll_never_reaches_the_player(stream, yard):
    """fleet8-bram T19: 'Sorrel's eyes drop to the straw' (before the roll)
    and 'Sorrel flinches back a step' (after it), both on screen."""
    assert roll_intent.directive(RISKY)
    shown, complete, frames = stream(RISKY, [{"data": BEFORE}, {"data": "@time 10m\n"}]
                                     + _roll_events(yard["player"], "success", "leaning on the stable hand", "t1")
                                     + [{"data": AFTER}])
    assert "eyes drop to the straw" not in shown
    assert "shoulders drop" in shown
    assert complete["commands"][0]["ok"] is True                       # commands land as they come
    audit = {w["check"]: w["detail"] for w in complete["audit"]}
    assert "threw it away unread" in audit["prose_before_roll"]
    assert [f["outcome"] for f in frames if f.get("type") == "check_result"] == ["success"]


def test_an_ordinary_turn_streams_untouched(stream, yard):
    shown, complete, _ = stream("How long have you worked the yard?",
                                [{"data": BEFORE}] + _roll_events(yard["player"], "success", "asking", "t2")
                                + [{"data": AFTER}])
    assert "eyes drop to the straw" in shown and "shoulders drop" in shown
    assert "player read an outcome" in {w["check"]: w["detail"] for w in complete["audit"]}["prose_before_roll"]


def test_a_reply_that_never_rolls_lets_its_text_go(stream):
    shown, complete, _ = stream(RISKY, [{"data": BEFORE}, {"data": AFTER}])
    assert "eyes drop to the straw" in shown and "shoulders drop" in shown
    assert "prose_before_roll" not in [w["check"] for w in complete["audit"]]


def test_the_hold_lets_go_at_its_cap(stream, yard):
    long_text = ("The yard is quiet but for the horses, shifting in their stalls. " * 12).strip() + "\n"
    assert len(long_text) > roll_intent.HOLD_CHARS
    shown, complete, _ = stream(RISKY, [{"data": long_text}]
                                + _roll_events(yard["player"], "success", "leaning on him", "t3")
                                + [{"data": AFTER}])
    assert "The yard is quiet" in shown and "shoulders drop" in shown
    assert "player read an outcome" in {w["check"]: w["detail"] for w in complete["audit"]}["prose_before_roll"]


# ---------------------------------------------------------------------------
# Review: false positives the detectors must not raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["getting the guard to ignore it", "spotting the spurious seal on the pass",
                                    "persuading him his help is not needed", "convincing him to discard the letter"])
def test_an_act_that_only_sounds_like_a_placeholder_is_rolled(player, monkeypatch, reason):
    _force(monkeypatch, 15)
    assert roll_check(player, "cunning", "easy", reason=reason)["event"] == "check_result"


@pytest.mark.parametrize("reason", ["roll not needed", "no roll needed", "dummy roll", "ignore this roll",
                                    "dropping from the loft (not needed)"])
def test_words_about_the_roll_itself_are_still_refused(player, monkeypatch, reason):
    _force(monkeypatch, 1)
    assert "nothing was rolled" in roll_check(player, "athletics", "easy", reason=reason)["error"]


@pytest.mark.parametrize("words", [
    "I don't wait for a full answer, I can read evasion.",                                     # fleet3-bram T10
    "I turn toward the Stable Yard road without waiting for more.",                            # fleet8-bram T11
    "A hot meal and a few hours flat on a mattress beats standing watch in the cold.",         # fleet7-bram T20
    "I glance back toward where Sorrel's still lingering by the well like he's waiting for permission.",
    "I'm thinking about how he knew to watch for me before I even knew he was there.",         # fleet6-tamsin T20
    '"He didn\'t say whose hand," I reply, watching for the flicker.',                         # fleet4-tamsin T11
    "I sit with the letter, letting the implications settle into place.",                     # fleet6-wenna T18
    '"Ten coin\'s not much, but I\'ll take the tincture and I\'ll listen."',                  # fleet8-bram T10
])
def test_a_watch_nobody_keeps_now_is_no_vigil(words):
    """Replayed over every playtest input, 13 of 32 vigils were these — each
    one a night-edge scout at a player who was watching for nothing."""
    assert complications.keeping_vigil(words) is None


def test_a_watch_already_facing_a_danger_brings_no_second_beast(emberfall_night):
    """fleet7-tamsin T18 "positioning myself where it can see me", between
    the Captain and a wolf: the watch has its answer already."""
    pid, yard_id = emberfall_night["player"], emberfall_night["Stable Yard"]
    with get_session() as session:
        session.add(NPC(name="Grey Wolf", species="wolf", danger_tier="weak", current_location_id=yard_id,
                        current_mood="hostile"))
        session.commit()
    _quiet(pid, complications.VIGIL_SCOUT_QUIET)
    assert complications.directive_line(pid, player_input=BRAM_T14) is None
    assert _scouts(yard_id) == []


def test_a_won_word_with_someone_else_faces_no_danger_down(yard):
    pid, place = yard["player"], yard["yard"]
    encounters.note_turn(pid, place)
    assert encounters.note_check(pid, "success", "persuasion", "pressing Sorrel for the truth") == []
    assert _lingering(pid, place, LOOKING) is not None


def test_a_stalled_beat_shows_the_dm_nothing_so_the_roll_stands(sorrel_beat, monkeypatch):
    """A stalled beat's directive is not in the context: "play it as the beat
    writes it" would be an order to invent."""
    from src.models.campaign import StoryBeat

    with get_session() as session:
        session.query(StoryBeat).first().status = "stalled"
        session.commit()
    _force(monkeypatch, 15)
    assert roll_check(sorrel_beat["player"], "persuasion", "tricky", reason="pressing Sorrel")["event"] \
        == "check_result"


def test_a_reason_naming_nobody_beside_a_second_person_is_rolled(sorrel_beat, monkeypatch):
    pid = sorrel_beat["player"]
    with get_session() as session:
        session.add(NPC(name="Hob Varr", profession="gate sentry", current_location_id=sorrel_beat["yard"]))
        session.commit()
    _force(monkeypatch, 15)
    assert roll_check(pid, "cunning", "tricky", reason="talking my way past")["event"] == "check_result"
    assert roll_check(pid, "persuasion", "tricky", reason="pressing Sorrel")["no_roll"] is True


def test_a_held_speaker_line_after_a_move_in_the_same_chunk_keeps_the_new_place(stream, yard):
    """One chunk carrying `@move` and the speaker line after it: the line was
    held before the move had landed, and resolved against the old room."""
    from src.models import Connection
    from src.services.dialogue import role_label

    with get_session() as session:
        lantern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, discovered=True,
                           description="A low, smoky tavern.")
        session.add(lantern)
        session.flush()
        keeper = NPC(name="Maera Vell", profession="innkeeper", current_location_id=lantern.id)
        session.add_all([keeper, Connection(from_location_id=yard["yard"], to_location_id=lantern.id,
                                            travel_type="walk", travel_time_hours=0.05, discovered=True)])
        session.commit()
        label, keeper_id = role_label(keeper), keeper.id
    chunk = f'@move The Drowned Lantern\n{label} (warm): "Sit down before you fall down."\n'
    _, complete, frames = stream(RISKY, [{"data": chunk}])
    assert complete["commands"][0]["ok"] is True
    assert [f.get("npc_id") for f in frames if f.get("type") == "speech"] == [keeper_id]
