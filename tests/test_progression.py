"""XP, levels and the legacy-row contract.

`grant_xp` is the ONE XP faucet (integration doc R2) — quest completion and
combat resolution both call it inside their own open transaction. The grant
sites themselves live in `world_write/quests.py::_complete_quest` (chunk A6)
and `services/combat.py::apply_combat_result` (chunk B1); their end-to-end
tests live with them. What is pinned here is the engine those callers use.
"""

from src.models import Player, Quest, get_session
from src.services import progression as prog


def _legacy_player() -> Player:
    """A pre-overhaul row: every new column reads NULL."""
    return Player(name="Old Hand", health_status="healthy", inventory=[], traits=[],
                  level=None, xp=None, equipment=None, quick_slot_item_id=None)


# --- Legacy defaults ------------------------------------------------------


def test_legacy_player_row_defaults():
    p = _legacy_player()
    assert prog.player_level(p) == 1
    assert prog.player_xp(p) == 0
    assert prog.resolve_loadout(p) == {"weapon": None, "armor": None, "trinket": None}
    assert prog.equipment_summary(p) == {"weapon": None, "armor": None, "trinket": None}
    assert prog.equipped_labels(p) == {}
    stats = prog.combat_stats(p)
    assert (stats["max_hearts"], stats["hearts"], stats["light_damage"]) == (5, 5, 1)


def test_level_is_clamped_to_the_table():
    assert prog.player_level(Player(name="x", level=0)) == 1
    assert prog.player_level(Player(name="x", level=99)) == prog.MAX_LEVEL
    assert prog.player_level(Player(name="x", level="nonsense")) == 1


def test_xp_progress_on_a_legacy_row():
    assert prog.xp_progress(_legacy_player()) == {
        "level": 1, "xp": 0, "xp_into_level": 0, "xp_for_level": 100,
    }


# --- grant_xp -------------------------------------------------------------


def test_grant_xp_levels_once_per_threshold(player):
    with get_session() as session:
        p = session.get(Player, player)
        block = prog.grant_xp(session, p, 99, "quest")
        session.commit()
        assert block["level"] == 1 and block["leveled_up"] is False
        assert block["xp_into_level"] == 99 and block["xp_for_level"] == 100

        block = prog.grant_xp(session, p, 1, "quest")
        session.commit()
        assert block["level"] == 2 and block["previous_level"] == 1
        assert block["leveled_up"] is True
        assert block["unlocked"] == [prog.LEVEL_TABLE[2].unlock_text]


def test_grant_xp_multi_level_jump(player):
    with get_session() as session:
        p = session.get(Player, player)
        block = prog.grant_xp(session, p, 1000, "combat")
        session.commit()
    assert block["level"] == 6 and block["previous_level"] == 1
    crossed = {prog.LEVEL_TABLE[level].unlock_text for level in range(2, 7)}
    assert set(block["unlocked"]) == crossed
    assert len(block["unlocked"]) == len(set(block["unlocked"])), "deduped, not repeated"


def test_xp_caps_at_level_10(player):
    with get_session() as session:
        p = session.get(Player, player)
        block = prog.grant_xp(session, p, 99999, "quest")
        session.commit()
    assert block["level"] == prog.MAX_LEVEL
    assert block["xp_for_level"] == 0
    assert block["xp_into_level"] == 0


def test_grant_xp_block_shape(player):
    with get_session() as session:
        p = session.get(Player, player)
        block = prog.grant_xp(session, p, 120, "quest")
        session.commit()
    assert set(block) == {
        "xp_gained", "source", "xp", "level", "previous_level", "leveled_up",
        "xp_into_level", "xp_for_level", "unlocked", "stats",
    }
    assert block["xp_gained"] == 120 and block["source"] == "quest"
    assert block["stats"] == {"max_hearts": 6, "damage": 1}


def test_grant_xp_persists_level_and_total(player):
    with get_session() as session:
        p = session.get(Player, player)
        prog.grant_xp(session, p, 260, "quest")
        session.commit()
    with get_session() as session:
        p = session.get(Player, player)
        assert (p.level, p.xp) == (3, 260)


def test_grant_xp_of_nothing_returns_an_empty_block(player):
    with get_session() as session:
        p = session.get(Player, player)
        assert prog.grant_xp(session, p, 0, "combat") == {}
        assert prog.grant_xp(session, p, -50, "combat") == {}
        assert prog.grant_xp(session, p, None, "combat") == {}
        session.commit()
    with get_session() as session:
        assert prog.player_xp(session.get(Player, player)) == 0


def test_grant_xp_runs_in_the_callers_transaction(player):
    """R2: no commit of its own — a caller that rolls back pays no XP."""
    with get_session() as session:
        p = session.get(Player, player)
        prog.grant_xp(session, p, 500, "quest")
        session.rollback()
    with get_session() as session:
        p = session.get(Player, player)
        assert prog.player_xp(p) == 0
        assert prog.player_level(p) == 1


def test_level_gains_show_up_in_combat_stats(player):
    with get_session() as session:
        p = session.get(Player, player)
        prog.grant_xp(session, p, 700, "quest")  # level 5
        session.commit()
        stats = prog.combat_stats(p)
    assert stats["level"] == 5
    assert stats["max_hearts"] == 5 + prog.LEVEL_TABLE[5].hearts_bonus
    assert stats["light_damage"] == 1 + prog.LEVEL_TABLE[5].damage_bonus
    assert stats["dodge_cooldown_ms"] == prog.LEVEL_TABLE[5].dodge_cooldown_ms


# --- Quest scale ----------------------------------------------------------


def test_quest_scale_maps_to_xp():
    for scale, expected in prog.QUEST_XP.items():
        assert prog.xp_for_quest(Quest(title="t", scale=scale)) == expected


def test_quest_null_scale_reads_as_job():
    quest = Quest(title="Unlabelled", scale=None)
    assert prog.quest_scale(quest) == "job"
    assert prog.xp_for_quest(quest) == 120


def test_quest_unknown_scale_falls_back_to_job():
    assert prog.xp_for_quest(Quest(title="t", scale="epic_saga_of_doom")) == 120
    assert prog.xp_for_quest(Quest(title="t", scale="  MISSION ")) == 220


def test_quest_rewards_cannot_inject_xp():
    """The DM authors rewards; XP is not theirs to author."""
    cleaned = prog.strip_authored_xp({"xp": 9999, "experience": 5, "level": 4, "gold": 10})
    assert cleaned == {"gold": 10}


def test_strip_authored_xp_copies_and_tolerates_none():
    original = {"gold": 5, "xp": 1}
    assert prog.strip_authored_xp(original) == {"gold": 5}
    assert original == {"gold": 5, "xp": 1}, "the caller's dict is not mutated"
    assert prog.strip_authored_xp(None) == {}
