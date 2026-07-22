"""Mechanical tension enforcement: floors from health loss, decay on calm.

Playtest regression: an entire wolf standoff played on the 'explore' track
because the DM never called update_dm_state — tension is now floored and
decayed in code.
"""

import pytest

from src.models import DMState, Player, get_session
from src.services.tension import (
    apply_tension_floor,
    enforce_tension,
    snapshot_turn_tension,
)


@pytest.fixture()
def world(db):
    with get_session() as session:
        p = Player(name="Tester")
        session.add(p)
        session.add(DMState(tension="low"))
        session.commit()
        return p.id


def _set_health(player_id, status):
    with get_session() as session:
        session.get(Player, player_id).health_status = status
        session.commit()


def _set_tension(value):
    with get_session() as session:
        session.query(DMState).first().tension = value
        session.commit()


def _tension():
    with get_session() as session:
        return session.query(DMState).first().tension


def test_health_loss_floors_tension(world):
    start = snapshot_turn_tension(world)
    _set_health(world, "hurt")
    result = enforce_tension(start, world)
    assert result["changed"] and _tension() == "rising"


def test_badly_hurt_floors_high(world):
    start = snapshot_turn_tension(world)
    _set_health(world, "badly_hurt")
    enforce_tension(start, world)
    assert _tension() == "high"


def test_critical_floors_climax(world):
    start = snapshot_turn_tension(world)
    _set_health(world, "critical")
    enforce_tension(start, world)
    assert _tension() == "climax"


def test_floor_never_lowers_dm_tension(world):
    _set_tension("climax")
    start = snapshot_turn_tension(world)
    _set_health(world, "winded")  # floor would be "rising"
    enforce_tension(start, world)
    assert _tension() == "climax"


def test_death_does_not_floor(world):
    start = snapshot_turn_tension(world)
    _set_health(world, "dead")
    enforce_tension(start, world)
    assert _tension() == "low"  # somber handling keys off health, not tension


def test_decay_steps_down_after_calm_turns(world):
    _set_tension("high")
    for _ in range(3):  # DECAY_TURNS["high"] == 3
        start = snapshot_turn_tension(world)
        enforce_tension(start, world)
    assert _tension() == "rising"


def test_dm_authored_change_resets_decay(world):
    _set_tension("high")
    # Two calm turns...
    for _ in range(2):
        enforce_tension(snapshot_turn_tension(world), world)
    # ...then the DM re-escalates mid-turn: snapshot low-context, DM writes climax
    start = snapshot_turn_tension(world)
    _set_tension("climax")
    enforce_tension(start, world)
    assert _tension() == "climax"
    # Decay counter restarted: two more calm turns must NOT decay climax->high->rising
    enforce_tension(snapshot_turn_tension(world), world)
    assert _tension() == "climax"
    enforce_tension(snapshot_turn_tension(world), world)
    assert _tension() == "high"


def test_apply_tension_floor_helper(world):
    result = apply_tension_floor("high", "combat start")
    assert result["changed"] and _tension() == "high"
    # Never lowers
    _set_tension("climax")
    result = apply_tension_floor("rising", "aftershock")
    assert not result["changed"] and _tension() == "climax"


def test_low_stays_low_on_calm_turns(world):
    for _ in range(6):
        enforce_tension(snapshot_turn_tension(world), world)
    assert _tension() == "low"
