"""The ledger after round 5 (fleet6-bram, -tamsin, -wenna): pay handed over
up front and then paid again, money and things moved only in prose, a letter
the DM wrote for itself, the evening howl told twice, the journal saying one
fact twice, and the @move that moved nothing.
"""

import pytest

from src.models import (
    DMState, Event, Location, LocationType, NPC, NPCTier, Player, Quest, QuestStatus, WorldClock,
    get_session,
)
from src.services import commands as commands_mod
from src.services import memory_files
from src.services.commands import CommandRunner
from src.services.scene_objects import normalize_objects, resolve_interaction
from src.tools.world_write.quests import reward_balance, reward_words
from src.web import turn_audit
from src.web.dialogue_markup import MetaSentenceFilter
from src.web.turn_audit import audit_turn

NOTICE_TEXT = ("TAKEN BY WOLVES — cart, driver unharmed, cargo gone, north road, third such this month. "
               "The Watch pays for true accounts of the howls.")
LETTER_TEXT = ("Tobin — your last letter frightened me. If the north road is as bad as you say, sell the "
               "cart and come home by the south road before the frost. — Nell")
TINCTURE = {"name": "Yarrow Tincture", "kind": "potion",
            "about": "Piper's own: green, bitter, and it closes a cut faster than it has any right to."}


@pytest.fixture()
def town(db):
    """The square with Piper (whose job pays 10 coin and a tincture) and the
    notice board, the Lantern with its locked satchel letter, and the howl
    scheduled for eight."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=35))
        session.add(DMState(tension="low"))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=1, discovered=True, visited=True,
                          objects=normalize_objects([{"kind": "read", "label": "Notice board",
                                                      "payload": {"text": NOTICE_TEXT}}]))
        session.add(square)
        session.flush()
        tavern = Location(name="The Drowned Lantern", type=LocationType.BUILDING, parent_id=square.id, depth=2,
                          discovered=True, objects=normalize_objects([{
                              "kind": "open", "label": "Lost traveler's satchel",
                              "payload": {"lock": "ask her first", "item": {
                                  "name": "Water-Stained Letter", "type": "quest_item",
                                  "description": "Addressed to a man the town buried three weeks ago.",
                                  "reads": LETTER_TEXT}}}]))
        elsewhere = Location(name="Mill", type=LocationType.POI, depth=1, discovered=True)
        session.add_all([tavern, elsewhere])
        session.flush()
        piper = NPC(name="Piper Quillow", tier=NPCTier.MINOR, profession="Herbalist's apprentice",
                    current_location_id=square.id)
        hesk = NPC(name="Hesk Bramble", tier=NPCTier.MINOR, profession="Hawker", current_location_id=square.id)
        session.add_all([piper, hesk])
        session.flush()
        quest = Quest(title="Counting the Howls", description="Mark the howls for Piper.",
                      status=QuestStatus.ACTIVE, assigned_by_npc_id=piper.id, scale="job",
                      rewards={"currency": 10, "items": [dict(TINCTURE)]},
                      objectives=[{"id": "o1", "text": "Stand out the next howl", "kind": "custom", "done": True}])
        session.add(quest)
        session.add(Event(name="The Evening Howl", event_type="meso", player_visible=True, scheduled_day=1,
                          scheduled_hour=20, description="At the stroke of eight, howling rises from the "
                          "northern hills — layered, answering itself.", locations_involved=[square.id]))
        player = Player(name="Tamsin Rook", current_location_id=square.id, currency=25,
                        active_quests=[quest.id])
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "tavern": tavern.id, "mill": elsewhere.id,
                "piper": piper.id, "hesk": hesk.id, "quest": quest.id}


def _runner(w):
    with get_session() as session:
        here = session.get(Player, w["player"]).current_location_id
        present = [n.id for n in session.query(NPC).filter(NPC.current_location_id == here).all()]
    return CommandRunner(w["player"], present_ids=present)


def _purse_and_pack(w):
    with get_session() as session:
        player = session.get(Player, w["player"])
        return player.currency, {i["name"]: i.get("quantity", 1) for i in player.inventory or []}


def _rewards(w):
    with get_session() as session:
        return dict(session.get(Quest, w["quest"]).rewards or {})


# ----------------------------------------------------------------------
# 1. Advance pay
# ----------------------------------------------------------------------

def test_pay_up_front_is_counted_and_completion_pays_only_the_balance(town):
    """fleet6-tamsin T6 won a roll to be paid up front (`@coin +10`, `@give
    Yarrow Tincture`); T11's @quest-done paid both again: 45 coin, two tinctures."""
    r = _runner(town)
    coin = r.run("@coin +10 | Piper pays up front")
    assert coin.ok and coin.message == ("counted as Counting the Howls's reward paid up front (10 coin); "
                                        "@quest-done will not pay it again")
    give = r.run("@give Yarrow Tincture | kind: potion | potion: heal_light | about: Piper's own")
    assert give.ok and "reward paid up front (the Yarrow Tincture)" in give.message
    assert _rewards(town)["advanced"] == {"currency": 10, "items": ["Yarrow Tincture"]}
    # The brief's quest line says what is already paid
    assert reward_words(_rewards(town), about=True) == (
        "nothing more (reward already paid up front: 10 coin and a Yarrow Tincture)")
    assert _purse_and_pack(town) == (35, {"Yarrow Tincture": 1})

    done = _runner(town).run("@quest-done Counting the Howls")
    # Nothing was left to pay: no "rest" for the DM to show changing hands
    assert done.ok and done.message.startswith(
        "paid nothing more — 10 coin and a Yarrow Tincture was paid up front and settled it")
    assert _purse_and_pack(town) == (35, {"Yarrow Tincture": 1})


def test_half_up_front_leaves_the_other_half_for_completion(town):
    r = _runner(town)
    assert "paid up front (5 coin)" in r.run("@coin +5 | half now, half when it's done").message
    assert reward_balance(_rewards(town))["currency"] == 5
    assert "a Yarrow Tincture (Piper's own" in reward_words(_rewards(town), about=True)
    assert "(reward already paid up front: 5 coin)" in reward_words(_rewards(town))
    done = _runner(town).run("@quest-done Counting the Howls")
    assert done.message.startswith("paid only the balance — 5 coin was paid up front; show the rest")
    assert _purse_and_pack(town) == (35, {"Yarrow Tincture": 1})


def test_a_sale_or_an_absent_giver_is_no_advance(town):
    r = _runner(town)
    sale = r.run("@coin +4 | Piper buys the sweetroot")
    assert sale.ok and not sale.message and "advanced" not in _rewards(town)
    with get_session() as session:
        session.get(NPC, town["piper"]).current_location_id = town["mill"]
        session.commit()
    away = CommandRunner(town["player"]).run("@coin +10")
    assert away.ok and not away.message and "advanced" not in _rewards(town)


def test_a_reward_paid_last_reply_is_not_paid_again(town):
    """fleet6-wenna T11-T13: @quest-done paid the tincture, the next reply
    showed it, an audit asked for a @give, and T13's @give made two."""
    assert _runner(town).run("@quest-done Counting the Howls").ok
    assert _purse_and_pack(town) == (35, {"Yarrow Tincture": 1})
    again = _runner(town)
    give = again.run("@give Yarrow Tincture | kind: potion")
    assert not give.ok and give.message.startswith('not paid twice — "Counting the Howls" already put the '
                                                    "Yarrow Tincture in their pack")
    coin = again.run("@coin +10 | Piper presses the coin into your hand")
    assert not coin.ok and "already put the 10 coin in their purse" in coin.message
    assert _purse_and_pack(town) == (35, {"Yarrow Tincture": 1})
    _runner(town)                                     # a reply with no commands
    late = _runner(town).run("@give Yarrow Tincture | kind: potion")   # three replies on: a new gift
    assert late.ok and _purse_and_pack(town)[1] == {"Yarrow Tincture": 2}


def test_recent_coin_from_someone_else_is_a_new_payment(town):
    _runner(town).run("@quest-done Counting the Howls")
    with get_session() as session:
        session.get(NPC, town["piper"]).current_location_id = town["mill"]
        session.commit()
    paid = CommandRunner(town["player"], present_ids=[town["hesk"]]).run("@coin +10 | Hesk pays for the rope")
    assert paid.ok and _purse_and_pack(town)[0] == 45


def _captain_with_his_own_job(w):
    """fleet6-tamsin T6 as it stood: Coldiron in the square too, his 60-coin
    job active since the opening."""
    with get_session() as session:
        captain = NPC(name="Brann Coldiron", tier=NPCTier.MAJOR, profession="Watch Captain",
                      current_location_id=w["square"])
        session.add(captain)
        session.flush()
        teeth = Quest(title="Teeth on the North Road", description="Find what drives the wolves.",
                      status=QuestStatus.ACTIVE, assigned_by_npc_id=captain.id, scale="mission",
                      rewards={"currency": 60, "items": ["Wolf-Tooth Charm"]})
        session.add(teeth)
        session.commit()
        return teeth.id


def test_the_giver_the_reason_names_is_the_one_paid_up_front(town):
    """Both jobs fit 10 coin; the reason says whose pay it is. Read as
    neither, T11's @quest-done paid Piper's 10 coin again."""
    teeth = _captain_with_his_own_job(town)
    # A bare payment that fits both is no telling
    bare = _runner(town).run("@coin +3")
    assert bare.ok and not bare.message
    # Named by trade works as well as by name
    assert "Teeth on the North Road's reward paid up front (5 coin)" in \
        _runner(town).run("@coin +5 | the Captain's advance").message
    res = _runner(town).run("@coin +10 | Piper pays up front")
    assert res.ok and "counted as Counting the Howls's reward paid up front (10 coin)" in res.message
    assert _rewards(town)["advanced"] == {"currency": 10}
    with get_session() as session:
        assert session.get(Quest, teeth).rewards["advanced"] == {"currency": 5}


def test_a_stable_hand_nearby_does_not_name_every_hand(town):
    """A trade's everyday head noun ("Stable hand") is nobody's name: the
    reason's "your hand" still leaves Piper's advance hers."""
    with get_session() as session:
        session.add(NPC(name="Sorrel Tack", tier=NPCTier.MINOR, profession="Stable hand",
                        current_location_id=town["square"]))
        session.commit()
    res = _runner(town).run("@coin +10 | pressed into your hand, up front")
    assert "paid up front (10 coin)" in res.message


def test_an_items_description_is_no_reason_to_refuse_its_advance(town):
    """`about:` describes the tincture; "a pinch of yarrow" is no theft."""
    give = _runner(town).run("@give Yarrow Tincture | kind: potion | about: Piper's own: a pinch of yarrow in "
                             "honey; it lifts a fever by morning")
    assert "reward paid up front (the Yarrow Tincture)" in give.message


def test_the_rest_when_you_are_back_is_still_an_advance(town):
    """Coin paid BACK is a refund; "the rest when you're back" is the deal."""
    res = _runner(town).run("@coin +5 | half now, the rest when you're back")
    assert "paid up front (5 coin)" in res.message
    refund = _runner(town).run("@coin +2 | Piper gives the coin back")
    assert refund.ok and not refund.message and _rewards(town)["advanced"] == {"currency": 5}


@pytest.mark.parametrize("line", [
    "@coin +8 | lifted from a drunk's purse",          # the thief persona's own work
    "@coin +5 | pickpocketed in the crowd",
    "@coin +6 | Hesk pays for the wolf pelt",          # someone else here, paying for something
])
def test_a_theft_or_another_persons_money_is_no_advance(town, line):
    _captain_with_his_own_job(town)
    res = _runner(town).run(line)
    assert res.ok and not res.message and "advanced" not in _rewards(town)
    with get_session() as session:
        assert all("advanced" not in (q.rewards or {}) for q in session.query(Quest).all())


# ----------------------------------------------------------------------
# 4. @move to where they already are
# ----------------------------------------------------------------------

def test_a_move_to_here_fails_and_names_npc_move_and_new_place(town):
    res = _runner(town).run("@move Market Square")
    assert not res.ok and res.result.get("already_here")
    assert "@npc-move <name> | Market Square" in res.message
    assert "@new-place <spot> then @move <spot>" in res.message


# ----------------------------------------------------------------------
# 5. Documents with text
# ----------------------------------------------------------------------

def _at_the_lantern(w):
    with get_session() as session:
        session.get(Player, w["player"]).current_location_id = w["tavern"]
        session.commit()


def test_an_opened_letter_is_quoted_whole_and_journaled_word_for_word(town):
    _at_the_lantern(town)
    r = CommandRunner(town["player"])
    res = r.run("@interact Lost traveler's satchel | through")
    assert res.ok and "found Water-Stained Letter" in res.message
    assert f'the Water-Stained Letter reads: "{LETTER_TEXT}" — quote this word for word' in res.message
    journal = memory_files.read_journal(town["player"])
    assert any(f'Water-Stained Letter reads: "{LETTER_TEXT}"' in e for e in journal), journal


def test_the_canvas_shows_the_letter_it_hands_over(town):
    _at_the_lantern(town)
    with get_session() as session:
        satchel = normalize_objects(session.get(Location, town["tavern"]).objects)[0]
    out = resolve_interaction(town["player"], town["tavern"], satchel["id"], unlocked=True)
    assert out["success"] and out["result"]["panel_title"] == "Water-Stained Letter"
    assert out["result"]["panel_text"] == LETTER_TEXT
    assert out["result"]["document"] == {"item": "Water-Stained Letter", "text": LETTER_TEXT}


def test_the_brief_shows_the_dm_the_letter_before_it_is_opened(town):
    from src.services.scene_brief import build_scene_brief

    _at_the_lantern(town)
    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, town["player"]))["text"]
    assert f'which reads, word for word: "{LETTER_TEXT}"' in brief


def test_an_authored_letter_text_is_kept_and_capped():
    rows = normalize_objects([{"kind": "open", "label": "Box",
                               "payload": {"item": {"name": "Note", "reads": "x" * 900, "slot": "weapon"}}}])
    item = rows[0]["payload"]["item"]
    assert len(item["reads"]) == 600 and "slot" not in item


def test_the_campaign_author_sees_what_the_world_already_says(town):
    """fleet6-tamsin: the notice says "driver unharmed", the spine authored a
    carter who came back dead, and the DM told the player both."""
    from src.agents.campaign_forge import build_campaign_brief

    brief = build_campaign_brief(town["player"])
    section = brief.split("## ALREADY WRITTEN", 1)[1]
    assert "every truth and beat must agree with them" in section
    assert 'Market Square · Notice board: "TAKEN BY WOLVES — cart, driver unharmed' in section
    assert "Lost traveler's satchel holds Water-Stained Letter" in section and "Tobin —" in section


def test_bookkeeping_filter_leaves_italic_and_quoted_text_alone():
    """fleet6-wenna T17: the letter's last sentence ("I'll have proof by the
    time you read this…*") opened like a plan and was dropped, closing italic
    and all."""
    meta = MetaSentenceFilter()
    text = ("The hand is careful: *If this reaches you, the road is worse than I said. Ask at the "
            "Lantern. I'll have proof by the time you read this...*\n\n"
            '"Stay here. I\'ll fetch the captain," she says.\n'
            "You fold the letter away. Let me check the exits next.\n")
    out = meta.feed(text) + meta.flush()
    assert "I'll have proof by the time you read this...*" in out
    assert "I'll fetch the captain,\" she says." in out
    assert "Let me check" not in out and meta.dropped == ["Let me check the exits next."]


# ----------------------------------------------------------------------
# 6. The clock: what is coming up
# ----------------------------------------------------------------------

def test_the_brief_says_the_howl_is_coming_and_has_not_happened(town):
    from src.services.scene_brief import build_scene_brief

    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, town["player"]))["text"]
    # Near its hour, in plain words (fleet8-tamsin T9: "not by a long count")
    assert ("Coming up: The Evening Howl at 20:00 — 25 minutes from now (well inside the hour; anyone who "
            "speaks of it knows how close it is). It has not happened yet; never narrate "
            "it, or anyone hearing or seeing it, before then; if the player waits for it, write @time 25m") in brief


# ----------------------------------------------------------------------
# 7. One fact, one journal line
# ----------------------------------------------------------------------

def test_a_journal_line_restating_the_engines_is_skipped(town):
    """fleet6-tamsin T7: the engine journaled the notice and the DM's
    @journal paraphrased it the same reply."""
    r = _runner(town)
    r.journal_mask = lambda text: text
    assert r.run("@interact Notice board").ok
    r.run("@journal Three carts taken by wolves on the north road this month — cargo gone, drivers left "
          "alive. The Watch is paying for true accounts of the howls.")
    r.run("@journal Piper says the howls repeat every third night.")
    ledger = r.ledger()
    journal = memory_files.read_journal(town["player"])
    assert len(journal) == 2 and journal[0].endswith(f'Notice board: "{NOTICE_TEXT}"')
    assert journal[1].endswith("Piper says the howls repeat every third night.")
    assert r.results[1].ok and "the journal already says it" in r.results[1].message
    assert any("the journal already says it" in line for line in ledger)


def test_without_a_mask_the_repeat_is_skipped_at_once(town):
    r = _runner(town)
    r.run("@interact Notice board")
    res = r.run("@journal The notice: three carts taken by wolves on the north road, cargo gone, "
                "the Watch pays for true accounts")
    assert res.ok and res.result.get("skipped") == "already_journaled"
    assert len(memory_files.read_journal(town["player"])) == 1


# ----------------------------------------------------------------------
# 2-4. The audit: money and things in prose, arrivals, rewards shown
# ----------------------------------------------------------------------

SILVER = {"name": "Greywater Silver Piece", "type": "misc", "quantity": 1}
GATE_PASS = {"name": "Gate Pass", "type": "quest_item", "quantity": 1}


def _state(*items, where="The Drowned Lantern"):
    return {"currency": 45, "inventory": [dict(i) for i in items], "health": "healthy",
            "location_id": "here", "location": where, "quests": []}


def _checks(text, before, player_input="", tools=()):
    warnings = audit_turn(text, [{"name": t, "status": "success"} for t in tools], before, before,
                          player_input=player_input)
    return {w["check"]: w["detail"] for w in warnings}


@pytest.mark.parametrize("prose, words", [
    # fleet6-tamsin T19: the player paid the Keeper 20 coin; no @coin -20
    ("The Keeper's fingers close over the coin without hurry, sliding it into some pocket of her apron.",
     "close over the coin"),
    # fleet6-tamsin T12: the player split Piper's pay back to her
    ("Piper's hand closes tight around the coin as you press it back toward splitting.",
     "closes tight around the coin"),
    # fleet6-tamsin T11
    ("She's already pressing coin and a second vial into your hands before you've asked for either.",
     "pressing coin and a second vial into your hands"),
])
def test_money_moved_in_prose_is_seen(prose, words):
    assert turn_audit._money_claim(prose) == words
    assert "money_without_command" in _checks(prose, _state())


def test_a_carried_coin_left_on_a_cart_and_kept_is_owed_a_hand():
    """fleet6-bram T15: "Keep that." — the Greywater coin set on Hesk's cart,
    and nobody wrote @hand; it came back out at T18 and T20."""
    prose = ("You set the fresh-minted coin on the cart and watch him stare at it like it might bite. The "
             "Hawker's hands have gone still on his twine again, saying nothing — but he doesn't push the coin "
             "away either, and that tells you enough for tonight.")
    checks = _checks(prose, _state(SILVER, where="Market Square"),
                     player_input='I set the fresh coin on his cart. "Keep that."')
    assert "money_without_command" not in checks
    assert "@hand Greywater Silver Piece | to: <person>" in checks["item_handover_without_command"]


def test_a_thing_only_set_down_to_be_seen_is_not_given():
    prose = "You set the cracked whistle on the table between you and wait for him to look at it."
    checks = _checks(prose, _state({"name": "Cracked Bone Whistle", "type": "quest_item"}))
    assert "item_handover_without_command" not in checks


def test_something_new_going_into_your_coat_is_owed_a_give():
    """fleet6-bram T5: Piper's parchment "goes into your coat beside the gate
    pass" — never @give'n, never in the pack."""
    prose = "The scrap goes into your coat beside the gate pass, charcoal already smudging grey."
    detail = _checks(prose, _state(GATE_PASS, where="Market Square"))["item_handover_without_command"]
    assert detail.startswith("Narration puts the scrap into the player's hands")
    own = "The pass goes into your coat, and you nod your thanks."
    assert "item_handover_without_command" not in _checks(own, _state(GATE_PASS, where="Market Square"))


@pytest.mark.parametrize("prose", [
    "Coldiron looks up from his ale as you reach the table, and whatever weariness sat on him sharpens.",
    "By the time you reach the bar, the Keeper's already watching you come.",
    "You reach the far end of the bar and lean on it.",
    "You step into the warmth and shake the cold off your shoulders.",
])
def test_crossing_the_room_is_no_arrival(prose):
    """fleet6-tamsin T15/T18, both inside the Drowned Lantern, where she was."""
    assert "arrival_without_move" not in _checks(prose, _state())


@pytest.mark.parametrize("prose, where", [
    ("You step into the Drowned Lantern and the warmth closes over you.", "Market Square"),
    ("You reach the stable yard as the light goes.", "Market Square"),
    ("At the gate he just waves you through.", "Wolf's Hollow"),
])
def test_a_real_arrival_still_needs_a_move(prose, where):
    assert "arrival_without_move" in _checks(prose, _state(where=where))


def test_youre_back_where_you_are_is_no_arrival():
    assert "arrival_without_move" not in _checks("You're back in Market Square, and it is quieter.",
                                                 _state(where="Market Square"))


@pytest.mark.parametrize("prose", [
    "You reach the Captain just as he turns to go.",
    "By the time you reach the Keeper, the mug is already poured.",
    "You reach the Ash-Cloaked Man before he can melt into the crowd.",
])
def test_reaching_a_person_is_no_arrival(prose):
    """A capitalised target is a place only when it is not someone's title:
    the warning holds back server._bring_speakers_here for the whole reply."""
    assert "arrival_without_move" not in _checks(prose, _state(where="Market Square"))


def test_stepping_into_an_inn_named_for_a_man_is_still_an_arrival():
    assert "arrival_without_move" in _checks("You step into the Drowned Man, out of the rain.",
                                             _state(where="Market Square"))


@pytest.mark.parametrize("prose", [
    # their own money put away — no payment either way
    "You count what is left, tucking your coins into your purse.",
    # the hawker's own takings on his own cart (the widened "close over")
    "Hesk's fingers close over the coins on his cart, counting the day's takings.",
])
def test_money_that_never_changed_hands_is_not_flagged(prose):
    assert "money_without_command" not in _checks(prose, _state(where="Market Square"))


@pytest.mark.parametrize("prose, given", [
    ("You set the cracked whistle on the table between you. Coldiron takes the measure of it, then of you.",
     False),
    ("You set the whistle on the table. He keeps the silence a long moment before he answers.", False),
    ("You set the whistle on the table. She takes the stool across from you and leans in.", False),
    ("You set the whistle on the table. He takes it, turns it over, and slips it into his coat.", True),
])
def test_only_taking_the_thing_itself_accepts_it(prose, given):
    """"Takes the measure", "keeps the silence", a seat "across from you" —
    nothing of the player's changed hands."""
    checks = _checks(prose, _state({"name": "Cracked Bone Whistle", "type": "quest_item"}))
    assert ("item_handover_without_command" in checks) is given


def test_keep_the_change_typed_is_not_the_thing_given():
    whistle = {"name": "Cracked Bone Whistle", "type": "quest_item"}
    prose = "You set the whistle on the table and wait."
    assert "item_handover_without_command" not in _checks(
        prose, _state(whistle), player_input='I set the whistle down. "Keep the change."')
    assert "item_handover_without_command" in _checks(
        prose, _state(whistle), player_input='I set the whistle down. "Keep it."')


def test_pocketing_the_coin_shows_the_pay():
    assert _unshown("You pocket the coins with a nod and a word of thanks.", 20) == []
    assert _unshown("The coins are heavier than you expected.", 20) == ["20 coin"]


def test_the_unopened_letter_is_told_by_its_phrases_not_its_loose_words():
    """The seed letter shares "road", "cart", "three" and "market" with every
    reply of a wolf story; read as a word cluster, ordinary talk of the north
    road flagged the satchel and marked it seen for good."""
    satchel = normalize_objects([{"kind": "open", "label": "Lost traveler's satchel", "payload": {
        "lock": "ask her first", "item": {"name": "Water-Stained Letter", "type": "quest_item",
                                          "description": "Addressed to a man the town buried three weeks ago.",
                                          "reads": LETTER_TEXT}}}])[0]

    def leaked(prose):
        warnings = audit_turn(prose, [], _state(), _state(), scene_objects=[satchel])
        return "object_contents_leak" in {w["check"] for w in warnings}

    assert not leaked("The north road has eaten three carts this month; the market will be thin come frost.")
    assert not leaked("If the north road is as bad as they say, nobody is selling carts this winter.")
    assert leaked("She begs him to sell the cart and come home, to settle his debts before the frost.")


def test_bookkeeping_in_italics_is_still_dropped():
    """The italic shield is for the fiction's own words; the model's planning
    set in italics was dropped before it and still is."""
    for line in ("*Let me move the player to the tavern first.*", "*(The quest is now active.)*",
                 "*Now I'll roll for the persuasion.*"):
        meta = MetaSentenceFilter()
        out = meta.feed(line + "\n") + meta.flush()
        assert meta.dropped and line not in out
    meta = MetaSentenceFilter("Wenna Cole")
    thought = "*I should turn back,* you think, but your feet keep going.\n"
    assert meta.feed(thought) + meta.flush() == thought and not meta.dropped


CHARM = {"name": "Wolf-Tooth Charm", "about": "A wolf's fang bound in brass wire on a Watch cord.",
         "potion": False}


def _unshown(text, coin=0, items=()):
    paid = [{"title": "Job", "coin": coin, "items": [i["name"] for i in items],
             "item_info": {i["name"]: i for i in items}}]
    return turn_audit._rewards_unshown(text, paid)


def test_a_reward_shown_by_its_own_description_or_vessel_is_shown():
    """fleet6-bram T20 showed the charm as "a brass-wired fang on a Watch
    cord"; fleet6-tamsin T11 the tincture as "a second vial"."""
    assert _unshown("He counts the coin into your hand, then presses a brass-wired fang on a Watch cord "
                    "into your other palm.", 60, [CHARM]) == []
    assert _unshown("She presses coin and a second vial into your hands.", 10,
                    [{"name": "Yarrow Tincture", "about": "", "potion": True}]) == []
    # Two of its words in idle talk are not a handover
    assert _unshown("The Watch keeps a cord of wolves at bay, he says.", 0, [CHARM]) == ["Wolf-Tooth Charm"]


def test_coin_in_letter_talk_is_not_pay():
    """fleet6-wenna T20: 20 coin paid with nobody handing it over; "mint marks
    on coin" in the letter talk kept the check quiet."""
    talk = ("Hollis. Greywater mint marks on coin that shouldn't exist on this road. He sets the letter "
            "down on the gatehouse table.")
    assert _unshown(talk, 20) == ["20 coin"]
    assert _unshown("Sixty coin, as promised.", 60) == []
    assert _unshown("He counts the silver into your palm.", 20) == []


def test_completion_after_an_advance_asks_only_for_the_balance(town):
    """reward_unnarrated reads the balance: what was paid up front was shown
    when it was paid."""
    r = _runner(town)
    r.run("@coin +10 | up front")
    with get_session() as session:
        quest = session.get(Quest, town["quest"])
        quest.status = QuestStatus.COMPLETED
        session.commit()
    before = {"quests": [{"id": town["quest"]}]}
    paid = turn_audit._paid_quests([{"name": "update_quest_status", "status": "success"}], before, {"quests": []})
    assert paid == [{"title": "Counting the Howls", "coin": 0, "items": ["Yarrow Tincture"],
                     "item_info": {"Yarrow Tincture": {"about": TINCTURE["about"], "potion": True}}}]


@pytest.fixture(autouse=True)
def _fresh_payouts():
    commands_mod._RECENT_PAYOUTS.clear()
    yield
    commands_mod._RECENT_PAYOUTS.clear()


# ----------------------------------------------------------------------
# 6. The seed's own howl, told early, happens once
# ----------------------------------------------------------------------

@pytest.fixture()
def seed(tmp_path):
    from src.data.playtest_seed import create_playtest_world
    from src.models import reset_engine

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    with get_session() as session:
        square = session.query(Location).filter_by(name="Market Square").one()
        player = Player(name="Wenna Cole", current_location_id=square.id)
        session.add(player)
        session.commit()
        player_id = player.id
    yield player_id
    reset_engine()


def test_the_seed_howl_told_at_ten_to_eight_is_the_howl(seed):
    """fleet6-wenna T8 (19:50): "The bell tolls eight… the howl rises… answers
    itself twice"; at T10 (20:15) it fired and was told again."""
    from src.services.scene_brief import build_scene_brief
    from src.services.world_tick import mark_event_delivery, run_world_tick

    with get_session() as session:
        brief = build_scene_brief(session, session.get(Player, seed))["text"]
    # the seed opens at 15:00 (fleet9: a 17:30 start barred the north road ten replies in)
    assert "Coming up: The Evening Howl at 20:00 (in 5 h 00 min)" in brief and "@time 300m" in brief
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.hour, clock.minute = 19, 50
        session.commit()
    told = mark_event_delivery(
        "The bell tolls eight, flat and small over the rooftops, and the horses go rigid at the rail. Then "
        "the howl rises, low and long, and answers itself twice just as Piper's chart promised.")
    assert told == ["The Evening Howl"]
    with get_session() as session:
        clock = session.query(WorldClock).first()
        assert (clock.hour, clock.minute) == (20, 0)
        howl = session.query(Event).filter_by(name="The Evening Howl").one()
        assert howl.narrated_to_player and howl.occurred_hour == 20
        brief = build_scene_brief(session, session.get(Player, seed))["text"]
    assert "Coming up: The Evening Howl" not in brief
    tick = run_world_tick(seed)
    assert all(e["name"] != "The Evening Howl" for e in tick["fired_events"] + tick["undelivered_events"])


def test_the_unopened_letter_told_in_prose_is_a_leak():
    """The brief now shows the DM the letter's words (to quote at @interact);
    told before the satchel is opened, they are its contents leaking."""
    satchel = normalize_objects([{"kind": "open", "label": "Lost traveler's satchel", "payload": {
        "lock": "ask her first", "item": {"name": "Water-Stained Letter", "type": "quest_item",
                                          "reads": LETTER_TEXT}}}])[0]
    prose = ("You can almost hear it already: sell the cart, come home by the south road before the "
             "frost — Nell's worry, folded small.")
    warnings = audit_turn(prose, [], _state(), _state(), scene_objects=[satchel])
    assert "object_contents_leak" in {w["check"] for w in warnings}
