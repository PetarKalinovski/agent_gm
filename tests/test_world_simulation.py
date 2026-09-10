"""World simulation runs exactly once per in-game day."""

from src.models import DMState, WorldClock, get_session
from src.services.world_simulation import maybe_advance_world


def _setup_world(day=1):
    with get_session() as session:
        session.add(WorldClock(day=day, hour=8))
        session.add(DMState())
        session.commit()


def test_first_day_is_claimed_but_never_simulated(db):
    """The opening day is authored: the first call claims it and runs no
    agent (playtesting saw the staged antagonist moved into the player's
    tavern on turn one). The same day never re-triggers."""
    _setup_world(day=1)
    calls = []

    result = maybe_advance_world(run_agent=lambda: calls.append(1) or "moved a faction")
    assert result["advanced"] is False
    assert calls == []

    result = maybe_advance_world(run_agent=lambda: calls.append(2) or "x")
    assert result["advanced"] is False
    assert result["reason"] == "already simulated today"
    assert calls == []


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
    maybe_advance_world(run_agent=lambda: "day1 claimed")
    with get_session() as session:
        session.query(WorldClock).first().advance(24)
        session.commit()

    def boom():
        raise RuntimeError("model exploded")

    result = maybe_advance_world(run_agent=boom)
    assert result["advanced"] is False

    # Day stays claimed — no retry storm on every subsequent turn
    result = maybe_advance_world(run_agent=lambda: "should not run")
    assert result["advanced"] is False
    assert result["reason"] == "already simulated today"


# ---------------------------------------------------------------------------
# The simulator's move_npc is guarded in code
# ---------------------------------------------------------------------------

from src.models import Location, LocationType, NPC, Player
from src.services.world_simulation import sim_move_npc, staged_npc_ids


def _world_with_two_rooms():
    with get_session() as session:
        square = Location(name="Square", type=LocationType.DISTRICT)
        hollow = Location(name="Hollow", type=LocationType.DISTRICT)
        session.add_all([square, hollow])
        session.flush()
        player = Player(name="Rook", current_location_id=square.id)
        vex = NPC(name="Vex", current_location_id=hollow.id)
        piper = NPC(name="Piper", current_location_id=hollow.id)
        session.add_all([player, vex, piper])
        session.commit()
        return square.id, hollow.id, vex.id, piper.id


def test_sim_never_moves_an_npc_onto_the_player(db):
    _setup_world(day=2)
    square, hollow, vex, piper = _world_with_two_rooms()

    result = sim_move_npc(piper, square)

    assert "refused" in result.get("error", "")
    with get_session() as session:
        assert session.get(NPC, piper).current_location_id == hollow


def test_sim_never_moves_a_staged_npc(db):
    from src.models.campaign import Campaign, StoryBeat

    _setup_world(day=2)
    square, hollow, vex, piper = _world_with_two_rooms()
    with get_session() as session:
        player = session.query(Player).first()
        campaign = Campaign(player_id=player.id, title="T", status="active",
                            villain_npc_id=vex.id if False else vex)
        session.add(campaign)
        session.flush()
        session.add(StoryBeat(
            campaign_id=campaign.id, act=1, sequence=1, title="Meet Piper",
            status="armed", trigger_type="location",
            trigger_data={"npc_id": piper},
        ))
        session.commit()
        other = Location(name="Elsewhere", type=LocationType.DISTRICT)
        session.add(other)
        session.commit()
        other_id = other.id
        assert {vex, piper} <= staged_npc_ids(session)

    assert "refused" in sim_move_npc(vex, other_id).get("error", "")
    assert "refused" in sim_move_npc(piper, other_id).get("error", "")


def test_sim_moves_an_unstaged_npc_between_empty_rooms(db):
    _setup_world(day=2)
    square, hollow, vex, piper = _world_with_two_rooms()
    with get_session() as session:
        other = Location(name="Elsewhere", type=LocationType.DISTRICT)
        session.add(other)
        session.commit()
        other_id = other.id

    result = sim_move_npc(piper, other_id)

    assert result.get("success") is True
    with get_session() as session:
        assert session.get(NPC, piper).current_location_id == other_id
