"""The absolute danger ladder: tier/archetype tables and their pure resolvers.

Danger is a property of the enemy, never of the player. Everything here is
table math with no DB and no player state — the tests exist to keep it that
way, and to prove that a world where every danger_tier is NULL still fights
exactly like the pre-ladder build.
"""

import inspect

import pytest

from src.models import NPC, NPCTier
from src.services.combat import (
    ARCHETYPES,
    ARCH_ROWS,
    BOSS_PATTERN,
    DANGER_TIERS,
    TIER_ROWS,
    WINDUP_FLOOR_MS,
    combat_params,
    infer_archetype,
    infer_danger_tier,
    is_boss,
    resolve_danger_tier,
)

# The numbers GameRenderer.ARCHETYPES shipped before the ladder existed.
# The `standard` rung must reproduce them exactly, field for field.
LEGACY_ROWS = {
    "brute":      {"hp": 7, "speed": 4.8, "windup": 650, "lungeDist": 10,
                   "lungeMs": 215, "recover": 800, "engage": 7, "scaleMul": 1.12},
    "skirmisher": {"hp": 3, "speed": 8.5, "windup": 340, "lungeDist": 8,
                   "lungeMs": 150, "recover": 430, "engage": 6, "scaleMul": 0.95},
    "stalker":    {"hp": 4, "speed": 6.8, "windup": 280, "lungeDist": 12,
                   "lungeMs": 140, "recover": 520, "engage": 9, "scaleMul": 1.0},
}

# §4.5 of the spec — the grid the tables are required to produce.
HP_GRID = {
    "harmless":  {"brute": 1,  "skirmisher": 1, "stalker": 1,  "ranged": 1},
    "weak":      {"brute": 4,  "skirmisher": 2, "stalker": 2,  "ranged": 2},
    "standard":  {"brute": 7,  "skirmisher": 3, "stalker": 4,  "ranged": 4},
    "dangerous": {"brute": 11, "skirmisher": 5, "stalker": 6,  "ranged": 6},
    "deadly":    {"brute": 20, "skirmisher": 8, "stalker": 11, "ranged": 10},
    "boss":      {"brute": 24, "skirmisher": 24, "stalker": 24, "ranged": 24},
}


def _npc(**kwargs) -> NPC:
    """A detached NPC row — these resolvers never touch the database."""
    kwargs.setdefault("name", "Nobody")
    kwargs.setdefault("tier", NPCTier.MINOR)
    return NPC(**kwargs)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

def test_vocabularies_are_the_frozen_menus():
    assert DANGER_TIERS == ("harmless", "weak", "standard", "dangerous", "deadly", "boss")
    assert ARCHETYPES == ("brute", "skirmisher", "stalker", "ranged")
    assert set(TIER_ROWS) == set(DANGER_TIERS)
    assert set(ARCH_ROWS) == set(ARCHETYPES)


def test_tier_xp_is_the_single_combat_xp_source():
    assert [TIER_ROWS[t]["xp"] for t in DANGER_TIERS] == [0, 5, 15, 40, 100, 300]


# --------------------------------------------------------------------------
# combat_params — purity and the no-scaling guarantee
# --------------------------------------------------------------------------

def test_combat_params_is_pure_and_deterministic():
    first = combat_params("dangerous", "brute")
    second = combat_params("dangerous", "brute")
    assert first == second
    # The structural no-scaling guarantee: no player value can reach an enemy
    # stat because there is nowhere to pass one.
    params = list(inspect.signature(combat_params).parameters)
    assert params == ["tier", "archetype"]


def test_combat_params_rejects_a_player_argument():
    with pytest.raises(TypeError):
        combat_params("standard", "brute", "player-id")  # type: ignore[call-arg]


def test_combat_params_returns_a_fresh_boss_pattern():
    a = combat_params("boss", "brute")
    a["bossPattern"]["slam_damage"] = 99
    assert combat_params("boss", "brute")["bossPattern"]["slam_damage"] == 3


def test_unknown_labels_fall_back_to_standard_skirmisher():
    fallback = combat_params("terrifying", "juggler")
    reference = combat_params("standard", "skirmisher")
    assert fallback["hp"] == reference["hp"]
    assert fallback["damage"] == reference["damage"]
    assert fallback["windup"] == reference["windup"]


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------

def test_resolved_hp_grid_matches_the_spec():
    for tier, per_archetype in HP_GRID.items():
        for archetype, hp in per_archetype.items():
            assert combat_params(tier, archetype)["hp"] == hp, f"{tier} {archetype}"


def test_hp_is_monotonic_across_the_ladder():
    for archetype in ARCHETYPES:
        hps = [combat_params(t, archetype)["hp"] for t in DANGER_TIERS]
        assert hps == sorted(hps), f"{archetype}: {hps}"


def test_damage_is_monotonic_and_absolute():
    for archetype in ARCHETYPES:
        dmg = [combat_params(t, archetype)["damage"] for t in DANGER_TIERS]
        assert dmg == sorted(dmg)
        assert dmg[0] == 0  # harmless deals literally nothing, forever


def test_standard_tier_reproduces_legacy_numbers():
    # Existing worlds (danger_tier IS NULL → mostly standard) play identically
    for archetype, legacy in LEGACY_ROWS.items():
        params = combat_params("standard", archetype)
        for key, value in legacy.items():
            assert params[key] == value, f"{archetype}.{key}"
    assert combat_params("standard", "stalker")["circles"] is True
    assert combat_params("standard", "brute")["circles"] is False


def test_harmless_dies_in_one_hit_and_flees():
    for archetype in ARCHETYPES:
        params = combat_params("harmless", archetype)
        assert params["hp"] == 1
        assert params["damage"] == 0
        assert params["flees"] is True


def test_only_harmless_flees():
    for tier in DANGER_TIERS[1:]:
        assert combat_params(tier, "brute")["flees"] is False


def test_boss_hp_ignores_archetype_multiplier():
    for archetype in ARCHETYPES:
        params = combat_params("boss", archetype)
        assert params["hp"] == 24
        assert params["boss"] is True
        assert params["bossPattern"]["phase2_at"] == 0.5
        assert params["knockMul"] == 0.0  # you cannot shove a boss around


def test_non_boss_tiers_carry_no_boss_pattern():
    for tier in DANGER_TIERS[:-1]:
        params = combat_params(tier, "brute")
        assert params["boss"] is False
        assert "bossPattern" not in params


def test_boss_pattern_has_no_intro_ms():
    # The entrance is a cinematic queued server-side, not a combat timing
    assert "intro_ms" not in BOSS_PATTERN


def test_windup_never_below_reaction_floor():
    assert WINDUP_FLOOR_MS == 220
    for tier in DANGER_TIERS:
        for archetype in ARCHETYPES:
            windup = combat_params(tier, archetype)["windup"]
            assert windup >= WINDUP_FLOOR_MS, f"{tier} {archetype} = {windup}"


def test_deadly_stalker_sits_on_the_windup_floor():
    # The reaction-time ladder bottoms out here rather than going unfair
    assert combat_params("deadly", "stalker")["windup"] == WINDUP_FLOOR_MS


def test_ranged_params_carry_standoff_and_ring():
    params = combat_params("standard", "ranged")
    assert params["ranged"] is True
    assert params["standoff"] == 14
    assert params["ringRadius"] == 9
    assert params["lungeDist"] == 0


def test_melee_params_carry_no_standoff_or_ring():
    for archetype in ("brute", "skirmisher", "stalker"):
        params = combat_params("standard", archetype)
        assert params["ranged"] is False
        assert params["standoff"] == 0
        assert params["ringRadius"] == 0


def test_params_echo_their_labels():
    params = combat_params("deadly", "stalker")
    assert params["tier"] == "deadly"
    assert params["archetype"] == "stalker"


# --------------------------------------------------------------------------
# Tier inference
# --------------------------------------------------------------------------

def test_infer_danger_tier_beggar_is_harmless():
    assert infer_danger_tier(_npc(profession="beggar")) == "harmless"


def test_infer_danger_tier_precedence():
    # highest danger first: a veteran guard is dangerous, not standard
    assert infer_danger_tier(_npc(profession="veteran guard")) == "dangerous"
    assert infer_danger_tier(_npc(profession="guard")) == "standard"


def test_infer_danger_tier_reads_every_authored_field():
    assert infer_danger_tier(_npc(description_physical="a hulking ogre")) == "dangerous"
    assert infer_danger_tier(_npc(description_personality="a champion of the pit")) == "deadly"
    assert infer_danger_tier(_npc(species="wolf")) == "standard"


@pytest.mark.parametrize("kwargs", [
    {"profession": "dragon"},
    {"profession": "god of the deep"},
    {"description_physical": "a titan wreathed in ash"},
    {"description_personality": "the nemesis, a legendary monstrosity"},
    {"species": "wyvern", "profession": "boss"},
    {"description_physical": "an enormous archmage demon warlord"},
])
def test_infer_danger_tier_never_returns_boss(kwargs):
    assert infer_danger_tier(_npc(**kwargs)) != "boss"


def test_legacy_npc_defaults_by_npc_tier():
    # No authored tier, no keywords: narrative weight is the last resort
    assert infer_danger_tier(_npc(tier=NPCTier.AMBIENT)) == "weak"
    assert infer_danger_tier(_npc(tier=NPCTier.MINOR)) == "standard"
    assert infer_danger_tier(_npc(tier=NPCTier.MAJOR)) == "standard"


def test_explicit_danger_tier_wins():
    npc = _npc(profession="dragon", description_physical="a titan of scale and fire",
               danger_tier="harmless")
    assert resolve_danger_tier(npc) == "harmless"


def test_resolve_ignores_a_garbage_tier_label():
    npc = _npc(profession="beggar", danger_tier="terrifying")
    assert resolve_danger_tier(npc) == "harmless"


def test_resolve_falls_back_to_inference_when_null():
    assert resolve_danger_tier(_npc(profession="bandit")) == "standard"


# --------------------------------------------------------------------------
# is_boss — the single boss flag (danger_tier, never boss_title)
# --------------------------------------------------------------------------

def test_is_boss_follows_danger_tier_only():
    assert is_boss(_npc(profession="wyrm", danger_tier="boss")) is True
    assert is_boss(_npc(profession="dragon")) is False


def test_boss_title_alone_is_not_a_boss():
    npc = _npc(profession="captain", boss_title="Warden of the Ashen Gate")
    assert is_boss(npc) is False


# --------------------------------------------------------------------------
# Archetype inference — the new `ranged` rung is reachable without authoring
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "a hooded archer", "carries a bow", "a crossbow across her back",
    "a sniper on the rooftop", "a hedge mage", "a spellcaster in grey",
    "a stone thrower", "a gunner behind the barricade", "a sling in one hand",
])
def test_infer_archetype_finds_ranged(text):
    assert infer_archetype(_npc(description_physical=text)) == "ranged"


@pytest.mark.parametrize("text", [
    "his face is a mass of damage", "the image of his father",
    "he bows deeply, elbow tucked", "a bowl of stew in his hands",
])
def test_ranged_words_do_not_fire_on_ordinary_prose(text):
    assert infer_archetype(_npc(description_physical=text)) != "ranged"


def test_explicit_ranged_archetype_wins():
    assert infer_archetype(_npc(combat_archetype="ranged",
                                description_physical="a hulking brute")) == "ranged"


def test_ranged_beats_body_words():
    # The weapon decides the shape of the fight, not the silhouette
    assert infer_archetype(_npc(description_physical="a massive hulking archer")) == "ranged"


def test_archetype_inference_is_unchanged_without_ranged_words():
    assert infer_archetype(_npc(description_physical="a hulking massive brawler")) == "brute"
    assert infer_archetype(_npc(description_physical="a silent shadow, a predator")) == "stalker"
    assert infer_archetype(_npc(description_physical="a wiry youth")) == "skirmisher"
