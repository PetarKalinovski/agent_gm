"""The constant tables — shape, coverage and the frozen label menus.

Progression numbers live in exactly one module. These tests pin the tables so
a retune stays a table edit (and a *deliberate* one), and so no vocabulary
drifts away from the integration doc's §2.1 menus.
"""

from src.services import progression as prog


# --- Levels and XP --------------------------------------------------------


def test_xp_thresholds_monotonic_and_cover_1_to_10():
    assert sorted(prog.XP_THRESHOLDS) == list(range(1, prog.MAX_LEVEL + 1))
    values = [prog.XP_THRESHOLDS[level] for level in range(1, prog.MAX_LEVEL + 1)]
    assert values[0] == 0
    assert all(b > a for a, b in zip(values, values[1:])), "thresholds must be strictly increasing"


def test_level_table_covers_every_level():
    assert sorted(prog.LEVEL_TABLE) == list(range(1, prog.MAX_LEVEL + 1))
    rows = [prog.LEVEL_TABLE[level] for level in range(1, prog.MAX_LEVEL + 1)]
    assert all(b.gear_tier_cap >= a.gear_tier_cap for a, b in zip(rows, rows[1:]))
    assert all(b.hearts_bonus >= a.hearts_bonus for a, b in zip(rows, rows[1:]))
    assert all(b.damage_bonus >= a.damage_bonus for a, b in zip(rows, rows[1:]))
    assert all(b.dodge_cooldown_ms <= a.dodge_cooldown_ms for a, b in zip(rows, rows[1:]))
    assert rows[0].unlock_text is None, "level 1 is where you start — nothing unlocks"
    assert all(r.unlock_text for r in rows[1:])


def test_level_one_row_is_the_legacy_baseline():
    row = prog.LEVEL_TABLE[1]
    assert (row.hearts_bonus, row.damage_bonus, row.dodge_cooldown_ms) == (0, 0, 750)
    assert prog.BASE_HEARTS == 5 and prog.BASE_DAMAGE == 1 and prog.BASE_IFRAME_MS == 350


def test_gear_tier_cap_never_exceeds_the_ladder():
    top = len(prog.TIER_LABELS) - 1
    assert all(row.gear_tier_cap <= top for row in prog.LEVEL_TABLE.values())


def test_min_level_for_tier_is_derived_from_the_level_table():
    for tier_idx in range(len(prog.TIER_LABELS)):
        level = prog.min_level_for_tier(tier_idx)
        assert prog.LEVEL_TABLE[level].gear_tier_cap >= tier_idx
        if level > 1:
            assert prog.LEVEL_TABLE[level - 1].gear_tier_cap < tier_idx


# --- XP sources -----------------------------------------------------------


def test_quest_xp_covers_the_frozen_scale_menu():
    assert set(prog.QUEST_XP) == {"errand", "job", "mission", "saga"}
    assert [prog.QUEST_XP[s] for s in ("errand", "job", "mission", "saga")] == [60, 120, 220, 400]
    assert prog.DEFAULT_QUEST_SCALE in prog.QUEST_XP


def test_combat_xp_is_not_a_progression_table():
    """Integration doc R1: combat XP totals come from the tier engine's
    TIER_ROWS. progression only exposes `grant_xp`."""
    for deleted in ("ENEMY_XP", "_ARCHETYPE_TO_TIER", "xp_for_enemy", "award_xp"):
        assert not hasattr(prog, deleted), f"{deleted} was cut by R1/R2"


# --- Gear -----------------------------------------------------------------


def test_tier_effect_tables_have_six_entries():
    assert len(prog.TIER_LABELS) == 6
    assert prog.TIER_LABELS == ("crude", "common", "fine", "superior", "master", "legendary")
    for table in (prog.WEAPON_DAMAGE, prog.ARMOR_HEARTS, prog.TRINKET_IFRAMES):
        assert len(table) == 6
        assert list(table) == sorted(table), "gear tiers must never get worse"
        assert table[0] == 0, "crude gear is the zero point"


def test_price_table_covers_every_slot_and_rises():
    assert set(prog.TIER_PRICE) == set(prog.SLOTS) == {"weapon", "armor", "trinket"}
    for slot, prices in prog.TIER_PRICE.items():
        assert len(prices) == 6, slot
        assert all(b > a for a, b in zip(prices, prices[1:])), slot


def test_max_gear_stays_inside_the_published_ranges():
    """The contract published to enemy-tiers: hearts 5->12, damage 1->8."""
    top = prog.LEVEL_TABLE[prog.MAX_LEVEL]
    assert prog.BASE_HEARTS + top.hearts_bonus + prog.ARMOR_HEARTS[-1] == prog.HEART_CAP == 12
    assert prog.BASE_DAMAGE + top.damage_bonus + prog.WEAPON_DAMAGE[-1] == 8


# --- Consumables ----------------------------------------------------------


def test_consumables_cover_the_frozen_menu():
    assert set(prog.CONSUMABLES) == {"heal_light", "heal_strong", "surge"}
    for tag, row in prog.CONSUMABLES.items():
        assert set(row) == {"hearts", "status_steps", "buff", "ms", "price"}, tag
        assert row["price"] > 0, tag
    assert prog.CONSUMABLES["surge"]["buff"] == "damage"
    assert prog.CONSUMABLES["heal_strong"]["hearts"] > prog.CONSUMABLES["heal_light"]["hearts"]
    assert prog.SURGE_DAMAGE_BONUS >= 1


# --- Checks ---------------------------------------------------------------


def test_skill_check_menus_are_the_frozen_vocabulary():
    assert prog.SKILLS == ("stealth", "persuasion", "athletics", "perception", "cunning")
    assert set(prog.DIFFICULTY_DC) == {"easy", "tricky", "hard", "desperate"}
    assert prog.MODIFIERS == ("none", "advantage", "disadvantage")
    assert prog.OUTCOMES == ("critical_success", "success", "partial", "failure", "critical_failure")


def test_difficulty_dcs_rise_with_the_word():
    order = ["easy", "tricky", "hard", "desperate"]
    dcs = [prog.DIFFICULTY_DC[d] for d in order]
    assert all(b > a for a, b in zip(dcs, dcs[1:]))


def test_trait_bonus_covers_every_skill():
    assert set(prog.TRAIT_BONUS) == set(prog.SKILLS)
    assert all(prog.TRAIT_BONUS[skill] for skill in prog.SKILLS)


def test_every_outcome_has_a_narration_instruction():
    assert set(prog.OUTCOME_INSTRUCTIONS) == set(prog.OUTCOMES)
    for text in prog.OUTCOME_INSTRUCTIONS.values():
        assert "Narrate the fiction only" in text
        assert not any(ch.isdigit() for ch in text), "no numbers reach the DM"


# --- Health ---------------------------------------------------------------


def test_health_fraction_covers_every_status():
    assert set(prog.HEALTH_FRACTION) == set(prog.HEALTH_ORDER)
    fractions = [prog.HEALTH_FRACTION[s] for s in prog.HEALTH_ORDER]
    assert fractions[0] == 1.0 and fractions[-1] == 0.0
    assert all(b < a for a, b in zip(fractions, fractions[1:]))
