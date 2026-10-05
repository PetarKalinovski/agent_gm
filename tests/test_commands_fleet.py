"""Pins for what the 2026-10-04 fleet playtest (three AI players, 21 turns
each) found in the command layer: typed searches that could not reach the
scene's objects, an inn bed charged twice, the player standing in "the whole
town", and talk-only quests paying mission XP."""

import pytest

from src.models import (
    DMState, Event, Location, LocationType, NPC, Player, Quest, WorldClock, get_session,
)
from src.services.commands import STATE_TOOLS, CommandRunner
from src.services.scene_objects import normalize_objects
from src.tools.world_write.creation import create_location


@pytest.fixture()
def town(db):
    """Emberfall (a map container) > Market Square > The Drowned Lantern (an
    inn), and Wolf's Hollow beside the square — the seed's shape."""
    with get_session() as session:
        session.add(WorldClock(day=1, hour=19, minute=0))
        session.add(DMState(tension="low"))
        emberfall = Location(name="Emberfall", type=LocationType.TOWN, depth=0, is_map_container=True,
                             discovered=True)
        session.add(emberfall)
        session.flush()
        square = Location(
            name="Market Square", type=LocationType.DISTRICT, parent_id=emberfall.id, depth=1,
            discovered=True, visited=True,
            objects=normalize_objects([
                {"kind": "read", "label": "Notice board",
                 "payload": {"text": "TAKEN BY WOLVES — the Watch pays for true accounts."}},
                {"kind": "search", "label": "Abandoned market crate",
                 "payload": {"item": {"name": "Coil of Hemp Rope", "type": "misc", "value": 4}}},
                {"kind": "open", "label": "Iron strongbox",
                 "payload": {"lock": "a Watch padlock", "item": {"name": "Ledger", "type": "misc"}}},
            ]),
        )
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, parent_id=emberfall.id, depth=1,
                          discovered=True)
        session.add_all([square, hollow])
        session.flush()
        lantern = Location(
            name="The Drowned Lantern", type=LocationType.BUILDING, parent_id=square.id, depth=2,
            discovered=True, rest={"kind": "inn", "price": 8, "quality": "good"},
            objects=normalize_objects([{"kind": "rest", "label": "Upstairs bed"}]),
        )
        session.add(lantern)
        session.flush()
        coldiron = NPC(name="Captain Brann Coldiron", profession="watch captain", current_location_id=square.id)
        wolf = NPC(name="Hollow Wolf", current_location_id=hollow.id, danger_tier="weak")
        player = Player(name="Wenna Hale", current_location_id=square.id, currency=25)
        session.add_all([coldiron, wolf, player])
        session.commit()
        return {"player": player.id, "emberfall": emberfall.id, "square": square.id, "hollow": hollow.id,
                "lantern": lantern.id, "coldiron": coldiron.id, "wolf": wolf.id}


def _runner(w, at=None):
    if at:
        with get_session() as session:
            session.get(Player, w["player"]).current_location_id = w[at]
            session.commit()
    return CommandRunner(w["player"], present_ids=[w["coldiron"]])


def _coin(w):
    with get_session() as session:
        return session.get(Player, w["player"]).currency


def _where(w):
    with get_session() as session:
        return session.get(Player, w["player"]).current_location_id


def _pack(w):
    with get_session() as session:
        return [i.get("name") for i in session.get(Player, w["player"]).inventory or []]


# ----------------------------------------------------------------------
# @interact — the canvas's own resolver, from a typed line
# ----------------------------------------------------------------------

def test_interact_searches_a_scene_object_once(town):
    r = _runner(town)
    res = r.run("@interact search the crate")
    assert res.ok and res.tool == "interact" and "interact" in STATE_TOOLS
    assert res.result["kind"] == "search" and res.result["consumed"]
    assert "Coil of Hemp Rope" in _pack(town)
    assert "found Coil of Hemp Rope" in res.message          # the DM wrote it blind

    again = r.run("@interact Abandoned market crate")
    assert not again.ok and "already searched" in again.message
    assert _pack(town).count("Coil of Hemp Rope") == 1


def test_interact_tells_the_dm_what_the_find_is(town):
    """The seed's proof is in the item's description, which the DM's pack
    listing never shows."""
    with get_session() as session:
        hollow = session.get(Location, town["hollow"])
        hollow.objects = normalize_objects([
            {"kind": "take", "label": "Cracked bone whistle",
             "payload": {"item": {"name": "Cracked Bone Whistle", "type": "quest_item",
                                  "description": "Split down the barrel. The same make as Coldiron's."}}},
            {"kind": "search", "label": "Split supply crate",
             "payload": {"text": "Under the oilcloth, a glint.",
                         "item": {"name": "Greywater Silver Piece", "type": "misc", "value": 12}}},
        ])
        session.commit()
    r = _runner(town, at="hollow")
    whistle = r.run("@interact the cracked bone whistle (take)")       # copied from the brief's listing
    assert whistle.ok and "Split down the barrel" in whistle.message
    crate = r.run("@interact search the split crate")
    assert crate.ok and "found Greywater Silver Piece —" in crate.message
    assert 'what they saw: "Under the oilcloth' in crate.message


def test_interact_finds_a_body_by_what_prose_calls_it(town):
    with get_session() as session:
        session.get(Location, town["hollow"]).objects = normalize_objects([
            {"kind": "body", "label": "Hollow Wolf's body", "src": "engine",
             "payload": {"items": [{"name": "Wolf Pelt", "type": "misc", "description": "Thick grey fur."}],
                         "coin": 3}},
        ])
        session.commit()
    r = _runner(town, at="hollow")
    res = r.run("@interact search the wolf's corpse")
    assert res.ok and "Wolf Pelt (Thick grey fur.)" in res.message and "3 coin" in res.message
    assert _coin(town) == 28
    again = r.run("@interact the dead wolf")
    assert not again.ok and "already searched" in again.message


def test_an_authored_remains_is_not_read_as_a_body(town):
    with get_session() as session:
        session.get(Location, town["hollow"]).objects = normalize_objects([
            {"kind": "read", "label": "Ancient remains", "payload": {"text": "A carter's ring, still on the bone."}},
            {"kind": "body", "label": "Hollow Wolf's body", "src": "engine", "payload": {"items": [], "coin": 0}},
        ])
        session.commit()
    res = _runner(town, at="hollow").run("@interact the ancient remains")
    assert res.ok and res.result["kind"] == "read" and "carter's ring" in res.message


def test_interact_reads_and_reports_the_text(town):
    r = _runner(town)
    res = r.run("@interact Notice board")
    assert res.ok and "TAKEN BY WOLVES" in res.message
    assert r.run("@interact notice board").ok                 # reading never uses it up


def test_interact_with_a_locked_box_is_its_own_refusal(town):
    res = _runner(town).run("@interact strongbox")
    assert not res.ok and "locked" in res.message and "Watch padlock" in res.message
    assert "Ledger" not in _pack(town)


def test_interact_names_only_what_is_here(town):
    res = _runner(town).run("@interact Bone whistle")
    assert not res.ok
    assert "Notice board" in res.message and "Abandoned market crate" in res.message
    nothing = _runner(town, at="hollow").run("@interact crate")
    assert not nothing.ok and "nothing at Wolf's Hollow" in nothing.message


def test_interact_with_a_bed_is_a_night_charged_once(town):
    r = _runner(town, at="lantern")
    paid = r.run("@coin -8 | Maera's bed")                   # paying for the bed IS the night
    assert paid.ok and "@rest" in paid.message
    res = r.run("@interact bed")
    assert res.ok and "already taken" in res.message           # never a second whole day
    assert _coin(town) == 17


# ----------------------------------------------------------------------
# One bed, one charge
# ----------------------------------------------------------------------

def test_coin_then_rest_charges_the_bed_once(town):
    r = _runner(town, at="lantern")
    assert r.run("@coin -8 | a bed for the night").ok
    res = r.run("@rest")
    assert res.ok and "already taken" in res.message
    assert _coin(town) == 25 - 8
    assert any("@rest" in line for line in r.ledger())


def test_rest_then_coin_skips_the_restated_charge(town):
    r = _runner(town, at="lantern")
    assert r.run("@rest").ok
    assert _coin(town) == 17
    twice = r.run("@coin -8")
    assert not twice.ok and "not charged twice" in twice.message
    assert _coin(town) == 17
    assert r.run("@coin -8 | a round for the room").ok      # only the one restatement is skipped
    assert _coin(town) == 9


def test_a_prepaid_bed_does_not_fail_on_the_coin_already_taken(town):
    with get_session() as session:
        session.get(Player, town["player"]).currency = 8
        session.commit()
    r = _runner(town, at="lantern")
    assert r.run("@coin -8").ok
    assert r.run("@rest").ok                                  # not "the bed costs 8 and they carry 0"
    assert _coin(town) == 0


def test_a_different_payment_is_not_the_bed(town):
    r = _runner(town, at="lantern")
    assert r.run("@coin -3 | bread").ok
    res = r.run("@rest")
    assert res.ok and res.message == ""
    assert _coin(town) == 25 - 3 - 8


def _evening_howl(w):
    """The seed's 20:00 set piece: it wakes anyone in Emberfall who lay down before it."""
    with get_session() as session:
        session.add(Event(name="The Evening Howl", description="The wolves sing from the Hollow.",
                          event_type="meso", scheduled_day=1, scheduled_hour=20,
                          locations_involved=[w["hollow"]], player_visible=True))
        session.commit()


def test_a_broken_night_gives_the_bed_fee_back(town):
    """rest.py does not charge a night an event breaks; the DM's own bed fee
    must not stay taken either, or the next turn's @rest charges it again."""
    _evening_howl(town)
    r = _runner(town, at="lantern")
    res = r.run("@coin -8 | the bed")                        # the night runs, and is broken
    assert res.ok and res.result["interrupted"]
    assert _coin(town) == 25
    fee = r.run("@coin -8")                                  # a broken night is never charged
    assert not fee.ok and _coin(town) == 25


def test_a_bed_fee_after_a_broken_night_is_skipped(town):
    _evening_howl(town)
    r = _runner(town, at="lantern")
    assert r.run("@rest").ok
    fee = r.run("@coin -8")
    assert not fee.ok and "broken night is not charged" in fee.message
    assert _coin(town) == 25


def test_rest_walks_to_the_inn_next_door_and_still_charges_once(town):
    r = _runner(town)
    assert r.run("@coin -8").ok                               # paid in the square, slept next door
    res = r.run("@rest")
    assert res.ok and "walked to The Drowned Lantern" in res.message and "charged once" in res.message
    assert _where(town) == town["lantern"] and _coin(town) == 17


# ----------------------------------------------------------------------
# Nobody stands in the whole town
# ----------------------------------------------------------------------

def test_move_into_a_map_container_is_refused(town):
    res = _runner(town).run("@move Emberfall | through")
    assert not res.ok and "Emberfall is the whole town" in res.message
    assert "Market Square" in res.message and "@new-place" in res.message
    assert _where(town) == town["square"]


def test_npc_move_into_a_map_container_is_refused(town):
    r = _runner(town)
    res = r.run("@npc-move Coldiron | Emberfall")
    assert not res.ok and "whole town" in res.message
    nobody = r.run("@new-npc Garrison Clerk | trade: clerk | looks: ink | manner: dry | voice: flat | at: Emberfall")
    assert not nobody.ok and "whole town" in nobody.message
    with get_session() as session:
        assert session.get(NPC, town["coldiron"]).current_location_id == town["square"]
        assert session.query(NPC).filter(NPC.name == "Garrison Clerk").count() == 0


def test_an_unflagged_parentless_town_is_a_container_but_a_root_scene_is_not(db):
    with get_session() as session:
        town = Location(name="Millbrook", type=LocationType.TOWN, depth=0, discovered=True)
        square = Location(name="Town Square", type=LocationType.DISTRICT, depth=0, discovered=True)
        session.add_all([town, square])
        session.flush()
        session.add(Location(name="Fountain Court", type=LocationType.DISTRICT, parent_id=town.id, depth=1))
        session.add(Location(name="Cellar", type=LocationType.INTERIOR, parent_id=square.id, depth=1))
        player = Player(name="P", current_location_id=square.id)
        session.add(player)
        session.commit()
        player_id = player.id
    r = CommandRunner(player_id)
    assert "whole town" in r.run("@move Millbrook").message
    assert r.run("@move Cellar").ok
    assert r.run("@move Town Square").ok                      # a scene with a room off it


def test_a_go_to_step_must_lead_somewhere_new(town):
    r = _runner(town)
    here = r.run("@quest-new Report In | about: x | step: go_to Market Square | step: talk_to Coldiron")
    assert not here.ok and "already in Market Square" in here.message and "talk_to" in here.message
    above = r.run("@quest-new The Debt in the Hills | about: x | step: go_to Emberfall")
    assert not above.ok and "already in Emberfall" in above.message
    with get_session() as session:
        assert session.query(Quest).count() == 0
    assert r.run("@quest-new Teeth | about: x | step: go_to Wolf's Hollow").ok


def test_a_go_to_step_into_another_town_names_a_place_in_it(town):
    with get_session() as session:
        greywater = Location(name="Greywater", type=LocationType.CITY, depth=0, is_map_container=True,
                             discovered=True)
        session.add(greywater)
        session.flush()
        # Undiscovered, and still named: the refusal is for the DM, not the player
        session.add(Location(name="Counting House", type=LocationType.BUILDING, parent_id=greywater.id,
                             depth=1))
        session.commit()
    res = _runner(town).run("@quest-new Follow the Silver | about: x | step: go_to Greywater")
    assert not res.ok and "Greywater is the whole city" in res.message and "Counting House" in res.message
    assert _runner(town).run("@quest-new Follow the Silver | about: x | step: go_to Counting House").ok


def test_a_city_inside_a_planet_is_still_the_whole_city(town):
    """A parent does not make a city a scene (sw's Galactic City sits under Coruscant)."""
    with get_session() as session:
        planet = Location(name="Coruscant", type=LocationType.PLANET, depth=0, discovered=True)
        session.add(planet)
        session.flush()
        city = Location(name="Galactic City", type=LocationType.CITY, parent_id=planet.id, depth=1,
                        discovered=True)
        session.add(city)
        session.flush()
        session.add(Location(name="Senate District", type=LocationType.DISTRICT, parent_id=city.id,
                             depth=2, discovered=True))
        session.commit()
    res = _runner(town).run("@move Galactic City")
    assert not res.ok and "Galactic City is the whole city" in res.message and "Senate District" in res.message
    assert "Coruscant is the whole planet" in _runner(town).run("@npc-move Coldiron | Coruscant").message


def test_a_town_with_nothing_in_it_yet_is_somewhere_to_stand(town):
    """With no place under it there is nothing else to name — refusing would
    make it unreachable."""
    with get_session() as session:
        session.add(Location(name="Greywater", type=LocationType.CITY, depth=0, is_map_container=True,
                             discovered=True))
        session.commit()
    assert _runner(town).run("@move Greywater").ok


@pytest.mark.parametrize("shape", ["temple", "home_town", "start_town"])
def test_generated_worlds_flag_scenes_as_map_containers(town, shape):
    """WorldForge sets is_map_container on places people stand in: sw's Jedi
    Temple (a building with thirteen halls), asoiaf's Ashford Town (the
    player's start, with residents). None of them is "the whole town"."""
    with get_session() as session:
        place = Location(
            name="The Old Keep", depth=1, is_map_container=True, discovered=True,
            type=LocationType.BUILDING if shape == "temple" else LocationType.TOWN,
            visited=shape == "start_town",
        )
        session.add(place)
        session.flush()
        session.add(Location(name="Keep Cellar", type=LocationType.ROOM, parent_id=place.id, depth=2))
        if shape == "home_town":
            session.add(NPC(name="Old Wenlock", current_location_id=town["square"], home_location_id=place.id))
        session.commit()
        keep_id = place.id
    r = _runner(town)
    assert r.run("@npc-move Coldiron | The Old Keep").ok
    assert r.run("@move The Old Keep").ok and _where(town) == keep_id


def test_new_places_sit_one_level_below_their_parent(town):
    inside = create_location(player_id=town["player"], name="Cooper's Rooms", description="A stair up.")
    outside = create_location(player_id=town["player"], name="North Road", description="Mud.",
                              kind="street", inside_current=False)
    with get_session() as session:
        rooms = session.get(Location, inside["id"])
        road = session.get(Location, outside["id"])
        assert (rooms.parent_id, rooms.depth) == (town["square"], 2)
        assert (road.parent_id, road.depth) == (town["emberfall"], 1)


# ----------------------------------------------------------------------
# Quest scale follows what the quest asks
# ----------------------------------------------------------------------

def _scale(title):
    with get_session() as session:
        return session.query(Quest).filter(Quest.title == title).one().scale


def test_a_talk_and_walk_quest_is_an_errand(town):
    res = _runner(town).run("@quest-new The Hand Above the Fangs | about: x | from: Coldiron | scale: mission "
                            "| step: go_to Wolf's Hollow | step: talk_to Coldiron")
    assert res.ok and "scale lowered to errand" in res.message
    assert _scale("The Hand Above the Fangs") == "errand"


def test_an_unscaled_small_quest_is_an_errand_without_comment(town):
    res = _runner(town).run("@quest-new Ask Around | about: x | step: talk_to Coldiron")
    assert res.ok and res.message == ""
    assert _scale("Ask Around") == "errand"


def test_mission_needs_three_steps_with_a_kill_or_delivery(town):
    r = _runner(town)
    job = r.run("@quest-new Pelts | about: x | scale: saga | step: go_to Wolf's Hollow "
                "| step: obtain Wolf Pelt | step: talk_to Coldiron")
    assert job.ok and "scale lowered to job" in job.message
    assert _scale("Pelts") == "job"
    mission = r.run("@quest-new Teeth on the Road | about: x | scale: mission | step: go_to Wolf's Hollow "
                    "| step: kill Hollow Wolf | step: talk_to Coldiron")
    assert mission.ok and mission.message == ""
    assert _scale("Teeth on the Road") == "mission"


def test_scale_and_xp_notes_travel_together(town):
    res = _runner(town).run("@quest-new Rats | about: x | scale: saga | step: custom Clear the cellar "
                            "| reward: 50 xp")
    assert res.ok and "scale lowered to errand" in res.message and "xp reward was dropped" in res.message
