"""Gear: the legacy-equality calibration first, then what tiers buy.

`test_level1_unarmed_matches_legacy` is the load-bearing test of the whole
progression overhaul: a level-1 character with nothing equipped must produce
byte-identical numbers to the pre-overhaul `_playerHpFromHealth` map in
game.js (5/4/3/2/1 hearts, damage 1). If it ever fails, `data/emberfall.db`
no longer plays the way it did.
"""

from src.models import Item, Player, get_session
from src.services import progression as prog


def _player(**kwargs) -> Player:
    """A detached Player row with explicit fields (SQLAlchemy column defaults
    only fire on flush, so a legacy-shaped row is what we get for free)."""
    fields = {
        "name": "Tester",
        "health_status": "healthy",
        "inventory": [],
        "traits": [],
        "level": None,
        "xp": None,
        "equipment": None,
    }
    fields.update(kwargs)
    return Player(**fields)


def _gear(item_id: str, name: str, slot: str, tier: str, **extra) -> dict:
    return Item(id=item_id, name=name, type=slot, slot=slot, tier=tier, **extra).to_dict()


# --- The calibration ------------------------------------------------------

LEGACY_HEARTS = {"healthy": 5, "winded": 4, "hurt": 3, "badly_hurt": 2, "critical": 1}


def test_level1_unarmed_matches_legacy():
    """A NULL-everything row plays exactly like the pre-progression build."""
    for health, expected in LEGACY_HEARTS.items():
        stats = prog.combat_stats(_player(health_status=health))
        assert stats["max_hearts"] == 5, health
        assert stats["hearts"] == expected, health
        assert stats["light_damage"] == 1, health
        assert stats["dodge_cooldown_ms"] == 750, health
        assert stats["iframe_ms"] == 350, health


def test_combat_stats_payload_is_the_frozen_shape():
    """Integration doc R3: the exact merged dict, nothing more, nothing less."""
    assert set(prog.combat_stats(_player())) == {
        "level", "max_hearts", "hearts", "light_damage", "heavy_damage",
        "dodge_cooldown_ms", "iframe_ms", "quick_slot",
        "parry_unlocked", "heavy_unlocked",
    }
    stats = prog.combat_stats(_player())
    assert stats["parry_unlocked"] is True
    assert stats["heavy_unlocked"] is True
    assert stats["quick_slot"] is None


def test_heavy_damage_is_light_twice_plus_one():
    for level in (1, 5, 10):
        p = _player(level=level)
        stats = prog.combat_stats(p)
        assert stats["heavy_damage"] == stats["light_damage"] * 2 + 1


def test_dead_player_has_zero_hearts():
    assert prog.combat_stats(_player(health_status="dead"))["hearts"] == 0


# --- What tiers buy -------------------------------------------------------


def test_weapon_tier_adds_damage():
    sword = _gear("blade", "Blade", "weapon", "fine")
    p = _player(level=1, inventory=[sword], equipment={"weapon": "blade"})
    assert prog.combat_stats(p)["light_damage"] == 1 + prog.WEAPON_DAMAGE[2]


def test_armor_tier_adds_hearts():
    mail = _gear("mail", "Harbor Mail", "armor", "common")
    p = _player(level=1, inventory=[mail], equipment={"armor": "mail"})
    assert prog.combat_stats(p)["max_hearts"] == 5 + prog.ARMOR_HEARTS[1]


def test_trinket_tier_adds_iframes():
    charm = _gear("charm", "Ash Charm", "trinket", "fine")
    p = _player(level=1, inventory=[charm], equipment={"trinket": "charm"})
    assert prog.combat_stats(p)["iframe_ms"] == 350 + prog.TRINKET_IFRAMES[2]


def test_hearts_respect_health_fraction_at_high_level():
    armor = _gear("plate", "Master Plate", "armor", "master")
    p = _player(level=10, health_status="hurt", inventory=[armor],
                equipment={"armor": "plate"})
    stats = prog.combat_stats(p)
    assert stats["max_hearts"] == min(prog.HEART_CAP, 5 + 4 + prog.ARMOR_HEARTS[4])
    assert stats["hearts"] == round(stats["max_hearts"] * 0.6)
    assert stats["hearts"] >= 1


def test_hearts_never_drop_below_one_while_alive():
    p = _player(level=1, health_status="critical")
    assert prog.combat_stats(p)["hearts"] >= 1


def test_heart_cap():
    """Nothing the tables can produce exceeds HEART_CAP."""
    armor = _gear("plate", "Legend Plate", "armor", "legendary")
    p = _player(level=10, inventory=[armor], equipment={"armor": "plate"})
    assert prog.combat_stats(p)["max_hearts"] <= prog.HEART_CAP


def test_damage_range_is_one_to_eight():
    weapon = _gear("blade", "Legend Blade", "weapon", "legendary")
    top = _player(level=10, inventory=[weapon], equipment={"weapon": "blade"})
    assert prog.combat_stats(_player())["light_damage"] == 1
    assert prog.combat_stats(top)["light_damage"] == 8


# --- can_equip / the gear tier cap ---------------------------------------


def test_equip_blocked_below_tier_cap_level():
    superior = _gear("fine_blade", "Superior Blade", "weapon", "superior")
    ok, reason = prog.can_equip(_player(level=1, inventory=[superior]), superior)
    assert (ok, reason) == (False, "level")


def test_equip_rejects_plain_item():
    note = Item(id="note", name="Crumpled Note", type="misc").to_dict()
    ok, reason = prog.can_equip(_player(inventory=[note]), note)
    assert (ok, reason) == (False, "not_equipment")


def test_min_level_for_tier_matches_the_level_table():
    assert [prog.min_level_for_tier(i) for i in range(6)] == [1, 1, 1, 3, 5, 7]


# --- Legacy repair --------------------------------------------------------


def test_legacy_typed_weapon_is_equippable_as_common(player):
    """A world that already contains an 'Iron Sword' becomes equippable the
    moment this ships — no migration, no re-authoring."""
    legacy = {"id": "iron_sword", "name": "Iron Sword", "type": "weapon",
              "value": 30, "quantity": 1}
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [legacy]
        result = prog.equip(session, p, "iron_sword")
        session.commit()
        assert result["success"] is True
        assert result["slot"] == "weapon"
        assert result["tier"] == "common"
        assert prog.combat_stats(p)["light_damage"] == 1 + prog.WEAPON_DAMAGE[1]


def test_mechanize_item_returns_none_for_plain_stuff():
    assert prog.mechanize_item({"id": "rope", "name": "Rope", "type": "misc"}) is None
    assert prog.mechanize_item(None) is None
    assert prog.mechanize_item("Old Sword") is None


# --- equip / unequip ------------------------------------------------------


def test_equipped_item_stays_in_inventory(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [_gear("blade", "Blade", "weapon", "common", quantity=1)]
        prog.equip(session, p, "blade")
        session.commit()

    with get_session() as session:
        p = session.get(Player, player)
        assert [e["id"] for e in p.inventory] == ["blade"]
        assert p.inventory[0]["quantity"] == 1
        assert p.equipment["weapon"] == "blade"


def test_equip_reports_what_it_replaced(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [_gear("old", "Padded Coat", "armor", "crude"),
                       _gear("new", "Harbor Mail", "armor", "common")]
        prog.equip(session, p, "old")
        result = prog.equip(session, p, "new")
        session.commit()
        assert result["replaced"] == "Padded Coat"
        assert result["equipment"]["armor"] == "new"


def test_equip_missing_item_errors(player):
    with get_session() as session:
        p = session.get(Player, player)
        result = prog.equip(session, p, "ghost_sword")
        assert result["reason"] == "missing"


def test_equip_above_cap_names_the_required_level(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [_gear("master_blade", "Master Blade", "weapon", "master")]
        result = prog.equip(session, p, "master_blade")
        assert result["reason"] == "level"
        assert result["required_level"] == 5
        assert result["level"] == 1


def test_unequip_clears_slot(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [_gear("blade", "Blade", "weapon", "common")]
        prog.equip(session, p, "blade")
        result = prog.unequip(session, p, "weapon")
        session.commit()
        assert result["item_name"] == "Blade"
        assert result["equipment"]["weapon"] is None
        assert prog.combat_stats(p)["light_damage"] == 1


def test_slot_auto_clears_when_item_leaves_inventory(player):
    """`remove_from_inventory` knows nothing about equipment — the loadout
    resolver drops the dangling pointer instead of crashing."""
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [_gear("blade", "Blade", "weapon", "common")]
        prog.equip(session, p, "blade")
        p.inventory = []
        assert prog.resolve_loadout(p)["weapon"] is None
        assert prog.combat_stats(p)["light_damage"] == 1


def test_loadout_drops_gear_above_the_current_cap():
    master = _gear("master_blade", "Master Blade", "weapon", "master")
    p = _player(level=1, inventory=[master], equipment={"weapon": "master_blade"})
    assert prog.resolve_loadout(p)["weapon"] is None
    assert prog.combat_stats(p)["light_damage"] == 1


def test_equipment_summary_and_labels():
    blade = _gear("blade", "Iron Sword", "weapon", "fine")
    p = _player(level=3, inventory=[blade], equipment={"weapon": "blade"})
    assert prog.equipment_summary(p)["weapon"] == {
        "item_id": "blade", "name": "Iron Sword", "tier": "fine",
    }
    assert prog.equipment_summary(p)["armor"] is None
    assert prog.equipped_labels(p) == {"weapon": "Iron Sword (fine)"}


def test_annotate_item_carries_the_hud_keys():
    blade = _gear("blade", "Blade", "weapon", "common", value=3)
    p = _player(inventory=[blade], equipment={"weapon": "blade"})
    annotated = prog.annotate_item(p, blade)
    assert annotated["slot"] == "weapon"
    assert annotated["tier"] == "common"
    assert annotated["equipped"] is True
    assert annotated["equippable"] is True
    assert annotated["price"] == prog.TIER_PRICE["weapon"][1]


def test_annotate_plain_item_keeps_authored_value():
    note = Item(id="note", name="Note", type="misc", value=12).to_dict()
    annotated = prog.annotate_item(_player(inventory=[note]), note)
    assert annotated["price"] == 12
    assert annotated["equippable"] is False
    assert annotated["slot"] is None


# --- Auto-equip -----------------------------------------------------------


def test_auto_equip_on_pickup_when_slot_empty(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [_gear("blade", "Blade", "weapon", "common")]
        result = prog.auto_equip(session, p, "blade")
        session.commit()
        assert result and result["success"] is True
        assert p.equipment["weapon"] == "blade"


def test_auto_equip_does_not_replace_worn_gear(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [_gear("old", "Old Blade", "weapon", "common"),
                       _gear("new", "New Blade", "weapon", "fine")]
        prog.equip(session, p, "old")
        assert prog.auto_equip(session, p, "new") is None
        assert p.equipment["weapon"] == "old"


def test_auto_equip_skips_gear_above_the_cap(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [_gear("master_blade", "Master Blade", "weapon", "master")]
        assert prog.auto_equip(session, p, "master_blade") is None
        assert prog.resolve_loadout(p)["weapon"] is None


def test_auto_equip_ignores_plain_items(player):
    with get_session() as session:
        p = session.get(Player, player)
        p.inventory = [Item(id="rope", name="Rope", type="misc").to_dict()]
        assert prog.auto_equip(session, p, "rope") is None
