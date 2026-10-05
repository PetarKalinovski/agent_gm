"""The command layer: `@verb …` lines the DM writes, applied by the engine.

Pins the grammar (segments, options, repeats, durations, signed numbers,
quantities), name resolution (present people first, ambiguity fails rather
than guesses), and real execution of the verbs against a temp SQLite world —
the same engine functions the old tools were, so the same rules hold.
"""

import re
import sys
import types
from pathlib import Path

import pytest

from src.models import (
    Connection, DMState, Faction, Location, LocationType, NPC, NPCRelationship, Player,
    Quest, QuestStatus, WorldClock, get_session,
)
from src.services import commands as cmds
from src.services.commands import COMMANDS, STATE_TOOLS, CommandRunner


# ----------------------------------------------------------------------
# A small world
# ----------------------------------------------------------------------

@pytest.fixture()
def world(db):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=17, minute=40))
        session.add(DMState(tension="low"))
        watch = Faction(name="Harbor Watch")
        session.add(watch)
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True, visited=True)
        tavern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, discovered=True)
        gate = Location(name="North Gate", type=LocationType.POI, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True)
        session.add_all([square, tavern, gate, hollow])
        session.flush()
        session.add_all([
            Connection(from_location_id=square.id, to_location_id=tavern.id, travel_type="walk",
                       travel_time_hours=0.1, bidirectional=True, discovered=True),
            Connection(from_location_id=square.id, to_location_id=gate.id, travel_type="walk",
                       travel_time_hours=0.2, bidirectional=True, discovered=True,
                       requirements=["a gate pass"]),
            Connection(from_location_id=square.id, to_location_id=hollow.id, travel_type="walk",
                       travel_time_hours=1.0, bidirectional=True, discovered=True),
        ])
        coldiron = NPC(name="Captain Brann Coldiron", profession="watch captain",
                       current_location_id=square.id, danger_tier="dangerous")
        piper = NPC(name="Piper Vell", profession="courier", current_location_id=square.id, danger_tier="weak")
        piper_rowe = NPC(name="Piper Rowe", profession="fishwife", current_location_id=gate.id)
        wolf = NPC(name="Hollow Wolf", current_location_id=square.id, danger_tier="standard",
                   description_physical="a lean grey wolf")
        guard_a = NPC(name="Gate Guard Alder", current_location_id=square.id)
        guard_b = NPC(name="Gate Guard Birch", current_location_id=square.id)
        sorrel = NPC(name="Sorrel Tack", profession="merchant", current_location_id=tavern.id,
                     secrets=["She fences stolen watch blades."])
        session.add_all([coldiron, piper, piper_rowe, wolf, guard_a, guard_b, sorrel])
        player = Player(name="Ilse Marr", current_location_id=square.id, currency=50,
                        position_x=50, position_y=50)
        session.add(player)
        session.commit()
        return {
            "player": player.id, "watch": watch.id,
            "square": square.id, "tavern": tavern.id, "gate": gate.id, "hollow": hollow.id,
            "coldiron": coldiron.id, "piper": piper.id, "piper_rowe": piper_rowe.id, "wolf": wolf.id,
            "guard_a": guard_a.id, "guard_b": guard_b.id, "sorrel": sorrel.id,
        }


def _runner(w, present=("coldiron", "piper", "wolf", "guard_a", "guard_b")):
    return CommandRunner(w["player"], present_ids=[w[k] for k in present])


def _player(w):
    with get_session() as session:
        p = session.get(Player, w["player"])
        return {"loc": p.current_location_id, "coin": p.currency, "inv": list(p.inventory or []),
                "active": list(p.active_quests or [])}


def _item(w, name):
    return next((i for i in _player(w)["inv"] if i.get("name", "").lower() == name.lower()), None)


# ----------------------------------------------------------------------
# Grammar
# ----------------------------------------------------------------------

def test_is_command_line():
    assert cmds.is_command_line("@give Rope")
    assert cmds.is_command_line("   @time 20m")
    assert cmds.is_command_line("`@coin -3`")
    assert not cmds.is_command_line("You meet her @ the docks.")
    assert not cmds.is_command_line("")


def test_parse_segments_options_repeats_and_extras():
    keys = frozenset({"about", "step", "reward"})
    p = cmds.parse_command(
        "@Quest_New The Wolf Problem | About: wolves at the gate | step: talk_to Coldiron "
        "| step: go_to Wolf's Hollow | reward: 30 coin | reward: Gate Pass | Panic: the crowd fled | urgent",
        keys,
    )
    assert p.verb == "quest-new"
    assert p.main == "The Wolf Problem"
    assert p.opt("about") == "wolves at the gate"
    assert p.opts("step") == ["talk_to Coldiron", "go_to Wolf's Hollow"]
    assert p.opts("reward") == ["30 coin", "Gate Pass"]
    # A key the verb does not know is prose, kept verbatim
    assert p.extras == ["Panic: the crowd fled", "urgent"]
    # Without a key list, every one-word key is an option
    loose = cmds.parse_command("@event Fire | Panic: the crowd fled")
    assert loose.opts("panic") == ["the crowd fled"] and loose.extras == []
    # Multi-word text before a colon is never a key
    assert cmds.parse_command("@event Fire | The gate fell: nobody saw", keys).extras == ["The gate fell: nobody saw"]
    assert cmds.parse_command("not a command") is None


def test_verbs_are_case_and_dash_insensitive(world):
    r = _runner(world)
    assert r.run("@QUEST_NEW Rats | step: custom Clear the cellar").verb == "quest-new"
    assert r.run("@Npc_Move Piper | The Drowned Lantern").ok


@pytest.mark.parametrize("text,minutes", [
    ("20m", 20), ("2h", 120), ("1h30m", 90), ("1h30", 90), ("90", 90), ("30 minutes", 30),
    ("an hour", 60), ("half an hour", 30), ("2 hours and 15 minutes", 135), ("1.5h", 90),
    ("forty-five minutes", 45), ("a few minutes", 3), ("2d", 2880),
])
def test_durations(text, minutes):
    assert cmds.parse_duration(text) == minutes


@pytest.mark.parametrize("text", ["", "soon", "a while", "3 fortnights"])
def test_bad_durations(text):
    assert cmds.parse_duration(text) is None


def test_signed_numbers_and_quantities():
    assert cmds.parse_signed("+30") == 30
    assert cmds.parse_signed("-12 coin") == -12
    assert cmds.parse_signed("−5") == -5            # a typographic minus
    assert cmds.parse_signed("30") == 30
    assert cmds.parse_signed("nothing") is None
    assert cmds.split_trailing_number("Harbor Watch -10") == ("Harbor Watch", -10)
    assert cmds.split_trailing_number("Piper Vell +15") == ("Piper Vell", 15)
    assert cmds.split_trailing_number("Coldiron [1]") == ("Coldiron", 1)
    assert cmds.split_trailing_number("The 3rd Legion +5") == ("The 3rd Legion", 5)
    assert cmds.split_trailing_number("Piper Vell") == ("Piper Vell", None)
    assert cmds.split_quantity("Torch x3") == ("Torch", 3)
    assert cmds.split_quantity("Torch (x3)") == ("Torch", 3)
    assert cmds.split_quantity("Arrows ×12") == ("Arrows", 12)
    assert cmds.split_quantity("2x Rope") == ("Rope", 2)
    assert cmds.split_quantity("Box") == ("Box", 1)


def test_a_bracketed_id_is_never_read_as_a_number():
    """Ids from the brief end in a digit most of the time; that digit is not a delta."""
    uid = "3f2a1b4c-9d8e-4f7a-b6c5-0123456789a7"
    assert cmds.split_trailing_number(f"Coldiron [{uid}]") == (f"Coldiron [{uid}]", None)
    assert cmds.split_trailing_number(f"Coldiron [{uid}] +5") == (f"Coldiron [{uid}]", 5)
    assert cmds.split_trailing_number(f"Coldiron [{uid}] 1") == (f"Coldiron [{uid}]", 1)
    assert cmds.split_trailing_number("Piper Vell+5") == ("Piper Vell", 5)
    assert cmds.split_trailing_number("Harbor Watch, -10") == ("Harbor Watch", -10)
    assert cmds.split_trailing_number("#3") == ("#3", None)
    bare = "a1b2c3d4-e5f6-4a7b-8c9d-123456789012"     # an unbracketed id, all-digit tail
    assert cmds.split_trailing_number(bare) == (bare, None)


def test_thousands_and_negative_durations():
    assert cmds.parse_signed("+1,000") == 1000
    assert cmds.parse_signed("-2,500 coin") == -2500
    assert cmds.parse_duration("-20m") is None
    assert cmds.parse_duration("−1h") is None


def test_bulleted_command_lines_are_commands():
    assert cmds.is_command_line("- @give Rope")
    assert cmds.is_command_line("* `@coin -3`")
    assert not cmds.is_command_line("*@give* is not a list bullet")
    assert cmds.parse_command("- @give Rope | kind: misc").main == "Rope"


@pytest.mark.parametrize("text,expected", [
    ("in 3h", (1, 20)), ("in 2d", (3, 17)), ("3h", (1, 20)), ("day 3 20:00", (3, 20)),
    ("tomorrow 08:00", (2, 8)), ("tomorrow", (2, 8)), ("tonight", (1, 21)), ("dawn", (2, 6)),
    ("20:00", (1, 20)), ("8pm", (1, 20)), ("noon", (2, 12)), ("day 1 09:00", None), ("whenever", None),
])
def test_schedule_times(text, expected):
    assert cmds.parse_when(text, 1, 17, 40) == expected


def test_a_short_delay_never_lands_on_an_hour_already_past():
    # 8:10 + 30m is 8:40; the 8:00 slot has gone, so it fires at 9:00
    assert cmds.parse_when("in 30m", 1, 8, 10) == (1, 9)
    assert cmds.parse_when("in 30m", 1, 23, 50) == (2, 0)


# ----------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------

def test_npc_resolution_present_first_then_world(world):
    r = _runner(world)
    # Two Pipers in the world; the one standing here wins
    assert r.npc("Piper") == (world["piper"], "Piper Vell")
    assert r.npc("Coldiron")[0] == world["coldiron"]           # surname
    assert r.npc("Captain Coldiron")[0] == world["coldiron"]   # title + surname
    assert r.npc("brann_coldiron")[0] == world["coldiron"]     # a slug
    assert r.npc("Captain Brann Coldiron of the Watch")[0] == world["coldiron"]  # a longer label
    assert r.npc("Coldirn")[0] == world["coldiron"]            # a typo
    assert r.npc("Rowe")[0] == world["piper_rowe"]             # not here, but unique
    assert r.npc(world["sorrel"])[0] == world["sorrel"]        # an id
    assert r.npc(f"Sorrel [{world['sorrel']}]")[0] == world["sorrel"]
    assert r.npc(world["sorrel"][:8])[0] == world["sorrel"]    # an id prefix


def test_ambiguous_or_missing_names_fail(world):
    r = _runner(world)
    with pytest.raises(cmds.CommandError, match="could be"):
        r.npc("Gate Guard")
    with pytest.raises(cmds.CommandError, match="no person called"):
        r.npc("Nobody Atall")
    result = r.run("@fight Gate Guard")
    assert not result.ok and "could be" in result.message and result.tool == "start_combat"


def test_with_nobody_present_two_namesakes_are_ambiguous(world):
    with get_session() as session:
        session.get(Player, world["player"]).current_location_id = world["hollow"]
        session.commit()
    with pytest.raises(cmds.CommandError, match="could be"):
        CommandRunner(world["player"]).npc("Piper")


def test_place_resolution_prefers_the_ways_out(world):
    r = _runner(world)
    assert r.place("the drowned lantern")[0] == world["tavern"]
    assert r.place("Wolfs Hollow")[0] == world["hollow"]
    assert r.place("gate")[0] == world["gate"]
    with pytest.raises(cmds.CommandError, match="no place called"):
        r.place("Atlantis")


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------

def test_unknown_verb_and_garbage_never_raise(world):
    r = _runner(world)
    res = r.run("@dance wildly")
    assert not res.ok and res.tool is None
    assert res.message.startswith("unknown command @dance (known: @move")
    assert not r.run("").ok
    assert not r.run("just prose").ok
    assert not r.run(None).ok


@pytest.mark.parametrize("line,fragment", [
    ("@time soon", "duration"),
    ("@health sleepy", "health is one of"),
    ("@give | kind: key", "name the item"),
    ("@give Heirloom | tier: fine", "a tier needs kind"),
    ("@give Sword | kind: polearm", "kind is one of"),
    ("@tension extreme", "tension is one of"),
    ("@weather hail", "weather is one of"),
    ("@coin lots", "+N or -N"),
    ("@trust Piper", "+N or -N"),
    ("@hand Rope", "say who gets it"),
    ("@remove Moonstone", "no item in their pack"),
    ("@take Moonstone", "@take is ambiguous"),
    ("@quest-new Nothing To Do | about: idle", "at least one step"),
    ("@npc Piper", "nothing to change"),
    ("@schedule someday | Raid | The raiders come", "future time"),
])
def test_bad_arguments_fail_with_a_reason(world, line, fragment):
    res = _runner(world).run(line)
    assert not res.ok
    assert fragment in res.message


def test_give_and_take(world):
    r = _runner(world)
    assert r.run("@give Torch x3 | about: Pitch-soaked rags on a stick.").ok
    assert _item(world, "Torch")["quantity"] == 3
    assert r.run("@remove torch x2").ok
    assert _item(world, "Torch")["quantity"] == 1
    res = r.run("@remove Torch x5")
    assert res.ok and "only had 1" in res.message
    assert _item(world, "Torch") is None

    assert r.run("@give Ashwood Blade | kind: weapon | tier: fine | looks: a pale ash-handled sword").ok
    blade = _item(world, "Ashwood Blade")
    assert blade["slot"] == "weapon" and blade["tier"] == "fine" and blade["visual_desc"].startswith("a pale")

    assert r.run("@give Healing Draught x2 | potion: heal_strong").ok
    draught = _item(world, "Healing Draught")
    assert draught["type"] == "consumable" and draught["effect_tag"] == "heal_strong" and draught["quantity"] == 2

    assert r.run("@give Watch Tabard | style: common | uniform: Harbor Watch | hood").ok
    tabard = _item(world, "Watch Tabard")
    assert tabard["slot"] == "armor"
    assert tabard["attire"] == {"style": "common", "uniform_faction_id": world["watch"], "concealing": True}

    assert r.run("@give Gate Pass | kind: key").ok
    assert _item(world, "Gate Pass")["type"] == "quest_item"


def test_coin(world):
    r = _runner(world)
    assert r.run("@coin +30 | the captain's advance").ok
    assert _player(world)["coin"] == 80
    assert r.run("@coin -12").ok
    assert _player(world)["coin"] == 68
    res = r.run("@coin -500")
    assert not res.ok and "only 68" in res.message
    assert _player(world)["coin"] == 68          # nothing was taken


def test_move_blocked_then_through(world):
    r = _runner(world)
    res = r.run("@move North Gate")
    assert not res.ok and res.tool == "move_player"
    assert res.result.get("blocked") and "| through" in res.message
    assert _player(world)["loc"] == world["square"]

    res = r.run("@move North Gate | through")
    assert res.ok and res.result["success"]
    assert _player(world)["loc"] == world["gate"]
    # The people at the gate are the present ones now: Piper means Piper Rowe
    assert r.npc("Piper")[0] == world["piper_rowe"]

    # Moving where they already are moves nothing, and says so as a failure
    # (fleet6-tamsin T16: a silent ✓ read as a move that never happened)
    res = r.run("@move North Gate")
    assert not res.ok and res.result.get("already_here") and "@npc-move" in res.message


def _meet(world, *keys):
    with get_session() as session:
        for key in keys:
            session.add(NPCRelationship(npc_id=world[key], player_id=world["player"],
                                        trust_level=50, current_disposition="neutral"))
        session.commit()


def test_trust_mood_and_reveal(world):
    _meet(world, "piper", "sorrel")
    r = _runner(world)
    assert r.run("@trust Piper +20 | friendly").ok
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=world["piper"], player_id=world["player"]).one()
        assert rel.trust_level == 70 and rel.current_disposition == "friendly"
    assert r.run("@mood Piper wary").ok
    assert r.run("@mood Coldiron | grim, tired").ok
    with get_session() as session:
        assert session.get(NPC, world["piper"]).current_mood == "wary"
        assert session.get(NPC, world["coldiron"]).current_mood == "grim, tired"

    res = r.run("@reveal Sorrel 0")
    assert not res.ok and "does not trust them" in res.message
    assert r.run("@trust Sorrel +45").ok
    res = r.run("@reveal Sorrel [0]")
    assert res.ok and res.result["secret"].startswith("She fences")


def test_a_strangers_trust_lands_and_their_name_stays_unknown(world):
    """fleet6-bram T14 / fleet6-tamsin T4: `@trust Hesk Bramble -5` and
    `@trust Sorrel Tack -5` were held for an introduction that never came,
    and the failed roll's cost was lost. The row is the relationship, not an
    introduction (fleet-wenna T17's leak was the row meaning both): the
    change lands now, on a row whose name the player still has not heard."""
    from src.services.dialogue import player_has_met

    r = _runner(world)
    res = r.run("@trust Sorrel -5 | wary")
    assert res.ok and not r.held_for_intro
    assert all("stranger" not in line for line in r.ledger())
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=world["sorrel"], player_id=world["player"]).one()
        assert rel.trust_level == 45 and rel.current_disposition == "wary"
        assert rel.name_known is False and rel.knows_player_name is False
        assert not player_has_met(session, world["player"], world["sorrel"])

    # An old acquaintance's row (from before the split) keeps both names
    _meet(world, "piper")
    assert r.run("@trust Piper +10").ok
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=world["piper"], player_id=world["player"]).one()
        assert rel.trust_level == 60 and rel.name_known is None
        assert player_has_met(session, world["player"], world["piper"])


def test_quest_new_then_done(world):
    r = _runner(world)
    res = r.run(
        "@quest-new The Wolf Problem | about: Wolves are taking travellers on the north road. "
        "| from: Coldiron | scale: job | step: talk_to Coldiron | step: go_to Wolf's Hollow "
        "| step: kill Hollow Wolf | step: deliver Wolf Pelt to Captain Coldiron | step: custom Warn the ferryman "
        "| reward: 30 coin | reward: Wolfhide Cloak"
    )
    assert res.ok, res.message
    assert res.tool == "create_quest" and res.result["event"] == "quest_activated"
    quest_id = res.result["id"]
    objectives = res.result["objectives"]
    assert [o["kind"] for o in objectives] == ["talk_to", "go_to", "kill", "deliver", "custom"]
    assert objectives[0]["text"] == "Talk to Captain Brann Coldiron"
    assert objectives[0]["target_id"] == world["coldiron"]
    assert objectives[1]["target_id"] == world["hollow"] and objectives[1]["text"] == "Go to Wolf's Hollow"
    assert objectives[3]["item_name"] == "Wolf Pelt" and objectives[3]["target_id"] == world["coldiron"]
    with get_session() as session:
        quest = session.get(Quest, quest_id)
        assert quest.status == QuestStatus.ACTIVE and quest.scale == "job"
        assert quest.assigned_by_npc_id == world["coldiron"]
        assert quest.rewards == {"currency": 30, "items": ["Wolfhide Cloak"]}
    assert quest_id in _player(world)["active"]

    # The same quest again is not a copy
    again = r.run("@quest-new Wolf Problem | step: custom whatever")
    assert again.ok and "already active" in again.message

    # A custom step is the DM's to tick
    step = r.run("@step-done Wolf Problem | warn the ferryman")
    assert step.ok and step.tool == "update_quest_objectives"
    with get_session() as session:
        done = {o["text"]: o["done"] for o in session.get(Quest, quest_id).objectives}
        assert done["Warn the ferryman"] is True and done["Talk to Captain Brann Coldiron"] is False

    # Never at the Hollow, never held a pelt: told is not done (fleet7-tamsin T14)
    early = r.run("@quest-done the wolf problem")
    assert not early.ok and "nothing was paid" in early.message
    assert "never stood at Wolf's Hollow" in early.message and "never held the Wolf Pelt" in early.message
    assert _player(world)["coin"] == 50
    # ...the work, done in play, and it pays
    with get_session() as session:
        session.get(Location, world["hollow"]).visited = True
        session.get(Player, world["player"]).inventory = [{"id": "wolf_pelt", "name": "Wolf Pelt", "type": "misc"}]
        session.commit()
    res = _runner(world).run("@quest-done the wolf problem")
    assert res.ok and res.result["event"] == "quest_completed"
    assert _player(world)["coin"] == 80
    assert _item(world, "Wolfhide Cloak") is not None
    assert not r.run("@quest-fail Wolf Problem").ok      # finished is finished


def test_trust_and_reveal_by_bracketed_id_that_ends_in_a_digit(db):
    """`@trust X [id] | friendly` must not read the id's last digit as +N."""
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True)
        session.add(square)
        session.flush()
        npc = NPC(id="3f2a1b4c-9d8e-4f7a-b6c5-012345678907", name="Sorrel Tack",
                  current_location_id=square.id, secrets=["She fences stolen blades."])
        player = Player(name="Ilse", current_location_id=square.id)
        session.add_all([npc, player])
        session.commit()
        npc_id, player_id = npc.id, player.id
    with get_session() as session:
        session.add(NPCRelationship(npc_id=npc_id, player_id=player_id, trust_level=50))
        session.commit()
    r = CommandRunner(player_id, present_ids=[npc_id])
    assert r.run(f"@trust Sorrel [{npc_id}] | friendly").ok
    with get_session() as session:
        rel = session.query(NPCRelationship).filter_by(npc_id=npc_id, player_id=player_id).one()
        assert rel.trust_level == 50 and rel.current_disposition == "friendly"
    # No index: the first untold secret, not a secret "[7]"
    res = r.run(f"@reveal Sorrel [{npc_id}]")
    assert not res.ok and "does not trust them" in res.message


def test_reveal_on_someone_with_no_secrets_is_a_clear_refusal(world):
    """Coldiron's secrets column is NULL; the engine would raise on len(None)."""
    res = _runner(world).run("@reveal Coldiron 0")
    assert not res.ok and res.message == "Captain Brann Coldiron has no secrets to tell"


def test_time_never_runs_backwards_and_coin_reads_thousands(world):
    r = _runner(world)
    res = r.run("@time -20m")
    assert not res.ok and "duration" in res.message
    with get_session() as session:
        clock = session.query(WorldClock).first()
        assert (clock.hour, clock.minute) == (17, 40)
    assert r.run("@coin +1,000").ok
    assert _player(world)["coin"] == 1050


def test_quest_rewards_coin_words_and_no_authored_xp(world):
    r = _runner(world)
    res = r.run("@quest-new Pelts | about: Bring pelts. | step: obtain Wolf Pelt "
                "| reward: 30 gold coins | reward: 50 xp | reward: Wolfhide Cloak")
    assert res.ok and "xp reward was dropped" in res.message
    with get_session() as session:
        assert session.get(Quest, res.result["id"]).rewards == {"currency": 30, "items": ["Wolfhide Cloak"]}


def test_quests_belong_to_this_player_in_a_shared_world(world):
    """The quest tools only manage "the" player when the world has one; a
    second character must still get the quest listed and its rewards paid."""
    with get_session() as session:
        other = Player(name="Other Marr", current_location_id=world["square"], currency=7)
        session.add(other)
        session.commit()
        other_id = other.id
    r = _runner(world)
    res = r.run("@quest-new Rats | about: Rats in the cellar. | scale: errand "
                "| step: custom Clear the cellar | reward: 20 coin")
    assert res.ok, res.message
    quest_id = res.result["id"]
    assert quest_id in _player(world)["active"]

    done = r.run("@quest-done Rats")
    assert done.ok and done.result["event"] == "quest_completed"
    assert done.result["progression"].get("xp_gained")
    assert _player(world)["coin"] == 70
    with get_session() as session:
        me = session.get(Player, world["player"])
        assert quest_id in (me.completed_quests or []) and quest_id not in (me.active_quests or [])
        other = session.get(Player, other_id)
        assert other.currency == 7 and not other.active_quests and not other.completed_quests
        assert session.get(Quest, quest_id).status == QuestStatus.COMPLETED


def test_step_done_ticks_only_the_named_quest(world):
    """Two quests share a step's words; naming one quest ticks that one."""
    r = _runner(world)
    assert r.run("@quest-new Rats | about: x | step: custom Report to the innkeeper").ok
    assert r.run("@quest-new Ghosts | about: y | step: custom Report to the innkeeper").ok
    res = r.run("@step-done Ghosts | report to the innkeeper")
    assert res.ok and res.result["objectives_done"]
    with get_session() as session:
        by_title = {q.title: q.objectives[0]["done"] for q in session.query(Quest).all()}
    assert by_title == {"Rats": False, "Ghosts": True}


def test_bulleted_command_runs(world):
    r = _runner(world)
    assert r.run("- @give Rope").ok
    assert _item(world, "Rope") is not None


def test_quest_new_with_unknown_target_creates_nothing(world):
    res = _runner(world).run("@quest-new Find Ghost | about: x | step: talk_to Nobody Atall")
    assert not res.ok and "no person called" in res.message
    with get_session() as session:
        assert session.query(Quest).count() == 0


def test_quest_start_an_authored_quest(world):
    with get_session() as session:
        q = Quest(title="The Missing Courier", description="Find Piper's brother.",
                  status=QuestStatus.NOT_STARTED, objectives=["Ask around the docks"])
        session.add(q)
        session.commit()
        quest_id = q.id
    r = _runner(world)
    res = r.run("@quest-start Missing Courier")
    assert res.ok and res.result["event"] == "quest_activated"
    assert r.run("@quest-start Missing Courier").ok        # already active: nothing to do
    with get_session() as session:
        assert session.get(Quest, quest_id).status == QuestStatus.ACTIVE


def test_fight_starts_combat(world):
    res = _runner(world).run("@fight Hollow Wolf")
    assert res.ok and res.tool == "start_combat"
    assert res.result["event"] == "combat_start"
    assert [e["npc_id"] for e in res.result["enemies"]] == [world["wolf"]]
    assert "start_combat" in STATE_TOOLS


def test_fight_several_and_one_not_here(world):
    r = _runner(world)
    res = r.run("@fight Hollow Wolf, Coldiron")
    assert res.ok and len(res.result["enemies"]) == 2
    res = r.run("@fight Sorrel")
    assert not res.ok and "not at the player's location" in res.message


def test_new_place_then_move_there_in_the_same_reply(world):
    r = _runner(world)
    res = r.run("@new-place Cellar | about: Damp stone, barrels, a rat's nest. | kind: room")
    assert res.ok and res.result.get("created") and res.message == ""
    res = r.run("@move Cellar")
    assert res.ok, res.message
    with get_session() as session:
        here = session.get(Location, _player(world)["loc"])
        assert here.name == "Cellar"

    dup = r.run("@new-place Market Square | about: again")
    assert dup.ok and "already existed" in dup.message


def test_new_npc_then_fight_them(world):
    r = _runner(world)
    res = r.run("@new-npc Grizzled Smuggler | trade: smuggler | looks: salt-crusted coat | "
                "manner: twitchy | voice: clipped | wants: get off this island | tier: weak | fights: skirmisher")
    assert res.ok and res.result.get("created") and res.message == ""
    with get_session() as session:
        npc = session.get(NPC, res.result["id"])
        assert npc.danger_tier == "weak" and npc.combat_archetype == "skirmisher"
        assert npc.goals == ["get off this island"]
    assert r.run("@fight Smuggler").ok

    thin = r.run("@new-npc Old Ned")
    assert thin.ok and "made without" in thin.message
    dup = r.run("@new-npc Piper Vell | trade: courier")
    assert dup.ok and "already existed" in dup.message


def test_join_refused_then_accepted(world):
    _meet(world, "piper")
    r = _runner(world)
    res = r.run("@join Piper")
    assert not res.ok and res.message.startswith("refused —")
    assert r.run("@trust Piper +40 | warm").ok
    res = r.run("@join Piper")
    assert res.ok and res.result["party"] == ["Piper Vell"]
    assert r.run("@join Piper").ok          # already with them — nothing to play
    assert r.run("@leave Piper | went home").ok


def test_stock_then_buy(world):
    r = _runner(world)
    res = r.run("@stock Sorrel | Ashwood Blade: weapon common | Tonic: potion heal_light x3")
    assert res.ok, res.message
    res = r.run("@buy Tonic | from: Sorrel")
    assert res.ok, res.message
    assert _item(world, "Tonic")["effect_tag"] == "heal_light"
    assert _player(world)["coin"] < 50
    poor = r.run("@buy Ashwood Blade x5 from Sorrel")
    assert not poor.ok and "no sale" in poor.message
    bad = r.run("@stock Sorrel | Moon Lance: weapon legendary")
    assert not bad.ok and "Legendary" in bad.message


def test_health_time_tension_kill(world):
    r = _runner(world)
    assert r.run("@health badly hurt | cause: a wolf's bite").ok
    assert r.run("@time 1h30m").ok
    with get_session() as session:
        assert session.get(Player, world["player"]).health_status == "badly_hurt"
        clock = session.query(WorldClock).first()
        assert (clock.day, clock.hour, clock.minute) == (1, 19, 10)
    assert r.run("@tension high").ok
    res = r.run("@kill Hollow Wolf | an arrow from the dark")
    assert res.ok and res.result["event"] == "npc_death"
    with get_session() as session:
        assert session.get(NPC, world["wolf"]).status == "dead"


def test_weather_rejection_is_silent(world):
    r = _runner(world)
    first = r.run("@weather fog")
    assert first.ok and first.result["event"] == "cinematic"
    again = r.run("@weather fog")             # already foggy: the engine refuses
    assert not again.ok and again.message == ""
    assert not any("weather" in line for line in r.ledger())


def test_event_and_schedule(world):
    r = _runner(world)
    res = r.run("@event Brawl at the Lantern | Panic: chairs flew | witnessed | npcs: Coldiron, Piper")
    assert res.ok
    from src.models import Event
    with get_session() as session:
        event = session.get(Event, res.result["id"])
        assert event.description == "Panic: chairs flew" and event.event_type == "player"
        assert event.player_witnessed and set(event.npcs_involved) == {world["coldiron"], world["piper"]}
        assert event.locations_involved == [world["square"]]
    res = r.run("@schedule tomorrow 08:00 | Wolf Raid | The pack hits the north farms.")
    assert res.ok and (res.result["scheduled_day"], res.result["scheduled_hour"]) == (2, 8)


def test_note_and_plan_go_to_memory(world, monkeypatch):
    calls = []
    fake = types.ModuleType("src.services.memory_files")
    fake.add_npc_note = lambda player_id, npc_id, text: calls.append(("npc", player_id, npc_id, text))
    fake.add_dm_note = lambda player_id, text: calls.append(("dm", player_id, text))
    monkeypatch.setitem(sys.modules, "src.services.memory_files", fake)
    r = _runner(world)
    assert r.run("@note Coldiron | She lied about the letter: it was never sealed.").ok
    assert r.run("@plan Sorrel's fence ties back to the missing watch blades | pay off in act 2").ok
    assert calls == [
        ("npc", world["player"], world["coldiron"], "She lied about the letter: it was never sealed."),
        ("dm", world["player"], "Sorrel's fence ties back to the missing watch blades | pay off in act 2"),
    ]


def test_ledger_reports_failures_and_notable_successes(world):
    r = _runner(world)
    r.run("@coin +5")                                  # ordinary: silent
    r.run("@new-place Market Square | about: dup")     # notable
    r.run("@move Atlantis")                            # failed
    lines = r.ledger()
    assert len(lines) == 2
    assert lines[0].startswith("@new-place Market Square | about: dup: ") and "already existed" in lines[0]
    assert lines[1].startswith("@move Atlantis: FAILED — no place called 'Atlantis'")
    assert [res.ok for res in r.results] == [True, True, False]


# ----------------------------------------------------------------------
# The DM's reference documents every verb
# ----------------------------------------------------------------------

def test_commands_md_documents_every_verb():
    doc = (Path(__file__).resolve().parent.parent / "src" / "agents" / "prompts" / "commands.md").read_text(encoding="utf-8")
    missing = [verb for verb in COMMANDS if not re.search(rf"@{verb}(?![\w-])", doc)]
    assert not missing, f"commands.md does not document: {missing}"
    assert len(doc.splitlines()) <= 190
