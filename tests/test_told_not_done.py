"""Work only told is not work done (round 6).

fleet7-tamsin T9-T14: the trip to Wolf's Hollow was prose (the DB had her at
the North Gatehouse), `@give Cracked Bone Whistle` was refused, `@hand` of it
failed — yet `@quest-done Teeth on the North Road` paid 60 coin, the charm
and 220 XP, and the prose carried a whistle she never held for four turns.
fleet7-tamsin T11: the DM pocketed loot the player never reached for.

- @quest-done refuses while a place step was never stood at or an item
  step's thing was never held (`objectives.undone_in_play`).
- @interact take/search needs the player's own reach (`commands._reached_for`).
- refused_but_narrated: a refused line the prose told as done, named once.
- carried_not_held: an authored clue carried in the prose, still lying out.
"""

import pytest

from src.models import Location, LocationType, NPC, Player, Quest, QuestStatus, WorldClock, get_session
from src.services.commands import CommandRunner
from src.services.scene_objects import normalize_objects
from src.web.turn_audit import carried_not_held, merge_refused, refused_but_narrated

WHISTLE = {"kind": "take", "label": "Cracked bone whistle", "x": 60, "y": 74,
           "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                                "description": "Split down the barrel — silent now."}}}
CRATE = {"kind": "search", "label": "Split supply crate", "x": 80, "y": 56,
         "payload": {"item": {"name": "Greywater Silver Piece", "type": "misc", "description": "Freshly minted."}}}
BUNDLE = {"kind": "take", "label": "Dropped sweetroot bundle", "x": 34, "y": 66,
          "payload": {"item": {"name": "Sweetroot Bundle", "type": "consumable", "description": "Steadies hands."}}}


@pytest.fixture()
def world(db):
    with get_session() as session:
        session.add(WorldClock(day=1, hour=21, minute=0))
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True, visited=True,
                          objects=normalize_objects([BUNDLE]))
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True,
                          objects=normalize_objects([WHISTLE, CRATE]))
        session.add_all([square, hollow])
        session.flush()
        captain = NPC(name="Captain Brann Coldiron", profession="Watch captain", current_location_id=square.id)
        session.add(captain)
        session.flush()
        player = Player(name="Tamsin Rook", current_location_id=square.id, currency=25,
                        inventory=[{"id": "gate_pass", "name": "Gate Pass", "type": "quest_item"}])
        session.add(player)
        session.flush()
        quest = Quest(
            title="Teeth on the North Road", status=QuestStatus.ACTIVE, assigned_by_npc_id=captain.id,
            objectives=[
                {"text": "Get the gate pass", "kind": "obtain", "item_name": "Gate Pass", "done": True},
                {"text": "Reach Wolf's Hollow", "kind": "go_to", "target_id": hollow.id},
                {"text": "Find out what is organizing the wolves", "kind": "custom"},
                {"text": "Bring the cracked bone whistle back to Captain Coldiron as proof",
                 "kind": "deliver", "item_name": "Cracked Bone Whistle", "target_id": captain.id},
            ],
            rewards={"currency": 60},
        )
        session.add(quest)
        session.commit()
        return {"player": player.id, "square": square.id, "hollow": hollow.id, "captain": captain.id,
                "quest": quest.id}


def _runner(w, said=None):
    runner = CommandRunner(w["player"], present_ids=[w["captain"]])
    runner.player_input = said
    return runner


def _coin(w):
    with get_session() as session:
        return session.get(Player, w["player"]).currency


# --- @quest-done: told is not done --------------------------------------------

def test_quest_done_refuses_a_trip_and_a_proof_that_only_happened_in_prose(world):
    """fleet7-tamsin T14: paid in full with 'Reach Wolf's Hollow' and the whistle undone."""
    done = _runner(world).run("@quest-done Teeth on the North Road")
    assert not done.ok and done.result["refused"] == "work_not_done"
    assert "nothing was paid" in done.message
    assert "'Reach Wolf's Hollow' — the player has never stood at Wolf's Hollow" in done.message
    assert ("they have never held the Cracked Bone Whistle (it still lies at Wolf's Hollow (Cracked bone "
            "whistle))") in done.message
    # No ruling offered for a trip never made and a proof still lying out
    assert "@step-done Teeth on the North Road | <step>" not in done.message
    assert "@step-done will not mark a place they never stood at" in done.message
    assert _coin(world) == 25
    with get_session() as session:
        assert session.get(Quest, world["quest"]).status == QuestStatus.ACTIVE


def test_the_work_done_in_play_pays(world):
    r = _runner(world)
    assert r.run("@move Wolf's Hollow").ok
    assert r.run("@interact Cracked bone whistle").ok
    assert r.run("@move Market Square").ok
    assert r.run("@hand Cracked Bone Whistle | to: Captain Brann Coldiron").ok
    done = r.run("@quest-done Teeth on the North Road")
    assert done.ok, done.message
    assert _coin(world) == 85


def test_the_proof_already_in_the_givers_hands_or_in_the_pack_counts(world):
    with get_session() as session:
        session.get(Location, world["hollow"]).visited = True
        captain = session.get(NPC, world["captain"])
        captain.inventory_notable = [{"name": "Cracked Bone Whistle"}]
        session.commit()
    assert _runner(world).run("@quest-done Teeth on the North Road").ok


def test_the_same_thing_by_another_name_is_held(db):
    """fleet-bram T13 carried the "North Road Pass" the DM gave for the step's "Gate Pass"."""
    with get_session() as session:
        player = Player(name="Bram", currency=0,
                        inventory=[{"id": "north_road_pass", "name": "North Road Pass", "type": "quest_item"}])
        session.add(player)
        session.flush()
        session.add(Quest(title="Pass Job", status=QuestStatus.ACTIVE,
                          objectives=[{"text": "Get the pass", "kind": "obtain", "item_name": "Gate Pass"}],
                          rewards={"currency": 5}))
        session.commit()
        player_id = player.id
    assert CommandRunner(player_id).run("@quest-done Pass Job").ok


def test_custom_and_talk_steps_are_not_the_guards(db):
    with get_session() as session:
        player = Player(name="Wenna", currency=0)
        session.add(player)
        session.flush()
        session.add(Quest(title="Quiet Job", status=QuestStatus.ACTIVE,
                          objectives=[{"text": "Learn who he was", "kind": "custom"}], rewards={"currency": 5}))
        session.commit()
        player_id = player.id
    assert CommandRunner(player_id).run("@quest-done Quiet Job").ok


def test_step_done_is_no_way_round_the_guard(world):
    """The refused @quest-done used to offer @step-done for any step: ticking
    "Reach Wolf's Hollow" and the whistle by ruling would have paid the same
    prose trip (fleet7-tamsin T14). Refused while play has not done them;
    once it has, the engine's own tick lands as ever."""
    r = _runner(world)
    trip = r.run("@step-done Teeth on the North Road | Reach Wolf's Hollow")
    assert not trip.ok and trip.result["refused"] == "work_not_done"
    assert "the player has never stood at Wolf's Hollow" in trip.message
    proof = r.run("@step-done Teeth on the North Road | Bring the cracked bone whistle back to Captain Coldiron "
                  "as proof")
    assert not proof.ok and "it still lies at Wolf's Hollow" in proof.message
    assert r.run("@step-done Teeth on the North Road | Find out what is organizing the wolves").ok   # custom
    assert not r.run("@quest-done Teeth on the North Road").ok
    with get_session() as session:
        done = {o["text"]: o.get("done") for o in session.get(Quest, world["quest"]).objectives}
    assert done["Reach Wolf's Hollow"] is not True


def test_a_thing_that_exists_nowhere_is_the_dms_to_rule(db):
    """An item no Thing holds and nobody has (a DM-made job's ledger the story
    burned): the refusal offers the ruling, and the ruling lands."""
    with get_session() as session:
        player = Player(name="Wenna", currency=0)
        session.add(player)
        session.flush()
        session.add(Quest(title="Ledger Job", status=QuestStatus.ACTIVE,
                          objectives=[{"text": "Recover the ledger", "kind": "obtain", "item_name": "Tally Ledger"}],
                          rewards={"currency": 5}))
        session.commit()
        player_id = player.id
    r = CommandRunner(player_id)
    first = r.run("@quest-done Ledger Job")
    assert not first.ok and "@step-done Ledger Job | <step>" in first.message
    assert r.run("@step-done Ledger Job | Recover the ledger").ok
    assert r.run("@quest-done Ledger Job").ok


# --- @interact: the player's own reach ----------------------------------------

def test_a_take_the_player_never_reached_for_is_refused(world):
    """fleet7-tamsin T11: a walk to Piper and a question; the reply pocketed sweetroot."""
    said = ("I move through the crowd toward Piper, keeping my pace steady despite the winded feeling in my "
            "chest. The abandoned basket, the wrapped arms, the way she's watching that runner.\n\n"
            '"Piper," I say quietly. "The whistle\'s real. But Coldiron sent me to find proof of something he '
            'already knew was there." I pull out the bone whistle just enough for her to see it.')
    res = _runner(world, said).run("@interact Dropped sweetroot bundle")
    assert not res.ok and res.result["refused"] == "not_reached_for"
    assert res.message.startswith("the player did not reach for it — put it in view and stop")
    with get_session() as session:
        assert not session.get(Player, world["player"]).inventory[1:]


@pytest.mark.parametrize("said", [
    "I pick it up carefully, turning it over, and pocket it.",          # fleet7-wenna T11
    "I crouch by the sweetroot and look it over.",                       # names the thing
    "I search the square for anything dropped in the rush.",
])
def test_a_take_the_player_asked_for_lands(world, said):
    assert _runner(world, said).run("@interact Dropped sweetroot bundle").ok


def test_reading_and_the_cli_path_are_not_held_back(world):
    assert _runner(world, None).run("@interact Dropped sweetroot bundle").ok   # no player words known


# --- refused_but_narrated -----------------------------------------------------

def test_a_refused_give_told_as_pocketed_is_named_once():
    """fleet7-tamsin T10."""
    commands = [{"line": "@give Cracked Bone Whistle | kind: clue", "ok": False,
                 "message": "that is the cracked bone whistle at Wolf's Hollow — the player finds it with "
                            "@interact there; nothing was given"}]
    prose = "The marks match, line for line. You pocket the whistle, cord and all, and ease back out."
    warnings = refused_but_narrated(commands, prose, here="North Gatehouse")
    assert [w["check"] for w in warnings] == ["refused_but_narrated"]
    detail = warnings[0]["detail"]
    assert "`@give Cracked Bone Whistle | kind: clue` was REFUSED" in detail
    assert "You pocket the whistle" in detail and "no Cracked Bone Whistle is in their pack" in detail
    generic = [{"check": "item_without_tool", "detail": "write @give <item>"},
               {"check": "too_talky", "detail": "x"},
               {"check": "carried_not_held", "detail": "carrying the Cracked Bone Whistle"}]
    merged = merge_refused(generic, warnings)
    assert [w["check"] for w in merged] == ["too_talky", "refused_but_narrated"]
    assert set(merged[-1]) == {"check", "detail"}


def test_a_refused_hand_and_a_refused_payout_told_anyway():
    """fleet7-tamsin T14 (with the round-6 guard, its @quest-done is refused too)."""
    commands = [
        {"line": "@hand Cracked Bone Whistle | to: Captain Brann Coldiron", "ok": False,
         "message": "no item in their pack called 'Cracked Bone Whistle'"},
        {"line": "@quest-done Teeth on the North Road", "ok": False,
         "message": "'Teeth on the North Road' is not done, so nothing was paid"},
    ]
    prose = ("He tucks the whistle away inside his coat, jaw tight. He counts coin into your palm without "
             "looking away from you.")
    checks = {w["detail"].split("`")[1].split()[0]: w["detail"] for w in refused_but_narrated(commands, prose)}
    assert "the player never had the Cracked Bone Whistle" in checks["@hand"]
    assert "Nothing was paid" in checks["@quest-done"] and "counts coin into your palm" in checks["@quest-done"]
    merged = merge_refused([{"check": "money_without_command", "detail": "write @coin +N"},
                            {"check": "item_handover_without_command", "detail": "hand over the scarf"}],
                           refused_but_narrated(commands, prose))
    # the coin advice goes; a handover of some other thing stays
    assert [w["check"] for w in merged] == ["item_handover_without_command", "refused_but_narrated",
                                            "refused_but_narrated"]


def test_a_refused_move_told_as_an_arrival():
    """fleet7-bram T11."""
    commands = [{"line": "@move Wolf's Hollow", "ok": False,
                 "message": "the way to Wolf's Hollow is blocked (the North Gate is barred) — they did not go"}]
    prose = "The road climbs through bare trees until the ground opens into a hollow below you."
    warnings = refused_but_narrated(commands, prose, here="Stable Yard")
    assert len(warnings) == 1 and "they are still at Stable Yard" in warnings[0]["detail"]
    merged = merge_refused([{"check": "arrival_without_move", "detail": "x"}], warnings)
    assert [w["check"] for w in merged] == ["refused_but_narrated"]
    # The server's own arrived_past_the_way already says it, with the way
    assert [w["check"] for w in merge_refused([{"check": "arrived_past_the_way", "detail": "x"}], warnings)] \
        == ["arrived_past_the_way"]


def test_a_refusal_the_prose_respected_is_quiet():
    commands = [{"line": "@give Cracked Bone Whistle", "ok": False, "message": "nothing was given"},
                {"line": "@move Wolf's Hollow", "ok": False, "message": "blocked"},
                {"line": "@hand Wolf Pelt | to: Coldiron", "ok": True, "message": ""}]
    prose = ("The whistle is not in your pocket — you left it lying in the bracken. The gate stays shut; "
             "you are still in the square.")
    assert refused_but_narrated(commands, prose, here="Market Square") == []


# --- carried_not_held ---------------------------------------------------------

UNHELD = [{"item": "Cracked Bone Whistle", "thing": "Cracked bone whistle", "place": "Wolf's Hollow"},
          {"item": "Greywater Silver Piece", "thing": "Split supply crate", "place": "Wolf's Hollow"},
          {"item": "Sweetroot Bundle", "thing": "Dropped sweetroot bundle", "place": "Market Square"}]


def test_prose_carrying_a_clue_still_lying_out_is_flagged():
    """fleet7-tamsin T13; fleet7-wenna T16 ("crate-silver" from a crate never searched)."""
    took = carried_not_held("He takes the whistle from your palm without quite touching your fingers.",
                            ["Gate Pass"], UNHELD)
    assert took and took["check"] == "carried_not_held"
    assert "it still lies at Wolf's Hollow (Cracked bone whistle)" in took["detail"]
    silver = carried_not_held("The whistle's broken weight knocking against notebooks and crate-silver with "
                              "every stride.", ["Cracked Bone Whistle"], UNHELD)
    assert silver and "Greywater Silver Piece" in silver["detail"]


@pytest.mark.parametrize("prose, pack", [
    ("You pocket the whistle, cord and all.", ["Cracked Bone Whistle"]),       # it IS in the pack
    ("You tuck the sweetroot-stained paper into memory.", []),                 # fleet7-wenna T3: a stain
    ("Without the whistle in your hand, he will not believe you.", []),        # said as missing
    ("Hesk counts ten silver into your palm.", []),                            # money, not the crate's piece
    ("The whistle lies in the bracken where it fell.", []),                    # in view, not carried
])
def test_carrying_that_is_not_a_claim(prose, pack):
    assert carried_not_held(prose, pack, UNHELD) is None


# --- The frame is tied to the thing (review of round 6) -----------------------
# A verb anywhere in the sentence was a claim: the honest replies a refusal
# asks for ("The bar does not lift", eyes on the bundle) were flagged as the
# opposite, and the DM told to take back what it never said.

@pytest.mark.parametrize("prose", [
    "You keep your eyes on the sweetroot bundle at her feet.",
    "You hold your breath, watching the whistle glint in the bracken.",
    "You keep your distance from the whistle lying in the mud.",
    "You hold out your hand for the whistle, but Coldiron keeps it.",
    "Your whistle question hangs in the air.",
    "The whistle is there for the taking, if you take it.",
    "Piper picks up the sweetroot and pockets it.",
])
def test_looking_at_a_thing_is_not_carrying_it(prose):
    assert carried_not_held(prose, [], UNHELD) is None
    refused = [{"line": "@give Cracked Bone Whistle", "ok": False, "message": "nothing was given"},
               {"line": "@interact Dropped sweetroot bundle", "ok": False,
                "message": "the player did not reach for it — put it in view and stop"}]
    assert refused_but_narrated(refused, prose) == []


def test_a_pocketing_by_pronoun_is_a_claim():
    """fleet7-tamsin T11: "a loose bundle of sweetroot… and you pocket it"."""
    prose = "You notice, stooping, a loose bundle of sweetroot by the crate, and you pocket it without breaking stride."
    assert carried_not_held(prose, [], UNHELD)["check"] == "carried_not_held"
    refused = [{"line": "@interact Dropped sweetroot bundle", "ok": False, "message": "the player did not reach for it"}]
    assert [w["check"] for w in refused_but_narrated(refused, prose)] == ["refused_but_narrated"]


@pytest.mark.parametrize("prose, flagged", [
    ("You stand at the gate, looking north toward Wolf's Hollow. The bar does not lift.", False),
    ("The road to Wolf's Hollow stays shut; you walk back to the square.", False),
    ("You come back from the gate empty-handed; Wolf's Hollow will have to wait.", False),
    ("You reach Wolf's Hollow as the moon rises.", True),
    ("You stand at the hollow's edge, listening.", True),
    ("Wolf's Hollow spreads out below you, silver in the moonlight.", True),
    ("The guard barely glances at the pass before waving you through.", True),     # fleet7-bram T11
])
def test_a_refused_move_is_flagged_only_for_an_arrival(prose, flagged):
    commands = [{"line": "@move Wolf's Hollow", "ok": False, "message": "the way to Wolf's Hollow is blocked"}]
    assert bool(refused_but_narrated(commands, prose, here="Stable Yard")) is flagged


def test_a_hand_over_is_tied_to_the_thing():
    commands = [{"line": "@hand Cracked Bone Whistle | to: Captain Brann Coldiron", "ok": False,
                 "message": "no item in their pack called 'Cracked Bone Whistle'"}]
    quiet = "Coldiron takes a long breath and asks where the whistle is now."
    assert refused_but_narrated(commands, quiet) == []
    told = "He takes the whistle from your palm and tucks it inside his coat."
    assert refused_but_narrated(commands, told)


def test_opening_a_thing_that_holds_an_item_is_a_take(world):
    """The seed's lost satchel puts its letter in the pack when opened: the
    player's to reach for, like a take. An open with nothing inside (a
    shutter) stays the DM's to resolve."""
    satchel = {"kind": "open", "label": "Lost traveler's satchel", "x": 52, "y": 68,
               "payload": {"item": {"name": "Water-Stained Letter", "type": "quest_item",
                                    "description": "Addressed to a dead man."}}}
    shutter = {"kind": "open", "label": "Rattling shutter", "x": 20, "y": 40,
               "payload": {"panel_text": "It swings open on the dark lane."}}
    with get_session() as session:
        square = session.get(Location, world["square"])
        square.objects = list(square.objects or []) + normalize_objects([satchel, shutter])
        session.commit()
    said = '"Evening, Keeper. Quiet night?"'
    refused = _runner(world, said).run("@interact Lost traveler's satchel")
    assert not refused.ok and refused.result["refused"] == "not_reached_for"
    assert _runner(world, said).run("@interact Rattling shutter").ok
    assert _runner(world, "I kneel and work the satchel's buckle loose.").run("@interact Lost traveler's satchel").ok
