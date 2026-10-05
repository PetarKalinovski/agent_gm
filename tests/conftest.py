"""Shared fixtures: a fresh temp database per test."""

import pytest

from src.models import Player, get_session, init_db, reset_engine


def pytest_configure(config):
    config.addinivalue_line("markers", "real_memory_root: the test reads memory_files.memory_root() itself")


@pytest.fixture(autouse=True)
def _memory_files_in_tmp(request, tmp_path, monkeypatch):
    """Memory lives in each test's own database; its folder (exports, and
    the pre-database files a first touch moves in) is the test's tmp dir,
    never data/memory/<world>: a test must neither write into the repo's
    default world nor move a real folder in."""
    if request.node.get_closest_marker("real_memory_root"):
        return
    from src.services import memory_files

    monkeypatch.setattr(memory_files, "memory_root", lambda: tmp_path / "memory")


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
