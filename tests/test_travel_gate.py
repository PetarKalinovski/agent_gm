"""Requirement-locked destinations stay locked on ANY approach path.

Playtest regression: square -> town container -> gated hollow bypassed the
north-gate requirements because hierarchy moves skipped the connection check.
"""

from src.models import Connection, Location, LocationType, Player, get_session
from src.tools.world_write.player import move_player


def _build_world(session):
    town = Location(name="Town", type=LocationType.TOWN, depth=0)
    session.add(town)
    session.flush()
    square = Location(name="Square", type=LocationType.DISTRICT, parent_id=town.id, depth=1)
    hollow = Location(name="Hollow", type=LocationType.POI, parent_id=town.id, depth=1)
    session.add_all([square, hollow])
    session.flush()
    session.add(Connection(
        from_location_id=square.id, to_location_id=hollow.id,
        travel_type="walk", travel_time_hours=0.75,
        requirements=["gate pass"], bidirectional=True, discovered=True,
    ))
    player = Player(name="Tester", current_location_id=square.id)
    session.add(player)
    session.commit()
    return player.id, town.id, square.id, hollow.id


def test_direct_locked_route_blocks(db):
    with get_session() as session:
        player_id, _, _, hollow_id = _build_world(session)
    result = move_player(player_id, hollow_id)
    assert result.get("blocked") and "gate pass" in result["requirements"]


def test_hierarchy_hop_cannot_bypass_gate(db):
    with get_session() as session:
        player_id, town_id, _, hollow_id = _build_world(session)
    # Hop into the parent container (ungated) ...
    assert move_player(player_id, town_id).get("success")
    # ... then into the gated child via hierarchy: must STILL block
    result = move_player(player_id, hollow_id)
    assert result.get("blocked") and "gate pass" in result["requirements"]


def test_requirements_met_unlocks_any_path(db):
    with get_session() as session:
        player_id, town_id, _, hollow_id = _build_world(session)
    assert move_player(player_id, town_id).get("success")
    assert move_player(player_id, hollow_id, requirements_met=True).get("success")


def test_ungated_hierarchy_moves_still_free(db):
    with get_session() as session:
        player_id, town_id, square_id, _ = _build_world(session)
    assert move_player(player_id, town_id).get("success")
    result = move_player(player_id, square_id)
    assert result.get("success") and result["travel_time_hours"] == 0.1