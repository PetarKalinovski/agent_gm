"""World simulation runs exactly once per in-game day."""

from src.models import DMState, WorldClock, get_session
from src.services.world_simulation import maybe_advance_world


def _setup_world(day=1):
    with get_session() as session:
        session.add(WorldClock(day=day, hour=8))
        session.add(DMState())
        session.commit()


def test_first_day_advances_once_then_gates(db):
    _setup_world(day=1)
    calls = []

    result = maybe_advance_world(run_agent=lambda: calls.append(1) or "moved a faction")
    assert result["advanced"] is True
    assert calls == [1]

    # Same day: no second run
    result = maybe_advance_world(run_agent=lambda: calls.append(2) or "x")
    assert result["advanced"] is False
    assert calls == [1]


def test_new_day_triggers_next_world_turn(db):
    _setup_world(day=1)
    maybe_advance_world(run_agent=lambda: "day1")

    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.advance(24)  # next day
        session.commit()

    result = maybe_advance_world(run_agent=lambda: "day2")
    assert result["advanced"] is True
    assert result["day"] == 2


def test_failed_sim_does_not_retry_same_day(db):
    _setup_world(day=1)

    def boom():
        raise RuntimeError("model exploded")

    result = maybe_advance_world(run_agent=boom)
    assert result["advanced"] is False

    # Day stays claimed — no retry storm on every subsequent turn
    result = maybe_advance_world(run_agent=lambda: "should not run")
    assert result["advanced"] is False
    assert result["reason"] == "already simulated today"
