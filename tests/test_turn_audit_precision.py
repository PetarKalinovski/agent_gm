"""Reality audit, round 3 (fleet4): consumables, arrivals, dressings, and
the contents of "Things here" told before anyone used them.

Every case is a line a fleet4 DM actually wrote (or the player typed).
"""

from src.models import Location, LocationType, get_session
from src.web import turn_audit
from src.web.turn_audit import audit_turn, unseen_scene_objects

DRAUGHT = {"name": "Willow-Bark Draught", "quantity": 1, "type": "consumable", "effect_tag": "heal_small"}
KNIFE = {"name": "Crude Knife", "quantity": 1, "type": "weapon"}


def _state(health="badly_hurt", inventory=None, location="hollow"):
    return {"health": health, "inventory": list(inventory if inventory is not None else [KNIFE, DRAUGHT]),
            "location_id": location, "currency": 24}


def _tools(*names):
    return [{"name": n, "status": "success"} for n in names]


def _checks(warnings):
    return {w["check"] for w in warnings}


def _detail(warnings, check):
    return next(w["detail"] for w in warnings if w["check"] == check)


# ---------------------------------------------------------------------------
# item_used_without_command — the player drinking, nobody else's words
# ---------------------------------------------------------------------------

# fleet4-tamsin T9, verbatim: the audit fired on it, and next turn the DM
# drank her only potion unasked
T9_PROSE = (
    "Vane watches you go the way he watches everything — unhurried, already certain of how it ends. "
    "He doesn't call after you. The wolves don't follow either, settling back into the birch-shadow "
    "like dogs told to heel, and the hollow's held breath lets go only once the trees have swallowed "
    "you whole.\n\n"
    "The walk back thins the adrenaline out of you fast, leaving the arm's damage plain and ugly under "
    "your sleeve — not a clean cut, something torn, something that'll need more than a willow-bark "
    "draught to close right."
)


def test_fleet4_tamsin_t9_is_not_a_drink():
    warnings = audit_turn(T9_PROSE, [], _state(), _state())
    assert "item_used_without_command" not in _checks(warnings)


def test_negated_and_hypothetical_frames_are_not_drinks():
    for text in ("You could drink the willow-bark draught now, but you don't.",
                 "Without the willow-bark draught you would never have made the gate.",
                 "If you drank the draught now, the ache might ease.",
                 "The herbalist's stall smells of willow-bark and you swallow hard."):
        assert "item_used_without_command" not in _checks(audit_turn(text, [], _state(), _state())), text


def test_the_player_drinking_it_still_needs_the_use():
    for text in ("You uncork the willow-bark draught and drink it in three bitter swallows.",
                 "You knock back the draught and wait for the warmth.",
                 "The draught goes down bitter, and the throb in your arm dulls."):
        warnings = audit_turn(text, [], _state(), _state())
        assert "item_used_without_command" in _checks(warnings), text
        assert "ONLY if the player actually drank or used it" in _detail(warnings, "item_used_without_command")
        assert "item_used_without_command" not in _checks(audit_turn(text, _tools("use_item"), _state(), _state()))


# ---------------------------------------------------------------------------
# consumable_used_without_command — the player SAID they drank it
# ---------------------------------------------------------------------------

def test_fleet4_bram_t13_the_player_drinks_and_nothing_happens():
    said = "I drink the willow-bark slow, let it settle, and study Piper's face while I do."
    prose = "Your ribs ache with every breath, but the willow-bark's taken the worst edge off."
    warnings = audit_turn(prose, [], _state(), _state(), player_input=said)
    assert "consumable_used_without_command" in _checks(warnings)
    assert "write @use Willow-Bark Draught" in _detail(warnings, "consumable_used_without_command")
    # one warning for one draught, not two
    assert "item_used_without_command" not in _checks(warnings)
    assert "consumable_used_without_command" not in _checks(
        audit_turn(prose, _tools("use_item"), _state(), _state(), player_input=said))
    used = _state(inventory=[KNIFE])
    assert "consumable_used_without_command" not in _checks(audit_turn(prose, [], _state(), used, player_input=said))


def test_talking_about_a_draught_is_not_drinking_it():
    for said in ("Should I drink the willow-bark now?",
                 "I'll save the draught for later and keep walking.",
                 "I don't drink the willow-bark — not yet.",
                 "I'll find the herbalist's apprentice, get the willow-bark down, and sleep it off.",
                 "[COMBAT OUTCOME] I drink the willow-bark draught."):
        warnings = audit_turn("The square is quiet.", [], _state(), _state(), player_input=said)
        assert "consumable_used_without_command" not in _checks(warnings), said


# ---------------------------------------------------------------------------
# arrival_without_move — waved through, back in town
# ---------------------------------------------------------------------------

def test_fleet4_tamsin_t9_walk_home_needs_a_move():
    text = "He doesn't ask questions. He just waves you through and reaches for a signal-bell."
    warnings = audit_turn(text, [], _state(), _state())
    assert "arrival_without_move" in _checks(warnings)
    assert "@move <place>" in _detail(warnings, "arrival_without_move")
    assert "arrival_without_move" not in _checks(audit_turn(text, _tools("move_player"), _state(), _state()))
    assert "arrival_without_move" in _checks(audit_turn("You're back in Market Square before the bells.",
                                                        [], _state(), _state()))


def test_being_back_at_something_is_not_arriving():
    for text in ("You're back in the fight before you can think.",
                 "You are back at it, counting coins under the table.",
                 "He waves you off and turns back to his ledger."):
        assert "arrival_without_move" not in _checks(audit_turn(text, [], _state(), _state())), text


# ---------------------------------------------------------------------------
# healing_without_command — a dressing is a heal
# ---------------------------------------------------------------------------

T18_PROSE = (
    "She works the linen bandage with the same unhurried competence she's probably given a dozen bar "
    "fights and worse.\n\nShe ties off the bandage with a neat, practiced knot, then sits back slightly."
)


def test_fleet4_tamsin_t18_maeras_dressing_needs_health():
    hurt = _state(health="hurt")
    warnings = audit_turn(T18_PROSE, [], hurt, hurt)
    assert "healing_without_command" in _checks(warnings)
    assert "@health winded | cause:" in _detail(warnings, "healing_without_command")
    assert "healing_without_command" not in _checks(audit_turn(T18_PROSE, _tools("update_player_health"), hurt, hurt))
    assert "healing_without_command" not in _checks(audit_turn(T18_PROSE, [], hurt, _state(health="winded")))
    # nothing to heal
    assert "healing_without_command" not in _checks(audit_turn(T18_PROSE, [], _state("healthy"), _state("healthy")))


def test_a_bandage_in_the_scenery_is_not_a_dressing():
    hurt = _state(health="hurt")
    for text in ("Someone behind the bar takes one look at your bandaged arm and asks no questions.",
                 "He studies you, weighing the ask against the bandage on your arm.",
                 "She hands you a clean bandage and a cup of something hot.",
                 "Maera works the room, eyeing your bandaged arm from the bar.",
                 "You tie off the bandage yourself, teeth gritted."):
        assert "healing_without_command" not in _checks(audit_turn(text, [], hurt, hurt)), text


# ---------------------------------------------------------------------------
# object_contents_leak — what the unread carving says
# ---------------------------------------------------------------------------

HOLLOW_OBJECTS = [
    {"id": "scratches_on_the_hut_wall_0", "kind": "read", "label": "Scratches on the hut wall",
     "payload": {"text": "Tally marks gouged into the doorframe, grouped in threes. Under them, one word, "
                         "carved deep and old: SING."}, "consumed": False},
    {"id": "cracked_bone_whistle_1", "kind": "take", "label": "Cracked bone whistle",
     "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                          "description": "Split down the barrel — silent now. The same make as the one "
                                         "Captain Coldiron never mentions."}}, "consumed": False},
]
NOTICE = {"id": "notice_board_0", "kind": "read", "label": "Notice board", "consumed": False,
          "payload": {"text": "TAKEN BY WOLVES — cart, driver unharmed, cargo gone, north road, third such "
                              "this month. The Watch pays for true accounts of the howls. — Capt. B. Coldiron."
                              "\n\nBeneath it, smaller: 'Sweetroot for nerves. Ask Piper.'"}}


def _leaks(prose, objects, spoken_text=""):
    warnings = audit_turn(prose, [], _state(), _state(), scene_objects=objects, spoken_text=spoken_text)
    return [w["detail"] for w in warnings if w["check"] == "object_contents_leak"]


def test_fleet4_tamsin_t9_the_unread_carving():
    prose = ("You have the words now, and somewhere in that ruined hut's tally marks and that one carved "
             "word, SING, there was more than you had time to read close.")
    leaks = _leaks(prose, HOLLOW_OBJECTS)
    assert len(leaks) == 1 and "Scratches on the hut wall" in leaks[0] and "(SING)" in leaks[0]
    assert "@interact Scratches on the hut wall" in leaks[0]
    # Read this reply (or before), it is no leak
    assert _leaks(prose, HOLLOW_OBJECTS[1:]) == []


def test_fleet4_wenna_t12_the_untouched_whistle():
    prose = ("The bone whistle under your boot — split, silent, the make too familiar — and the crate's "
             "spilled contents sit back there in the dark.")
    leaks = _leaks(prose, HOLLOW_OBJECTS)
    assert len(leaks) == 1 and "Cracked bone whistle" in leaks[0]
    # Its label is public: seeing the whistle is not reading its description
    assert _leaks("A cracked bone whistle lies half-buried by the hut.", HOLLOW_OBJECTS) == []


def test_sharing_the_worlds_words_is_not_reading_the_notice():
    """fleet-bram T18 and kin: the notice's words are the whole town's words."""
    for prose in ("Coldiron watches you go with the particular stillness of a man adding up a debt he "
                  "can't pay yet.",
                  "The road waits all the same, and somewhere up past the second bend, a cart and its "
                  "driver are still unaccounted for."):
        assert _leaks(prose, [NOTICE]) == [], prose
    # A captain talking about his stolen carts knows it without his own board
    assert _leaks("He taps the board.", [NOTICE], spoken_text="Third cart taken this month.") == []


def test_quoted_payload_text_and_capitals_are_tells():
    assert _leaks("Beneath the notice someone has added: sweetroot for nerves, ask Piper.", [NOTICE])
    assert _leaks("TAKEN BY WOLVES, the board shouts across the square.", [NOTICE])


def test_unseen_objects_come_from_the_scene_and_forget_what_was_used(db):
    with get_session() as session:
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, depth=0, objects=HOLLOW_OBJECTS + [
            {"id": "bed", "kind": "rest", "label": "Bedroll", "payload": {}, "consumed": False}])
        session.add(hollow)
        session.commit()
        hollow_id = hollow.id
    turn_audit._OBJECTS_OUT.pop("p-leak", None)
    labels = [o["label"] for o in unseen_scene_objects("p-leak", hollow_id)]
    assert labels == ["Scratches on the hut wall", "Cracked bone whistle"]
    # @interact this reply: seen, and stays seen (a read object is never consumed)
    labels = [o["label"] for o in unseen_scene_objects("p-leak", hollow_id, ["@interact Scratches on the hut wall"])]
    assert labels == ["Cracked bone whistle"]
    unseen = unseen_scene_objects("p-leak", hollow_id)
    assert [o["label"] for o in unseen] == ["Cracked bone whistle"]
    # A leak is warned about once: the words are out
    prose = "The whistle under your boot — split, silent, the make too familiar."
    assert len(_leaks(prose, unseen)) == 1
    assert unseen_scene_objects("p-leak", hollow_id) == []


# ---------------------------------------------------------------------------
# Review pass: the same checks against lines that are NOT what they look for
# ---------------------------------------------------------------------------

def test_a_body_part_or_a_blade_is_no_dressing():
    """Body parts count only as YOUR arm, the verb's own object; an article
    is never the one tending ("The wind…")."""
    hurt = _state(health="hurt")
    for text in ("The wind tugs at your arm as you climb.",
                 "She works her way to your side through the crowd.",
                 "He cleans his blade on the dead wolf's side and stands.",
                 "Hesk packs the crate to the side of the stall.",
                 "The fire works its warmth into your shoulder.",
                 "They bind your hands behind your back."):
        assert "healing_without_command" not in _checks(audit_turn(text, [], hurt, hurt)), text
    for text in ("He wraps your arm in clean linen.",
                 "Maera cleans the gash on your forearm with something that stings.",
                 "She stitches the cut shut in six quick passes."):
        assert "healing_without_command" in _checks(audit_turn(text, [], hurt, hurt)), text


def test_the_draught_must_be_the_verbs_own_object():
    """The object ends with its clause: a draught named after a comma, a
    semicolon or 'to cut…' was never what the player drank."""
    for text in ("You eat the stew, and Piper sets a willow-bark draught by your bowl.",
                 "You sip the broth; the draught stays corked in your pack.",
                 "You swallow the retort, eyes on the willow-bark draught in his hand.",
                 "The willow-bark draught sits on the table as the sun goes down."):
        assert "item_used_without_command" not in _checks(audit_turn(text, [], _state(), _state())), text
    for said in ("I use my knife to cut a strip of willow-bark from the tree.",
                 "I eat with Brann and tell him about my willow-bark draught.",
                 "I hand Piper the sweetroot bundle and eat my bread."):
        warnings = audit_turn("The square is quiet.", [], _state(), _state(), player_input=said)
        assert "consumable_used_without_command" not in _checks(warnings), said
    assert "consumable_used_without_command" in _checks(audit_turn(
        "The square is quiet.", [], _state(), _state(), player_input="I uncork the willow-bark and drink it down."))


def test_the_real_combat_outcome_input_is_the_engines():
    """combat.combat_outcome_message writes "[COMBAT OUTCOME — resolved …",
    not "[COMBAT OUTCOME]": the tag ends at the dash."""
    from src.services.combat import combat_outcome_message

    message = combat_outcome_message({"outcome": "victory"})
    assert message.startswith("[COMBAT OUTCOME —")
    assert turn_audit._ENGINE_INPUT.match(message)
    assert not turn_audit._ENGINE_INPUT.match("[I drink the willow-bark draught]")


def test_words_said_aloud_this_turn_are_not_read_off_the_board():
    prose = "Third cart this month, taken on the north road — the words hang in the cold."
    assert _leaks(prose, [NOTICE])                               # nobody said it: the board did
    assert _leaks(prose, [NOTICE], spoken_text="Third cart taken this month, on the north road.") == []
    warnings = audit_turn(prose, [], _state(), _state(), scene_objects=[NOTICE],
                          player_input="I ask whether a third cart was taken this month.")
    assert "object_contents_leak" not in _checks(warnings)


def test_a_canvas_read_is_seen_where_it_stands(db):
    """Read the notice on the canvas, walk into the inn, type: the next
    typed turn starts in the inn, but the notice was still read."""
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, objects=[NOTICE])
        session.add(square)
        session.commit()
        square_id = square.id
    turn_audit._OBJECTS_OUT.pop("p-canvas", None)
    assert [o["label"] for o in unseen_scene_objects("p-canvas", square_id)] == ["Notice board"]
    turn_audit.mark_object_seen("p-canvas", square_id, "notice_board_0")
    assert unseen_scene_objects("p-canvas", square_id) == []
    # The engine's resolved id is enough: "@interact the board" opened it
    turn_audit._OBJECTS_OUT.pop("p-canvas", None)
    assert unseen_scene_objects("p-canvas", square_id, ["@interact the board notice_board_0"]) == []


def test_the_brief_marks_each_things_contents_unseen():
    """fleet4-tamsin T9 / fleet4-wenna T12: contents the DM could see went
    into prose as if the player had. Every line carries the warning."""
    from src.services.scene_brief import _holds

    carving = _holds({"kind": "read"}, HOLLOW_OBJECTS[0]["payload"])
    assert carving.startswith(" — UNSEEN by the player until @interact: reads/shows: \"Tally marks")
    whistle = _holds({"kind": "take"}, HOLLOW_OBJECTS[1]["payload"])
    assert whistle.startswith(" — UNSEEN by the player until @interact: holds: Cracked Bone Whistle")
    assert _holds({"kind": "rest"}, {"text": "A straw pallet."}) == ""


def test_back_at_a_persons_thing_is_not_a_place():
    for text in ("You're back at Coldiron's board, the ink still wet.",
                 "You're back in Coldiron's good graces, for now."):
        assert "arrival_without_move" not in _checks(audit_turn(text, [], _state(), _state())), text
    assert "arrival_without_move" in _checks(audit_turn("You're back in Wolf's Hollow before dark.",
                                                        [], _state(), _state()))
