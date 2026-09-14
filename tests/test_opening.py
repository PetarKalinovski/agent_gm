"""The opening turn: a new character's first moment is a story, not a prompt."""

from types import SimpleNamespace

from src.models import (
    Campaign, DMState, Location, LocationType, NPC, Player, StoryBeat, WorldBible, WorldClock,
    Cinematic, get_session,
)
from src.services import opening
from src.services.cinematics import request_cinematic


def _world(session, *, with_bible=True):
    session.add(WorldClock(day=1, hour=20, minute=0))
    session.add(DMState(current_arc="Something is organizing the wolves."))
    if with_bible:
        session.add(WorldBible(name="Emberfall", genre="fantasy", tagline="A dusk frontier town",
                               current_situation="Wolves press the north road.",
                               pc_starting_situation="A courier with one last letter."))
    square = Location(name="Market Square", type=LocationType.DISTRICT, depth=0, discovered=True,
                      description="Stalls fold under a copper sky; the Watch stand near the well.")
    session.add(square)
    session.flush()
    session.add(NPC(name="Captain Brann Coldiron", current_location_id=square.id))
    session.add(NPC(name="Dead Man", current_location_id=square.id, status="dead"))
    player = Player(name="Rook Fennick", current_location_id=square.id)
    session.add(player)
    session.commit()
    return player.id, square.id


def test_is_first_turn_reads_the_dm_history():
    assert opening.is_first_turn(SimpleNamespace(agent=SimpleNamespace(messages=[]))) is True
    assert opening.is_first_turn(SimpleNamespace(agent=SimpleNamespace(messages=[{"role": "user"}]))) is False

    class Broken:
        @property
        def agent(self):
            raise RuntimeError("no session file")

    assert opening.is_first_turn(Broken()) is False


def test_opening_plays_a_title_card_past_the_daily_cap_and_is_ledgered(db):
    with get_session() as session:
        player_id, _ = _world(session)
    # A DM card already burned today's allowance …
    assert request_cinematic("title_card", player_id=player_id, title="Earlier", source="dm")["ok"]
    assert not request_cinematic("title_card", player_id=player_id, title="Again", source="dm")["ok"]

    payload = opening.opening_payload(player_id)
    assert payload["opening"] is True
    assert payload["cinematic"]["kind"] == "title_card"
    assert payload["cinematic"]["title"] == "Emberfall"
    assert payload["cinematic"]["subtitle"] == "A dusk frontier town"
    with get_session() as session:
        rows = session.query(Cinematic).filter(Cinematic.source == "opening").all()
        assert len(rows) == 1 and rows[0].status == "played"


def test_scene_is_the_location_row_and_the_living_present(db):
    with get_session() as session:
        player_id, _ = _world(session)
    payload = opening.opening_payload(player_id)
    assert payload["scene"]["location"] == "Market Square"
    assert "copper sky" in payload["scene"]["description"]
    assert payload["scene"]["time_of_day"] == "evening"
    assert payload["scene"]["npcs"] == ["Captain Brann Coldiron"]


def test_directive_without_a_campaign_uses_the_world_and_the_arc(db):
    with get_session() as session:
        player_id, _ = _world(session)
    text = opening.opening_payload(player_id)["dm_message"]
    assert text.startswith("[OPENING]")
    assert "Rook Fennick's first moment" in text
    assert "Wolves press the north road." in text
    assert "A courier with one last letter." in text
    assert "Something is organizing the wolves." in text
    assert "do not re-describe the place" in text
    assert "END this response by handing them one concrete next thing to do" in text
    assert "Present: Captain Brann Coldiron." in text


def test_directive_with_a_campaign_uses_the_spine_and_the_beat_lead_only(db):
    with get_session() as session:
        player_id, _ = _world(session)
        campaign = Campaign(player_id=player_id, title="The Whistle on the North Road", status="active",
                            premise="Someone is teaching the wolves to hunt on command.",
                            stakes_personal="The last letter you carried was to a dead man.",
                            acts=[{"act": 1, "title": "Teeth", "premise": "Find who whistles."}])
        session.add(campaign)
        session.flush()
        session.add(StoryBeat(campaign_id=campaign.id, act=1, sequence=1, beat_type="revelation",
                              title="Sealed", status="armed",
                              directive="SECRET: Kestrel breeds the wolves in the kennel.",
                              lead="Ask the Captain what the howling means."))
        session.commit()
    payload = opening.opening_payload(player_id)
    text = payload["dm_message"]
    assert '"The Whistle on the North Road"' in text
    assert "teaching the wolves" in text and "dead man" in text
    assert "Act I — Teeth: Find who whistles." in text
    assert 'Their lead, already on their screen: "Ask the Captain what the howling means."' in text
    assert "Kestrel" not in text, "a beat's directive never leaks into the opening"
    assert payload["cinematic"]["title"] == "The Whistle on the North Road"
    assert payload["cinematic"]["subtitle"] == "Teeth"


def test_pending_campaign_counts_as_none(db):
    with get_session() as session:
        player_id, _ = _world(session)
        session.add(Campaign(player_id=player_id, title="Half-written", status="pending"))
        session.commit()
    payload = opening.opening_payload(player_id)
    assert payload["cinematic"]["title"] == "Emberfall"
    assert "Half-written" not in payload["dm_message"]


def test_unknown_player_and_unplaced_player(db):
    assert opening.opening_payload("nobody") is None
    with get_session() as session:
        player_id, _ = _world(session)
        session.get(Player, player_id).current_location_id = None
        session.commit()
    payload = opening.opening_payload(player_id)
    assert payload["scene"] is None and payload["opening"] is True
