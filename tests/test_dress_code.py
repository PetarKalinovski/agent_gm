"""A door policy is enforced in `check_route`, the one shared choke point.

`move_player`, `travel_now` and the canvas door's `locked` flag all read
`check_route`, so one insertion point gives all three. The legacy guarantee
that matters most: `check_route(player=None)` never evaluates a dress code,
which is why `tests/test_travel_gate.py` passes unmodified.
"""

from sqlalchemy import inspect

from src.models import (
    Connection,
    Location,
    LocationType,
    Player,
    WorldClock,
    get_engine,
    get_session,
)
from src.services import progression as prog
from src.services.travel import check_route, travel_now
from src.tools.world_write.player import move_player

LANTERN_REASON = (
    "Maera keeps a warm room and a clean floor: no covered faces under her "
    "roof, and nobody who has been sleeping in the mudflats."
)

HOOD = {
    "id": "ashfall_hood", "name": "Ashfall Hood", "type": "armor",
    "slot": "armor", "tier": "common", "quantity": 1,
    "visual_desc": "a soot-grey hood that swallows the face",
    "attire": {"style": "rough", "uniform_faction_id": None, "concealing": True},
}


def _build_world(session, *, dress_code=None, hooded=False, route_locked=False):
    """Square -> The Drowned Lantern, with the tavern optionally picky."""
    town = Location(name="Town", type=LocationType.TOWN, depth=0, discovered=True)
    session.add(town)
    session.flush()

    square = Location(name="Square", type=LocationType.DISTRICT, parent_id=town.id,
                      depth=1, discovered=True, visited=True)
    tavern = Location(name="The Drowned Lantern", type=LocationType.BUILDING,
                      parent_id=square.id, depth=2, discovered=True,
                      dress_code=dress_code)
    session.add_all([square, tavern])
    session.flush()

    session.add(Connection(
        from_location_id=square.id, to_location_id=tavern.id,
        travel_type="walk", travel_time_hours=0.1,
        requirements=["the yard key"] if route_locked else [],
        bidirectional=True, discovered=True,
    ))
    session.add(WorldClock(day=1, hour=8, minute=0))

    player = Player(
        name="Rook Fennick", current_location_id=square.id, level=10,
        inventory=[dict(HOOD)] if hooded else [],
        equipment={"armor": prog.item_key(HOOD)} if hooded else {},
    )
    session.add(player)
    session.commit()
    return {"player": player.id, "square": square.id, "tavern": tavern.id}


_LANTERN = {"min_style": None, "banned_styles": ["concealing", "filthy"],
            "reason": LANTERN_REASON}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_locations_carry_a_nullable_dress_code(db):
    columns = {c["name"]: c for c in inspect(get_engine()).get_columns("locations")}
    assert "dress_code" in columns
    assert columns["dress_code"]["nullable"] is True

    with get_session() as session:
        ids = _build_world(session)
        assert session.get(Location, ids["square"]).dress_code is None


# ---------------------------------------------------------------------------
# Legacy parity: no player row, no dress evaluation
# ---------------------------------------------------------------------------


def test_check_route_without_a_player_never_reads_a_dress_code(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=True)

    with get_session() as session:
        route = check_route(session, ids["square"], ids["tavern"])

    assert route == {"travel_time_hours": 0.1, "blocked": False,
                     "requirements": [], "reason": ""}


def test_a_null_dress_code_is_never_blocked(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=None, hooded=True)
        player = session.get(Player, ids["player"])
        route = check_route(session, ids["square"], ids["tavern"], player)

    assert route["blocked"] is False and route["reason"] == ""


def test_a_dress_code_admits_anyone_dressed_right(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=False)
        player = session.get(Player, ids["player"])
        route = check_route(session, ids["square"], ids["tavern"], player)

    assert route["blocked"] is False and route["requirements"] == []


# ---------------------------------------------------------------------------
# Rule 3
# ---------------------------------------------------------------------------


def test_a_dress_only_block_reports_reason_dress(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=True)
        player = session.get(Player, ids["player"])
        route = check_route(session, ids["square"], ids["tavern"], player)

    assert route["blocked"] is True and route["reason"] == "dress"
    assert route["requirements"] == [
        f"{LANTERN_REASON} (the player currently reads as someone who does not "
        "want to be seen)"
    ]


def test_a_route_requirement_keeps_precedence_but_both_ride_along(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=True,
                           route_locked=True)
        player = session.get(Player, ids["player"])
        route = check_route(session, ids["square"], ids["tavern"], player)

    assert route["reason"] == "route", "the harder-to-explain blocker names the refusal"
    assert route["requirements"][0] == "the yard key"
    assert LANTERN_REASON in route["requirements"][1]


def test_a_dress_code_is_a_door_policy_not_a_road_condition(db):
    """No modeled Connection at all — the hierarchy hop still hits the door."""
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=True)
        session.query(Connection).delete()
        session.commit()
        player = session.get(Player, ids["player"])
        route = check_route(session, ids["square"], ids["tavern"], player)

    assert route["blocked"] is True and route["reason"] == "dress"


# ---------------------------------------------------------------------------
# move_player — the third blocked message, and the one override
# ---------------------------------------------------------------------------


def test_move_player_refuses_then_yields_to_requirements_met(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=True)

    blocked = move_player(ids["player"], ids["tavern"])
    assert set(blocked) == {"blocked", "destination", "requirements", "message"}
    assert blocked["message"].startswith(
        "The Drowned Lantern will not take them dressed as they are: "
    )
    assert LANTERN_REASON in blocked["message"]
    assert "@move <place> | through" in blocked["message"]

    with get_session() as session:
        assert session.get(Player, ids["player"]).current_location_id == ids["square"]

    assert move_player(ids["player"], ids["tavern"], requirements_met=True).get("success")
    with get_session() as session:
        assert session.get(Player, ids["player"]).current_location_id == ids["tavern"]


def test_move_player_keeps_the_route_message_when_both_block(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=True,
                           route_locked=True)

    blocked = move_player(ids["player"], ids["tavern"])
    assert blocked["message"].startswith(
        "This route has requirements the player must satisfy: "
    )
    assert "the yard key" in blocked["message"] and LANTERN_REASON in blocked["message"]


# ---------------------------------------------------------------------------
# travel_now — the canvas path blocks on exactly the same verdict
# ---------------------------------------------------------------------------


def test_travel_now_blocks_on_the_dress_code(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=True)

    result = travel_now(ids["player"], ids["tavern"])
    assert result["blocked"] is True and result["reason"] == "dress"
    assert result["destination_name"] == "The Drowned Lantern"
    assert LANTERN_REASON in result["requirements"][0]

    with get_session() as session:
        assert session.get(Player, ids["player"]).current_location_id == ids["square"]
        clock = session.query(WorldClock).first()
        assert (clock.day, clock.hour, clock.minute) == (1, 8, 0), "a refusal costs no time"


def test_travel_now_walks_straight_in_without_the_hood(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=False)

    assert travel_now(ids["player"], ids["tavern"])["success"] is True


def test_taking_the_hood_off_opens_the_door(db):
    with get_session() as session:
        ids = _build_world(session, dress_code=_LANTERN, hooded=True)

    assert travel_now(ids["player"], ids["tavern"]).get("blocked") is True

    with get_session() as session:
        player = session.get(Player, ids["player"])
        player.equipment = {"armor": None}      # they took the coat off
        session.commit()

    assert travel_now(ids["player"], ids["tavern"])["success"] is True
