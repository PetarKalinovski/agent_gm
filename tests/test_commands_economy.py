"""Pins for what the fleet4 playtest (Bram, Tamsin, Wenna on the Emberfall
seed) found in the economy and the fight commands: quest rewards and finds
paid twice through @give/@rep, a reward charm merged into an inert
duplicate, a sheaf of notes worn as a combat trinket, the boss chained into a
[COMBAT OUTCOME] aftermath with no campaign to hold him back, and coin with
nothing visible to spend it on."""

import pytest

from src.models import (
    Campaign, DMState, Faction, Location, LocationType, NPC, Player, Quest, QuestStatus, WorldClock,
    get_session,
)
from src.services import memory_files
from src.services.commands import CommandRunner, _is_document, answers_combat_outcome
from src.services.scene_objects import normalize_objects
from src.tools.world_write.player import add_to_inventory
from src.tools.world_write.quests import reward_item


@pytest.fixture()
def hollow(db):
    """Wolf's Hollow with its authored crate, the boss, a scout and a wolf,
    the Watch, and a level-1 player carrying the starting knife."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=0))
        session.add(DMState(tension="low"))
        watch = Faction(name="The Lantern Watch")
        session.add(watch)
        place = Location(
            name="Wolf's Hollow", type=LocationType.POI, depth=0, discovered=True, visited=True,
            objects=normalize_objects([
                {"kind": "search", "label": "Split supply crate",
                 "payload": {"item": {"name": "Greywater Silver Piece", "type": "misc", "value": 12}}},
            ]),
        )
        session.add(place)
        session.flush()
        aldric = NPC(name="Aldric Vane", current_location_id=place.id, danger_tier="boss",
                     combat_archetype="brute")
        vex = NPC(name="Vex Marrow", current_location_id=place.id, danger_tier="dangerous",
                  combat_archetype="stalker")
        wolf = NPC(name="Hollow Wolf", current_location_id=place.id, danger_tier="weak")
        player = Player(name="Bram Holloway", current_location_id=place.id, currency=25)
        session.add_all([aldric, vex, wolf, player])
        session.commit()
        ids = {"player": player.id, "watch": watch.id, "hollow": place.id,
               "aldric": aldric.id, "vex": vex.id, "wolf": wolf.id}
    add_to_inventory(ids["player"], "Worn Knife", item_type="weapon", slot="weapon", tier="crude")
    return ids


def _runner(w, **kw):
    return CommandRunner(w["player"], present_ids=[w["aldric"], w["vex"], w["wolf"]], **kw)


def _pack(w):
    with get_session() as session:
        return {i["name"]: i for i in session.get(Player, w["player"]).inventory or []}


def _equipped(w):
    with get_session() as session:
        return dict(session.get(Player, w["player"]).equipment or {})


def _quest(w, rewards):
    with get_session() as session:
        session.add(Quest(title="Teeth on the North Road", description="Bring proof.",
                          status=QuestStatus.ACTIVE, rewards=rewards,
                          objectives=[{"text": "Report back", "kind": "custom", "done": True}]))
        session.commit()


# ----------------------------------------------------------------------
# 1. Paid once: @quest-done's items and standing, @interact's finds
# ----------------------------------------------------------------------

def test_a_quest_reward_item_and_standing_are_never_paid_twice(hollow):
    """fleet4-bram T10 / -wenna T14: @quest-done, then `@give Wolf-Tooth
    Charm` and `@rep Lantern Watch +10` — two charms and Watch standing 70."""
    _quest(hollow, {"currency": 60, "items": ["Wolf-Tooth Charm"], "reputation": {hollow["watch"]: 10}})
    r = _runner(hollow)
    assert r.run("@quest-done Teeth on the North Road").ok
    charm = r.run("@give Wolf-Tooth Charm")
    assert not charm.ok and "not paid twice" in charm.message and "Wolf-Tooth Charm" in charm.message
    spelled = r.run("@give wolf tooth charm x1")                # the slug, not the spelling
    assert not spelled.ok and "not paid twice" in spelled.message
    rep = r.run("@rep Lantern Watch +10")
    assert not rep.ok and "already moved The Lantern Watch by +10" in rep.message
    assert r.run("@rep Lantern Watch -5").ok                    # a slight after payday still lands
    assert _pack(hollow)["Wolf-Tooth Charm"]["quantity"] == 1
    with get_session() as session:
        assert session.get(Player, hollow["player"]).reputation[hollow["watch"]] == 50 + 10 - 5
    assert any("not paid twice" in line for line in r.ledger())


def test_a_plain_reward_restated_as_gear_becomes_that_gear_not_a_second_one(hollow):
    """The DM's kind and tier are what the plain reward really is: the one
    charm becomes a common trinket and goes on, and no second one appears."""
    _quest(hollow, {"items": ["Wolf-Tooth Charm"]})
    r = _runner(hollow)
    assert r.run("@quest-done Teeth on the North Road").ok
    fix = r.run("@give Wolf-Tooth Charm | kind: trinket | tier: common | about: A fang in brass wire.")
    assert fix.ok and "not paid twice" in fix.message and "a common trinket now" in fix.message
    charm = _pack(hollow)["Wolf-Tooth Charm"]
    assert charm["quantity"] == 1 and charm["slot"] == "trinket" and charm["tier"] == "common"
    assert charm["description"] == "A fang in brass wire."
    assert _equipped(hollow).get("trinket") == charm["id"]


def test_a_find_from_interact_is_not_given_again_in_the_same_reply(hollow):
    """fleet4-bram T9: the crate's silver, then `@give Greywater Silver Piece
    | kind: trinket` — a second coin, and a coin worn as a charm."""
    r = _runner(hollow)
    assert r.run("@interact Split supply crate").ok
    again = r.run("@give Greywater Silver Piece | kind: trinket | tier: common")
    # (dropped quietly since round 10: the pickup happened, nothing failed —
    # fleet10-bram T15)
    assert again.ok and again.result.get("skipped") == "already_found" and not again.message
    silver = _pack(hollow)["Greywater Silver Piece"]
    assert silver["quantity"] == 1 and silver.get("slot") is None
    # The guard is this reply's; the next reply's DM sees the pack
    assert CommandRunner(hollow["player"]).run("@give Greywater Silver Piece").ok


# ----------------------------------------------------------------------
# 2. One name, one thing: a @give that disagrees with the pack says so
# ----------------------------------------------------------------------

def test_gear_onto_a_plain_entry_regrades_it(hollow):
    # (A "Lucky Coin" here once; coin is never gear since fleet5-tamsin T12)
    add_to_inventory(hollow["player"], "Rabbit's Foot")
    res = _runner(hollow).run("@give Rabbit's Foot | kind: trinket | tier: common")
    assert res.ok and "already in their pack as plain stuff" in res.message
    foot = _pack(hollow)["Rabbit's Foot"]
    assert foot["quantity"] == 1 and foot["slot"] == "trinket" and foot["value"] > 0


def test_different_gear_under_a_carried_name_is_refused_with_the_fix(hollow):
    res = _runner(hollow).run("@give Worn Knife | kind: weapon | tier: fine")
    assert not res.ok
    assert "they already carry a crude weapon called Worn Knife" in res.message
    assert "@give Fine Worn Knife | kind: weapon | tier: fine" in res.message
    knife = _pack(hollow)["Worn Knife"]
    assert knife["quantity"] == 1 and knife["tier"] == "crude"


def test_evidence_restated_as_gear_stays_evidence(hollow):
    """The Hollow's whistle as `kind: trinket` a reply after it was found:
    no renamed-copy advice — "Common Cracked Bone Whistle" would be a second,
    wearable whistle."""
    add_to_inventory(hollow["player"], "Cracked Bone Whistle", item_type="quest_item")
    res = _runner(hollow).run("@give Cracked Bone Whistle | kind: trinket | tier: common")
    assert not res.ok and "as a clue or key — it stays one" in res.message
    assert "e.g." not in res.message
    whistle = _pack(hollow)["Cracked Bone Whistle"]
    assert whistle["quantity"] == 1 and whistle["type"] == "quest_item" and not _equipped(hollow).get("trinket")


def test_a_plain_give_onto_gear_stacks_and_says_what_it_joined(hollow):
    res = _runner(hollow).run("@give Worn Knife")
    assert res.ok and "stacked onto the Worn Knife they already carry (a crude weapon)" in res.message
    assert _pack(hollow)["Worn Knife"]["quantity"] == 2


def test_the_same_thing_again_stacks_silently(hollow):
    res = _runner(hollow).run("@give Worn Knife | kind: weapon | tier: crude")
    assert res.ok and not res.message
    assert _pack(hollow)["Worn Knife"]["quantity"] == 2


# ----------------------------------------------------------------------
# 3. Paper is a clue, never gear
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name, paper", [
    ("Piper's Howl Notes", True), ("Ledger Page", True), ("Letter of Marque", True),
    ("Map of the Hollow", True), ("Coldiron's Sealed Letter", True), ("Watch Roster", True),
    ("Page's Dagger", False), ("Charm for Luck", False), ("Wolf-Tooth Charm", False),
    ("Notched Blade", False),
    # fleet-tamsin T5's paper scrap; a stick and a killer's blade are not paper
    ("Piper's Howl Tally", True), ("Sealed Orders", True), ("Tally Stick", False),
    ("Contract Killer's Blade", False),
])
def test_paper_is_read_off_the_head_noun(name, paper):
    assert _is_document(name) is paper


def test_notes_given_as_a_trinket_are_kept_as_a_clue(hollow):
    """fleet4-tamsin T15: `@give Piper's Howl Notes | kind: trinket | tier:
    common` became her combat trinket."""
    res = _runner(hollow).run("@give Piper's Howl Notes | kind: trinket | tier: common")
    assert res.ok and "is paper — kept as a clue" in res.message
    notes = _pack(hollow)["Piper's Howl Notes"]
    assert notes["type"] == "quest_item" and notes.get("slot") is None and notes.get("tier") is None
    assert not _equipped(hollow).get("trinket")


def test_clue_is_a_kind_and_paper_with_no_kind_is_one_too(hollow):
    r = _runner(hollow)
    assert r.run("@give Toll Ledger | kind: clue | about: Three weeks of crossings.").ok
    assert r.run("@give Torn Map").ok and not r.results[-1].message
    assert r.run("@give Page's Dagger | kind: weapon | tier: crude").ok
    pack = _pack(hollow)
    assert pack["Toll Ledger"]["type"] == "quest_item" and pack["Torn Map"]["type"] == "quest_item"
    assert pack["Page's Dagger"]["slot"] == "weapon"


def test_a_quest_item_is_never_auto_equipped(hollow):
    """Whatever slot an author gave it: a clue or key is carried, not worn."""
    add_to_inventory(hollow["player"], "Sealed Writ", item_type="quest_item", slot="trinket", tier="common")
    assert not _equipped(hollow).get("trinket")


# ----------------------------------------------------------------------
# 4. An aftermath is not a new fight
# ----------------------------------------------------------------------

def test_the_combat_outcome_turn_is_recognised():
    assert answers_combat_outcome("[COMBAT OUTCOME — resolved in real-time by the player: victory.")
    assert answers_combat_outcome("  [COMBAT OUTCOME] defeat")
    assert not answers_combat_outcome("I attack the wolf. [COMBAT OUTCOME]")
    assert not answers_combat_outcome(None)


def test_an_aftermath_reply_starts_no_fight_and_spends_no_entrance(hollow, monkeypatch):
    """fleet4-bram T7: the wolves' [COMBAT OUTCOME] reply wrote `@boss Aldric
    Vane` + `@fight Aldric Vane, Vex Marrow` — a certain loss nobody chose."""
    import src.services.commands as commands

    started, played = [], []
    monkeypatch.setattr(commands, "start_combat", lambda **kw: started.append(kw) or {"event": "combat_start"})
    monkeypatch.setattr(commands, "play_cinematic", lambda **kw: played.append(kw) or {"success": True})
    r = _runner(hollow, after_combat=True)
    boss = r.run("@boss Aldric Vane")
    fight = r.run("@fight Aldric Vane, Vex Marrow")
    for res in (boss, fight):
        assert not res.ok and "no new one starts" in res.message and "@fight next turn" in res.message
    assert started == [] and played == [] and r.pending_boss == {}
    assert not any("saved" in line for line in r.ledger())


# ----------------------------------------------------------------------
# 5. The boss stays out of a group fight, spine or no spine
# ----------------------------------------------------------------------

def _fight(w, monkeypatch, line):
    import src.services.commands as commands

    started = {}
    monkeypatch.setattr(commands, "start_combat",
                        lambda player_id, npc_ids: started.setdefault("ids", npc_ids) and
                        {"event": "combat_start", "enemies": npc_ids})
    res = _runner(w).run(line)
    return res, started.get("ids")


def _campaign(w, status, act=1, villain=None):
    with get_session() as session:
        session.add(Campaign(player_id=w["player"], title="The Drilled Pack", status=status,
                             current_act=act, villain_npc_id=villain))
        session.commit()


@pytest.mark.parametrize("status", [None, "pending", "failed"])
def test_without_a_spine_the_boss_tier_is_held_back(hollow, monkeypatch, status):
    """fleet4-bram: pt_i's campaign had failed, the guard returned nothing,
    and Aldric (boss) went in beside Vex against a winded level-1 player."""
    if status:
        _campaign(hollow, status)
    res, ids = _fight(hollow, monkeypatch, "@fight Aldric Vane, Vex Marrow")
    assert res.ok and ids == [hollow["vex"]]
    assert "Aldric Vane did not join this fight" in res.message


def test_without_a_spine_ordinary_enemies_fight_together(hollow, monkeypatch):
    res, ids = _fight(hollow, monkeypatch, "@fight Vex Marrow, Hollow Wolf")
    assert res.ok and set(ids) == {hollow["vex"], hollow["wolf"]} and not res.message


def test_an_active_spine_holds_its_villain_and_the_boss_tier(hollow, monkeypatch):
    _campaign(hollow, "active", act=1, villain=hollow["vex"])
    res, ids = _fight(hollow, monkeypatch, "@fight Aldric Vane, Vex Marrow, Hollow Wolf")
    assert res.ok and ids == [hollow["wolf"]]
    assert "Aldric Vane" in res.message and "Vex Marrow" in res.message


@pytest.mark.parametrize("status, act", [("active", 3), ("won", 2), ("lost", 1)])
def test_the_last_act_or_a_finished_story_holds_nobody(hollow, monkeypatch, status, act):
    _campaign(hollow, status, act=act, villain=hollow["aldric"])
    res, ids = _fight(hollow, monkeypatch, "@fight Aldric Vane, Vex Marrow")
    assert res.ok and set(ids) == {hollow["aldric"], hollow["vex"]}


def test_the_boss_alone_is_a_fight_the_player_chose(hollow, monkeypatch):
    res, ids = _fight(hollow, monkeypatch, "@fight Aldric Vane")
    assert res.ok and ids == [hollow["aldric"]]


# ----------------------------------------------------------------------
# Structured reward items (the seed's charm, a DM's dict)
# ----------------------------------------------------------------------

def test_reward_items_speak_gives_words():
    authored = {"name": "Wolf-Tooth Charm", "kind": "trinket", "tier": "common", "about": "A fang."}
    charm = reward_item(authored)
    assert (charm.slot, charm.tier, charm.type, charm.description) == ("trinket", "common", "misc", "A fang.")
    assert authored == {"name": "Wolf-Tooth Charm", "kind": "trinket", "tier": "common", "about": "A fang."}
    assert reward_item("Old Coin").type == "misc" and reward_item("Old Coin").slot is None
    assert reward_item({"name": "Yarrow Tincture", "kind": "potion"}).effect_tag == "heal_light"
    assert reward_item({"name": "Toll Ledger", "kind": "clue"}).type == "quest_item"
    assert reward_item({"name": "Iron Sword", "kind": "weapon"}).tier == "common"
    assert reward_item("  ") is None


# ----------------------------------------------------------------------
# 7. Money should feel spendable: the counter says what coin would buy
# ----------------------------------------------------------------------

@pytest.fixture()
def market(db, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")
    monkeypatch.setattr(memory_files, "BACKGROUND_SUMMARY", False)
    from src.data.playtest_seed import _garment, _ware

    with get_session() as session:
        session.add(WorldClock(day=1, hour=10, minute=0))
        session.add(DMState(tension="low"))
        square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0,
                          discovered=True, visited=True)
        session.add(square)
        session.flush()
        hesk = NPC(name="Hesk Bramble", profession="hawker", current_location_id=square.id, inventory_notable=[
            _ware("Gutting Knife", "weapon", "crude", description="Short."),
            _ware("Woodsman's Hatchet", "weapon", "common", description="Iron on ash."),
            _ware("Padded Jerkin", "armor", "common", description="Quilted."),
            _ware("Ashwood Blade", "weapon", "fine", description="Pale."),
            _ware("Willow-Bark Draught", effect_tag="heal_light", quantity=3, description="Bitter."),
            _ware("Lamp-Oil Flask", "trinket", "crude", description="Thrown."),
            _garment("Ashfall Hood", "common", "rough", concealing=True, description="Fangs work.",
                     visual_desc="a soot-grey hood"),
        ])
        player = Player(name="Bram Holloway", current_location_id=square.id, currency=72)
        session.add_all([hesk, player])
        session.commit()
        player_id = player.id
    add_to_inventory(player_id, "Worn Knife", item_type="weapon", slot="weapon", tier="crude")
    return player_id


def _sells(player_id):
    from src.services.scene_brief import build_scene_brief

    with get_session() as session:
        text = build_scene_brief(session, session.get(Player, player_id))["text"]
    line = next(line for line in text.splitlines() if "Sells" in line)
    return {part.split(" (")[0].strip(): part for part in line.split("): ", 1)[1].split("; ")}


def test_affordable_upgrades_are_marked_on_the_shelf(market):
    """fleet4-bram/-wenna ended on 72-85 unspent coin and the crude knife
    while Hesk sold a hatchet."""
    goods = _sells(market)
    assert "they can afford this, better than their Worn Knife" in goods["Woodsman's Hatchet"]
    assert "they can afford this, and they have no armor" in goods["Padded Jerkin"]
    assert "afford" not in goods["Gutting Knife"]               # no better than the knife
    assert "afford" not in goods["Ashwood Blade"]               # beyond the purse
    assert "afford" not in goods["Willow-Bark Draught x3"]      # not gear
    assert "afford" not in goods["Lamp-Oil Flask"]              # crude adds nothing over an empty slot
    assert "afford" not in goods["Ashfall Hood"]                # a hood is who you become, not an upgrade


def test_an_empty_purse_marks_nothing(market):
    with get_session() as session:
        session.get(Player, market).currency = 0
        session.commit()
    assert not any("afford" in part for part in _sells(market).values())


def test_a_sidegrade_says_it_is_no_better_whatever_the_purse(market):
    """fleet9-tamsin T4-T5: Hesk sold a crude Gutting Knife for 10 of her 25
    coin beside the crude Worn Knife she wore; it sat in the bag."""
    goods = _sells(market)
    assert "no better than the Worn Knife they carry" in goods["Gutting Knife"]
    assert "no better" not in goods["Woodsman's Hatchet"] and "no better" not in goods["Padded Jerkin"]
    assert "no better" not in goods["Lamp-Oil Flask"], "an empty slot is no comparison"
    with get_session() as session:
        session.get(Player, market).currency = 0
        session.commit()
    assert "no better than the Worn Knife they carry" in _sells(market)["Gutting Knife"]


def _wielding(player_id):
    with get_session() as session:
        player = session.get(Player, player_id)
        key = (player.equipment or {}).get("weapon")
        return next((i["name"] for i in player.inventory or [] if i.get("id") == key), None)


def test_a_bought_upgrade_is_wielded_at_once_and_a_sidegrade_is_not(market):
    """The fight after fleet9-tamsin's purchase used the old knife: a weapon
    bought strictly better than the one in hand goes on (the counter and
    @buy share transfer_item); a sidegrade, or a gift, never swaps."""
    with get_session() as session:
        hesk = session.query(NPC).filter_by(name="Hesk Bramble").one().id
    assert _wielding(market) == "Worn Knife"
    runner = CommandRunner(market, present_ids=[hesk])
    assert runner.run("@buy Gutting Knife | from: Hesk Bramble").ok
    assert _wielding(market) == "Worn Knife"
    assert runner.run("@buy Woodsman's Hatchet | from: Hesk Bramble").ok
    assert _wielding(market) == "Woodsman's Hatchet"
    add_to_inventory(market, "Ashwood Blade", item_type="weapon", slot="weapon", tier="fine")
    assert _wielding(market) == "Woodsman's Hatchet", "a gift never swaps what they wield"


def _wearing(player_id):
    with get_session() as session:
        player = session.get(Player, player_id)
        key = (player.equipment or {}).get("armor")
        return next((i["name"] for i in player.inventory or [] if i.get("id") == key), None)


def test_a_bought_upgrade_never_takes_off_a_disguise(market):
    """Strictly better armor bought over a concealing hood (or a faction's
    uniform) would unmask the wearer at the counter: what they chose to wear
    stays on; over plain armor the upgrade goes on."""
    from src.data.playtest_seed import _ware

    with get_session() as session:
        hesk = session.query(NPC).filter_by(name="Hesk Bramble").one()
        hesk.inventory_notable = list(hesk.inventory_notable) + [
            _ware("Ringmail Coat", "armor", "fine", description="Riveted."),
            _ware("Scale Hauberk", "armor", "fine", description="Overlapping plates.")]
        session.get(Player, market).currency = 900
        session.commit()
        hesk_id = hesk.id
    runner = CommandRunner(market, present_ids=[hesk_id])
    assert runner.run("@buy Ashfall Hood | from: Hesk Bramble").ok
    assert _wearing(market) == "Ashfall Hood"
    assert runner.run("@buy Ringmail Coat | from: Hesk Bramble").ok
    assert _wearing(market) == "Ashfall Hood", "the hood stays on"
    # ...and over plain armor the better one goes on
    with get_session() as session:
        player = session.get(Player, market)
        jerkin = next(i for i in _validate(hesk_id) if i["name"] == "Padded Jerkin")
        player.inventory = list(player.inventory) + [dict(jerkin)]
        player.equipment = dict(player.equipment or {}, armor=jerkin["id"])
        session.commit()
    assert _wearing(market) == "Padded Jerkin"
    assert runner.run("@buy Scale Hauberk | from: Hesk Bramble").ok
    assert _wearing(market) == "Scale Hauberk"


def _validate(npc_id):
    with get_session() as session:
        return [dict(i) for i in session.get(NPC, npc_id).inventory_notable or []]
