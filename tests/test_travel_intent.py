"""Where the player says they go, this reply goes (src/services/travel_intent.py).

fleet7-tamsin T20 "I'm already moving toward the Stable Yard": no @move, and
the reply left her in the square. fleet7-wenna T20 "I head east toward the
healer": no healer exists, and the DM moved her into the tavern instead.
Replayed over every fleet player input (390) in the seed world from the
square: 68 name somewhere they go, 26 of them a place (23 moves, one shut
road, "the chapel" and "the healer" to make) — the moves the DM wrote, or
the ones it dropped (fleet7-tamsin T20, fleet5-tamsin T3, fleet6-bram T3);
the rest are people, things in the scene, or a road.
"""

import pytest

from src.models import Location, Player, get_session, reset_engine
from src.services import travel_intent
from src.services.travel_intent import destination_phrase

# Setting out, now, for somewhere (corpus lines, quotes kept as typed)
SETTING_OUT = [
    ("I'm already moving toward the Stable Yard, looking for the hawker.", "the Stable Yard"),  # fleet7-tamsin T20
    ("I head east toward the healer without another word.", "the healer"),                     # fleet7-wenna T20
    ("I head toward the Drowned Lantern, jaw tight.", "the Drowned Lantern"),                    # fleet3-bram T11
    ("I head back toward the square, looking for Coldiron.", "the square"),                      # fleet3-bram T8
    ("I head toward the stable yard before she can answer, already wondering.", "the stable yard"),  # fleet5-tamsin T3
    ("I tear my eyes away and move quick toward the stables at a brisk walk.", "the stables"),
    ("Let's go to the Hollow.", "the Hollow"),
    ("I make for the Hollow, knife loose in its sheath.", "the Hollow"),
    # The last place named is where they end up
    ("I head back toward Market Square, but I don't linger — instead I make for the north road.", "the north road"),
]

# Talk about places, facing, plans, questions and other people's walks
NOT_SETTING_OUT = [
    "I tell her about the Hollow and what the howls mean.",
    "I ask Piper what the stable yard hears after dark.",
    "I glance toward the gatehouse, then back to Piper.",
    "I turn toward the stables.",
    "I'm heading to Wolf's Hollow at first light.",                                             # fleet-tamsin T5
    "Before I head for an inn to sleep, I want to see who's still lingering.",                  # fleet-tamsin T2
    "I need to know what I'm walking into before the second bell sounds.",                      # fleet2-bram T7
    "I'll come find you with my answer on whether I'm heading north tonight.",
    "Should I go to the Hollow?",
    "I watch Coldiron walk back to his notice board.",                                          # fleet-bram T1
    "\"I'm going to the stable yard,\" I tell her.",
    "I don't go to the tavern; I stay by the well.",
    "I'd go to the Lantern if I had the coin.",
]


@pytest.mark.parametrize("text,phrase", SETTING_OUT)
def test_setting_out_names_where(text, phrase):
    assert destination_phrase(text) == phrase


@pytest.mark.parametrize("text", NOT_SETTING_OUT)
def test_talk_plans_and_facing_are_no_setting_out(text):
    assert destination_phrase(text) is None


def test_engine_turns_are_not_read():
    assert destination_phrase("[OPENING] They head toward the Drowned Lantern.") is None


@pytest.fixture()
def emberfall(tmp_path):
    from src.data.playtest_seed import create_playtest_world

    reset_engine()
    create_playtest_world(str(tmp_path / "emberfall.db"))
    with get_session() as session:
        places = {loc.name: loc.id for loc in session.query(Location).all()}
        player = Player(name="Wenna Cole", current_location_id=places["Market Square"], currency=25)
        session.add(player)
        session.commit()
        ids = {"player": player.id, **places}
    yield ids
    reset_engine()


def test_a_known_reachable_place_binds_the_move(emberfall):
    """fleet7-tamsin T20."""
    line = travel_intent.directive(emberfall["player"], "I'm already moving toward the Stable Yard.")
    assert line.startswith("[THIS REPLY: the player goes to Stable Yard — write @move Stable Yard first")
    assert "never take them somewhere they did not say" in line
    # By what the place is, in its own words: "the tavern", "the Watch post"
    assert "@move The Drowned Lantern first" in travel_intent.directive(emberfall["player"], "I head for the tavern.")
    assert "@move North Gatehouse first" in travel_intent.directive(
        emberfall["player"], "I'm already moving toward the Watch post.")
    assert "@move North Gatehouse first" in travel_intent.directive(emberfall["player"], "I head for the gate.")


def test_a_place_that_does_not_exist_is_made_not_swapped(emberfall):
    """fleet7-wenna T20: the healer the Captain sent her to did not exist, and
    the DM moved her into the tavern instead."""
    line = travel_intent.directive(emberfall["player"], "I head east toward the healer without another word.")
    assert line.startswith('[THIS REPLY: the player sets out for "the healer" — no such place exists yet. '
                           "@new-place it, then @move there first")
    # The herbalist stands in the square with her: no "@move Market Square"
    # to where she already is
    assert "Market Square" not in line
    assert "Unless they turn instead to Piper Quillow (Herbalist's apprentice), who is right here — then no @move." in line
    # From the yard the square is somewhere to go
    with get_session() as session:
        session.get(Player, emberfall["player"]).current_location_id = emberfall["Stable Yard"]
        session.commit()
    line = travel_intent.directive(emberfall["player"], "I head east toward the healer without another word.")
    assert "@new-place it (or use Market Square, where Piper Quillow (Herbalist's apprentice) is), then @move there first" in line
    assert "right here" not in line


def test_a_shut_way_takes_them_as_far_as_it_lets_them(emberfall):
    """No pass: the Hollow's road is shut — the line never sends them past it."""
    line = travel_intent.directive(emberfall["player"], "I make for the Hollow, knife loose in its sheath.")
    assert line.startswith("[THIS REPLY: the player sets out for Wolf's Hollow, but the way is shut to them")
    assert "@move North Gatehouse, show what stops them" in line
    assert "@move Wolf's Hollow | through" in line and "Never narrate them arriving otherwise" in line
    with get_session() as session:
        session.get(Player, emberfall["player"]).inventory = [{"id": "p", "name": "Gate Pass", "type": "key"}]
        session.commit()
    assert "write @move Wolf's Hollow first" in travel_intent.directive(emberfall["player"], "I make for the Hollow.")


def test_a_shut_way_never_sends_them_into_a_whole_town(db):
    """With no road known, the route to a locked place runs through the town
    node itself — and `@move Emberfall` is refused (a town is a map of
    places): the line names no hop then."""
    from src.models import Connection, LocationType

    with get_session() as session:
        town = Location(name="Emberfall", type=LocationType.TOWN, depth=0, discovered=True)
        session.add(town)
        session.flush()
        square = Location(name="Market Square", type=LocationType.DISTRICT, parent_id=town.id, depth=1,
                          discovered=True)
        hollow = Location(name="Wolf's Hollow", type=LocationType.POI, parent_id=town.id, depth=1,
                          discovered=True)
        ridge = Location(name="The Ridge", type=LocationType.POI, depth=0, discovered=False)
        session.add_all([square, hollow, ridge])
        session.flush()
        session.add(Connection(from_location_id=ridge.id, to_location_id=hollow.id, travel_type="walk",
                               travel_time_hours=1, requirements=["a gate pass"], discovered=False))
        player = Player(name="Wenna Cole", current_location_id=square.id)
        session.add(player)
        session.commit()
        pid = player.id
    line = travel_intent.directive(pid, "I make for the Hollow.")
    assert line.startswith("[THIS REPLY: the player sets out for Wolf's Hollow, but the way is shut to them")
    assert "Take them as far as it lets them (show what stops them)" in line and "@move Emberfall" not in line


@pytest.mark.parametrize("text", [
    "I tell her about the Hollow and what the howls mean.",
    "I head toward the stable boy, moving easy.",            # a person, though the yard shares his trade word
    "I head toward the notice board.",                       # a thing in the scene
    "I head over to the well.",
    "I walk toward the paddock gate.",                        # a gate, not the gatehouse
    "I head toward the square.",                              # already there
])
def test_no_line_for_talk_people_things_or_here(emberfall, text):
    assert travel_intent.directive(emberfall["player"], text) is None


def test_the_line_rides_in_the_context_after_the_roll(monkeypatch, emberfall):
    """dm_orchestrator._build_context adds it on an ordinary turn, after any
    roll-first line, never on an engine turn."""
    from types import SimpleNamespace

    from src.agents import dm_orchestrator
    from src.agents.dm_orchestrator import REPLY_BUDGET, DMOrchestrator
    from src.services import complications, stakes

    monkeypatch.setattr(dm_orchestrator, "get_player", lambda pid: {"name": "Wenna Cole", "inventory": []})
    monkeypatch.setattr(dm_orchestrator, "get_active_quests", lambda: [])
    monkeypatch.setattr(dm_orchestrator, "appearance_lines", lambda pid: [])
    monkeypatch.setattr(dm_orchestrator, "run_world_tick", lambda *a, **k: {"fired_events": []})
    monkeypatch.setattr(dm_orchestrator, "format_world_tick_context", lambda t: "### WORLD STATE")
    monkeypatch.setattr(dm_orchestrator, "run_campaign_tick", lambda pid: {})
    monkeypatch.setattr(dm_orchestrator, "format_campaign_context", lambda t: "")
    monkeypatch.setattr(dm_orchestrator, "no_spine_context", lambda pid: "", raising=False)
    monkeypatch.setattr(complications, "directive_line", lambda *a, **k: None)
    monkeypatch.setattr(complications, "unanswered_line", lambda *a, **k: None)
    monkeypatch.setattr(stakes, "waited_line", lambda *a, **k: None)
    fake = SimpleNamespace(context=SimpleNamespace(player_id=emberfall["player"]), _player_name="Wenna Cole")
    fake._player_block = lambda: ["### THE PLAYER"]
    fake._quest_block = lambda: ["### ACTIVE QUESTS", "- none"]
    sneaking = "I sneak toward the Stable Yard, keeping to the shadows."
    context = DMOrchestrator._build_context(fake, sneaking)
    assert context.index("call roll_check FIRST") < context.index("[THIS REPLY: the player goes to Stable Yard")
    assert context.index("[THIS REPLY: the player goes to Stable Yard") < context.index(REPLY_BUDGET)
    assert "the player goes to" not in DMOrchestrator._build_context(fake, "I ask Piper about the yard.")
    assert "the player goes to" not in DMOrchestrator._build_context(fake, sneaking, constrained=True)
