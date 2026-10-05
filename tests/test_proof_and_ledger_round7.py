"""Proof, refusals and the ledger after round 7 (fleet8-bram, -tamsin, -wenna).

- The phantom-proof loop (fleet8-wenna T13-T20): the DM put the Hollow's
  whistle on a dead stand-in wolf; refusals named no place, the prose kept
  claiming it by pronoun, then planted it in the yard's mud. Now: refusals
  name the real place and a clean way out, a [SETTLE THIS FIRST] line opens
  the next context, the aftermath lists what bodies really carry, and the
  audits read "it" after a refusal and leave denials alone.
- The reach guard holds for @give through a Thing (fleet8-tamsin T15), and
  an engine input is nobody reaching.
- Ledger slips: a line applied twice in one reply, "x0" (fleet8-tamsin T9).
- Paid twice from another room (fleet8-wenna T17/T18), and an advance
  charged back to its giver (fleet8-wenna T4).
- Payoffs off screen: [PAY THIS REPLY] and a payout made apart from its giver.
"""

from types import SimpleNamespace

import pytest

from src.models import (
    DMState, Location, LocationType, NPC, NPCTier, Player, Quest, QuestStatus, WorldClock, get_session,
)
from src.services import commands as cmds
from src.services import settle
from src.services.commands import CommandError, CommandRunner
from src.services.scene_objects import normalize_objects
from src.web import turn_audit
from src.web.turn_audit import carried_not_held, refused_but_narrated

WHISTLE = {"kind": "take", "label": "Cracked bone whistle", "x": 60, "y": 74,
           "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                                "description": "Split down the barrel — silent now."}}}
SCRATCHES = {"kind": "read", "label": "Scratches on the hut wall", "x": 30, "y": 40,
             "payload": {"text": "Tally marks, gouged in threes. Under them, one word, carved deep and old: SING."}}
TINCTURE = {"name": "Yarrow Tincture", "kind": "potion", "about": "Piper's own: green and bitter."}


@pytest.fixture()
def world(db):
    """The square (Piper, whose job pays 10 coin and a tincture; the Captain,
    whose job wants the whistle), the Stable Yard, and Wolf's Hollow with the
    whistle and the scratches."""
    settle.reset()
    with get_session() as session:
        session.add(WorldClock(day=1, hour=21, minute=0))
        session.add(DMState(tension="low"))
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True, visited=True)
        yard = Location(name="Stable Yard", type=LocationType.POI, discovered=True, visited=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True,
                          objects=normalize_objects([WHISTLE, SCRATCHES]))
        session.add_all([square, yard, hollow])
        session.flush()
        piper = NPC(name="Piper Quillow", tier=NPCTier.MINOR, profession="Herbalist's apprentice",
                    current_location_id=square.id)
        captain = NPC(name="Captain Brann Coldiron", tier=NPCTier.MAJOR, profession="Watch captain",
                      current_location_id=square.id)
        session.add_all([piper, captain])
        session.flush()
        howls = Quest(title="Counting the Howls", status=QuestStatus.ACTIVE, assigned_by_npc_id=piper.id,
                      scale="job", rewards={"currency": 10, "items": [dict(TINCTURE)]},
                      objectives=[{"id": "o1", "text": "Stand out the next howl", "kind": "custom", "done": True}])
        teeth = Quest(title="Teeth on the North Road", status=QuestStatus.ACTIVE, assigned_by_npc_id=captain.id,
                      scale="mission", rewards={"currency": 60},
                      objectives=[{"text": "Reach Wolf's Hollow", "kind": "go_to", "target_id": hollow.id},
                                  {"text": "Bring the cracked bone whistle back to the Captain", "kind": "deliver",
                                   "item_name": "Cracked Bone Whistle", "target_id": captain.id}])
        session.add_all([howls, teeth])
        session.flush()
        player = Player(name="Wenna Cole", current_location_id=square.id, currency=25,
                        inventory=[{"id": "willow_bark_draught", "name": "Willow-Bark Draught", "type": "consumable",
                                    "quantity": 1}], active_quests=[howls.id, teeth.id])
        session.add(player)
        session.commit()
        ids = {"player": player.id, "square": square.id, "yard": yard.id, "hollow": hollow.id,
               "piper": piper.id, "captain": captain.id, "howls": howls.id, "teeth": teeth.id}
    yield ids
    settle.reset()


def _runner(w, said=None):
    with get_session() as session:
        here = session.get(Player, w["player"]).current_location_id
        present = [n.id for n in session.query(NPC).filter(NPC.current_location_id == here).all()]
    runner = CommandRunner(w["player"], present_ids=present)
    runner.player_input = said
    return runner


def _place(w, where):
    with get_session() as session:
        session.get(Player, w["player"]).current_location_id = w[where]
        session.commit()


def _purse_and_pack(w):
    with get_session() as session:
        player = session.get(Player, w["player"])
        return player.currency, {i["name"]: i.get("quantity", 1) for i in player.inventory or []}


# --- 3. Ledger slips ------------------------------------------------------------

def test_a_count_of_nought_is_an_error_not_one(world):
    """fleet8-tamsin T9: `@give Willow-Bark Draught x0` made the pack's one draught two."""
    with pytest.raises(CommandError):
        cmds.split_quantity("Willow-Bark Draught x0")
    with pytest.raises(CommandError):
        cmds.split_quantity("0x Torch")
    res = _runner(world).run("@give Willow-Bark Draught x0 | kind: potion")
    assert not res.ok and "x0 is nothing" in res.message
    assert _purse_and_pack(world)[1] == {"Willow-Bark Draught": 1}
    assert cmds.split_quantity("Torch x3") == ("Torch", 3)
    # Both fleet x0 lines meant "none" (a cancelled line; fleet7-wenna T3
    # "given away this turn"): the answer never invites a rewrite with no count
    assert "leave it so" in res.message and "no count for one" not in res.message


def test_the_same_line_twice_in_one_reply_is_applied_once(world):
    """fleet8-tamsin T9: the tossed coin line, written again after a roll, cost two coins."""
    r = _runner(world)
    line = "@coin -1 | tossed to Sorrel for his watch tonight"
    assert r.run(line).ok
    again = r.run(line)
    assert not again.ok and again.message.startswith("already applied this reply")
    assert r.run("@coin -1 | and one more for the horses").ok       # a different line is a different payment
    assert r.run("@give Lantern Oil | kind: misc").ok
    assert not r.run("@give Lantern Oil | kind: misc").ok
    assert _purse_and_pack(world) == (23, {"Willow-Bark Draught": 1, "Lantern Oil": 1})
    # A new reply is a new ledger
    assert _runner(world).run(line).ok
    assert _purse_and_pack(world)[0] == 22


# --- 2. The reach guard on @give through a Thing ---------------------------------

def test_give_through_a_thing_needs_the_players_reach(world):
    """fleet8-tamsin T15: `@interact` refused ("did not reach for it"), then
    `@give Cracked Bone Whistle` took it through the Thing."""
    _place(world, "hollow")
    said = "Whatever that whistle is, it's bait. I need higher ground before I touch anything."
    r = _runner(world, "I freeze and scan the hollow's edges.")
    assert r.run("@interact Cracked bone whistle").result["refused"] == "not_reached_for"
    give = r.run("@give Cracked Bone Whistle | kind: clue")
    assert not give.ok and give.result["refused"] == "not_reached_for"
    assert give.message.startswith("the player did not reach for it")
    assert "Cracked Bone Whistle" not in _purse_and_pack(world)[1]
    # Naming it is no reach since round 10 (fleet10-bram T14: the crate named,
    # its silver pocketed) — and this player says they touch nothing yet
    assert not _runner(world, said).run("@give Cracked Bone Whistle | kind: clue").ok
    assert _runner(world, "I crouch and pick up the whistle.").run("@give Cracked Bone Whistle | kind: clue").ok


def test_an_engine_input_is_nobody_reaching(world):
    """fleet8-bram T5: the aftermath took the whistle; "the player can search"
    in the [COMBAT OUTCOME] text is the engine's word, not the player's."""
    _place(world, "hollow")
    outcome = ("[COMBAT OUTCOME — resolved in real-time by the player, mechanics already applied: victory. "
               "What they carried is still on them, where they fell — a body on the canvas the player can search.]")
    res = _runner(world, outcome).run("@give Cracked Bone Whistle | kind: clue")
    assert not res.ok and res.result["refused"] == "not_reached_for"
    assert not _runner(world, "[OPENING] This is their first moment.").run("@interact Cracked bone whistle").ok


# --- 1(b)(c). Refusals that name the real place and a way out --------------------

def test_a_refused_give_offers_the_lesser_thing_and_names_the_place(world):
    """fleet8-wenna T14/T15."""
    _place(world, "yard")
    res = _runner(world, "I work the pale knotted thing free.").run("@give Cracked Bone Whistle | kind: clue")
    assert not res.ok and res.result["refused"] == "authored_elsewhere"
    assert res.result["place"] == "Wolf's Hollow"
    assert res.message.startswith("that is the cracked bone whistle at Wolf's Hollow")
    assert ("If they found something here, it is a different, lesser thing — @give it under its own name "
            "(kind: clue), never another whistle — the real one still waits at Wolf's Hollow") in res.message
    # ...and the lesser thing goes in under its own name
    assert _runner(world).run("@give Knotted Bone Toggle | kind: clue").ok


def test_a_refused_interact_names_the_item_and_where_it_lies(world):
    """fleet8-wenna T18/T20: "there is nothing at Stable Yard to search" never
    said where the whistle was, and the prose kept it in the yard's mud."""
    _place(world, "yard")
    res = _runner(world, "I kneel and retrieve the whistle.").run("@interact Cracked Bone Whistle | through")
    assert not res.ok and res.result["refused"] == "authored_elsewhere"
    assert res.message.startswith("that is the cracked bone whistle at Wolf's Hollow, not here — nothing was taken")
    # A label naming nothing authored still gets the plain answer
    other = _runner(world, "I search the straw.").run("@interact Straw pile")
    assert not other.ok and "there is nothing at Stable Yard" in other.message


def test_a_refused_hand_says_where_the_proof_still_lies(world):
    res = _runner(world).run("@hand Cracked Bone Whistle | to: Captain Brann Coldiron")
    assert not res.ok and res.message.startswith("no item in their pack called 'Cracked Bone Whistle'")
    assert "still lies at Wolf's Hollow" in res.message and res.result["refused"] == "authored_elsewhere"


# --- 1(a). [SETTLE THIS FIRST] ----------------------------------------------------

def test_settle_line_once_per_item_and_again_only_when_claimed(world):
    _place(world, "yard")
    r = _runner(world, "I work it free.")
    r.run("@give Cracked Bone Whistle | kind: clue")
    settle.note_reply(world["player"], r.results, [])
    line = settle.settle_line(world["player"])
    assert line.startswith("[SETTLE THIS FIRST: the Cracked Bone Whistle is not here and never was — it lies at "
                           "Wolf's Hollow. In one sentence say what the player really has (an empty cord")
    assert "never another whistle) and never place the Cracked Bone Whistle anywhere else.]" in line
    assert settle.settle_line(world["player"]) == ""                    # spent
    # Refused again, the prose honest: nothing new to settle
    again = _runner(world, "I look.")
    again.run("@interact Cracked Bone Whistle")
    settle.note_reply(world["player"], again.results, [])
    assert settle.settle_line(world["player"]) == ""
    # ...claimed again: settled again
    claim = {"check": "carried_not_held", "detail": "Your prose has the player carrying the Cracked Bone Whistle"}
    settle.note_reply(world["player"], [], [claim])
    assert settle.settle_line(world["player"]).startswith("[SETTLE THIS FIRST: the Cracked Bone Whistle")


def test_settle_line_is_dropped_once_play_settles_it(world):
    _place(world, "yard")
    r = _runner(world, "I take it.")
    r.run("@give Cracked Bone Whistle | kind: clue")
    settle.note_reply(world["player"], r.results, [])
    _place(world, "hollow")                       # standing right where it lies: theirs to take in play
    assert settle.settle_line(world["player"]) == ""


def test_the_settle_line_opens_the_context(monkeypatch):
    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import DMOrchestrator

    monkeypatch.setattr(dm_orchestrator, "get_player", lambda pid: {"name": "Wenna", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda *a, **k: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)
    monkeypatch.setattr(dm_orchestrator, "offer_directive", lambda *a, **k: "")
    monkeypatch.setattr(settle, "settle_line", lambda pid: "[SETTLE THIS FIRST: the X is not here and never was.]")
    monkeypatch.setattr(settle, "pay_lines", lambda pid, present, strangers: ['[PAY THIS REPLY: Piper pays.]'])
    fake = SimpleNamespace(context=SimpleNamespace(player_id="p1"), _player_name="Wenna")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- none"]
    context = DMOrchestrator._build_context(fake, "I show her the whistle.")
    assert context.startswith("Current context:\n\n[SETTLE THIS FIRST: the X is not here and never was.]\n\n"
                              "### THE PLAYER")
    assert context.index("[PAY THIS REPLY: Piper pays.]") < context.index("Player says:")
    ambush = DMOrchestrator._build_context(fake, "I wait.", constrained=True)
    assert "[SETTLE THIS FIRST" not in ambush and "[PAY THIS REPLY" not in ambush


# --- 1(d). The aftermath lists what the bodies carry ------------------------------

def test_the_combat_outcome_lists_what_each_body_carries(world):
    from src.services.combat import apply_combat_result, combat_outcome_message

    with get_session() as session:
        wolf = NPC(name="Torn-Ear Wolf", tier=NPCTier.AMBIENT, profession="wolf", danger_tier="weak",
                   current_location_id=world["square"])
        cutthroat = NPC(name="Ash-Cloaked Cutthroat", tier=NPCTier.MINOR, profession="cutthroat",
                        danger_tier="standard", current_location_id=world["square"],
                        inventory_notable=[{"name": "Notched Knife", "type": "weapon"}])
        session.add_all([wolf, cutthroat])
        session.commit()
        ids = [wolf.id, cutthroat.id]
    changes = apply_combat_result(world["player"], "victory", defeated_npc_ids=ids)
    message = combat_outcome_message(changes)
    # A beast leaves its trophy now (fleet10-bram T13/T17: "carry nothing
    # worth taking", and a wolf fight paid nothing a merchant would buy)
    assert ("On the fallen, per the engine (for you; the player learns it by searching): Torn-Ear Wolf carries "
            "Wolf Fang") in message
    assert "Ash-Cloaked Cutthroat carries Notched Knife" in message
    assert "Nothing else is on them — no cord, token, note or clue; put nothing else on a body." in message
    assert not any(ch.isdigit() for ch in message)
    assert "whistle" not in message.lower()         # the proof lying elsewhere is never named


# --- 1(e). The audits: pronoun claims, denials ------------------------------------

REFUSED_GIVE = [{"line": "@give Cracked Bone Whistle | kind: clue", "ok": False,
                 "message": "that is the cracked bone whistle at Wolf's Hollow — the player finds it with "
                            "@interact there; nothing was given"}]
UNHELD = [{"item": "Cracked Bone Whistle", "thing": "Cracked bone whistle", "place": "Wolf's Hollow"}]


@pytest.mark.parametrize("prose", [
    # fleet8-wenna T14, T15, T20 — every one a pronoun
    "The cord gives with a soft tug, and the thing in your palm is lighter than you expect.",
    "Sorrel watches you tuck it away with the look of a man who'd like to un-know the last ten minutes.",
    "This time the whistle comes free of the mud with a soft suck, and you wrap it twice in cloth.",
])
def test_a_claim_by_pronoun_after_a_refusal_is_caught(prose):
    warnings = refused_but_narrated(REFUSED_GIVE, prose, here="Stable Yard")
    assert [w["check"] for w in warnings] == ["refused_but_narrated"]
    # The fix names the real place, never this scene's mud
    assert "it lies at Wolf's Hollow and nowhere else — never move it into this scene" in warnings[0]["detail"]
    assert carried_not_held(prose, [], UNHELD, refused=["Cracked Bone Whistle"])["check"] == "carried_not_held"


@pytest.mark.parametrize("prose", [
    # fleet8-wenna T16 and T19: the honest repairs
    "You reach into your satchel for the whistle — and come up with nothing but a fold of empty cloth. "
    "The memory of it is sharp in your hands. It isn't there.",
    "Your fingers close around nothing but cloth. The whistle isn't there — you never actually lifted it from "
    "the mud, only meant to.",
    "You come away empty-handed; it is still lying where it fell.",
])
def test_a_denial_is_not_a_claim(prose):
    assert carried_not_held(prose, [], UNHELD, refused=["Cracked Bone Whistle"]) is None
    assert refused_but_narrated(REFUSED_GIVE, prose) == []


@pytest.mark.parametrize("prose", [
    "You notice a loose bundle of sweetroot by the crate, and you pocket it.",   # "it" is the bundle
    "The whistle is there for the taking, if you take it.",                       # an offer
    "He presses coin into your hand and counts it himself.",                     # "it" is the coin
    "The wind finds every gap in your coat; you take it in stride.",
])
def test_it_is_only_the_thing_when_nothing_else_is(prose):
    assert refused_but_narrated(REFUSED_GIVE, prose) == []
    assert carried_not_held(prose, [], UNHELD, refused=["Cracked Bone Whistle"]) is None


@pytest.mark.parametrize("prose", [
    # The empty "it" of the hour and the moment is no thing in the palm
    "It takes a moment for the warmth to come back into your fingers.",
    "It is late, and the cold has got into your hands.",
    "It's well past ten, and you shove both hands into your coat pockets.",
])
def test_the_empty_it_of_the_hour_is_not_the_thing(prose):
    assert refused_but_narrated(REFUSED_GIVE, prose) == []
    assert carried_not_held(prose, [], UNHELD, refused=["Cracked Bone Whistle"]) is None


@pytest.mark.parametrize("prose", [
    # The repair [SETTLE THIS FIRST] and the refusal ask for, then "it" is
    # the lesser thing — never the whistle the DM was just told to drop
    "What you worked free of the wolf's neck is only a plain bone toggle. You turn it over in your palm, frowning.",
    "The pale thing on the cord is a plain bone toggle, nothing more. You slip it into your pocket.",
    "Up close it is no whistle at all, just a bone toggle on a greasy cord. You pocket it.",
    "You pocket the toggle. It sits cold in your palm a moment first.",
])
def test_the_lesser_thing_said_instead_is_what_it_means(prose):
    assert refused_but_narrated(REFUSED_GIVE, prose) == []
    assert carried_not_held(prose, [], UNHELD, refused=["Cracked Bone Whistle"]) is None
    # ...while the thing itself, said to be the thing, is still the claim,
    # and so is "it" after what someone else is
    for claim in ("The thing in your palm is a whistle, pale as old tooth.",
                  "Sorrel is a statue at the rail. He watches you tuck it away."):
        assert carried_not_held(claim, [], UNHELD, refused=["Cracked Bone Whistle"])["check"] == "carried_not_held"


def test_without_a_refusal_it_is_not_read_as_the_thing():
    assert carried_not_held("Sorrel watches you tuck it away.", [], UNHELD) is None


# --- 2. A beat that orders a reveal is no leak -----------------------------------

def test_objects_a_beat_directive_names_are_exempt():
    """fleet8-tamsin T15: the beat said 'the word SING carved beneath them exactly
    as written'; object_contents_leak told the DM never to mention SING."""
    objects = normalize_objects([SCRATCHES, WHISTLE, {"kind": "search", "label": "Split supply crate",
                                                       "payload": {"item": {"name": "Greywater Silver Piece"}}}])
    directive = ("Have her find the tally marks gouged in threes on the hut wall and the word SING carved "
                 "beneath them exactly as written, before she pockets the whistle.")
    named = {o["label"] for o in turn_audit.beat_named_objects(objects, directive)}
    assert named == {"Scratches on the hut wall", "Cracked bone whistle"}
    assert turn_audit.beat_named_objects(objects, "") == []


# --- 4. Paid twice, from another room ---------------------------------------------

def test_the_balance_restated_from_another_room_is_not_paid_again(world):
    """fleet8-wenna T17/T18: @quest-done paid the 10 coin as she left; the next
    reply, in the Stable Yard, `@coin +10 | Piper's remaining payment` paid it again."""
    assert _runner(world).run("@quest-done Counting the Howls").ok
    assert _purse_and_pack(world)[0] == 35
    _place(world, "yard")                                      # Piper is not here
    res = _runner(world).run("@coin +10 | Piper's remaining payment for the howl-count")
    assert not res.ok and "already put the 10 coin in their purse" in res.message
    assert _purse_and_pack(world)[0] == 35
    # Someone else's ten coin, said as theirs, is new money
    assert _runner(world).run("@coin +10 | a stable boy's wager won").ok


def test_an_unshown_payout_holds_the_coin_whoever_is_here(world):
    r = _runner(world)
    assert r.run("@quest-done Counting the Howls").ok
    cmds.note_reward_unnarrated(world["player"], ["Counting the Howls"])
    _place(world, "yard")
    res = _runner(world).run("@coin +10 | counted out earlier")
    assert not res.ok and "not paid twice" in res.message


def test_coin_pushed_back_is_no_payout_restated(world):
    assert _runner(world).run("@quest-done Counting the Howls").ok
    assert _runner(world).run("@coin +10 | Piper returns the coin").ok


def test_the_giver_returning_with_the_pay_is_the_payout_again(world):
    """[PAID WHILE APART] asks for the pay to arrive with the giver: the coin
    coming back is a refund, the giver coming back with it is not."""
    assert _runner(world).run("@quest-done Counting the Howls").ok
    _place(world, "yard")
    res = _runner(world).run("@coin +10 | Piper returns with the rest of your pay")
    assert not res.ok and "not paid twice" in res.message
    assert _purse_and_pack(world)[0] == 35


def test_an_advance_charged_back_to_its_giver_is_flagged(world):
    """fleet8-wenna T4: the tincture booked as Piper's pay up front, and
    `@coin -10 | paid Piper for the tincture` in the same reply."""
    with get_session() as session:
        session.get(Quest, world["howls"]).status = QuestStatus.NOT_STARTED
        session.commit()
    r = _runner(world)
    assert r.run("@coin -10 | paid Piper for the tincture and her tally").ok
    give = r.run("@give Yarrow Tincture | kind: potion | potion: heal_light")
    assert give.ok and "paid up front" in give.message
    warnings = turn_audit.advance_bought_warnings(r.results, r.advances)
    assert [w["check"] for w in warnings] == ["advance_bought"]
    assert "Pay up front is never bought" in warnings[0]["detail"]
    assert "(@coin +10 | Piper returns the coin)" in warnings[0]["detail"]
    # A payment to someone else is their business
    other = _runner(world)
    other.run("@coin -2 | paid Hesk for rope")
    assert turn_audit.advance_bought_warnings(other.results, r.advances) == []


# --- 5. Payoffs on screen ----------------------------------------------------------

def test_a_finished_job_with_its_giver_here_is_paid_this_reply(world):
    present = {world["piper"]: "Piper Quillow", world["captain"]: "Captain Brann Coldiron"}
    lines = settle.pay_lines(world["player"], present)
    assert len(lines) == 1
    assert lines[0].startswith('[PAY THIS REPLY: Piper Quillow pays for "Counting the Howls" (10 coin and a '
                               'Yarrow Tincture')
    assert lines[0].endswith("— every step is done. Write @quest-done Counting the Howls and show the coin "
                             "counted into their hand, in this scene, before anyone walks off.]")
    # A stranger keeps their role label
    stranger = settle.pay_lines(world["player"], present, {world["piper"]: "Herbalist's Apprentice"})
    assert stranger[0].startswith("[PAY THIS REPLY: the Herbalist's Apprentice (Piper Quillow — a name the player "
                                  "has not learned: keep the role label) pays")
    assert settle.pay_lines(world["player"], present) == []          # pushed twice: the quest log alone now
    # Teeth is not done; nobody is pushed for it
    settle.reset()
    assert settle.pay_lines(world["player"], {world["captain"]: "Captain Brann Coldiron"}) == []


def test_a_payout_made_apart_from_its_giver_arrives_as_a_message(world):
    """fleet8-tamsin T11/T12: "the pay settles into your pocket before you reach the gate"."""
    _place(world, "yard")
    r = _runner(world)
    assert r.run("@quest-done Counting the Howls").ok
    assert r.paid_away and r.paid_away[0]["title"] == "Counting the Howls"
    settle.note_reply(world["player"], r.results, [], paid_apart=r.paid_away)
    lines = settle.pay_lines(world["player"], {})
    assert len(lines) == 1 and lines[0].startswith('[PAID WHILE APART: "Counting the Howls" (10 coin and a Yarrow '
                                                   'Tincture')
    # Never met by name: the giver away from the scene keeps their role label too
    assert "is already paid — the engine put it in their purse while the " in lines[0]
    assert "(Piper Quillow — a name the player has not learned: keep the role label)" in lines[0]
    assert "as a message or an arrival" in lines[0] and "never 'already among your things'" in lines[0]
    assert settle.pay_lines(world["player"], {}) == []                 # said once


def test_an_unshown_payout_with_the_giver_gone_arrives_as_a_message(world):
    """fleet8-wenna T17/T18: paid in the square, unshown; next reply she is in the yard."""
    r = _runner(world)
    assert r.run("@quest-done Counting the Howls").ok and not r.paid_away
    unshown = {"check": "reward_unnarrated", "detail": "Your @quest-done for \"Counting the Howls\" paid…"}
    settle.note_reply(world["player"], r.results, [unshown], paid_apart=r.paid_away)
    assert settle.pay_lines(world["player"], {world["piper"]: "Piper Quillow"}) == []   # still here: the audit asks
    settle.note_reply(world["player"], r.results, [unshown], paid_apart=r.paid_away)
    lines = settle.pay_lines(world["player"], {})
    assert lines and lines[0].startswith('[PAID WHILE APART: "Counting the Howls"')


def test_reward_unnarrated_never_suggests_the_pack(world):
    warning = turn_audit.reward_unnarrated_warning("She nods.", [{"title": "Counting the Howls", "coin": 10,
                                                                  "items": [], "item_info": {}}])
    assert "find it in their pack" not in warning["detail"]
    assert "never 'already among your things'" in warning["detail"]


# --- Through /api/play: the refusal, the audit, the next context -------------------

class _FakeAgent:
    def __init__(self, events):
        self.events = events
        self.messages = []

    async def stream_async(self, context):
        for event in self.events:
            yield event


class _FakeDM:
    def __init__(self, events, present):
        self.agent = _FakeAgent(events)
        self.last_brief = {"present": present}
        self._player_name = "Wenna Cole"

    def _build_context(self, player_input, **_kwargs):
        return "THE SCENE\nPlayer says: " + player_input


def _play(world, monkeypatch, reply, player_input):
    import asyncio
    import json

    from src.services import memory_files
    from src.web import server
    from src.web.streaming import ToolUsageTracker

    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    dm = _FakeDM([{"data": reply}], {})
    monkeypatch.setattr(server, "get_or_create_session", lambda pid: {"dm": dm, "tool_tracker": ToolUsageTracker()})
    monkeypatch.setattr(server, "_engine_ambush", lambda *a, **k: None)
    monkeypatch.setattr(server, "maybe_advance_world", lambda: None)
    server._pending_audit_feedback.pop(world["player"], None)

    async def run():
        response = await server.play_sse(server.GameRequest(player_input=player_input, player_id=world["player"]))
        return [chunk async for chunk in response.body_iterator]

    frames = []
    for chunk in asyncio.run(run()):
        for line in str(chunk).splitlines():
            if line.startswith("data: ") and line[6:].strip() not in ("", "{}"):
                frames.append(json.loads(line[6:]))
    return next(f for f in frames if f.get("type") == "complete")


def test_a_phantom_proof_reply_is_named_and_settled_first_next_turn(world, monkeypatch):
    """fleet8-wenna T15, end to end: the refused @give, "you tuck it away",
    and the next context's binding line."""
    _place(world, "yard")
    complete = _play(world, monkeypatch,
                     "@give Cracked Bone Whistle | kind: clue\nSorrel watches you tuck it away, jaw tight.\n",
                     "I tuck the whistle carefully into my satchel.")
    assert [c["ok"] for c in complete["commands"]] == [False]
    assert "never another whistle" in complete["commands"][0]["message"]
    refused = [w for w in complete["audit"] if w["check"] == "refused_but_narrated"]
    assert refused and "it lies at Wolf's Hollow and nowhere else" in refused[0]["detail"]
    assert settle.settle_line(world["player"]).startswith(
        "[SETTLE THIS FIRST: the Cracked Bone Whistle is not here and never was — it lies at Wolf's Hollow.")
    assert settle.recent_refusals(world["player"]) == ["Cracked Bone Whistle"]
