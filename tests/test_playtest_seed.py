"""The Emberfall playtest seed builds a world that exercises every system."""

import pytest

from src.data.playtest_seed import create_playtest_world
from src.models import (
    NPC,
    Connection,
    DMState,
    Event,
    Faction,
    Location,
    Player,
    Quest,
    QuestStatus,
    WorldBible,
    WorldClock,
    get_session,
    reset_engine,
)


@pytest.fixture()
def playtest_db(tmp_path):
    db_path = str(tmp_path / "emberfall.db")
    reset_engine()
    create_playtest_world(db_path)
    yield db_path
    reset_engine()


def test_seed_is_idempotent(playtest_db, capsys):
    with get_session() as session:
        before = session.query(Location).count()
    # Same path again: must detect the existing world and change nothing
    create_playtest_world(playtest_db)
    with get_session() as session:
        assert session.query(Location).count() == before
    assert "already exists" in capsys.readouterr().out


def test_world_shape(playtest_db):
    with get_session() as session:
        assert session.query(WorldBible).count() == 1
        bible = session.query(WorldBible).first()
        # Music style + character-creation prefill both read the bible
        assert bible.genre == "fantasy"
        assert bible.pc_suggested_name and bible.pc_suggested_description

        assert session.query(Location).count() == 4
        assert session.query(NPC).count() == 8
        # No pre-made player: character creation (and sprite/walk generation)
        # is part of the playtest
        assert session.query(Player).count() == 0

        # Start scene is visited so the game opens there
        square = session.query(Location).filter_by(name="Market Square").one()
        assert square.visited and square.discovered
        # Obstacles unset -> auto-detection runs after background generation
        assert square.obstacles is None

        # Three wanderers share the start scene
        assert session.query(NPC).filter_by(current_location_id=square.id).count() == 3

        # The tier ladder has a face at both ends: harmless townsfolk, weak
        # wolves, dangerous named fighters, and exactly one authored boss
        tiers = {n.name: n.danger_tier for n in session.query(NPC).all()}
        assert tiers["Piper Quillow"] == "harmless"
        assert tiers["Vex Marrow"] == "dangerous"
        bosses = [n for n in session.query(NPC).all() if n.danger_tier == "boss"]
        assert len(bosses) == 1 and bosses[0].boss_title

        # Every seeded location carries canvas objects (E-to-interact surface)
        for loc_name in ("Market Square", "The Drowned Lantern", "Wolf's Hollow"):
            loc = session.query(Location).filter_by(name=loc_name).one()
            assert loc.objects and 1 <= len(loc.objects) <= 3


def test_locked_connection_and_quest(playtest_db):
    with get_session() as session:
        hollow = session.query(Location).filter_by(name="Wolf's Hollow").one()
        locked = session.query(Connection).filter_by(to_location_id=hollow.id).one()
        assert locked.requirements, "north road must test move_player requirement blocking"

        quest = session.query(Quest).one()
        assert quest.status == QuestStatus.NOT_STARTED
        assert quest.rewards.get("currency") == 60
        assert quest.rewards.get("items") and quest.rewards.get("reputation")
        brann = session.query(NPC).filter_by(name="Captain Brann Coldiron").one()
        assert quest.assigned_by_npc_id == brann.id


def test_the_lantern_keeps_a_door_policy(playtest_db):
    with get_session() as session:
        tavern = session.query(Location).filter_by(name="The Drowned Lantern").one()
        code = tavern.dress_code
        assert code["banned_styles"] == ["concealing", "filthy"]
        assert code["min_style"] is None
        assert "Maera keeps a warm room" in code["reason"]

        # Everywhere else stays open to anyone — a dress code is rare on purpose
        others = session.query(Location).filter(Location.id != tavern.id).all()
        assert all(loc.dress_code is None for loc in others)


def test_three_garments_cover_the_attire_matrix(playtest_db):
    """Uniform, concealing hood and filthy oilskin, one NPC each."""
    with get_session() as session:
        watch_id = session.query(Faction).filter_by(name="The Lantern Watch").one().id
        shelves = {
            npc.name: (npc.inventory_notable or [])
            for npc in session.query(NPC).all()
        }

    coat = shelves["Captain Brann Coldiron"][0]
    hood = shelves["Vex Marrow"][0]
    oilskin = shelves["Sorrel Tack"][0]

    for garment in (coat, hood, oilskin):
        assert garment["slot"] == "armor" and garment["tier"] in ("crude", "common")
        assert garment["visual_desc"]
        assert set(garment["attire"]) == {"style", "uniform_faction_id", "concealing"}

    assert coat["attire"] == {"style": "common", "uniform_faction_id": watch_id,
                              "concealing": False}
    assert hood["attire"]["concealing"] is True
    assert hood["attire"]["uniform_faction_id"] is None
    assert oilskin["attire"]["style"] == "filthy"


def test_the_seeded_matrix_actually_reaches_the_door(playtest_db):
    """Hood -> refused, oilskin -> refused, Watch coat -> welcome."""
    from src.services import perception
    from src.services import progression as prog

    with get_session() as session:
        tavern = session.query(Location).filter_by(name="The Drowned Lantern").one()
        code = tavern.dress_code
        shelves = {npc.name: (npc.inventory_notable or [])
                   for npc in session.query(NPC).all()}

    def _verdict(garment):
        player = Player(name="Rook", level=10, inventory=[garment],
                        equipment={"armor": prog.item_key(garment)})
        return perception.dress_verdict(perception.perception_profile(player), code)

    assert _verdict(shelves["Vex Marrow"][0])["token"] == "concealing"
    assert _verdict(shelves["Sorrel Tack"][0])["token"] == "filthy"
    assert _verdict(shelves["Captain Brann Coldiron"][0])["allowed"] is True


def test_clock_and_scheduled_events(playtest_db):
    with get_session() as session:
        clock = session.query(WorldClock).one()
        assert clock.get_time_of_day() == "evening"

        events = session.query(Event).all()
        assert len(events) == 2
        for event in events:
            assert not event.narrated_to_player
            # Strictly in the future relative to the seeded clock
            assert (event.scheduled_day, event.scheduled_hour) > (clock.day, clock.hour)

        assert session.query(DMState).one().tension == "low"
