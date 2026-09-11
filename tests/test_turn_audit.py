"""Reality audit: code-action suppression and the checks added by the overhaul.

The pre-existing checks are covered by `tests/test_turn_filters.py`, which must
keep passing unmodified — this module covers the new keyword-only parameters
(`code_actions`, `campaign`) and the four new checks.
"""

from src.web import pending_actions
from src.web.turn_audit import audit_turn

STATE = {"health": "healthy", "inventory": [], "location_id": "loc1", "currency": 10}


def _tools(*names):
    return [{"name": n, "status": "success"} for n in names]


def _checks(warnings):
    return {w["check"] for w in warnings}


# ---------------------------------------------------------------------------
# code_actions — the single suppression mechanism
# ---------------------------------------------------------------------------

ARRIVAL = "You step into the tavern's heat and the door swings shut behind you."


def test_arrival_claim_suppressed_by_code_move():
    warnings = audit_turn(ARRIVAL, [], STATE, STATE, code_actions={"move_player"})
    assert "arrival_without_move" not in _checks(warnings)


def test_arrival_claim_still_fires_without_code_action():
    # Guards against over-suppression: no ledger entry, no excuse
    warnings = audit_turn(ARRIVAL, [], STATE, STATE)
    assert "arrival_without_move" in _checks(warnings)
    warnings = audit_turn(ARRIVAL, [], STATE, STATE, code_actions=None)
    assert "arrival_without_move" in _checks(warnings)


def test_item_claim_suppressed_by_code_take():
    text = "The oilcloth is added to your pack, stiff with old salt."
    assert "item_without_tool" in _checks(audit_turn(text, [], STATE, STATE))
    warnings = audit_turn(text, [], STATE, STATE, code_actions={"add_to_inventory"})
    assert "item_without_tool" not in _checks(warnings)


def test_combat_result_ledger_suppresses_damage_check():
    # /api/combat/result applies health BEFORE the [COMBAT OUTCOME] turn, so
    # state_before == state_after and the DM's wound prose used to be flagged
    text = "The wolf rakes your arm — you are bleeding badly."
    assert "damage_without_tool" in _checks(audit_turn(text, [], STATE, STATE))

    pending_actions.record_action("p-audit-combat", "combat", "fight resolved: victory",
                                  tools=("update_player_health", "kill_npc"))
    code_actions = pending_actions.tool_equivalents(pending_actions.drain("p-audit-combat"))
    warnings = audit_turn(text, [], STATE, STATE, code_actions=code_actions)
    assert "damage_without_tool" not in _checks(warnings)


def test_campaign_category_labels_translate_to_tools():
    text = "QUEST ACCEPTED: The Long Howl"
    assert "quest_banner_without_tool" in _checks(audit_turn(text, [], STATE, STATE))
    warnings = audit_turn(text, [], STATE, STATE, code_actions={"quest"})
    assert "quest_banner_without_tool" not in _checks(warnings)


def test_every_category_label_suppresses_its_check():
    cases = [
        ("consequence", "Consequences applied: Coldiron's disposition is now hostile.",
         "consequences_without_tools"),
        ("health", "The wolf rakes your arm — you are bleeding badly.", "damage_without_tool"),
        ("arrival", ARRIVAL, "arrival_without_move"),
        ("item", "The knife is added to your inventory.", "item_without_tool"),
    ]
    for label, text, check in cases:
        assert check in _checks(audit_turn(text, [], STATE, STATE)), check
        warnings = audit_turn(text, [], STATE, STATE, code_actions={label})
        assert check not in _checks(warnings), label


def test_default_signature_unchanged():
    # Four positional args — every existing caller and test compiles as-is
    warnings = audit_turn("The market hums around you.", [], STATE, STATE)
    assert warnings == []


# ---------------------------------------------------------------------------
# arrival_without_move — the phrases a refused door produces
# ---------------------------------------------------------------------------

def test_talked_past_the_door_still_needs_the_move():
    for text in ("The doorman reads your coat, then your face, and you are waved through.",
                 "Maera tips her chin at the back stair and you are let in."):
        assert "arrival_without_move" in _checks(audit_turn(text, [], STATE, STATE)), text


def test_waved_in_is_suppressed_by_a_real_move():
    text = "You are waved inside before the words are out of your mouth."
    warnings = audit_turn(text, _tools("move_player"), STATE, STATE)
    assert "arrival_without_move" not in _checks(warnings)


def test_ordinary_waving_prose_stays_clean():
    for text in ("You are waved at from across the square.",
                 "A dockhand waves you off and goes back to his rope.",
                 "You are let down gently, and the rope goes slack."):
        assert "arrival_without_move" not in _checks(audit_turn(text, [], STATE, STATE)), text


# ---------------------------------------------------------------------------
# risky_action_without_check
# ---------------------------------------------------------------------------

def test_risky_resolution_without_roll_check_flags():
    warnings = audit_turn("You slip past the guard unseen.", [], STATE, STATE)
    assert "risky_action_without_check" in _checks(warnings)


def test_risky_resolution_with_roll_check_is_clean():
    warnings = audit_turn("You slip past the guard unseen.", _tools("roll_check"), STATE, STATE)
    assert "risky_action_without_check" not in _checks(warnings)


def test_risky_resolution_during_combat_is_clean():
    warnings = audit_turn(
        "You slip past the guard unseen.", _tools("start_combat"), STATE, STATE
    )
    assert "risky_action_without_check" not in _checks(warnings)


def test_convince_yourself_is_not_flagged():
    warnings = audit_turn(
        "You convince yourself the shape in the fog is only a fishing pole.",
        [], STATE, STATE,
    )
    assert "risky_action_without_check" not in _checks(warnings)


def test_npc_on_npc_prose_is_not_flagged():
    warnings = audit_turn("The smuggler slips past the guard.", [], STATE, STATE)
    assert "risky_action_without_check" not in _checks(warnings)


def test_distant_you_does_not_anchor_the_check():
    # The 40-character anchor keeps unrelated sentences apart
    text = ("You wait by the rail for a long while, watching the tide turn and the "
            "lamps come up one by one, and somewhere behind you a courier slips past "
            "the gate.")
    assert "risky_action_without_check" not in _checks(audit_turn(text, [], STATE, STATE))


def test_persuasion_resolution_flags():
    warnings = audit_turn("You manage to convince the harbourmaster to look away.",
                          [], STATE, STATE)
    assert "risky_action_without_check" in _checks(warnings)


# ---------------------------------------------------------------------------
# mechanics_in_narration (R10: progression + enemy-tiers merged into one check)
# ---------------------------------------------------------------------------

def test_xp_numbers_in_narration_flagged():
    assert "mechanics_in_narration" in _checks(
        audit_turn("You gain 40 experience.", [], STATE, STATE)
    )


def test_damage_numbers_in_narration_flagged():
    for text in ("Your axe lands for 12 damage.", "The brute is down to HP 7.",
                 "You take 3 hit points of damage."):
        assert "mechanics_in_narration" in _checks(audit_turn(text, [], STATE, STATE)), text


def test_bare_level_words_flagged():
    for text in (
        "You level up.",
        "You are now level 3.",
        "You feel stronger than you were this morning.",
        "That fight was worth a lot of XP.",
        "You have earned enough experience points for it to show.",
    ):
        assert "mechanics_in_narration" in _checks(audit_turn(text, [], STATE, STATE)), text


def test_mechanics_check_fires_even_with_code_actions():
    # A REAL level-up still may not be narrated mechanically
    warnings = audit_turn(
        "You level up — the blade feels lighter now.", [], STATE, STATE,
        code_actions={"update_player_health", "use_item"},
    )
    assert "mechanics_in_narration" in _checks(warnings)


def test_level_up_prose_without_numbers_is_clean():
    warnings = audit_turn("Your hands are steadier than they were.", [], STATE, STATE)
    assert warnings == []


def test_ordinary_prose_with_numbers_is_clean():
    warnings = audit_turn(
        "Three gulls ride the mast. The tide is two hours out and the water level "
        "has dropped below the pilings.",
        [], STATE, STATE,
    )
    assert warnings == []


# ---------------------------------------------------------------------------
# cinematic_text_in_narration
# ---------------------------------------------------------------------------

def test_audit_flags_printed_title_card():
    warnings = audit_turn("ACT II — THE LONG DARK\n\nYou walk on.", [], STATE, STATE)
    assert "cinematic_text_in_narration" in _checks(warnings)


def test_audit_flags_screen_directions():
    for text in (
        "The screen fades and the gate closes behind you.",
        "Cut to black. The horn is still sounding.",
        "A title card would not do it justice.",
    ):
        assert "cinematic_text_in_narration" in _checks(audit_turn(text, [], STATE, STATE)), text


def test_audit_ignores_ordinary_prose():
    text = ("The act of climbing takes an hour; the fog cuts the light to a chapter "
            "of grey.")
    assert "cinematic_text_in_narration" not in _checks(audit_turn(text, [], STATE, STATE))


# ---------------------------------------------------------------------------
# Campaign checks (only when a campaign dict is passed)
# ---------------------------------------------------------------------------

BEAT = {
    "beat": {"beat_id": "b1", "title": "The Howl Answers",
             "delivery_keywords": ["howl", "north road"]},
    "sealed_truths": [],
}

TRUTH = {
    "beat": None,
    "sealed_truths": [
        {"id": "t1", "spoiler_keywords": ["Vell", "brother", "ledger"]},
        {"id": "t2", "spoiler_keywords": ["mine"]},
    ],
}


def test_audit_flags_undelivered_beat():
    warnings = audit_turn("The market hums around you.", [], None, None, campaign=BEAT)
    assert "campaign_beat_not_delivered" in _checks(warnings)
    detail = next(w for w in warnings if w["check"] == "campaign_beat_not_delivered")["detail"]
    assert "The Howl Answers" in detail


def test_audit_ignores_delivered_beat():
    warnings = audit_turn(
        "A howl rolls down off the ridge and the market goes quiet.",
        [], None, None, campaign=BEAT,
    )
    assert "campaign_beat_not_delivered" not in _checks(warnings)


def test_beat_check_silent_without_campaign():
    warnings = audit_turn("The market hums around you.", [], None, None)
    assert warnings == []


def test_audit_flags_truth_frontrun():
    text = "Vell keeps a ledger, and the name of his brother is the first line in it."
    assert "campaign_truth_frontrun" in _checks(
        audit_turn(text, [], None, None, campaign=TRUTH)
    )


def test_partial_spoiler_keywords_are_clean():
    text = "Vell keeps a ledger nobody is allowed to read."
    assert "campaign_truth_frontrun" not in _checks(
        audit_turn(text, [], None, None, campaign=TRUTH)
    )


def test_single_keyword_truth_never_fires():
    # A truth with one keyword is not precise enough to be a conjunction
    text = "The mine has been quiet since spring."
    assert "campaign_truth_frontrun" not in _checks(
        audit_turn(text, [], None, None, campaign=TRUTH)
    )


def test_empty_turn_text_is_clean():
    assert audit_turn("", [], STATE, STATE, campaign=BEAT) == []


# ---------------------------------------------------------------------------
# item claims in the forms the live DM actually used
# ---------------------------------------------------------------------------

def test_taking_an_item_from_an_npc_needs_a_tool():
    text = "I take the brass token from Captain Coldiron, the metal warm from his hand."
    assert "item_without_tool" in _checks(audit_turn(text, [], STATE, STATE))
    text2 = "He hands you the gate pass without a word."
    assert "item_without_tool" in _checks(audit_turn(text2, [], STATE, STATE))
    text3 = "She presses a small iron key into your palm."
    assert "item_without_tool" in _checks(audit_turn(text3, [], STATE, STATE))


def test_item_claim_is_clean_when_the_bag_changed():
    text = "He hands you the gate pass without a word."
    after = {**STATE, "inventory": [{"name": "Gate Pass"}]}
    assert "item_without_tool" not in _checks(audit_turn(text, [], STATE, after))


def test_taking_a_look_is_not_an_item():
    text = "You take a long look at the square from the well. The wind takes the lantern smoke."
    assert "item_without_tool" not in _checks(audit_turn(text, [], STATE, STATE))
