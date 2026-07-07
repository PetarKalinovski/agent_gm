"""Shared fixtures: a fresh temp database per test."""

import pytest

from src.models import Player, get_session, init_db, reset_engine


@pytest.fixture()
def db(tmp_path):
    """Fresh SQLite database for each test."""
    reset_engine()
    init_db(tmp_path / "test.db")
    yield
    reset_engine()


@pytest.fixture()
def player(db):
    """A player in the fresh database."""
    with get_session() as session:
        p = Player(name="Tester", currency=100)
        session.add(p)
        session.commit()
        player_id = p.id
    return player_id
