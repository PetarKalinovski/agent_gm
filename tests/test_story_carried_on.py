"""The prose walked on past the reply's @move (fleet7-tamsin T9).

`@move North Gatehouse | through`, then "Wolf's Hollow opens below you" and
the Hollow's cracked bone whistle at her feet: the database kept her at the
gatehouse, so the whistle she pocketed never reached her pack. A neighbouring
place the prose names AND arrives at (or shows by its own Things) is where
the story put them — the engine follows when the way is open, and says what
did not happen when it is not.
"""

import pytest

from src.models import Connection, Location, LocationType, Player, get_session
from src.web.server import _carried_on_to

T9 = ("The gate guard glances at your pass and waves you through. The north road unspools pale and empty "
      "ahead of you.\n\nWolf's Hollow opens below you — a half-collapsed shepherd's shelter. On the ground "
      "just inside the threshold lies a whistle carved from bone — cracked along one side, a leather cord "
      "still knotted through it.")


@pytest.fixture()
def gate(db):
    with get_session() as session:
        square = Location(name="Market Square", type=LocationType.DISTRICT, discovered=True)
        gatehouse = Location(name="North Gatehouse", type=LocationType.POI, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, discovered=True, objects=[
            {"id": "whistle", "kind": "take", "label": "Cracked bone whistle", "x": 50, "y": 60,
             "payload": {"item": {"name": "Cracked Bone Whistle", "type": "misc"}}},
        ])
        session.add_all([square, gatehouse, hollow])
        session.flush()
        session.add_all([
            Connection(from_location_id=square.id, to_location_id=gatehouse.id, travel_type="walk",
                       travel_time_hours=0.1, bidirectional=True, discovered=True, requirements=[]),
            Connection(from_location_id=gatehouse.id, to_location_id=hollow.id, travel_type="walk",
                       travel_time_hours=0.75, bidirectional=True, discovered=True,
                       requirements=["the player needs a gate pass from the Captain"]),
        ])
        player = Player(name="Tamsin Rook", current_location_id=gatehouse.id,
                        inventory=[{"id": "gate_pass", "name": "Gate Pass", "type": "quest_item"}])
        session.add(player)
        session.commit()
        return {"player": player.id, "square": square.id, "gatehouse": gatehouse.id, "hollow": hollow.id}


def test_the_hollow_the_prose_reached_is_where_the_story_put_her(gate):
    carried = _carried_on_to(gate["player"], T9, left_from=gate["square"])
    assert carried["id"] == gate["hollow"] and carried["from"] == "North Gatehouse"
    assert carried["blocked"] is False
    assert carried["shown"] == ["Cracked bone whistle"]


def test_without_the_pass_nothing_there_happened(gate):
    with get_session() as session:
        session.get(Player, gate["player"]).inventory = []
        session.commit()
    carried = _carried_on_to(gate["player"], T9, left_from=gate["square"])
    assert carried["blocked"] is True
    assert carried["requirements"] == ["the player needs a gate pass from the Captain"]


@pytest.mark.parametrize("prose", [
    "The road to Wolf's Hollow climbs into the dark ahead of you.",
    "Wolf's Hollow lies an hour north, past the last lanterns.",
    "Sorrel swears the whistle came from Wolf's Hollow.\nYou say nothing.",
    "You turn your back on the gate. Nothing moves on the road.",
])
def test_naming_it_is_not_being_there(gate, prose):
    assert _carried_on_to(gate["player"], prose, left_from=gate["square"]) is None


def test_reaching_it_by_name_is_enough(gate):
    carried = _carried_on_to(gate["player"], "You crest the last rise and reach Wolf's Hollow at a run.",
                             left_from=gate["square"])
    assert carried["id"] == gate["hollow"] and carried["shown"] == []


def test_the_place_they_just_left_is_no_destination(gate):
    prose = "You come back down from Wolf's Hollow, the cracked bone whistle still on your mind."
    with get_session() as session:
        session.get(Player, gate["player"]).current_location_id = gate["gatehouse"]
        session.commit()
    assert _carried_on_to(gate["player"], prose, left_from=gate["hollow"]) is None
