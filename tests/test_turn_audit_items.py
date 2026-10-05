"""A carried thing changing hands in prose only (round 4, fleet5-bram T16).

"Coldiron takes the coin from your shaking hand… Instead he pockets the coin
himself." No @hand: the Greywater Silver Piece stayed in the pack (sellable
at Hesk's counter), and money_without_command told the DM to write
`@coin -N` — for an item. The money check now leaves "the coin" to the pack
when the pack carries a coin; item_handed_without_command asks for @hand.
"""

from src.web.turn_audit import audit_turn

SILVER = {"name": "Greywater Silver Piece", "type": "misc", "quantity": 1}
WHISTLE = {"name": "Cracked Bone Whistle", "type": "quest_item", "quantity": 1}
KNIFE = {"name": "Worn Knife", "type": "weapon", "slot": "weapon", "quantity": 1}
PASS = {"name": "Gate Pass", "type": "quest_item", "quantity": 1}

T16 = ("Coldiron takes the coin from your shaking hand and turns it once under the lantern light, and "
       "something in his face goes very still. His jaw works like he's chewing on the question you actually "
       "asked. Instead he pockets the coin himself, and calls over his shoulder toward the healer's stall.")


def _state(*items, coin=25):
    return {"currency": coin, "inventory": [dict(i) for i in items], "health": "healthy", "location_id": "sq"}


def _checks(text, before, after=None, tools=()):
    warnings = audit_turn(text, [{"name": t, "status": "success"} for t in tools], before, after or before)
    return {w["check"]: w["detail"] for w in warnings}


def test_fleet5_bram_t16_is_an_item_not_a_payment():
    checks = _checks(T16, _state(KNIFE, SILVER))
    assert "money_without_command" not in checks
    detail = checks["item_handed_without_command"]
    assert "@hand Greywater Silver Piece | to: <person>" in detail
    assert "Coldiron takes the coin" in detail


def test_without_a_carried_coin_it_is_still_money():
    checks = _checks(T16, _state(KNIFE))
    assert "money_without_command" in checks
    assert "item_handed_without_command" not in checks


def test_counted_coin_is_money_even_with_a_coin_in_the_pack():
    text = "Hesk's hand closes around the ten coin and the knife is yours."
    checks = _checks(text, _state(SILVER))
    assert "money_without_command" in checks


def test_a_carried_clue_kept_by_name():
    # ("pockets the whistle" was already item_handover_without_command's)
    checks = _checks("Coldiron palms the whistle without a word.", _state(WHISTLE))
    assert "@hand Cracked Bone Whistle" in checks.get("item_handed_without_command", ""), checks
    checks = _checks("He snatches your bone whistle and turns away.", _state(WHISTLE))
    assert "item_handed_without_command" in checks


def test_handed_back_or_their_own_or_handed_over_properly_is_clean():
    looked = "Coldiron takes the coin, turns it over twice, and hands it back."
    assert "item_handed_without_command" not in _checks(looked, _state(SILVER))
    own = "Coldiron takes the whistle from his belt and blows one thin note."
    assert "item_handed_without_command" not in _checks(own, _state(WHISTLE))
    purse = "She keeps the coin purse close to her ribs."
    assert "item_handed_without_command" not in _checks(purse, _state(SILVER))
    handed = _checks(T16, _state(KNIFE, SILVER), _state(KNIFE), tools=("transfer_item",))
    assert "item_handed_without_command" not in handed and "money_without_command" not in handed


def test_the_player_keeping_their_own_is_not_a_handover():
    text = "You take the whistle back out and turn it in the light. Then you pocket the coin again."
    assert "item_handed_without_command" not in _checks(text, _state(WHISTLE, SILVER))


def test_weapons_and_passes_are_not_asked_for():
    text = "The guard takes the pass, squints, and waves you on. Coldiron takes the knife from you."
    checks = _checks(text, _state(KNIFE, PASS))
    assert "item_handed_without_command" not in checks


def test_one_warning_when_the_handover_check_already_caught_it():
    text = "You hand Coldiron the whistle. He pockets the whistle."
    checks = _checks(text, _state(WHISTLE))
    assert "item_handover_without_command" in checks
    assert "item_handed_without_command" not in checks


def test_a_paid_coin_is_the_payment_not_the_carried_piece():
    """The purse moved this reply (@coin): "the barman pockets the coin" was
    that payment — asking for @hand Greywater Silver Piece would hand away
    the clue the player still holds."""
    paid = _state(SILVER, coin=24)
    for text in ("The barman pockets the coin and pours.", "Hesk takes the silver and slides the bread across."):
        checks = _checks(text, _state(SILVER), paid, tools=("adjust_currency",))
        assert "item_handed_without_command" not in checks, (text, checks)
        assert "money_without_command" not in checks
    # The piece by its own name is still the piece, paid or not
    checks = _checks("Coldiron palms the whistle.", _state(SILVER, WHISTLE), _state(SILVER, WHISTLE, coin=24),
                     tools=("adjust_currency",))
    assert "@hand Cracked Bone Whistle" in checks.get("item_handed_without_command", "")


def test_an_unpaid_coin_kept_says_it_may_have_been_a_fare():
    detail = _checks("The ferryman takes the coin you offer.", _state(SILVER))["item_handed_without_command"]
    assert "@hand Greywater Silver Piece" in detail and "@coin -N instead" in detail
    whistle = _checks("Coldiron palms the whistle.", _state(WHISTLE))["item_handed_without_command"]
    assert "@coin" not in whistle, "a whistle is never a fare"
