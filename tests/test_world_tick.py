"""World tick mechanics: time, event delivery, tension escalation."""

from src.models import DMState, Event, WorldClock, get_session
from src.services.world_tick import (
    enforce_minimum_time_cost,
    run_world_tick,
    snapshot_clock,
)


def _setup_clock(day=1, hour=8):
    with get_session() as session:
        clock = WorldClock(day=day, hour=hour)
        session.add(clock)
        session.commit()


def test_fractional_time_accumulates(db):
    _setup_clock()
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.advance(0.25)  # 15 min
        clock.advance(0.25)
        clock.advance(0.5)
        session.commit()
        assert clock.hour == 9, "three fractional advances must add up to a full hour"


def test_minimum_time_cost_enforced_when_clock_unchanged(db):
    _setup_clock()
    start = snapshot_clock()
    result = enforce_minimum_time_cost(start, minimum_hours=1.0)
    assert result["enforced"] is True
    assert snapshot_clock() != start


def test_minimum_time_cost_skipped_when_dm_advanced(db):
    _setup_clock()
    start = snapshot_clock()
    with get_session() as session:
        clock = session.query(WorldClock).first()
        clock.advance(2)
        session.commit()
    result = enforce_minimum_time_cost(start)
    assert result["enforced"] is False


def test_scheduled_event_fires_and_redelivers_until_giveup(db):
    _setup_clock(day=2, hour=10)
    with get_session() as session:
        session.add(Event(
            name="Blockade",
            description="The docks are blockaded",
            event_type="macro",
            scheduled_day=2,
            scheduled_hour=9,
        ))
        session.commit()

    # Fires on the first tick
    tick = run_world_tick()
    assert any(e["name"] == "Blockade" for e in tick["fired_events"])

    # Not narrated → re-surfaced on following ticks
    tick2 = run_world_tick()
    assert any(e["name"] == "Blockade" for e in tick2["undelivered_events"])

    # After the attempt budget it stops nagging and is marked delivered
    run_world_tick()
    run_world_tick()
    tick5 = run_world_tick()
    assert not any(e["name"] == "Blockade" for e in tick5["undelivered_events"])
    with get_session() as session:
        event = session.query(Event).filter(Event.name == "Blockade").first()
        assert event.narrated_to_player is True


def test_tension_escalates_after_quiet_turns(db):
    _setup_clock()
    for _ in range(7):
        tick = run_world_tick()
        assert not tick["auto_escalated"]

    tick = run_world_tick()  # 8th quiet turn
    assert tick["auto_escalated"] is True
    with get_session() as session:
        state = session.query(DMState).first()
        assert state.tension == "rising"
