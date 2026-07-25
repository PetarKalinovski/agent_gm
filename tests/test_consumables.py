"""Potions: one item, two honest readings of the same fiction.

`hearts` applies only in a fight (hearts do not exist outside one);
`status_steps` applies only out of it. The quick slot is what the Q key drinks
without asking the DM — the DM finds out afterwards, for free.
"""

import inspect

import pytest

from src.models import Item, Player, get_session
from src.services import progression as prog


def _potion(item_id="draught", name="Healing Draught", tag="heal_light", quantity=3) -> dict:
    return Item(id=item_id, name=name, type="consumable", effect_tag=tag,
                quantity=quantity).to_dict()


def _stock(player_id: str, *items: dict) -> None:
    with get_session() as session:
        p = session.get(Player, player_id)
        p.inventory = list(items)
        session.commit()


# --- The quick slot -------------------------------------------------------


def test_default_quick_slot_is_first_heal():
    p = Player(name="T", inventory=[_potion("a"), _potion("b", "Strong Draught", "heal_strong")])
    assert prog.default_quick_slot(p) == "a"


def test_default_quick_slot_prefers_healing_over_surge():
    p = Player(name="T", inventory=[_potion("surge_vial", "Surge Vial", "surge"), _potion("a")])
    assert prog.default_quick_slot(p) == "a"


def test_default_quick_slot_falls_back_to_any_consumable():
    p = Player(name="T", inventory=[_potion("surge_vial", "Surge Vial", "surge")])
    assert prog.default_quick_slot(p) == "surge_vial"


def test_default_quick_slot_is_none_without_consumables():
    p = Player(name="T", inventory=[Item(id="rope", name="Rope", type="misc").to_dict()])
    assert prog.default_quick_slot(p) is None


def test_quick_slot_payload_carries_what_the_client_needs():
    p = Player(name="T", health_status="healthy", inventory=[_potion()], equipment=None)
    payload = prog.combat_stats(p)["quick_slot"]
    assert payload == {
        "item_id": "draught", "name": "Healing Draught",
        "effect_tag": "heal_light", "hearts": 2, "quantity": 3,
    }


def test_explicit_quick_slot_binding_wins():
    p = Player(name="T", inventory=[_potion("a"), _potion("b", "Strong Draught", "heal_strong")],
               quick_slot_item_id="b")
    assert prog.quick_slot_payload(p)["item_id"] == "b"


def test_quick_slot_ignores_a_binding_that_left_the_bag():
    p = Player(name="T", inventory=[_potion("a")], quick_slot_item_id="gone")
    assert prog.quick_slot_payload(p) is None


# --- Drinking -------------------------------------------------------------


def test_use_in_combat_returns_hearts_and_decrements(player):
    _stock(player, _potion())
    with get_session() as session:
        p = session.get(Player, player)
        result = prog.apply_consumable(session, p, "draught", in_combat=True)
        session.commit()
    assert result["effect"] == {"kind": "heal", "hearts": 2, "buff": None, "duration_ms": 0}
    assert result["remaining"] == 2
    assert result["item_name"] == "Healing Draught"
    with get_session() as session:
        p = session.get(Player, player)
        assert p.inventory[0]["quantity"] == 2
        assert p.health_status == "healthy", "hearts are a combat reading, not a status change"


def test_use_out_of_combat_improves_health_status(player):
    _stock(player, _potion())
    with get_session() as session:
        p = session.get(Player, player)
        p.health_status = "hurt"
        result = prog.apply_consumable(session, p, "draught", in_combat=False)
        session.commit()
    assert result["effect"]["hearts"] == 0
    assert result["effect"]["health"] == "hurt -> winded"
    with get_session() as session:
        assert session.get(Player, player).health_status == "winded"


def test_heal_strong_moves_two_steps(player):
    _stock(player, _potion("strong", "Strong Draught", "heal_strong"))
    with get_session() as session:
        p = session.get(Player, player)
        p.health_status = "critical"
        result = prog.apply_consumable(session, p, "strong", in_combat=False)
        session.commit()
    assert result["effect"]["health"] == "critical -> hurt"


def test_use_out_of_combat_does_not_exceed_healthy(player):
    _stock(player, _potion("strong", "Strong Draught", "heal_strong"))
    with get_session() as session:
        p = session.get(Player, player)
        result = prog.apply_consumable(session, p, "strong", in_combat=False)
        session.commit()
    assert result["effect"]["health"] == "healthy -> healthy"


def test_surge_returns_buff_and_duration_and_no_hearts(player):
    _stock(player, _potion("surge_vial", "Surge Vial", "surge", quantity=1))
    with get_session() as session:
        p = session.get(Player, player)
        p.health_status = "hurt"
        result = prog.apply_consumable(session, p, "surge_vial", in_combat=True)
        session.commit()
    assert result["effect"]["buff"] == "damage"
    assert result["effect"]["hearts"] == 0
    assert result["effect"]["bonus"] == prog.SURGE_DAMAGE_BONUS
    assert result["effect"]["duration_ms"] == prog.CONSUMABLES["surge"]["ms"]
    with get_session() as session:
        p = session.get(Player, player)
        assert p.health_status == "hurt", "a surge is not a heal"
        assert p.inventory == []


def test_last_potion_leaves_the_bag(player):
    _stock(player, _potion(quantity=1))
    with get_session() as session:
        p = session.get(Player, player)
        result = prog.apply_consumable(session, p, "draught", in_combat=True)
        session.commit()
    assert result["remaining"] == 0
    with get_session() as session:
        assert session.get(Player, player).inventory == []


def test_use_missing_item_errors(player):
    with get_session() as session:
        p = session.get(Player, player)
        assert prog.apply_consumable(session, p, "ghost_vial", in_combat=True)["reason"] == "missing"


def test_use_non_consumable_errors(player):
    _stock(player, Item(id="rope", name="Rope", type="misc").to_dict())
    with get_session() as session:
        p = session.get(Player, player)
        result = prog.apply_consumable(session, p, "rope", in_combat=False)
    assert result["reason"] == "not_consumable"


def test_a_dead_player_cannot_drink(player):
    _stock(player, _potion())
    with get_session() as session:
        p = session.get(Player, player)
        p.health_status = "dead"
        result = prog.apply_consumable(session, p, "draught", in_combat=False)
        session.commit()
    assert "error" in result
    with get_session() as session:
        assert session.get(Player, player).inventory[0]["quantity"] == 3, "no potion is spent"


def test_apply_consumable_runs_in_the_callers_transaction(player):
    _stock(player, _potion())
    with get_session() as session:
        p = session.get(Player, player)
        prog.apply_consumable(session, p, "draught", in_combat=True)
        session.rollback()
    with get_session() as session:
        assert session.get(Player, player).inventory[0]["quantity"] == 3


def test_item_can_be_named_instead_of_ided(player):
    """The HUD sends ids; the DM and legacy data send names."""
    _stock(player, _potion())
    with get_session() as session:
        p = session.get(Player, player)
        result = prog.apply_consumable(session, p, "Healing Draught", in_combat=True)
        session.commit()
    assert result["success"] is True


# --- Legacy repair --------------------------------------------------------


def test_legacy_heal_potion_reads_as_a_draught(player):
    """A world that already has `effects: {heal: 30}` potions gains Q for free."""
    legacy = {"id": "old_potion", "name": "Old Potion", "type": "consumable",
              "effects": {"heal": 30}, "quantity": 1}
    _stock(player, legacy)
    with get_session() as session:
        p = session.get(Player, player)
        assert prog.quick_slot_payload(p)["effect_tag"] == "heal_light"
        result = prog.apply_consumable(session, p, "old_potion", in_combat=True)
        session.commit()
    assert result["effect"]["hearts"] == prog.CONSUMABLES["heal_light"]["hearts"]


def test_legacy_big_heal_reads_as_heal_strong():
    legacy = {"id": "elixir", "name": "Elixir", "type": "consumable",
              "effects": {"heal": 60}, "quantity": 1}
    assert prog.mechanize_item(legacy)["effect_tag"] == "heal_strong"


# --- The hand-off into the post-fight severity math (R8) ------------------
#
# `hearts_healed` travels from the client's optimistic Q press to
# /api/combat/result, where combat.py folds it into the health floor. That
# arithmetic is chunk B1's; these two pin the contract from this side and
# activate the moment `apply_combat_result` grows the parameter.


def _combat_module():
    try:
        from src.services import combat
    except ImportError:  # pragma: no cover - chunk mid-flight
        pytest.skip("services.combat unavailable")
    if "hearts_healed" not in inspect.signature(combat.apply_combat_result).parameters:
        pytest.skip("apply_combat_result(hearts_healed=...) lands with chunk B1")
    return combat


def test_hearts_healed_reduces_effective_hits(player):
    combat = _combat_module()
    changes = combat.apply_combat_result(player, "victory", hits_taken=3, hearts_healed=2)
    assert changes["health"] == "winded"


def test_hearts_healed_cannot_rescue_a_defeat(player):
    combat = _combat_module()
    changes = combat.apply_combat_result(player, "defeat", hits_taken=3, hearts_healed=3)
    assert changes["health"] == "critical"
