"""A world with teeth: a quiet streak earns one complication.

fleet5-wenna: 21 turns, no fight, no wound, no failed roll; eight night turns
in town with no incident. A turn that cost the player nothing is quiet; after
six to eight of them the next ordinary reply gets ONE complication line,
picked in code for where the player stands.
"""

import pytest

from src.models import (
    Location, LocationType, NPC, NPCRelationship, Player, Quest, QuestStatus, WorldClock, get_session,
)
from src.services import complications, encounters
from src.services.commands import CommandResult


@pytest.fixture()
def town(db):
    complications.reset_state()
    encounters._parley.clear()
    encounters._talking.clear()
    with get_session() as session:
        session.add(WorldClock(day=1, hour=21))
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True,
                          description="A bowl of birch and bracken north of the gate.")
        session.add_all([square, hollow])
        session.flush()
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=12,
                        inventory=[{"id": "howl_chart", "name": "Howl-Chart", "type": "misc"}])
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "hollow": hollow.id}


def _quiet(pid, n=1, **kw):
    for _ in range(n):
        complications.note_turn(pid, player_input="I ask Maera about the carter.", **kw)


def _ok(line, **result):
    return CommandResult(line=line, verb="", ok=True, tool=None, result=result)


def test_the_count_climbs_on_quiet_turns_and_fires_once(town):
    pid = town["player"]
    need = complications.threshold(pid)
    assert complications.QUIET_TURNS <= need < complications.QUIET_TURNS + complications.JITTER
    _quiet(pid, need - 1)
    assert complications.directive_line(pid) is None              # not yet
    _quiet(pid)
    line = complications.directive_line(pid)
    assert line and line.startswith("[COMPLICATION — the world pushes back THIS reply")
    assert "cost them something or force a choice" in line
    # Fired: the count starts over, so the next reply never gets a second one
    assert complications.quiet_turns(pid) == 0
    assert complications.directive_line(pid) is None


@pytest.mark.parametrize("teeth", [
    {"roll_outcomes": ["partial"]},
    {"roll_outcomes": ["failure"]},
    {"state_before": {"health": "healthy"}, "state_after": {"health": "winded"}},
    {"commands": [_ok("@fight Hollow Wolf")]},
    {"commands": [_ok("@coin -5 | the bribe")]},
    {"commands": [_ok("@remove Howl-Chart")]},
    {"commands": [CommandResult(line="@move Wolf's Hollow", verb="move", ok=False, tool="move_player",
                                result={"blocked": True})]},
    {"ambushed": True},
    {"refused": True},
])
def test_a_turn_with_teeth_starts_the_count_over(town, teeth):
    pid = town["player"]
    _quiet(pid, 4)
    complications.note_turn(pid, player_input="I press on.", **teeth)
    assert complications.quiet_turns(pid) == 0


def test_spending_and_good_news_are_still_quiet(town):
    pid = town["player"]
    _quiet(pid, 2, commands=[_ok("@coin +10 | paid"), _ok("@trust Piper +5")],
           roll_outcomes=["success"])
    assert complications.quiet_turns(pid) == 2


def test_engine_turns_do_not_count_and_a_combat_outcome_is_a_fight(town):
    pid = town["player"]
    _quiet(pid, 3)
    complications.note_turn(pid, player_input="[OPENING] This is Wenna's first moment.")
    assert complications.quiet_turns(pid) == 3
    complications.note_turn(pid, player_input="[COMBAT OUTCOME — resolved in real-time] They won.")
    assert complications.quiet_turns(pid) == 0


def test_never_on_an_engine_turn_in_a_parley_or_when_something_owns_the_reply(town):
    pid = town["player"]
    _quiet(pid, 10)
    assert complications.directive_line(pid, engine_turn=True) is None
    encounters._talking[pid] = True
    assert complications.directive_line(pid) is None
    encounters._talking[pid] = False
    must = {"fired_events": [{"id": "e1", "must_deliver": True}], "undelivered_events": []}
    assert complications.directive_line(pid, world_tick=must) is None
    rested = "[SINCE YOUR LAST TURN ...\n- Rested: They slept the night at The Drowned Lantern.]"
    assert complications.directive_line(pid, canvas_notes=rested) is None
    assert complications.quiet_turns(pid) == 10                    # held, not spent
    assert complications.directive_line(pid) is not None


def test_a_parley_the_lingering_roll_held_still_holds_the_complication(town):
    """play_sse rolls the encounter BEFORE _build_context asks: with wolves
    present a held roll spent the parley flag, and the complication cut into
    the talk the hold was protecting."""
    import random

    pid = town["player"]
    with get_session() as session:
        session.add(NPC(name="Gray-Flank Wolf", species="wolf", danger_tier="weak",
                        current_location_id=town["square"]))
        caller = NPC(name="Vex Marrow", danger_tier="dangerous", current_mood="hostile",
                     current_location_id=town["square"])
        session.add(caller)
        session.commit()
        caller_id = caller.id
    for state in (encounters._linger, encounters._since_ambush, encounters._parley_skips):
        state.clear()
    _quiet(pid, 10)
    assert encounters.note_parley(pid, [caller_id]) is True       # the reply was a talk
    encounters.note_turn(pid, town["square"])                      # the next turn...
    encounters.note_turn(pid, town["square"])
    strikes = type("Strikes", (random.Random,), {"random": lambda self: 0.0})()
    assert encounters.roll(pid, arrived=False, rng=strikes) is None  # ...holds the roll for it
    assert complications.directive_line(pid) is None
    assert complications.quiet_turns(pid) == 10                    # held, not spent


def test_a_campaign_beat_this_turn_owns_the_reply(town, monkeypatch):
    from src.services import campaign

    pid = town["player"]
    _quiet(pid, 10)
    monkeypatch.setattr(campaign, "get_last_tick", lambda _pid: {"fired_beat": {"title": "A Cloak by the Wall"}})
    briefing = 'THIS TURN — MUST DELIVER  [reveal · "A Cloak by the Wall"]'
    assert complications.directive_line(pid, campaign_briefing=briefing) is None
    # A resolved campaign's old tick lingers in the cache, not in the text
    assert complications.directive_line(pid, campaign_briefing="[NO STORY SPINE]") is not None


def test_town_and_wild_get_their_own_menus_and_never_the_same_twice(town):
    pid = town["player"]
    town_kinds = {k for k, *_ in complications._TOWN}
    wild_kinds = {k for k, *_ in complications._WILD}
    seen = []
    for _ in range(4):
        _quiet(pid, 10)
        complications.directive_line(pid)
        seen.append(complications._last_kind[pid])
        complications._open.clear()               # answered: the next may come
    assert set(seen) <= town_kinds
    assert all(a != b for a, b in zip(seen, seen[1:]))
    with get_session() as session:
        session.get(Player, pid).current_location_id = town["hollow"]
        session.commit()
    _quiet(pid, 10)
    line = complications.directive_line(pid)
    kind = complications._last_kind[pid]
    assert kind in wild_kinds
    wordings = [what for k, _, what, _ in complications._WILD if k == kind] + list(complications._WORDINGS[kind])
    assert any(what in line for what in wordings)


def test_a_pickpocket_needs_something_to_steal(town):
    pid = town["player"]
    with get_session() as session:
        player = session.get(Player, pid)
        player.currency, player.inventory = 0, []
        session.commit()
    for _ in range(6):
        _quiet(pid, 10)
        complications.directive_line(pid)
        assert complications._last_kind[pid] not in ("pickpocket", "rival")   # no goods, no quest
        complications._open.clear()


def test_what_they_wear_and_the_quests_proof_are_not_goods(town):
    """@remove has no guard: the knife in hand (every starting kit) and the
    bone whistle the quest hangs on must never be the pickpocket's prize."""
    pid = town["player"]
    with get_session() as session:
        player = session.get(Player, pid)
        player.currency = 0
        player.inventory = [{"id": "crude_knife", "name": "Crude Knife", "type": "weapon"},
                            {"id": "bone_whistle", "name": "Cracked Bone Whistle", "type": "quest_item"}]
        player.equipment = {"weapon": "crude_knife"}
        session.commit()
        assert complications._stealable(player) is False
        player.inventory = player.inventory + [{"id": "draught", "name": "Healing Draught", "type": "consumable"}]
        session.commit()
        assert complications._stealable(player) is True
    pick = next(bite for kind, _, _, bite in complications._TOWN if kind == "pickpocket")
    assert "never what they wear, never a clue or a quest's proof" in pick


def test_a_place_full_of_beasts_reads_as_wild(town):
    with get_session() as session:
        den = Location(name="Old Cistern", type=LocationType.POI, discovered=True)
        session.add(den)
        session.flush()
        session.add(NPC(name="Gray-Flank Wolf", species="wolf", current_location_id=den.id))
        player = session.get(Player, town["player"])
        player.current_location_id = den.id
        session.commit()
        assert complications._setting(session, player) == "wild"


def test_the_line_rides_in_the_context_before_the_reply_budget(monkeypatch):
    """dm_orchestrator._build_context asks once per turn; an engine turn is told so."""
    from types import SimpleNamespace

    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import REPLY_BUDGET, DMOrchestrator

    monkeypatch.setattr(dm_orchestrator, "get_player", lambda pid: {"name": "Wenna Cole", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [])
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {"fired_events": []})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "### WORLD STATE")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)
    asked = []

    def line(player_id, **kw):
        asked.append(kw)
        return None if kw["engine_turn"] else "[COMPLICATION — test]"

    monkeypatch.setattr(complications, "directive_line", line)
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Wenna Cole")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- Teeth [q1]"]
    context = DMOrchestrator._build_context(fake, "I sit by the window and wait.", canvas_notes="")
    assert context.index("[COMPLICATION — test]") < context.index(REPLY_BUDGET)
    assert asked[-1]["engine_turn"] is False and asked[-1]["world_tick"] == {"fired_events": []}
    opening = DMOrchestrator._build_context(fake, "[OPENING] This is Wenna's first moment.")
    assert "[COMPLICATION" not in opening and asked[-1]["engine_turn"] is True


# ---------------------------------------------------------------------------
# Round 5: threats that act (fleet6-tamsin T17-T20, fleet6-wenna T7-T10, T18)
# ---------------------------------------------------------------------------

def _force(monkeypatch, kind):
    """The menu's pick, pinned: these tests are about what follows it."""
    menu = {k: (k, what, bite) for k, _, what, bite in complications._TOWN + complications._WILD}
    monkeypatch.setattr(complications, "_pick", lambda *a, **k: menu[kind])


def _row(line, ok=True):
    return {"line": line, "ok": ok}


@pytest.fixture()
def villains(town):
    """The story's villain and successor stand at the Hollow, in their looks."""
    from src.models.campaign import Campaign

    with get_session() as session:
        vex = NPC(name="Vex Marrow", description_physical="a lean man in a cloak the color of wet ash",
                  current_location_id=town["hollow"], danger_tier="dangerous")
        aldric = NPC(name="Aldric Vane", description_physical="a grey man under a wolf-pelt mantle",
                     current_location_id=town["hollow"], danger_tier="boss")
        session.add_all([vex, aldric])
        session.flush()
        session.add(Campaign(player_id=town["player"], title="The Toll Road", status="active",
                             villain_npc_id=aldric.id, successor_npc_id=vex.id))
        session.commit()
        return {**town, "vex": vex.id, "aldric": aldric.id}


def test_a_watcher_is_a_real_stranger_never_the_villains_look(villains, monkeypatch):
    """fleet6-tamsin T18 dressed the T17 watcher in Vex Marrow's wet-ash cloak
    while Vex stood at the Hollow; the watcher was never made a person."""
    from src.services.dialogue import role_label

    pid = villains["player"]
    _force(monkeypatch, "watcher")
    _quiet(pid, 10)
    line = complications.directive_line(pid)
    assert "Who: a stranger — make them real FIRST with this line, exactly: @new-npc " in line
    assert "| at: Market Square" in line
    with get_session() as session:
        for npc_id in (villains["vex"], villains["aldric"]):
            assert f"the {role_label(session.get(NPC, npc_id))}" in line
    assert "in your own words" in line
    opened = complications.open_complication(pid)
    assert opened["kind"] == "watcher" and opened["stub"].startswith("@new-npc ")


def test_an_unanswered_complication_is_pressed_once_then_let_go(villains, monkeypatch):
    pid = villains["player"]
    _force(monkeypatch, "watcher")
    _quiet(pid, 10)
    complications.directive_line(pid)
    # The reply that brought it (its @tension is no answer), then two more
    complications.note_turn(pid, player_input="I keep Piper between us.", commands=[_row("@tension rising")],
                            location_id=villains["square"])
    assert complications.unanswered_line(pid) is None
    for words in ("I settle the errand at the bar.", "Show me the satchel."):
        complications.note_turn(pid, player_input=words, commands=[_row("@time 15m")],
                                location_id=villains["square"])
    pressed = complications.unanswered_line(pid)
    assert pressed.startswith("[COMPLICATION — UNANSWERED.")
    assert "Act on it NOW, in this reply, with a command" in pressed
    assert "@npc-move" in pressed and "write this first, exactly — @new-npc" in pressed
    assert complications.unanswered_line(pid) is None              # once
    complications.note_turn(pid, player_input="I ignore him.", location_id=villains["square"])
    assert complications.open_complication(pid) is None            # let go


@pytest.mark.parametrize("answer", [
    {"commands": ["@npc-move {who} | Market Square"]},
    {"commands": ["@fight {who}"]},
    {"commands": ["@time 45m | slipping out the back"]},
    {"ambushed": True},
])
def test_a_command_that_acts_on_it_answers_it(villains, monkeypatch, answer):
    pid = villains["player"]
    _force(monkeypatch, "watcher")
    _quiet(pid, 10)
    complications.directive_line(pid)
    who = complications.open_complication(pid)["entity_name"]
    answer = {**answer, "commands": [_row(c.format(who=who)) for c in answer.get("commands", [])]}
    complications.note_turn(pid, player_input="I watch him back.", location_id=villains["square"], **answer)
    assert complications.open_complication(pid) is None


def test_moving_someone_else_or_the_turns_own_time_is_no_answer(villains, monkeypatch):
    pid = villains["player"]
    _force(monkeypatch, "watcher")
    _quiet(pid, 10)
    complications.directive_line(pid)
    complications.note_turn(pid, player_input="I walk Piper to the door.", location_id=villains["square"],
                            commands=[_row("@npc-move Piper Quillow | Market Square"), _row("@time 15m"),
                                      _row("@trust Piper Quillow +5")])
    assert complications.open_complication(pid) is not None


def test_the_watcher_speaking_answers_it_once_made_real(villains, monkeypatch):
    pid = villains["player"]
    _force(monkeypatch, "watcher")
    _quiet(pid, 10)
    complications.directive_line(pid)
    name = complications.open_complication(pid)["entity_name"]
    with get_session() as session:
        made = NPC(name=name, current_location_id=villains["square"])
        session.add(made)
        session.commit()
        made_id = made.id
    complications.note_turn(pid, player_input="I cross to his table.", location_id=villains["square"])
    assert complications.open_complication(pid)["entity_id"] == made_id
    complications.note_turn(pid, player_input="I ask him what he wants.", speaker_ids=[made_id],
                            location_id=villains["square"])
    assert complications.open_complication(pid) is None


def test_walking_off_lets_it_follow_sooner(villains, monkeypatch):
    pid = villains["player"]
    _force(monkeypatch, "watcher")
    _quiet(pid, 10)
    complications.directive_line(pid)
    complications.note_turn(pid, player_input="I notice.", location_id=villains["square"])
    complications.note_turn(pid, player_input="I walk out.", location_id=villains["hollow"])
    assert complications.unanswered_line(pid) is not None


def test_the_wording_rotates(villains, monkeypatch):
    """fleet6-tamsin T17 copied the watcher line almost word for word."""
    pid = villains["player"]
    _force(monkeypatch, "watcher")
    lines = []
    for _ in range(3):
        _quiet(pid, 10)
        lines.append(complications.directive_line(pid))
        complications._open.clear()
    wordings = (complications._TOWN[0][2],) + complications._WORDINGS["watcher"]
    used = {w for w in wordings for line in lines if w in line}
    assert len(used) == 3


def test_bad_news_never_runs_ahead_of_the_schedule(town, monkeypatch):
    """fleet6-wenna T9 told the Greywater cart overturned and driverless; the
    world event of it limping in with its driver was still scheduled."""
    from src.models import Event

    pid = town["player"]
    with get_session() as session:
        session.add(Event(name="The Greywater Cart Arrives Short", description="It limps in.",
                          event_type="meso", scheduled_day=2, scheduled_hour=9))
        session.commit()
    _force(monkeypatch, "bad_news")
    _quiet(pid, 10)
    line = complications.directive_line(pid)
    assert ("Never anything on the world's own schedule or the story's clock "
            "(The Greywater Cart Arrives Short)") in line
    assert "THEY set in motion" in line


def _summons_news(town):
    """A summons carries news or a clock (fleet10-tamsin T7-T10): one due
    within the hours a summons sends for."""
    from src.models import Event

    with get_session() as session:
        session.add(Event(name="The Night Bell", description="The bell calls the Watch to the walls.",
                          event_type="meso", player_visible=True, scheduled_day=1, scheduled_hour=23))
        session.commit()


def test_a_summons_names_who_sends_and_what_they_could_know(town, monkeypatch):
    """fleet6-wenna T18: the runner's Captain knew 'about the letter' she
    had read alone upstairs minutes before."""
    pid = town["player"]
    with get_session() as session:
        brann = NPC(name="Captain Brann Coldiron", current_location_id=town["hollow"])
        session.add(brann)
        session.flush()
        session.add(NPCRelationship(npc_id=brann.id, player_id=pid, name_known=True))
        quest = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE, assigned_by_npc_id=brann.id)
        session.add(quest)
        session.flush()
        session.get(Player, pid).active_quests = [quest.id]
        session.commit()
    _summons_news(town)
    _force(monkeypatch, "summons")
    _quiet(pid, 10)
    line = complications.directive_line(pid)
    assert "Who sends: Captain Brann Coldiron (not here)" in line
    assert "The Night Bell, due at 23:00" in line
    assert "the job they took from Captain Brann Coldiron in the open ('Teeth on the North Road')" in line
    assert "never anything the player found, read or heard in private" in line
    assert "@trust Captain Brann Coldiron -5" in complications.open_complication(pid)["ignored"]


def test_a_name_only_heard_of_sends_for_nobody(town, monkeypatch):
    """A name the player only heard (heard_names) makes them 'met' for the
    masks, but nobody they never spoke with sends a runner for them."""
    from src.services import dialogue

    pid = town["player"]
    with get_session() as session:
        session.add(NPC(name="Captain Brann Coldiron", current_location_id=town["hollow"]))
        session.commit()
    dialogue.remember_heard(pid, ["Brann", "Coldiron"])
    _summons_news(town)
    _force(monkeypatch, "summons")
    _quiet(pid, 10)
    line = complications.directive_line(pid)
    assert "Coldiron" not in line and "The summoner knows only what the town saw" in line


def test_a_summons_from_nobody_known_asks_for_no_trust_it_cannot_name(town, monkeypatch):
    """Nobody met stands elsewhere: the press names the side, never a
    '@trust the one who sent for them -5' that could only fail."""
    pid = town["player"]
    _summons_news(town)
    _force(monkeypatch, "summons")
    _quiet(pid, 10)
    assert "The summoner knows only what the town saw" in complications.directive_line(pid)
    ignored = complications.open_complication(pid)["ignored"]
    assert "@trust" not in ignored and "@rep down with the side that sent for them" in ignored


# ---------------------------------------------------------------------------
# Night at the edge of town: one weak beast scout (the Emberfall seed)
# ---------------------------------------------------------------------------

@pytest.fixture()
def emberfall_night(tmp_path):
    from src.data.playtest_seed import create_playtest_world
    from src.models import DMState, reset_engine

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    complications.reset_state()
    for state in (encounters._linger, encounters._since_ambush, encounters._parley, encounters._parley_skips,
                  encounters._talking, encounters._held, encounters._drawn):
        state.clear()
    with get_session() as session:
        places = {loc.name: loc.id for loc in session.query(Location).all()}
        clock = session.query(WorldClock).first()
        clock.hour, clock.minute = 21, 10
        session.query(DMState).first().tension = "rising"
        # The yard on the paddock fence: the square is no edge of town since
        # the north road leaves from the (indoor) gatehouse (fleet7-tamsin)
        player = Player(name="Wenna Cole", current_location_id=places["Stable Yard"], currency=10)
        session.add(player)
        session.commit()
        ids = {"player": player.id, **places}
    yield ids
    reset_engine()


def _scouts(location_id):
    with get_session() as session:
        return [n for n in session.query(NPC).filter(NPC.current_location_id == location_id).all()
                if n.profession and n.profession.endswith("scout")]


def test_a_night_at_the_edge_of_town_brings_one_weak_scout(emberfall_night):
    """fleet6-wenna stood at the paddock fence three night turns while the
    howl "walked the fence line in" — and nothing came."""
    pid, square = emberfall_night["player"], emberfall_night["Stable Yard"]
    _quiet(pid, complications.SCOUT_QUIET)
    line = complications.directive_line(pid)
    assert line.startswith("[COMPLICATION — night at the edge of town")
    scouts = _scouts(square)
    assert len(scouts) == 1
    scout = scouts[0]
    with get_session() as session:
        wolf = session.query(NPC).filter_by(name="Hollow Wolf").one()
        assert (scout.danger_tier, scout.species, scout.faction_id) == ("weak", "wolf", wolf.faction_id)
    assert f"@fight {scout.name}" in line and f"@npc-move {scout.name} | Wolf's Hollow" in line
    assert "feed it" in line and "Do not start the fight for them" in line
    # A fed scout leaves: a beast left standing in the square is the next ambush
    assert f"a win and it slinks off: @npc-move {scout.name} | Wolf's Hollow" in line
    # Once a night
    complications._open.clear()
    _quiet(pid, complications.SCOUT_QUIET)
    assert complications.directive_line(pid) is None
    assert len(_scouts(square)) == 1


def test_the_scout_is_a_choice_before_it_is_an_ambush(emberfall_night):
    import random

    pid, square = emberfall_night["player"], emberfall_night["Stable Yard"]
    _quiet(pid, complications.SCOUT_QUIET)
    complications.directive_line(pid)
    strikes = type("Strikes", (random.Random,), {"random": lambda self: 0.0})()
    encounters.note_turn(pid, square)
    encounters.note_turn(pid, square)
    assert encounters.roll(pid, arrived=False, rng=strikes) is None        # held for the player's answer
    struck = encounters.roll(pid, arrived=False, rng=strikes)
    assert struck and struck["names"] == [_scouts(square)[0].name]          # ignored, it comes for them


def test_the_stable_yard_fence_is_the_edge_too(emberfall_night):
    pid, yard = emberfall_night["player"], emberfall_night["Stable Yard"]
    with get_session() as session:
        session.get(Player, pid).current_location_id = yard
        session.commit()
    _quiet(pid, complications.SCOUT_QUIET)
    line = complications.directive_line(pid)
    assert line and "from the Wolf's Hollow side" in line
    assert len(_scouts(yard)) == 1


@pytest.mark.parametrize("change", ["indoors", "daytime", "calm"])
def test_no_scout_indoors_by_day_or_in_a_calm(emberfall_night, change):
    from src.models import DMState

    pid = emberfall_night["player"]
    with get_session() as session:
        if change == "indoors":
            session.get(Player, pid).current_location_id = emberfall_night["The Drowned Lantern"]
        elif change == "daytime":
            session.query(WorldClock).first().hour = 14
        else:
            session.query(DMState).first().tension = "low"
        session.commit()
    _quiet(pid, complications.SCOUT_QUIET)
    assert complications.directive_line(pid) is None      # below the ordinary threshold: nothing at all
    assert not any(_scouts(emberfall_night[name])
                   for name in ("Stable Yard", "Market Square", "The Drowned Lantern"))


# --- Round 6 (fleet7-wenna): a gift is no friction -------------------------------

@pytest.mark.parametrize("words, line", [
    # T3: her lavender, offered as a gift — the @remove has no reason
    ('I pull out a small bundle of dried lavender and offer it to her. "A gift, for being brave."',
     "@remove Dried Lavender Bundle"),
    # T6: coin pressed into Sorrel's hand for his trouble
    ("I press a page into Sorrel's hand along with a couple coin.",
     "@coin -2 | a few coins pressed into Sorrel's hand for his trouble"),
    ("I toss the beggar a coin.", "@coin -1"),
])
def test_what_the_player_gives_away_is_still_quiet(town, words, line):
    pid = town["player"]
    _quiet(pid, 3)
    complications.note_turn(pid, player_input=words, commands=[_ok(line)])
    assert complications.quiet_turns(pid) == 4


@pytest.mark.parametrize("words, line", [
    ("I give the lavender to Piper.", "@remove Howl-Chart"),                  # not what she gave
    ("I offer him a coin.", "@remove Howl-Chart | lifted by the pickpocket"),  # the world took it
    ("I hand over the coin.", "@coin -5 | the toll at the gate"),
    ("I walk to the gate.", "@coin -3"),
    # A price the world sets, however it is worded (review, round 6)
    ("I pay up.", "@coin -5 | paid for the toll at the North Gate"),
    ("I hand over the coin the guard demands.", "@coin -4"),
    ("I offer the sergeant a coin to look away.", "@coin -3 | offered as a bribe"),
])
def test_what_the_world_takes_still_has_teeth(town, words, line):
    pid = town["player"]
    _quiet(pid, 3)
    complications.note_turn(pid, player_input=words, commands=[_ok(line)])
    assert complications.quiet_turns(pid) == 0
