"""Round 4 (fleet5) audits: wounds without a fight, dice without a price,
and a goodbye that is not a speaker in the wrong place.

  * wound_without_fight — fleet5-bram T8: `@health winded | cause: a wolf
    breaks cover and rakes a shoulder` with Gray-Flank Wolf standing in the
    brief and no @fight.
  * partial_without_cost — fleet5-tamsin T10: a partial handed over the whole
    gate pass, free. Replayed over every fleet transcript (294 turns): 19
    turns rolled partial/failure, 7 of them charged nothing.
  * speaker_not_here — fleet5-bram T7: Sorrel's "You be careful out there"
    came after the reply's @move, so it was stamped at the Hollow.
"""

import pytest

from src.models import Location, LocationType, NPC, Player, get_session
from src.services.commands import CommandResult
from src.web.turn_audit import (
    consequence_warnings,
    partial_without_cost,
    people_for_audit,
    wound_without_fight,
)


def _ok(line, verb="", **result):
    return CommandResult(line=line, verb=verb, ok=True, tool=None, result=result)


@pytest.fixture()
def hollow(db):
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True)
        place = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True)
        session.add_all([square, place])
        session.flush()
        wolf = NPC(name="Gray-Flank Wolf", species="wolf", danger_tier="weak", current_location_id=place.id)
        vane = NPC(name="Aldric Vane", profession="wolf-caller", danger_tier="boss",
                   description_physical="a tall man in a wolf-pelt mantle", current_location_id=place.id)
        sorrel = NPC(name="Sorrel Tack", profession="stable hand", current_location_id=square.id)
        session.add_all([wolf, vane, sorrel])
        player = Player(name="Bram Holloway", current_location_id=place.id, health_status="healthy")
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "hollow": place.id,
                "wolf": wolf.id, "vane": vane.id, "sorrel": sorrel.id}


# ----------------------------------------------------------------------
# wound_without_fight
# ----------------------------------------------------------------------

def test_the_fleet5_bram_wolf_rake_is_a_fight_not_a_wound(hollow):
    people = people_for_audit(hollow["player"], [hollow["wolf"], hollow["vane"]])
    assert {p["name"]: p["species"] for p in people}["Gray-Flank Wolf"] == ["wolf"]
    cmds = [_ok("@health winded | cause: a wolf breaks cover and rakes a shoulder")]
    hit = wound_without_fight(cmds, people, "Pain lances white-hot across your shoulder.", "healthy")
    assert hit["check"] == "wound_without_fight"
    assert "@fight Gray-Flank Wolf" in hit["detail"] and "@npc-move Gray-Flank Wolf" in hit["detail"]


def test_a_blow_in_the_prose_by_someone_here_counts(hollow):
    people = people_for_audit(hollow["player"])
    cmds = [_ok("@health hurt | cause: a glancing blow")]
    prose = "The wolves circle. The gray-flank wolf lunges and its teeth rake your forearm."
    assert wound_without_fight(cmds, people, prose, "healthy")["check"] == "wound_without_fight"


@pytest.mark.parametrize("cmds, prose", [
    # The fight was written: its wounds are the fight's
    ([_ok("@health winded | cause: a wolf's bite"), _ok("@fight Gray-Flank Wolf")], ""),
    # Nobody here did it: a fall is a fall
    ([_ok("@health winded | cause: twisted ankle on the scree")], "You slip on the wet stone."),
    # Someone here, but not striking the player
    ([_ok("@health winded | cause: the cold")], "A branch snaps somewhere while the wolf watches you."),
    # Better, not worse
    ([_ok("@health healthy | cause: Piper's salve")], "The wolf bites the air."),
])
def test_no_flag_without_a_blow_from_someone_here(hollow, cmds, prose):
    people = people_for_audit(hollow["player"])
    assert wound_without_fight(cmds, people, prose, "winded" if "salve" in cmds[0].line else "healthy") is None


def test_people_for_audit_adds_whoever_stands_here_after_a_move(hollow):
    names = {p["name"] for p in people_for_audit(hollow["player"], [hollow["sorrel"]])}
    assert names == {"Sorrel Tack", "Gray-Flank Wolf", "Aldric Vane"}


def test_a_person_named_in_the_cause_must_have_done_it(hollow):
    """A rescued or watching person in the cause is not the blow; their fist
    or blade is. A beast named there always is (fleet-wenna T6: "a wolf's
    jaws closing hard on her forearm")."""
    people = people_for_audit(hollow["player"], [hollow["sorrel"]])
    rescued = [_ok("@health winded | cause: hauling Sorrel out of the flooded ditch")]
    assert wound_without_fight(rescued, people, "", "healthy") is None
    struck = [_ok("@health hurt | cause: Sorrel's fist, then his boot")]
    assert "@fight Sorrel Tack" in wound_without_fight(struck, people, "", "healthy")["detail"]
    jaws = [_ok("@health winded | cause: a wolf's jaws closing hard on her forearm")]
    assert "Gray-Flank Wolf" in wound_without_fight(jaws, people, "", "healthy")["detail"]


def test_a_helper_catching_the_player_is_no_blow(hollow):
    people = people_for_audit(hollow["player"], [hollow["sorrel"]])
    cmds = [_ok("@health winded | cause: slipped on the wet scree")]
    assert wound_without_fight(cmds, people, "Sorrel catches your elbow before you hit the stones.",
                               "healthy") is None


def test_a_companion_is_never_the_one_who_struck(hollow):
    """They never attack the player, and @fight refuses a party member."""
    with get_session() as session:
        player = session.get(Player, hollow["player"])
        player.party_members = [hollow["sorrel"]]
        session.get(NPC, hollow["sorrel"]).current_location_id = hollow["hollow"]
        session.commit()
    people = people_for_audit(hollow["player"], [hollow["sorrel"]])
    assert "Sorrel Tack" not in {p["name"] for p in people}
    cmds = [_ok("@health hurt | cause: Sorrel's elbow, swinging wild in the dark")]
    assert wound_without_fight(cmds, people, "", "healthy") is None


# ----------------------------------------------------------------------
# partial_without_cost
# ----------------------------------------------------------------------

def test_the_fleet5_tamsin_free_partial(db):
    cmds = [_ok("@give Gate Pass | kind: key | about: A charcoal-scrawled pass."), _ok("@time 15m")]
    hit = partial_without_cost(cmds, ["partial"], "healthy")
    assert hit["check"] == "partial_without_cost"
    assert hit["detail"].startswith("Your roll_check came back PARTIAL")


@pytest.mark.parametrize("line", [
    "@time 40m | the long way round",
    "@health winded | cause: a slip",
    "@coin -5 | the guard's palm",
    "@trust Coldiron -5 | wary",
    "@rep Lantern Watch -10",
    "@remove Howl-Chart",
    "@tension rising",
    "@fight Gray-Flank Wolf",
    "@hand Silver Coin | to: Coldiron",
])
def test_any_concrete_price_answers_it(db, line):
    assert partial_without_cost([_ok(line)], ["failure"], "healthy") is None


@pytest.mark.parametrize("line", ["@time 10m", "@trust Coldiron +5 | warm", "@coin +10", "@tension low"])
def test_a_gain_or_the_turn_minimum_is_no_price(db, line):
    assert partial_without_cost([_ok(line)], ["partial"], "healthy") is not None


def test_success_needs_no_price_and_the_worst_outcome_is_named(db):
    assert partial_without_cost([], ["success", "critical_success"], "healthy") is None
    hit = partial_without_cost([], ["partial", "critical_failure"], "healthy")
    assert "CRITICAL FAILURE" in hit["detail"]


def test_a_failed_command_is_no_price(db):
    failed = CommandResult(line="@coin -500", verb="coin", ok=False, tool=None)
    assert partial_without_cost([failed], ["failure"], "healthy") is not None


def test_transcript_rows_are_read_too(db):
    rows = [{"line": "@coin -3 | a bribe", "ok": True, "message": ""}]
    assert partial_without_cost(rows, ["partial"], "healthy") is None


def test_an_engine_fight_answers_both(hollow):
    cmds = [_ok("@health hurt | cause: a wolf's jaws")]
    assert consequence_warnings(hollow["player"], cmds, ["failure"], "", fought=True) == []
    checks = {w["check"] for w in consequence_warnings(hollow["player"], [], ["failure"], "")}
    assert checks == {"partial_without_cost"}
    checks = {w["check"] for w in consequence_warnings(
        hollow["player"], cmds, ["failure"], "", present_ids=[hollow["wolf"]],
        state_before={"health": "healthy"})}
    assert checks == {"wound_without_fight"}          # the wound is the price; the fight is missing


# ----------------------------------------------------------------------
# speaker_not_here: a goodbye from the place the player just left
# ----------------------------------------------------------------------

def test_a_goodbye_after_the_move_is_not_a_speaker_out_of_place(hollow):
    from src.web.server import _absent_speakers

    spoken = [{"npc_id": hollow["sorrel"], "npc_name": "Sorrel", "_where": hollow["hollow"],
               "text": "You be careful out there, mister."}]
    # Without the turn's start: flagged, as fleet5-bram T7 was
    assert _absent_speakers(hollow["player"], spoken)
    # The turn began in the square, where Sorrel still stands: a farewell
    assert _absent_speakers(hollow["player"], spoken, left_from=hollow["square"]) == []
    # A turn that never left the square is still checked
    assert _absent_speakers(hollow["player"], spoken, left_from=hollow["hollow"])


# ----------------------------------------------------------------------
# fleet6-tamsin T13-19: someone voiced at the player's side is brought there
# ----------------------------------------------------------------------

def test_a_person_voiced_beside_the_player_is_moved_to_them(hollow):
    from src.web.server import _absent_speakers, _bring_speakers_here

    spoken = [{"npc_id": hollow["sorrel"], "npc_name": "Sorrel", "_where": hollow["hollow"],
               "text": "Three voices, taking turns."}]
    moved = _bring_speakers_here(hollow["player"], spoken, left_from=hollow["hollow"])
    assert moved == ["Sorrel Tack → Wolf's Hollow"]
    with get_session() as session:
        assert session.get(NPC, hollow["sorrel"]).current_location_id == hollow["hollow"]
    assert _absent_speakers(hollow["player"], spoken, left_from=hollow["hollow"]) == []


def test_a_goodbye_the_villain_and_the_dead_stay_where_they_are(hollow):
    from src.models.campaign import Campaign
    from src.web.server import _bring_speakers_here

    goodbye = [{"npc_id": hollow["sorrel"], "npc_name": "Sorrel", "_where": hollow["hollow"],
                "text": "You be careful out there."}]
    assert _bring_speakers_here(hollow["player"], goodbye, left_from=hollow["square"]) == []

    with get_session() as session:
        square_man = NPC(name="Old Tam", profession="beggar", status="dead",
                         current_location_id=hollow["square"])
        session.add(square_man)
        session.add(Campaign(player_id=hollow["player"], status="active", title="t", premise="p",
                             villain_npc_id=hollow["sorrel"]))
        session.commit()
        dead_id = square_man.id
    spoken = [{"npc_id": hollow["sorrel"], "_where": hollow["hollow"], "text": "…"},
              {"npc_id": dead_id, "_where": hollow["hollow"], "text": "…"}]
    assert _bring_speakers_here(hollow["player"], spoken, left_from=hollow["hollow"]) == []


def test_a_reward_already_in_the_pack_is_not_a_missing_handover(db):
    # fleet6-wenna T12: @quest-done paid the tincture at T11, reward_unnarrated
    # asked for it on screen, and T12 showed it — then got told it never arrived
    from src.web.turn_audit import audit_turn

    prose = ("Piper presses the Yarrow Tincture into your other hand along with the coin already "
             "there, closing your fingers over both like she's sealing a promise.")
    state = {"location": "Market Square", "health": "healthy", "currency": 35,
             "inventory": [{"name": "Yarrow Tincture", "type": "consumable"}]}
    checks = {w["check"] for w in audit_turn(prose, [], state, state)}
    assert "item_handover_without_command" not in checks
    empty = {**state, "inventory": []}
    checks = {w["check"] for w in audit_turn(prose, [], empty, empty)}
    assert checks & {"item_handover_without_command", "item_without_tool"}


# ---------------------------------------------------------------------------
# healed_without_command (fleet6-bram T16)
# ---------------------------------------------------------------------------

# The Keeper's dressing, as the reply wrote it
KEEPER_PROSE = (
    "The Keeper's eyes go straight to the blood before they find your face. She comes around the bar "
    "unhurried, already reaching for a clean cloth and a jar of something that smells of pine tar and "
    "vinegar.\n\nShe works the makeshift binding loose with practiced hands, not flinching at the wound, "
    "and presses the jar's contents into it hard enough to make your teeth click."
)
KEEPER_SAID = "That'll hold till morning if you don't go looking for more trouble tonight."


def test_a_healer_dressing_the_wound_with_nothing_applied_is_flagged():
    from src.web.turn_audit import healed_without_command

    warning = healed_without_command([], KEEPER_PROSE, KEEPER_SAID, "hurt", "hurt")
    assert warning and warning["check"] == "healed_without_command"
    assert "@health winded | cause: <who>'s dressing" in warning["detail"]
    assert "works the makeshift binding" in warning["detail"]


@pytest.mark.parametrize("prose,said", [
    # A healer's verdict said aloud, the dressing only implied
    ("She wipes her hands and nods at the fresh linen over the cut along your ribs.", KEEPER_SAID),
    # Relief felt
    ("By the time the fire burns low the ache in your side has dulled to something bearable.", ""),
])
def test_a_verdict_or_relief_with_nothing_applied_is_flagged(prose, said):
    from src.web.turn_audit import healed_without_command

    assert healed_without_command([], prose, said, "badly_hurt", "badly_hurt")


@pytest.mark.parametrize("case", ["applied", "drank", "slept", "healthy", "moved", "canvas", "denied", "eyed"])
def test_healing_that_landed_or_never_happened_is_not_flagged(case):
    from src.web.turn_audit import healed_without_command

    commands, before, after, actions, prose = [], "hurt", "hurt", (), KEEPER_PROSE
    if case == "applied":
        commands = [_ok("@health winded | cause: the Keeper's dressing")]
    elif case == "drank":
        commands = [_ok("@use Healing Draught")]
    elif case == "slept":
        commands = [_ok("@rest")]
    elif case == "healthy":
        before = after = "healthy"
    elif case == "moved":
        after = "winded"
    elif case == "canvas":
        actions = ("use_item",)                     # drunk from the HUD between turns
    elif case == "denied":
        prose = "The ache in your side doesn't ease, whatever you tell yourself."
    else:
        prose = "She works the room, eyeing your bandaged arm and the blood on your sleeve."
    said = KEEPER_SAID if case not in ("denied", "eyed") else ""
    assert healed_without_command(commands, prose, said, before, after, actions) is None


def test_the_older_dressing_check_is_not_said_twice():
    """healing_without_command (audit_turn) already reads "<she> ties off the
    bandage on your arm"; the new audit stays out of its way."""
    from src.web.turn_audit import healed_without_command

    prose = "Maera ties off the bandage on your arm with a neat, practiced knot."
    assert healed_without_command([], prose, "", "hurt", "hurt") is None


def test_consequence_warnings_carry_the_heal_and_the_unasked_roll(hollow):
    def checks_at(place):
        with get_session() as session:
            session.get(Player, hollow["player"]).current_location_id = hollow[place]
            session.commit()
        return [w["check"] for w in consequence_warnings(
            hollow["player"], [], ["critical_success"], KEEPER_PROSE, state_before={"health": "hurt"},
            state_after={"health": "hurt"}, spoken_text=KEEPER_SAID,
            player_input="I nod. \"Where do I find the Captain this hour?\"",
            rolls=[{"outcome": "critical_success", "skill": "perception", "reason": "spotting the Captain"}],
        )]

    assert checks_at("square") == ["healed_without_command", "roll_without_attempt"]
    # Among the wolves and their caller, a roll on a question is a confrontation
    assert checks_at("hollow") == ["healed_without_command"]


def test_a_failed_recognition_roll_is_paid_by_the_recognition(hollow):
    """A hooded player failed the recognition roll the brief ordered: the
    person placed them, and that is the price — no partial_without_cost, and
    nothing for the engine to charge (src/services/stakes.py)."""
    from src.services import stakes

    stakes.reset_state()
    seen = {"outcome": "failure", "skill": "stealth", "reason": "is the hooded figure placed",
            "about_npc_id": hollow["sorrel"]}
    checks = [w["check"] for w in consequence_warnings(
        hollow["player"], [], ["failure"], "Sorrel's eyes narrow under your hood.",
        state_before={"health": "healthy"}, player_input="I keep my hood low and ask about the mare.",
        rolls=[seen])]
    assert "partial_without_cost" not in checks
    assert stakes.owe(hollow["player"], [seen]) is None
    # ...but a sneak that failed beside it still owes its price
    sneak = {"outcome": "partial", "skill": "stealth", "reason": "slipping past the stalls"}
    checks = [w["check"] for w in consequence_warnings(
        hollow["player"], [], ["failure", "partial"], "You are seen.", state_before={"health": "healthy"},
        player_input="I sneak past the stalls.", rolls=[seen, sneak])]
    assert "partial_without_cost" in checks
    assert stakes.owe(hollow["player"], [seen, sneak])["outcome"] == "partial"
