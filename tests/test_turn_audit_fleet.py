"""Reality-audit checks added after the three-player fleet playtest.

Each check has a positive case lifted from (or modelled on) a fleet
transcript and the ordinary prose it must stay quiet on — a nag that fires
on "you pay attention" teaches the DM to ignore the audit.
"""

from src.web.turn_audit import audit_turn, format_audit_feedback

STATE = {"health": "healthy", "inventory": [], "location_id": "loc1", "currency": 85}
HORN = {"name": "Wolf-Caller's Horn", "quantity": 1, "type": "trinket", "slot": "trinket"}
PASS = {"name": "North Gate Pass", "quantity": 1, "type": "key"}
CHARM = {"name": "Lucky Charm", "quantity": 1, "type": "trinket"}
CARRYING = {**STATE, "inventory": [HORN, PASS, CHARM]}


def _tools(*names):
    return [{"name": n, "status": "success"} for n in names]


def _checks(warnings):
    return [w["check"] for w in warnings]


def _detail(warnings, check):
    return next(w["detail"] for w in warnings if w["check"] == check)


# ---------------------------------------------------------------------------
# money_without_command
# ---------------------------------------------------------------------------

def test_fleet_bram_knife_paid_in_prose_only():
    text = ("Hesk's hand closes around the ten coin fast, pocketing it with a practiced "
            "flick, but his eyes don't leave your face.")
    warnings = audit_turn(text, [], STATE, STATE)
    assert "money_without_command" in _checks(warnings)
    detail = _detail(warnings, "money_without_command")
    assert "@coin" in detail and "@buy" in detail and "ten coin" in detail


def test_money_changing_hands_either_way_flags():
    for text in (
        "You hand over ten coin without haggling.",
        "You pay the innkeeper eight coins for the night.",
        "You slip the boy a coin for his trouble.",
        "He counts eight coin into your palm, one at a time.",
        "Maera slides eight coins across the counter.",
        "She takes the coins without counting them twice.",
        "He pays you twenty coin and turns back to his ledger.",
        "He presses a small pouch of coin into your hand.",
        "The coin lands light in your pocket.",
    ):
        assert "money_without_command" in _checks(audit_turn(text, [], STATE, STATE)), text


def test_ordinary_money_words_stay_clean():
    for text in (
        "You pay attention to the gate and the men on it.",
        "Her coin-bright eyes follow you across the square.",
        "Near the well a man counts the square the way a miser counts coin.",
        "You rest a hand on the coin purse at your belt.",
        "If you pay him ten coin, he'll tell you where the cart went.",
        "You put the coins back in your purse and keep walking.",
        "He wants twenty coin for it, and he isn't joking.",
        "Something silver glints in the mud.",
        "You hand him the silver locket's chain to look at, then take it back.",
    ):
        assert "money_without_command" not in _checks(audit_turn(text, [], STATE, STATE)), text


def test_quoted_offer_is_not_a_payment():
    text = 'Hesk spreads his hands. "You hand over ten coin and the knife is yours."'
    assert "money_without_command" not in _checks(audit_turn(text, [], STATE, STATE))


def test_money_claim_clean_when_the_purse_moved():
    text = "You hand over ten coin without haggling."
    assert "money_without_command" not in _checks(
        audit_turn(text, [], STATE, {**STATE, "currency": 75})
    )
    for tool in ("adjust_currency", "transfer_item"):
        assert "money_without_command" not in _checks(audit_turn(text, _tools(tool), STATE, STATE))
    # A canvas sale between turns (pending_actions "shop" -> these tools)
    warnings = audit_turn(text, [], STATE, STATE, code_actions={"adjust_currency", "transfer_item"})
    assert "money_without_command" not in _checks(warnings)


def test_failed_coin_command_does_not_excuse_the_prose():
    text = "You hand over ten coin without haggling."
    failed = [{"name": "adjust_currency", "status": "error"}]
    assert "money_without_command" in _checks(audit_turn(text, failed, STATE, STATE))


# ---------------------------------------------------------------------------
# item_handover_without_command
# ---------------------------------------------------------------------------

def test_fleet_bram_branch_handed_over_in_prose():
    for text in (
        "You hand him the branch again and he takes it without looking.",
        "Coldiron takes the branch from your hand and turns it once in the lantern light.",
        "Piper takes the pages from you as if they might tear.",
        "You press the sealed letter into her hand.",
        "You hand over the notched branch.",
        "You give Coldiron the notched branch.",
    ):
        warnings = audit_turn(text, [], STATE, STATE)
        assert "item_handover_without_command" in _checks(warnings), text
        assert "@hand" in _detail(warnings, "item_handover_without_command")


def test_detail_names_the_object():
    warnings = audit_turn("You hand him the branch again and he takes it.", [], STATE, STATE)
    assert "the branch" in _detail(warnings, "item_handover_without_command")


def test_fleet_tamsin_npc_takes_a_carried_item_by_name():
    text = ("Coldiron takes the horn like it might bite him, turning it once in his "
            "scarred hand.")
    warnings = audit_turn(text, [], CARRYING, CARRYING)
    assert "item_handover_without_command" in _checks(warnings)
    assert "Wolf-Caller's Horn" in _detail(warnings, "item_handover_without_command")
    # Not carried -> no claim about the player's pack at all
    assert "item_handover_without_command" not in _checks(audit_turn(text, [], STATE, STATE))


def test_carried_item_checks_skip_shown_keys_and_other_sources():
    for text in (
        "The guard takes the pass, squints at the seal, and waves you on.",
        "Hesk takes the charm from the shelf and turns it to the light.",
        "Maera takes the charm, weighs it, and hands it back.",
    ):
        warnings = audit_turn(text, [], CARRYING, CARRYING)
        assert "item_handover_without_command" not in _checks(warnings), text


def test_fleet_wenna_notebook_shown_then_returned_is_clean():
    text = ("He takes the notebook from your hands and reads, slow and careful, lips "
            "moving faintly over the clerk's formal hand.\n\nThe lantern gutters. Somewhere "
            "behind the stalls a dog barks twice and goes quiet, and the square waits on "
            "him.\n\nHe says the name like it's bitter in his mouth, and hands the notebook back.")
    assert "item_handover_without_command" not in _checks(audit_turn(text, [], STATE, STATE))


def test_received_item_flags_when_existing_check_misses_it():
    # item_without_tool needs "into your hand"; an adjective slipped past it
    text = "He presses the pass into your ink-stained hand, warm from his coat."
    warnings = audit_turn(text, [], STATE, STATE)
    assert "item_handover_without_command" in _checks(warnings)
    assert "@give" in _detail(warnings, "item_handover_without_command")


def test_received_item_is_reported_once():
    # Already item_without_tool — the same sentence is not nagged twice
    warnings = audit_turn("She presses a small iron key into your palm.", [], STATE, STATE)
    assert "item_without_tool" in _checks(warnings)
    assert "item_handover_without_command" not in _checks(warnings)


def test_fleet_wenna_bread_bought_with_coin_only():
    text = ("Maera takes the coins without counting them twice, sliding a wrapped loaf "
            "across the counter.")
    warnings = audit_turn(text, _tools("adjust_currency"), STATE, {**STATE, "currency": 82})
    assert "item_handover_without_command" in _checks(warnings)
    assert "money_without_command" not in _checks(warnings)
    assert "a wrapped loaf" in _detail(warnings, "item_handover_without_command")


def test_across_the_counter_needs_a_payment_and_skips_drinks():
    shown = "The clerk slides a ledger across the desk so you can read it."
    assert "item_handover_without_command" not in _checks(audit_turn(shown, [], STATE, STATE))
    drink = "She slides a tankard of ale across the bar."
    warnings = audit_turn(drink, _tools("adjust_currency"), STATE, {**STATE, "currency": 84})
    assert "item_handover_without_command" not in _checks(warnings)


def test_gestures_favours_and_words_are_not_items():
    for text in (
        "You give him a long, hard look.",
        "You give her a nod and leave.",
        "You hand Coldiron the whole story, start to finish.",
        "You give the door a shove.",
        "You give them a wide berth.",
        "You return the favour to Piper with a grin.",
        "You give him a hand up onto the wall.",
        "You give the guard your name.",
        "You toss him a coin-bright grin.",
        "Coldiron takes the weight from your shoulders with one word.",
    ):
        assert "item_handover_without_command" not in _checks(audit_turn(text, [], STATE, STATE)), text


def test_handover_clean_when_the_pack_changed_or_a_command_ran():
    text = "You hand him the branch and step back."
    after = {**STATE, "inventory": [{"name": "Lantern"}]}
    assert "item_handover_without_command" not in _checks(audit_turn(text, [], STATE, after))
    for tool in ("transfer_item", "remove_from_inventory", "add_to_inventory"):
        warnings = audit_turn(text, _tools(tool), STATE, STATE)
        assert "item_handover_without_command" not in _checks(warnings), tool


# ---------------------------------------------------------------------------
# too_talky
# ---------------------------------------------------------------------------

def _lines(*texts, name="Captain Brann Coldiron", npc_id="c1"):
    return [{"npc_name": name, "text": t, "npc_id": npc_id} for t in texts]


def test_more_than_four_lines_is_too_talky():
    spoken = _lines(*["Short line here."] * 5)
    warnings = audit_turn("The square waits.", [], STATE, STATE, spoken=spoken)
    assert "too_talky" in _checks(warnings)
    detail = _detail(warnings, "too_talky")
    assert "at most 3 short lines" in detail and "let the player answer" in detail


def test_one_monologue_is_too_talky():
    speech = " ".join(["word"] * 61)
    warnings = audit_turn("The square waits.", [], STATE, STATE, spoken=_lines(speech))
    assert "too_talky" in _checks(warnings)


def test_a_normal_exchange_is_not_too_talky():
    spoken = _lines(*[" ".join(["word"] * 60)] * 4)
    assert "too_talky" not in _checks(audit_turn("The square waits.", [], STATE, STATE, spoken=spoken))
    assert "too_talky" not in _checks(audit_turn("The square waits.", [], STATE, STATE))
    assert "too_talky" not in _checks(audit_turn("The square waits.", [], STATE, STATE, spoken=[]))


# ---------------------------------------------------------------------------
# name_before_introduction
# ---------------------------------------------------------------------------

def _named(spoken, *, met=frozenset(), prose="The lanterns gutter.", player_input=""):
    return audit_turn(prose, [], STATE, STATE, spoken=spoken, player_name="Tamsin Rook",
                      met_before=set(met), player_input=player_input)


def test_fleet_tamsin_stranger_uses_the_surname():
    warnings = _named(_lines("Get some sleep, Rook."))
    assert "name_before_introduction" in _checks(warnings)
    detail = _detail(warnings, "name_before_introduction")
    assert "Captain Brann Coldiron" in detail and '"Rook"' in detail


def test_first_name_counts_too_and_each_npc_is_reported_once():
    warnings = _named(_lines("Tamsin. Sit.", "I said sit, Tamsin."))
    assert _checks(warnings).count("name_before_introduction") == 1


def test_someone_who_has_met_the_player_may_use_the_name():
    assert "name_before_introduction" not in _checks(_named(_lines("Rook."), met={"c1"}))


def test_name_check_needs_to_know_who_has_met():
    warnings = audit_turn("The lanterns gutter.", [], STATE, STATE, spoken=_lines("Rook."),
                          player_name="Tamsin Rook")
    assert "name_before_introduction" not in _checks(warnings)


def test_a_name_given_this_turn_is_fair_game():
    assert "name_before_introduction" not in _checks(
        _named(_lines("Tamsin, is it?"), player_input='"I\'m Tamsin," I say.')
    )
    assert "name_before_introduction" not in _checks(
        _named(_lines("Tamsin, is it?"), prose="You tell him your name and nothing else.")
    )
    # Said aloud by someone who knew it, earlier in the same reply
    spoken = (_lines("This is Tamsin. She's with me.", name="Piper Quillow", npc_id="p1")
              + _lines("Tamsin, then."))
    assert "name_before_introduction" not in _checks(_named(spoken, met={"p1"}))


def test_cover_story_prose_does_not_excuse_a_leak():
    # fleet-wenna T5's shape: the DM explained the leak instead of avoiding it
    warnings = _named(_lines("Tamsin Rook. From the town."),
                      prose="He says your own name back to you like he's testing a coin.")
    assert "name_before_introduction" in _checks(warnings)


def test_name_check_ignores_ambient_voices_and_lookalike_words():
    ambient = [{"npc_name": "Gate Guard", "text": "Move along, Rook."}]
    assert "name_before_introduction" not in _checks(_named(ambient))
    assert "name_before_introduction" not in _checks(_named(_lines("A rook on the wall, see?")))
    bram = audit_turn("The lanterns gutter.", [], STATE, STATE, player_name="Bram Holloway",
                      met_before=set(), spoken=_lines("Ask Hesk Bramble, not me."))
    assert "name_before_introduction" not in _checks(bram)


# ---------------------------------------------------------------------------
# campaign_beat_not_delivered — speaker labels count as delivery
# ---------------------------------------------------------------------------

HOLLOW = {
    "beat": {"beat_id": "b5", "title": "Into the Hollow",
             "delivery_keywords": ["Wolf's Hollow", "whistle-note", "Vex Marrow"]},
    "sealed_truths": [],
}


HOWL = {
    "beat": {"beat_id": "b2", "title": "The Howl Answers",
             "delivery_keywords": ["howl", "north road"]},
    "sealed_truths": [],
}


def test_fleet_bram_beat_delivered_through_a_speaker_label():
    # T9: the prose said "the hollow" and never named the man; his label did
    prose = "Bones lie in sorted piles at the edge of the hollow. A figure watches."
    spoken = [{"npc_name": "Vex Marrow", "text": "Turn around.", "npc_id": "v1"}]
    assert "campaign_beat_not_delivered" in _checks(
        audit_turn(prose, [], None, None, campaign=HOLLOW)
    )
    warnings = audit_turn(prose, [], None, None, campaign=HOLLOW, spoken=spoken)
    assert "campaign_beat_not_delivered" not in _checks(warnings)


def test_beat_check_reads_spoken_lines_without_spoken_text():
    spoken = [{"npc_name": "Piper Quillow", "text": "The howl came again.", "npc_id": "p1"}]
    warnings = audit_turn("The square waits.", [], None, None, campaign=HOWL, spoken=spoken)
    assert "campaign_beat_not_delivered" not in _checks(warnings)
    assert "campaign_beat_not_delivered" in _checks(
        audit_turn("The square waits.", [], None, None, campaign=HOWL)
    )


def test_no_fired_beat_means_no_beat_check():
    warnings = audit_turn("The square waits.", [], None, None,
                          campaign={"beat": None, "sealed_truths": []})
    assert "campaign_beat_not_delivered" not in _checks(warnings)


# ---------------------------------------------------------------------------
# feedback block
# ---------------------------------------------------------------------------

def test_feedback_block_points_at_commands_not_tools():
    out = format_audit_feedback([{"check": "too_talky", "detail": "Too many lines."}])
    assert "REALITY AUDIT" in out and "Too many lines." in out and "@command" in out
    assert "tool call" not in out


# ---------------------------------------------------------------------------
# Review probes: ordinary DM prose each first draft of these checks flagged
# ---------------------------------------------------------------------------

PACK = {**STATE, "inventory": [
    {"name": "Worn Knife", "type": "weapon", "slot": "weapon"},
    {"name": "North Gate Pass", "type": "quest_item"},   # what @give | kind: key files
    {"name": "Map of the North Road", "type": "misc"},
    {"name": "Lantern", "type": "misc"},
    HORN,
]}


def _flags(text, state=STATE, tools=()):
    keep = {"money_without_command", "item_handover_without_command", "item_without_tool"}
    return [c for c in _checks(audit_turn(text, _tools(*tools), state, state)) if c in keep]


def test_coin_near_a_verb_is_not_a_payment():
    for text in (
        "You put your back to the wall, watching the coins change hands at the dice table.",
        "You set the mug down beside the coins.",
        "You pay him no mind, watching the coins.",
        "You give the room a once-over: a few coppers on the bar, a dog asleep by the fire.",
        "You slip past the moneylender's table where the coins are stacked.",
        "You push through the crowns of the saplings.",
    ):
        assert _flags(text) == [], text


def test_the_players_own_money_going_nowhere_is_not_a_payment():
    for text in (
        "You flip a coin across your knuckles while you wait.",
        "You toss a coin into the air and catch it.",
        "You count out your coins. Eighty-five, same as this morning.",
        "Your coins clink in your purse with every step.",
        "Your hand closes around the coins in your pocket.",
        "Hesk takes the coins from the dead man's purse and pockets them.",
        "Unless he pays you ten coin, you walk.",
        "He hands you back your coins with a shake of his head.",
    ):
        assert _flags(text) == [], text


def test_coppers_as_police_are_not_money():
    for text in ("You hand the coppers your papers.", "You give the coppers the slip."):
        assert "money_without_command" not in _flags(text), text


def test_payments_still_flag_in_their_usual_shapes():
    for text in (
        "You set three coins on the counter.",
        "You dig out your last three coppers and hand them over.",
        "You give him the gold.",
        "You toss the beggar a coin.",
        "She tosses you a copper.",
        "Coldiron counts out twenty coins for you.",
        "Maera takes the coins from the counter and drops them in her apron.",
    ):
        assert "money_without_command" in _flags(text), text


def test_gestures_sensations_and_furniture_are_not_handovers():
    for text in (
        "You pass the gate a few minutes later.",
        "You pass the smithy a dozen paces on.",
        "You give the bench a wipe with your sleeve.",
        "You give him a clap on the shoulder.",
        "You give him a dry laugh.",
        "You give him a firm handshake.",
        "You give him the side-eye.",
        "You give her the gist of it.",
        "You give the captain the facts.",
        "You push the door open to Coldiron's office.",
        "You slide the chair over to Piper.",
        "You deliver the line to Vex with a straight face.",
        "You return the glare to Vex.",
        "She presses her thumb into your palm, reading the lines there.",
        "He puts his life in your hands.",
        "She places the investigation in your hands.",
        "The fire takes the chill from your fingers.",
        "Piper pulls the splinter from your palm with her teeth.",
        "Sorrel plucks the burr from your coat.",
        "If you hand him the horn, he'll know you were there.",
    ):
        assert _flags(text, PACK) == [], text


def test_money_words_get_no_hand_command_advice():
    # "@hand bribe" would fail: there is no such item in the pack
    for text in ("You give him the money.", "You slip the guard a bribe."):
        assert "item_handover_without_command" not in _flags(text), text


def test_a_nod_is_not_an_item_received():
    # The pre-existing item claim read "gives you a ..." as an item
    for text in ("He gives you a nod and a coin-bright grin.", "She gives you his word on it.",
                 "She presses a kiss into your palm."):
        assert "item_without_tool" not in _flags(text), text
    assert "item_without_tool" in _flags("She gives you a heel of bread and a wink.")


def test_handover_nouns_survive_adjectives_and_commas():
    assert "item_handover_without_command" in _flags("You hand him the cold iron key.")
    warnings = audit_turn("You hand him the branch, watching his face.", [], STATE, STATE)
    assert "the branch" in _detail(warnings, "item_handover_without_command")


def test_carried_item_names_resolve_to_their_noun():
    # @give files a pass as a quest_item, so the type alone cannot skip it
    for text in (
        "The guard takes the pass, squints at the seal, and waves you on.",
        "The patrol takes the north road at dawn.",          # Map of the North Road
        "Sorrel takes the lantern and lights the way.",       # her own, likely
    ):
        assert _flags(text, PACK) == [], text
    assert "item_handover_without_command" in _flags("He pockets your lantern without asking.", PACK)
    warnings = audit_turn("Hesk takes the map and folds it into his coat.", [], PACK, PACK)
    assert "item_handover_without_command" not in _checks(warnings)   # common prop needs "your"


def test_speech_only_reply_is_still_audited():
    spoken = _lines(*["Short line here."] * 5)
    assert "too_talky" in _checks(audit_turn("", [], STATE, STATE, spoken=spoken))
    spoken = [{"npc_name": "Piper Quillow", "text": "The howl came again.", "npc_id": "p1"}]
    assert "campaign_beat_not_delivered" not in _checks(
        audit_turn("", [], None, None, campaign=HOWL, spoken=spoken)
    )


def test_malformed_spoken_entries_do_not_crash_the_audit():
    spoken = ["not a dict", None, {"npc_name": "Vex Marrow", "text": None}]
    assert audit_turn("The square waits.", [], STATE, STATE, spoken=spoken,
                      player_name="Tamsin Rook", met_before=set()) == []


def _says(player_name, text, player_input=""):
    warnings = audit_turn("The lanterns gutter.", [], STATE, STATE, spoken=_lines(text),
                          player_name=player_name, met_before=set(), player_input=player_input)
    return "name_before_introduction" in _checks(warnings)


def test_surnames_that_are_words_or_places_are_not_the_player():
    assert not _says("Kael Wolf", "Turn back before Wolf's Hollow eats you.")
    assert not _says("Will Thorne", "Will you walk, or will you run?")
    assert not _says("Mara Grey", "The Grey Company rode north.")
    assert not _says("Mara Grey", "Osric Grey owes me money.")
    assert _says("Kael Wolf", "Walk away, Wolf.")
    assert _says("Mara Grey", "Mistress Grey. Sit.")
    assert _says("Sir Aldous Kent", "You're Kent, aren't you.")


def test_titles_in_the_players_name_are_not_names():
    assert not _says("Sir Aldous Kent", "Sir, the gate is closed.")
    assert not _says("Captain Mara Vell", "Captain Coldiron won't help you.")


def test_engine_directives_do_not_count_as_the_player_giving_a_name():
    opening = "[OPENING] This is Bram Holloway's first moment in the world."
    assert _says("Bram Holloway", "Holloway. Thought so.", player_input=opening)
    assert not _says("Bram Holloway", "Holloway. Thought so.", player_input="I'm Bram Holloway.")
