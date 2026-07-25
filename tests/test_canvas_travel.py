"""Canvas travel resolves in code and never walks around a locked gate.

`travel_now` and the `move_player` tool share one route check, so anything
`tests/test_travel_gate.py` proves about the DM path must hold here too.
"""

from src.models import (
    Connection,
    Location,
    LocationType,
    Player,
    WorldClock,
    get_session,
)
from src.services.travel import check_route, travel_now
from src.tools.world_write.player import move_player


def _build_world(session) -> dict[str, str]:
    """Square <-> Dock Row is open; Square <-> Hollow needs a gate pass."""
    town = Location(name="Town", type=LocationType.TOWN, depth=0, discovered=True)
    session.add(town)
    session.flush()

    square = Location(
        name="Square", type=LocationType.DISTRICT, parent_id=town.id, depth=1,
        discovered=True, visited=True, position_x=40.0, position_y=50.0,
    )
    dock = Location(
        name="Dock Row", type=LocationType.DISTRICT, parent_id=town.id, depth=1,
        discovered=True, position_x=70.0, position_y=50.0,
    )
    hollow = Location(
        name="Hollow", type=LocationType.POI, parent_id=town.id, depth=1,
        discovered=True, position_x=40.0, position_y=20.0,
    )
    # No connection, no parent/child relation — unreachable from Square
    island = Location(name="Far Isle", type=LocationType.POI, depth=0, discovered=True)
    session.add_all([square, dock, hollow, island])
    session.flush()

    session.add_all([
        Connection(
            from_location_id=square.id, to_location_id=dock.id,
            travel_type="walk", travel_time_hours=0.3,
            requirements=[], bidirectional=True, discovered=True,
        ),
        Connection(
            from_location_id=square.id, to_location_id=hollow.id,
            travel_type="walk", travel_time_hours=0.75,
            requirements=["gate pass"], bidirectional=True, discovered=True,
        ),
    ])
    session.add(WorldClock(day=1, hour=8, minute=0))

    player = Player(name="Tester", current_location_id=square.id)
    session.add(player)
    session.commit()

    return {
        "player": player.id, "town": town.id, "square": square.id,
        "dock": dock.id, "hollow": hollow.id, "island": island.id,
    }


def _location_of(player_id: str) -> str:
    with get_session() as session:
        return session.get(Player, player_id).current_location_id


def _clock() -> tuple[int, int, int]:
    with get_session() as session:
        clock = session.query(WorldClock).first()
        return clock.day, clock.hour, clock.minute or 0


def test_unlocked_travel_moves_and_advances_time(db):
    with get_session() as session:
        ids = _build_world(session)

    result = travel_now(ids["player"], ids["dock"])

    assert result["success"] is True
    assert result["from_id"] == ids["square"] and result["from_name"] == "Square"
    assert result["destination_id"] == ids["dock"]
    assert result["destination_name"] == "Dock Row"
    assert result["minutes"] == round(0.3 * 60) == 18
    assert _location_of(ids["player"]) == ids["dock"]
    assert _clock() == (1, 8, 18), "the clock must move by exactly the travel time"


def test_locked_route_blocks_and_does_not_move(db):
    with get_session() as session:
        ids = _build_world(session)

    result = travel_now(ids["player"], ids["hollow"])

    assert result.get("blocked") is True
    assert "gate pass" in result["requirements"]
    assert result["destination_name"] == "Hollow"
    assert _location_of(ids["player"]) == ids["square"], "a blocked route must not move the player"
    assert _clock() == (1, 8, 0), "a blocked route must not spend time"


def test_hierarchy_gate_bypass_still_blocked(db):
    """Mirrors test_travel_gate.py through the canvas path."""
    with get_session() as session:
        ids = _build_world(session)

    # Step out into the ungated parent container ...
    assert travel_now(ids["player"], ids["town"])["success"] is True
    # ... then into the gated child via hierarchy: must STILL block
    result = travel_now(ids["player"], ids["hollow"])

    assert result.get("blocked") is True
    assert "gate pass" in result["requirements"]
    assert _location_of(ids["player"]) == ids["town"]


def test_unreachable_destination_rejected(db):
    with get_session() as session:
        ids = _build_world(session)

    result = travel_now(ids["player"], ids["island"])

    assert result == {"error": "unreachable"}
    assert _location_of(ids["player"]) == ids["square"]


def test_missing_player_and_destination_rejected(db):
    with get_session() as session:
        ids = _build_world(session)

    assert travel_now("nobody", ids["dock"]) == {"error": "no_player"}
    assert travel_now(ids["player"], "nowhere") == {"error": "not_found"}


def test_first_visit_flag(db):
    with get_session() as session:
        ids = _build_world(session)

    assert travel_now(ids["player"], ids["dock"])["first_visit"] is True
    # Square was already visited when the world was built
    assert travel_now(ids["player"], ids["square"])["first_visit"] is False
    assert travel_now(ids["player"], ids["dock"])["first_visit"] is False


def test_move_player_tool_behaviour_unchanged(db):
    """The refactor onto check_route must not alter the tool's contract."""
    with get_session() as session:
        ids = _build_world(session)

    success = move_player(ids["player"], ids["dock"])
    assert set(success) == {"success", "destination", "travel_time_hours", "reminder"}
    assert success["destination"] == "Dock Row"
    assert success["travel_time_hours"] == 0.3

    move_player(ids["player"], ids["square"])
    blocked = move_player(ids["player"], ids["hollow"])
    assert set(blocked) == {"blocked", "destination", "requirements", "message"}
    assert blocked["destination"] == "Hollow"
    assert blocked["requirements"] == ["gate pass"]
    assert blocked["message"].startswith("This route has requirements the player must satisfy: ")
    assert "requirements_met=True" in blocked["message"]

    # Confirming the requirements still unlocks the same route
    assert move_player(ids["player"], ids["hollow"], requirements_met=True).get("success")


def test_check_route_reports_hierarchy_hops(db):
    with get_session() as session:
        ids = _build_world(session)

    with get_session() as session:
        into_child = check_route(session, ids["town"], ids["square"])
        out_to_parent = check_route(session, ids["square"], ids["town"])
        unmodeled = check_route(session, ids["dock"], ids["island"])

    assert into_child == {
        "travel_time_hours": 0.1, "blocked": False, "requirements": [], "reason": "",
    }
    assert out_to_parent["travel_time_hours"] == 0.1
    assert unmodeled["travel_time_hours"] == 0.5
