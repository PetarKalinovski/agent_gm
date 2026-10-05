"""The world simulator writes events about real places, with the story intact.

fleet5-wenna T18 (a rest ran the day tick): "Northern Howl Investigation" sent
Captain Coldiron — kept in the square by the campaign — on a patrol to
'northern_hills', and "Third Night of the Calling" finished a "three-night
ritual" at 'north_gate' and 'town_square'. None of those places exists, and
the campaign's truth is whistle and scent-marker training.
"""

import pytest

from src.models import Event, Location, LocationType, NPC, Player, get_session
from src.models.campaign import Campaign
from src.services import world_simulation as sim


@pytest.fixture()
def emberfall(db):
    with get_session() as session:
        town = Location(name="Emberfall", type=LocationType.TOWN, discovered=True)
        session.add(town)
        session.flush()
        square = Location(name="Market Square", type=LocationType.DISTRICT, parent_id=town.id, discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, parent_id=town.id, discovered=True)
        session.add_all([square, hollow])
        session.flush()
        coldiron = NPC(name="Captain Brann Coldiron", current_location_id=square.id)
        hesk = NPC(name="Hesk Bramble", current_location_id=square.id)
        session.add_all([coldiron, hesk])
        player = Player(name="Wenna Cole", current_location_id=square.id)
        session.add(player)
        session.flush()
        session.add(Campaign(
            player_id=player.id, title="The Pattern in the Howling", status="active",
            ally_npc_ids=[coldiron.id],
            fixed_truths=[{"id": "truth_location", "kind": "the_location", "revealed": False,
                           "statement": "Vane trains the wolves at Wolf's Hollow with a bone whistle "
                                        "and buried scent-markers."}],
        ))
        session.commit()
        return {"town": town.id, "square": square.id, "hollow": hollow.id,
                "coldiron": coldiron.id, "hesk": hesk.id}


def _events():
    with get_session() as session:
        return {e.name: list(e.locations_involved or []) for e in session.query(Event).all()}


def test_an_event_at_a_place_that_does_not_exist_is_refused(emberfall):
    res = sim.sim_create_event(name="Third Night of the Calling", description="A ritual.", event_type="meso",
                               locations_involved=["northern_hills", "north_gate"])
    assert "are not places in this world" in res["error"] and "'northern_hills'" in res["error"]
    res = sim.sim_schedule_world_event(name="Watch Patrols Increased", description="Doubled.",
                                       event_type="meso", scheduled_day=2, scheduled_hour=9,
                                       locations_involved=["town_square"])
    assert "'town_square' is not a place in this world" in res["error"]
    assert _events() == {}


def test_names_and_slugs_resolve_to_the_real_ids(emberfall):
    res = sim.sim_create_event(name="Cart Talk", description="Rationing talk starts.", event_type="meso",
                               locations_involved=["market_square", "Wolf's Hollow", emberfall["town"]])
    assert "error" not in res
    assert _events()["Cart Talk"] == [emberfall["square"], emberfall["hollow"], emberfall["town"]]


def test_a_staged_person_cannot_be_sent_elsewhere_by_an_event(emberfall):
    res = sim.sim_create_event(name="Northern Howl Investigation", description="A patrol to the Hollow.",
                               event_type="meso", locations_involved=[emberfall["hollow"]],
                               npcs_involved=[emberfall["coldiron"]])
    assert "Captain Brann Coldiron is staged by the campaign at Market Square" in res["error"]
    # Where they stand — or the town that holds it — is fine; unstaged people go anywhere
    assert "error" not in sim.sim_create_event(
        name="Muster", description="The Watch musters.", event_type="meso",
        locations_involved=[emberfall["town"]], npcs_involved=[emberfall["coldiron"]])
    assert "error" not in sim.sim_schedule_world_event(
        name="Hesk Goes North", description="Hesk takes a cart north.", event_type="meso",
        scheduled_day=3, scheduled_hour=8, locations_involved=[emberfall["hollow"]],
        npcs_involved=[emberfall["hesk"]])


def test_the_simulator_is_told_the_places_and_the_truths(emberfall):
    with get_session() as session:
        facts = sim.world_facts(session)
    assert f"- {emberfall['square']} — Market Square" in facts
    assert "STAGED BY THE STORY" in facts and "Captain Brann Coldiron (at Market Square)" in facts
    assert "FIXED TRUTHS (constraints" in facts and "buried scent-markers" in facts


def test_the_agent_gets_the_facts_and_the_guarded_tools(emberfall, monkeypatch):
    seen = {}

    class FakeAgent:
        def __call__(self, prompt):
            seen["prompt"] = prompt
            return "nothing changed"

    import src.agents.base as base
    monkeypatch.setattr(base, "create_agent",
                        lambda **kw: seen.update(tools=[getattr(t, "tool_name", getattr(t, "__name__", ""))
                                                         for t in kw["tools"]]) or FakeAgent())
    assert sim._run_sim_agent() == "nothing changed"
    assert "PLACES (the only ones that exist" in seen["prompt"] and "FIXED TRUTHS" in seen["prompt"]
    assert {"create_event", "schedule_world_event", "move_npc"} <= set(seen["tools"])
    guarded = {t.tool_name: t for t in sim._get_sim_tools() if hasattr(t, "tool_name")}
    assert guarded["create_event"]._tool_func is sim.sim_create_event
