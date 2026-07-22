"""The Emberfall playtest seed builds a world that exercises every system."""

import pytest

from src.data.playtest_seed import create_playtest_world
from src.models import (
    NPC,
    Connection,
    DMState,
    Event,
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
        assert session.query(NPC).count() == 5
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
