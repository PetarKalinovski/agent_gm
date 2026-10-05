"""Round 10 (fleet10): the player's choices, rolls and body.

  * fleet10-wenna T20: the night-edge scout was shown, "You plant your feet
    and go still" written for her, rolled and sent off in ONE reply — the
    session's only danger decided for the player.
  * fleet10-tamsin T14/T19: a fight won with no hit taken got "your arm
    throbs where teeth caught you" and @health winded; one that added no
    wound, @health badly_hurt. T20: her only draught drunk unasked.
  * fleet10-bram T18-T19: the DM's disadvantage and the wound's step stacked
    into a ~1% climb the DM itself had offered.
  * fleet10-tamsin T18: turning for the gate, the wolf was "already airborne".
  * fleet10-wenna T13 ("not quite a lie") and fleet10-tamsin T1 ("sixty feels
    thin") went unrolled.
  * fleet10-bram T14-T15: a look took the crate's silver, a reach for the
    coin took the whistle, and the @give after the @interact failed.
  * fleet10-bram T19: "the knife skittering off somewhere in the dark", and
    the knife stayed equipped.
"""

import random

import pytest

from src.models import NPC, DMState, Location, LocationType, Player, WorldClock, get_session
from src.services import combat, complications, encounters, roll_intent, stakes
from src.services import progression as prog
from src.services.commands import CommandRunner
from src.services.scene_objects import normalize_objects
from src.tools.world_write import progression as roll_tool
from src.tools.world_write.progression import _REPLY_ROLLS, note_turn_input, roll_check
from src.web.turn_audit import audit_turn, lost_gear


class _Dice:
    """A d20 that rolls exactly what the test says."""

    def __init__(self, *values: int):
        self._values = list(values)

    def randint(self, a: int, b: int) -> int:
        return self._values.pop(0)


class _AlwaysStrikes(random.Random):
    def random(self):
        return 0.0


def _force(monkeypatch, *rolls: int) -> None:
    monkeypatch.setattr(prog, "_RNG", _Dice(*rolls))


def _clear():
    for state in (encounters._linger, encounters._since_ambush, encounters._parley, encounters._parley_skips,
                  encounters._talking, encounters._held, encounters._drawn, encounters._standoff,
                  encounters._standoff_held, encounters._answering, encounters._provoked, encounters._calmed_now,
                  encounters._stood, encounters._shown_now, encounters._warned, encounters._fought_at):
        state.clear()
    complications.reset_state()
    stakes.reset_state()
    _REPLY_ROLLS.clear()


WHISTLE = {"kind": "take", "label": "Cracked bone whistle", "x": 60, "y": 74,
           "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                                "description": "Split down the barrel — silent now."}}}
CRATE = {"kind": "search", "label": "Split supply crate", "x": 80, "y": 56,
         "payload": {"item": {"name": "Greywater Silver Piece", "type": "misc", "description": "Freshly minted."}}}
SCRATCHES = {"kind": "read", "label": "Scratches on the hut wall", "x": 40, "y": 30,
             "payload": {"text": "Tally marks gouged into the doorframe, grouped in threes."}}


@pytest.fixture()
def hollow(db):
    """Wolf's Hollow at night: a weak wolf in the bracken, the whistle, the
    crate, and a hurt player with the starting kit."""
    _clear()
    with get_session() as session:
        place = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True,
                         objects=normalize_objects([WHISTLE, CRATE, SCRATCHES]))
        yard = Location(name="Stable Yard", type=LocationType.DISTRICT, discovered=True)
        session.add_all([place, yard, WorldClock(day=1, hour=21), DMState(tension="low")])
        session.flush()
        wolf = NPC(name="Torn-Ear Wolf", species="wolf", danger_tier="weak", current_location_id=place.id,
                   current_mood="hostile")
        sorrel = NPC(name="Sorrel Tack", profession="stable hand", danger_tier="harmless",
                     current_location_id=yard.id)
        player = Player(name="Tamsin Rook", current_location_id=place.id, currency=25,
                        inventory=[{"id": "worn_knife", "name": "Worn Knife", "type": "weapon", "slot": "weapon",
                                    "tier": "crude"},
                                   {"id": "willow-bark_draught", "name": "Willow-Bark Draught",
                                    "type": "consumable", "effect_tag": "heal_light", "quantity": 1}],
                        equipment={"weapon": "worn_knife"}, health_status="hurt")
        session.add_all([wolf, sorrel, player])
        session.commit()
        ids = {"player": player.id, "wolf": wolf.id, "hollow": place.id, "yard": yard.id, "sorrel": sorrel.id}
    yield ids
    _clear()


def _health(pid, value=None):
    with get_session() as session:
        player = session.get(Player, pid)
        if value is not None:
            player.health_status = value
            session.commit()
        return player.health_status


def _noon():
    """Daylight: no night or weather word of the world's own on the roll."""
    with get_session() as session:
        session.query(WorldClock).first().hour = 12
        session.commit()


def _pack(pid):
    with get_session() as session:
        return {i.get("name"): i for i in session.get(Player, pid).inventory or [] if isinstance(i, dict)}


# ---------------------------------------------------------------------------
# 1. A danger this reply brought in is the player's to answer
# ---------------------------------------------------------------------------

WENNA_T20 = ("I close my notebook carefully. \"Tonight,\" I say firmly. \"Piper needs to know what we found — "
             "wolves communicating past the ridge like they're stationed there.\" I squeeze Sorrel's shoulder. "
             "Then I turn toward the town proper, moving quickly through the darkened square. \"Where does "
             "Piper stay?\"")
FACING = "holding ground and facing down the scout wolf without provoking it"


def _scout_brought_now(ids):
    """The complication director made the wolf this reply's scout."""
    complications._open[ids["player"]] = {
        "kind": "scout", "entity_id": ids["wolf"], "entity_name": "Torn-Ear Wolf", "stub": None,
        "what": "a lone wolf scout came in close", "ignored": "it comes for them", "replies": 0,
        "where": ids["hollow"],
    }


def test_a_roll_on_the_scout_this_reply_brought_waits_for_the_players_answer(hollow, monkeypatch):
    """fleet10-wenna T20: introduced, answered for her, rolled — in one reply."""
    pid = hollow["player"]
    stakes.begin_turn(pid)
    _scout_brought_now(hollow)
    note_turn_input(pid, WENNA_T20)
    _force(monkeypatch, 18)
    result = roll_check(pid, "cunning", "tricky", reason=FACING)
    assert "error" in result and result["error"].startswith("Not yet — nothing was rolled")
    assert "Torn-Ear Wolf only came into the scene in THIS reply" in result["error"]
    assert "event" not in result and not encounters._calmed_now.get(pid)
    # ...and the roll-first stand-off line does not count a beast the player has not seen
    assert encounters.introduced_now(pid) == {hollow["wolf"]}
    assert encounters.beast_here(pid) is False


def test_the_reply_after_the_scout_is_shown_rolls_it(hollow, monkeypatch):
    pid = hollow["player"]
    stakes.begin_turn(pid)
    _scout_brought_now(hollow)
    complications._open[pid]["replies"] = 1          # the reply that showed it is done
    stakes.begin_turn(pid)
    note_turn_input(pid, "I hold my ground and stare the wolf down, slow and steady.")
    assert encounters.beast_here(pid) is True
    _force(monkeypatch, 18)
    assert roll_check(pid, "cunning", "tricky", reason=FACING)["event"] == "check_result"


def test_a_watch_kept_for_the_wolf_that_came_may_roll_at_once(hollow, monkeypatch):
    """A vigil the player chose names what came: their act already met it."""
    pid = hollow["player"]
    stakes.begin_turn(pid)
    _scout_brought_now(hollow)
    note_turn_input(pid, "I stand watch at the fence for the wolf, and if it comes I hold my ground.")
    _force(monkeypatch, 18)
    assert roll_check(pid, "cunning", "tricky", reason=FACING)["event"] == "check_result"


def test_a_roll_about_something_else_the_player_chose_still_rolls(hollow, monkeypatch):
    pid = hollow["player"]
    stakes.begin_turn(pid)
    _scout_brought_now(hollow)
    note_turn_input(pid, "I climb up onto the hut's broken roof to see the ridge.")
    _force(monkeypatch, 18)
    assert roll_check(pid, "athletics", "tricky", reason="climbing onto the hut roof")["event"] == "check_result"


# ---------------------------------------------------------------------------
# 2. Wounds follow how the fight went
# ---------------------------------------------------------------------------

def test_the_combat_outcome_always_says_where_health_stands(hollow):
    """fleet10-tamsin T13-T14 (no hit, healthy) and T18-T19 (a hit on a hurt player)."""
    pid = hollow["player"]
    _health(pid, "healthy")
    clean = combat.combat_outcome_message(combat.apply_combat_result(pid, "victory", hits_taken=0))
    assert "came through unhurt" in clean and "write no @health" in clean
    _health(pid, "hurt")
    bitten = combat.combat_outcome_message(combat.apply_combat_result(pid, "victory", hits_taken=1))
    assert "Health stays 'hurt'" in bitten and "added no wound beyond it; write no @health" in bitten
    untouched = combat.combat_outcome_message(combat.apply_combat_result(pid, "victory", hits_taken=0))
    assert "health stays 'hurt'" in untouched and "no new wound" in untouched
    worse = combat.combat_outcome_message(combat.apply_combat_result(pid, "victory", hits_taken=3))
    assert "Player health is now 'badly_hurt'" in worse
    assert not any(ch.isdigit() for ch in clean + bitten + untouched + worse)


def _aftermath(pid):
    return CommandRunner(pid, after_combat=True)


def test_an_aftermath_never_makes_the_wounds_worse(hollow):
    pid = hollow["player"]
    _health(pid, "healthy")
    refused = _aftermath(pid).run("@health winded | cause: a wolf's teeth before the blade found it")
    assert not refused.ok and refused.result["refused"] == "after_combat_wound"
    assert "their health stays 'healthy'" in refused.message and "came through the fight unhurt" in refused.message
    assert _health(pid) == "healthy"
    # A wound dressed lands; outside an aftermath a new wound lands
    _health(pid, "badly_hurt")
    assert _aftermath(pid).run("@health hurt | cause: the bite bound tight").ok and _health(pid) == "hurt"
    assert CommandRunner(pid).run("@health badly_hurt | cause: a fall").ok and _health(pid) == "badly_hurt"


def test_death_after_a_defeat_stays_the_dms_call(hollow):
    pid = hollow["player"]
    _health(pid, "critical")
    assert _aftermath(pid).run("@health dead | cause: the pack closed over them").ok


# ---------------------------------------------------------------------------
# 3. The player's things are the player's to use
# ---------------------------------------------------------------------------

TAMSIN_T20 = ("I sit down hard on a fallen log, breathing through my teeth. The hut. I need shelter, fire if I can "
              "manage it, and time to think. I limp toward the doorframe, favoring my good leg.")


def _said(pid, words, **kw):
    runner = CommandRunner(pid, **kw)
    runner.player_input = words
    return runner


def test_a_draught_the_player_never_reached_for_is_offered_not_drunk(hollow):
    """fleet10-tamsin T20."""
    pid = hollow["player"]
    refused = _said(pid, TAMSIN_T20).run("@use Willow-Bark Draught")
    assert not refused.ok and refused.result["refused"] == "not_chosen"
    assert refused.message.startswith("offer it — the player did not reach for it")
    assert _pack(pid)["Willow-Bark Draught"]["quantity"] == 1 and _health(pid) == "hurt"


@pytest.mark.parametrize("words", [
    "I uncork the vial with my teeth and drink it down.",            # fleet3-tamsin T13
    "I need shelter, I need to stop the bleeding, and I need to think.",   # fleet10-bram T20
    "I reach for the willow-bark in my pack.",
])
def test_a_draught_the_player_reached_for_is_drunk(hollow, words):
    pid = hollow["player"]
    assert _said(pid, words).run("@use Willow-Bark Draught").ok
    assert "Willow-Bark Draught" not in _pack(pid) or _pack(pid)["Willow-Bark Draught"]["quantity"] == 0


@pytest.mark.parametrize("words", [
    "I keep the draught for later and limp on.",
    "[COMBAT OUTCOME — resolved in real-time by the player, mechanics already applied: victory.]",
])
def test_saving_it_or_an_engine_turn_is_no_reach(hollow, words):
    assert not _said(hollow["player"], words).run("@use Willow-Bark Draught").ok


def test_a_draught_drunk_from_the_quick_slot_is_not_drunk_again(hollow):
    runner = _said(hollow["player"], "I catch my breath.")
    runner.code_actions = {"use_item", "update_player_health"}
    again = runner.run("@use Willow-Bark Draught")
    assert not again.ok and again.result["refused"] == "already_used"
    assert _pack(hollow["player"])["Willow-Bark Draught"]["quantity"] == 1


def test_the_cli_path_is_not_held_back(hollow):
    assert CommandRunner(hollow["player"]).run("@use Willow-Bark Draught").ok


# ---------------------------------------------------------------------------
# 4. No offered choice is a ~1% roll
# ---------------------------------------------------------------------------

def test_the_dms_disadvantage_for_the_wound_is_not_charged_twice(hollow, monkeypatch, caplog):
    """fleet10-bram T19: stealth/desperate (disadvantage) [hurt: asked hard] —
    a natural 19-20 on both dice. Now one die, DC 20: a long shot, said."""
    pid = hollow["player"]
    _noon()
    _force(monkeypatch, 19)                          # one die only: a second would raise IndexError
    result = roll_check(pid, "stealth", "hard", modifier="disadvantage",
                        reason="moving hurt, in the dark, toward where the shape on the ridge vanished")
    assert result["difficulty"] == "desperate" and result["outcome"] == "success"
    assert result["odds"] == "a long shot" and "Going in, it was a long shot" in result["instruction"]
    assert not any(ch.isdigit() for ch in " ".join(str(v) for v in result.values()))


def test_no_roll_goes_below_the_floor(hollow, monkeypatch):
    """Desperate at a disadvantage for a level-1 hand is ~1%: the disadvantage goes."""
    pid = hollow["player"]
    _noon()
    _health(pid, "healthy")
    _force(monkeypatch, 19)
    result = roll_check(pid, "persuasion", "desperate", modifier="disadvantage", reason="talking past the gate guard")
    assert result["outcome"] == "success" and result["odds"] == "a long shot"
    assert roll_tool._chance("desperate", 1, "disadvantage") < roll_tool.ODDS_FLOOR <= roll_tool._chance(
        "desperate", 1, "none")


def test_a_fair_roll_says_no_odds_word(hollow, monkeypatch):
    pid = hollow["player"]
    _noon()
    _health(pid, "healthy")
    _force(monkeypatch, 11)
    assert "odds" not in roll_check(pid, "stealth", "tricky", reason="slipping past the dock guard")


def test_a_disadvantage_for_another_cause_still_stacks_above_the_floor(hollow, monkeypatch):
    """Hurt and watched: the wound's step and the DM's disadvantage for the
    watchers are two causes — rolled with both while the odds allow."""
    pid = hollow["player"]
    _noon()
    _force(monkeypatch, 15, 5)
    result = roll_check(pid, "stealth", "easy", modifier="disadvantage", reason="slipping past the watchers")
    assert result["difficulty"] == "tricky" and result["outcome"] == "failure"   # the lower die


# ---------------------------------------------------------------------------
# 5. Beasts show themselves to a player turning to go
# ---------------------------------------------------------------------------

TAMSIN_T18 = ("I've got what Coldiron sent me for. I turn back toward the gate, moving as fast as my knee allows, the "
              "bone heavy in my doublet. I need to get back to Piper before the gate closes.")


@pytest.mark.parametrize("words,leaving", [
    (TAMSIN_T18, True),
    (WENNA_T20, True),
    ("I head back toward Market Square, the wolves still on my mind.", True),
    ("I back away slowly and retreat toward the road.", True),
    ("I'm not leaving yet. I settle in by the fire ring and listen.", False),
    ('"I\'ll go back to town at first light," I tell Sorrel.', False),
    ("I turn the bone over in my fingers.", False),
    ("I turn toward Sorrel with a nod.", False),                      # facing someone is no leaving
    ("I watch Coldiron walk back to his notice board.", False),       # someone else's walk
    ("I need to know more before I walk back up there.", False),      # a plan
    ("[COMBAT OUTCOME — victory.]", False),
])
def test_turning_to_go_is_read_from_what_they_do(words, leaving):
    assert encounters.leaving(words) is leaving


def _linger(pid, place, words):
    stakes.begin_turn(pid)
    encounters.note_turn(pid, place)
    return encounters.roll(pid, arrived=False, rng=_AlwaysStrikes(), player_input=words)


def test_a_player_turning_to_go_sees_the_beast_first_and_chooses(hollow):
    """fleet10-tamsin T18: the wolf was airborne before she had seen it."""
    pid, place = hollow["player"], hollow["hollow"]
    _linger(pid, place, "I look around the clearing.")          # (an ambush springs; the cooldown runs out)
    for _ in range(encounters.COOLDOWN_TURNS + encounters.AFTER_FIGHT_TURNS):
        encounters.note_turn(pid, place)
    shown = _linger(pid, place, TAMSIN_T18)
    assert shown and shown["shown_first"] is True and shown["names"] == ["Torn-Ear Wolf"]
    line = encounters.directive(shown)
    assert "NO fight has started" in line and "no @move this reply" in line and "Never write their answer" in line
    assert encounters.introduced_now(pid) == {hollow["wolf"]}
    # The reply after is their answer: the wolf sits that roll out...
    assert _linger(pid, place, "I draw my knife and wait to see what it does.") is None
    assert encounters.introduced_now(pid) == set()
    # ...and walking on from it again, it comes
    struck = _linger(pid, place, TAMSIN_T18)
    assert struck and not struck.get("shown_first")


def test_a_danger_shown_long_ago_is_shown_again(hollow):
    """Walked on from within a few replies, it comes; days later, it shows itself first again."""
    pid, place = hollow["player"], hollow["hollow"]
    assert _linger(pid, place, TAMSIN_T18)["shown_first"] is True
    for _ in range(encounters.WARNED_REPLIES + 1):
        stakes.begin_turn(pid)
    encounters._held.clear()
    assert _linger(pid, place, TAMSIN_T18)["shown_first"] is True


def test_a_place_that_just_fought_them_strikes_at_lower_odds(hollow):
    pid, place = hollow["player"], hollow["hollow"]
    _linger(pid, place, "I look around the clearing.")
    assert encounters._fought_at[pid] == place
    for _ in range(encounters.COOLDOWN_TURNS):
        encounters.note_turn(pid, place)
    damped = _linger(pid, place, "I look around the clearing.")
    encounters._fought_at.clear()
    for _ in range(encounters.COOLDOWN_TURNS + encounters.AFTER_FIGHT_TURNS):
        encounters.note_turn(pid, place)
    plain = _linger(pid, place, "I look around the clearing.")
    assert damped and plain and damped["chance"] == pytest.approx(plain["chance"] * encounters.AFTER_FIGHT_DAMP, 0.01)


def test_the_server_starts_no_fight_for_a_danger_shown_first(hollow, monkeypatch):
    from src.web import server

    shown = {"npc_ids": [hollow["wolf"]], "names": ["Torn-Ear Wolf"], "chance": 0.5, "why": "lingering, night",
             "where": "Wolf's Hollow", "shown_first": True}
    monkeypatch.setattr(server.encounters, "roll", lambda *a, **k: shown)
    out = server._engine_ambush(hollow["player"], arrived=False, player_input=TAMSIN_T18)
    assert out == {"encounter": shown, "combat_start": None}
    assert hollow["player"] not in server._pending_combat


# ---------------------------------------------------------------------------
# 6. A half-truth and a haggle are rolled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("words,family", [
    # fleet10-wenna T13
    ("I approach the Watchman at the doors with purpose. \"Captain's expecting me — Piper sent word I'd be "
     "coming by about the howls.\" It's not quite a lie; Piper did know I'd likely show up.", "deception"),
    ("\"The Captain's expecting me,\" I tell the guard, and keep walking.", "deception"),
    ("I claim the Captain sent me and hold out my hand for the key.", "deception"),
    # fleet10-tamsin T1, fleet8-tamsin T2
    ("\"That's interesting work, not just coin-work. But sixty feels thin for proof and travel both.\"", "haggling"),
    ("\"Sixty sounds thin for walking into something that's already taken a cart, Captain.\"", "haggling"),
    ("\"Twelve coin is steep for a bed. Make it eight.\"", "haggling"),
])
def test_a_half_truth_or_an_objection_to_a_price_is_rolled_first(words, family):
    act = roll_intent.risky_act(words)
    assert act is not None and act[0] == family
    assert roll_intent.directive(words).startswith(roll_intent.ROLL_FIRST_MARK)


@pytest.mark.parametrize("words", [
    "\"Piper Quillow sent me — she says you hear the howls at night.\"",   # 25 of 26 true in the fleet
    "I tell him the truth. It's not a lie, and he can see it.",
    "The slope is steep, so I take it slow.",
    "Sixty is good money, and a favor from the law is worth more than sixty some nights.",
    "\"The Captain expects me to report by dawn,\" I say.",
])
def test_a_true_introduction_or_a_fair_price_is_no_act(words):
    assert roll_intent.risky_act(words) is None


# ---------------------------------------------------------------------------
# 7. Looking is not taking; a pickup is not given twice
# ---------------------------------------------------------------------------

BRAM_T14 = ("\"Easy,\" I say low, not moving toward the hut yet. I step sideways slow, knife ready, trying to get a "
            "better angle on that metallic glint in the crate without turning my back to those pale eyes.")
BRAM_T15 = ("I pocket the coin, my gut turning cold. I keep my eyes on the two pale wolves but edge my body toward "
            "the doorframe, ready to move fast.")


def test_a_better_angle_on_the_crate_is_a_look(hollow):
    """fleet10-bram T14: the crate named, nothing reached for — its silver went in his pack."""
    pid = hollow["player"]
    looked = _said(pid, BRAM_T14).run("@interact Split supply crate")
    assert looked.ok and looked.result.get("looked") is True
    assert "inside: Greywater Silver Piece" in looked.message and "nothing taken" in looked.message
    assert "Greywater Silver Piece" not in _pack(pid)


def test_a_reach_for_the_coin_takes_nothing_else(hollow):
    """fleet10-bram T15: "I pocket the coin" took the whistle by the doorframe."""
    pid = hollow["player"]
    refused = _said(pid, BRAM_T15).run("@interact Cracked bone whistle")
    assert not refused.ok and refused.result["refused"] == "not_reached_for"
    assert "Cracked Bone Whistle" not in _pack(pid)


def test_a_thing_named_without_a_reach_is_seen_where_it_lies(hollow):
    pid = hollow["player"]
    looked = _said(pid, "I stare at the cracked whistle lying in the bracken.").run("@interact Cracked bone whistle")
    assert looked.ok and "there: Cracked Bone Whistle" in looked.message and "it lies where it was" in looked.message
    assert "Cracked Bone Whistle" not in _pack(pid)


@pytest.mark.parametrize("words", [
    "I grab the bone — it's small enough to pocket.",                    # fleet10-tamsin T16
    "My hand finds the whistle. I pick it up carefully and pocket it.",  # fleet7-wenna T11
    "I pick it up carefully, turning it over, and pocket it.",
])
def test_a_reach_for_the_whistle_takes_it(hollow, words):
    assert _said(hollow["player"], words).run("@interact Cracked bone whistle").ok
    assert "Cracked Bone Whistle" in _pack(hollow["player"])


def test_a_give_after_the_pickup_is_dropped_quietly(hollow):
    """fleet10-bram T15 / -tamsin T16: 'not given twice' booked as a failure."""
    runner = _said(hollow["player"], "I grab the whistle.")
    assert runner.run("@interact Cracked bone whistle").ok
    again = runner.run("@give Cracked Bone Whistle | kind: clue")
    assert again.ok and not again.message and again.result.get("skipped") == "already_found"
    assert _pack(hollow["player"])["Cracked Bone Whistle"].get("quantity", 1) == 1
    assert not any(line.startswith("@give") for line in runner.ledger())


# ---------------------------------------------------------------------------
# 8. Gear the story lost
# ---------------------------------------------------------------------------

BRAM_T19 = ("The wound makes a liar out of every careful step — your boot finds a loose shelf of rock instead of "
            "solid ground. You go down hard, shoulder first, the knife skittering off somewhere in the dark, and "
            "the breath that was already short tears all the way out of you on the landing.")
_KIT = {"inventory": [{"name": "Worn Knife", "type": "weapon", "slot": "weapon", "quantity": 1},
                      {"name": "Willow-Bark Draught", "type": "consumable", "quantity": 1}],
        "equipment": {"weapon": {"name": "Worn Knife"}}}


def test_a_knife_the_prose_lost_is_flagged():
    assert lost_gear(BRAM_T19, _KIT, _KIT, set()) == ["Worn Knife"]
    warnings = audit_turn(BRAM_T19, [], _KIT, _KIT)
    lost = [w for w in warnings if w["check"] == "lost_gear"]
    assert len(lost) == 1 and "@remove Worn Knife" in lost[0]["detail"]
    assert "in their hand for the next fight" in lost[0]["detail"]


@pytest.mark.parametrize("prose", [
    BRAM_T19 + " You grope in the bracken until your fingers find the knife again.",
    "The knife nearly skitters out of your grip, but you hold on.",
    "His knife skitters off across the stones as the wolf hits him.",
    "You wipe the knife clean on the bracken and sheathe it.",
    "Your blade flashes, and the wolf's fang snaps in two against the steel.",
])
def test_a_near_miss_someone_elses_or_a_recovered_knife_is_not(prose):
    assert lost_gear(prose, _KIT, _KIT, set()) == []


def test_a_written_remove_settles_it():
    after = {"inventory": [_KIT["inventory"][1]], "equipment": {}}
    assert lost_gear(BRAM_T19, _KIT, after, {"remove_from_inventory"}) == []


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,new", [("leaving", False), ("summons", False), ("watcher", True)])
def test_someone_the_player_already_knows_is_no_danger_brought_in(hollow, monkeypatch, kind, new):
    """The one packing up for the night is who they were talking with: "I lean
    in close" on them is theirs to roll, not a choice still to come."""
    pid = hollow["player"]
    with get_session() as session:
        session.get(NPC, hollow["sorrel"]).current_location_id = hollow["hollow"]
        session.commit()
    stakes.begin_turn(pid)
    complications._open[pid] = {"kind": kind, "entity_id": hollow["sorrel"], "entity_name": "Sorrel Tack",
                                "stub": None, "what": "packing up", "ignored": "gone", "replies": 0,
                                "where": hollow["hollow"]}
    assert encounters.introduced_now(pid) == ({hollow["sorrel"]} if new else set())
    _noon()
    note_turn_input(pid, "I lean in close: \"You saw who it was. Say it, or I'll tell the Captain.\"")
    _force(monkeypatch, 18)
    result = roll_check(pid, "persuasion", "tricky", reason="pressing Sorrel Tack to name the rider")
    assert ("error" in result) is new


def test_a_danger_shown_first_and_walked_on_from_comes_for_them(hollow):
    """The shown line says "if they ignore it, it comes for them": the reply
    after, walking on again is no answer — the hold that gave the choice used
    to let them walk off free (the move follows the roll)."""
    pid, place = hollow["player"], hollow["hollow"]
    assert _linger(pid, place, TAMSIN_T18)["shown_first"] is True
    struck = _linger(pid, place, "I ignore it and head back toward the gate, moving as fast as my knee allows.")
    assert struck and not struck.get("shown_first") and struck["names"] == ["Torn-Ear Wolf"]
    assert struck["chance"] == 1.0 and struck["why"].startswith("ignored")


@pytest.mark.parametrize("words", [
    # (a flight is a risky act: the roll-first line decides it, and a lost
    # roll provokes the lunge — encounters.note_check)
    "I turn and run for the gate.",
    # ...and a retreat with an eye on it answers it: the reply rolls it
    "I retreat slowly toward the gate, keeping my eyes on the wolf.",
])
def test_a_flight_or_a_watchful_retreat_from_the_danger_is_not_sprung(hollow, words):
    pid, place = hollow["player"], hollow["hollow"]
    assert _linger(pid, place, TAMSIN_T18)["shown_first"] is True
    assert _linger(pid, place, words) is None


def test_a_reply_a_danger_stops_on_the_way_out_gets_no_travel_line(monkeypatch):
    from src.agents import dm_orchestrator
    from src.services import travel_intent
    from tests.test_context_directives import _fake

    monkeypatch.setattr(travel_intent, "directive",
                        lambda pid, text: "[THIS REPLY: the player goes to North Gatehouse — @move North Gatehouse]")
    shown = encounters.directive({"names": ["Torn-Ear Wolf"], "npc_ids": ["w"], "shown_first": True})
    assert shown.startswith(encounters.SHOWN_FIRST_MARK)
    stopped = dm_orchestrator.DMOrchestrator._build_context(_fake(monkeypatch), TAMSIN_T18, extra_blocks=[shown])
    assert "@move North Gatehouse]" not in stopped and shown in stopped
    walking = dm_orchestrator.DMOrchestrator._build_context(_fake(monkeypatch), TAMSIN_T18)
    assert "the player goes to North Gatehouse" in walking


@pytest.mark.parametrize("words", [
    "I insist I'm fine and wave her off.",                            # no claim of a sender
    "I insist that I'm paying for the drinks tonight.",
    "\"They are expecting us at the gate by dusk,\" I tell Sorrel.",  # to a friend
    "\"Nobody's expecting us to come back,\" I mutter to Piper.",
    "\"Who is expecting us?\"",
    "I pat my belt. The purse feels light.",                          # a theft found out
    "\"How about five minutes to look around?\" I ask the guard.",    # no sum
    "I suspect his tale is not entirely true, so I watch him.",       # someone else's story
])
def test_no_bluff_or_haggle_in_plain_words(words):
    assert roll_intent.risky_act(words) is None


@pytest.mark.parametrize("words,family", [
    ("I tell him I served with the Watch, which is not exactly true.", "deception"),
    ("I insist the Captain sent me.", "deception"),
    ("\"The Captain is expecting me,\" I tell the guard.", "deception"),
    ("\"Your price is high, friend. How about fifteen?\"", "haggling"),
])
def test_a_half_truth_a_claimed_sender_or_a_counter_offer_still_rolls(words, family):
    assert roll_intent.risky_act(words)[0] == family


def test_a_quick_slot_draught_does_not_refuse_the_bread(hollow):
    """Only the thing the canvas consumed is "not used twice"."""
    pid = hollow["player"]
    with get_session() as session:
        player = session.get(Player, pid)
        player.inventory = list(player.inventory) + [{"id": "trail_bread", "name": "Trail Bread",
                                                     "type": "consumable", "effect_tag": "heal_light",
                                                     "quantity": 1}]
        session.commit()
    runner = _said(pid, "I eat the trail bread and catch my breath.")
    runner.code_actions = {"use_item", "update_player_health"}
    runner.canvas_used = ["They drank a Willow-Bark Draught, and look steadier (0 left)."]
    assert runner.run("@use Trail Bread").ok
    again = runner.run("@use Willow-Bark Draught")
    assert not again.ok and again.result["refused"] == "already_used"


@pytest.mark.parametrize("words", [
    "If it gets worse I'll drink the draught.",
    "Unless I find a healer, I'll take the potion. I limp on toward the hut.",
])
def test_a_drink_planned_for_later_is_no_reach(hollow, words):
    refused = _said(hollow["player"], words).run("@use Willow-Bark Draught")
    assert not refused.ok and refused.result["refused"] == "not_chosen"


@pytest.mark.parametrize("line", ["@give Bone Whistle | kind: clue", "@give the cracked bone whistle",
                                  "@give Cracked Whistle"])
def test_a_found_thing_given_again_by_another_name_is_not_doubled(hollow, line):
    runner = _said(hollow["player"], "I grab the whistle.")
    assert runner.run("@interact Cracked bone whistle").ok
    again = runner.run(line)
    assert again.ok and again.result.get("skipped") == "already_found"
    assert [n for n in _pack(hollow["player"]) if "histle" in n] == ["Cracked Bone Whistle"]


@pytest.mark.parametrize("prose", [
    "Your blade shatters the wolf's skull.",                          # the blade doing it
    "Your knife sinks into the wolf's flank.",
    "The knife snaps clean through the rope.",
    "You drop the whistle into your satchel.",                        # put away
    "The draught's warmth is gone from your belly now.",              # the thing's own warmth
    "The knife slips from your fingers and you catch it before it hits the ground.",
])
def test_a_blow_a_thing_put_away_or_caught_is_no_loss(prose):
    kit = {"inventory": _KIT["inventory"] + [{"name": "Cracked Bone Whistle", "type": "quest_item"}]}
    assert lost_gear(prose, kit, kit, set()) == []


def test_a_knife_lost_in_the_water_is_still_flagged():
    assert lost_gear("The knife sinks into the black water and is gone.", _KIT, _KIT, set()) == ["Worn Knife"]


def test_the_ground_is_no_wound_for_the_dms_disadvantage(hollow, monkeypatch):
    """"Over the broken palisade" is the DM's own cause: hurt and at a
    disadvantage for the footing, both stand (two dice)."""
    pid = hollow["player"]
    _noon()
    _force(monkeypatch, 15, 5)
    result = roll_check(pid, "athletics", "easy", modifier="disadvantage", reason="over the broken palisade")
    assert result["difficulty"] == "tricky" and result["outcome"] == "failure"


def _stream(hollow, monkeypatch, reply, player_input):
    """One /api/play turn with a scripted DM reply (tests/test_jobs_stream.py)."""
    import asyncio
    import json

    from src.services import memory_files
    from src.web import server
    from src.web.streaming import ToolUsageTracker
    from tests.test_jobs_stream import _FakeDM

    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    dm = _FakeDM([{"data": reply}], {})
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    server._pending_audit_feedback.pop(hollow["player"], None)
    server._pending_engine_notes.pop(hollow["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=player_input, player_id=hollow["player"]))
        return [chunk async for chunk in response.body_iterator]

    frames = []
    for chunk in asyncio.run(run()):
        for line in str(chunk).splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                frames.append(json.loads(line[6:]))
    return next(f for f in frames if f.get("type") == "complete")


def test_an_aftermath_wound_is_refused_and_not_pressed_next_turn(hollow, monkeypatch):
    """fleet10-tamsin T14 through /api/play: the worse @health is refused, and
    no audit asks the next reply to book the wound the fight never gave (the
    fight's own tools count as the engine's on an aftermath)."""
    pid = hollow["player"]
    _health(pid, "healthy")
    outcome = combat.combat_outcome_message(combat.apply_combat_result(pid, "victory", hits_taken=0))
    complete = _stream(hollow, monkeypatch,
                       "The wolf lies still. You're bleeding where its teeth caught your arm.\n"
                       "@health winded | cause: teeth before the blade found it\n", outcome)
    assert [(c["ok"], c["message"][:40]) for c in complete["commands"]] == [
        (False, "refused — the fight's wounds are already")]
    assert "damage_without_tool" not in {w["check"] for w in complete["audit"]}
    assert _health(pid) == "healthy"


def test_a_quick_slot_draught_is_named_to_the_reply(hollow, monkeypatch):
    """The canvas drank the draught: the reply's @use of it is refused, the
    bread the player eats now is not."""
    from src.web import pending_actions

    pid = hollow["player"]
    with get_session() as session:
        player = session.get(Player, pid)
        player.inventory = list(player.inventory) + [{"id": "trail_bread", "name": "Trail Bread",
                                                     "type": "consumable", "effect_tag": "heal_light",
                                                     "quantity": 1}]
        session.commit()
    pending_actions.record_action(pid, "consume", pending_actions.consume_note("Willow-Bark Draught", 0))
    complete = _stream(hollow, monkeypatch, "You chew the bread slowly.\n@use Trail Bread\n@use Willow-Bark Draught\n",
                       "I eat the trail bread and catch my breath.")
    assert [c["ok"] for c in complete["commands"]] == [True, False]
    assert complete["commands"][1]["message"].startswith("not used twice")
