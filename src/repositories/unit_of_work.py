"""Unit of Work pattern for transactional database operations."""

from contextlib import contextmanager
from typing import Any, Generator

from src.models.base import get_session


class UnitOfWork:
    """Unit of Work pattern for transactional database operations.

    Provides a single transaction context for multiple repository operations.
    All changes are committed together or rolled back on error.

    Example:
        with UnitOfWork() as uow:
            player = uow.players.get_by_id("player_123")
            if player.success:
                player.data.health_status = "injured"
            uow.commit()

    Or using the context manager:
        with unit_of_work() as uow:
            uow.players.update_position(player_id, 10, 20)
            uow.npcs.move(npc_id, location_id)
            # Auto-commits on success, rolls back on exception
    """

    def __init__(self):
        """Initialize the Unit of Work."""
        self.session = None
        self._players = None
        self._npcs = None
        self._locations = None
        self._items = None
        self._factions = None
        self._quests = None

    def __enter__(self) -> "UnitOfWork":
        """Enter the context and create a new session."""
        self.session = get_session()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context and cleanup the session."""
        if exc_type:
            self.session.rollback()
        self.session.close()
        self.session = None
        # Clear cached repositories
        self._players = None
        self._npcs = None
        self._locations = None
        self._items = None
        self._factions = None
        self._quests = None

    @property
    def players(self):
        """Get the PlayerRepository instance."""
        if self._players is None:
            from src.repositories.player_repository import PlayerRepository
            self._players = PlayerRepository(self.session)
        return self._players

    @property
    def npcs(self):
        """Get the NPCRepository instance."""
        if self._npcs is None:
            from src.repositories.npc_repository import NPCRepository
            self._npcs = NPCRepository(self.session)
        return self._npcs

    @property
    def locations(self):
        """Get the LocationRepository instance."""
        if self._locations is None:
            from src.repositories.location_repository import LocationRepository
            self._locations = LocationRepository(self.session)
        return self._locations

    @property
    def items(self):
        """Get the ItemRepository instance."""
        if self._items is None:
            from src.repositories.item_repository import ItemRepository
            self._items = ItemRepository(self.session)
        return self._items

    @property
    def factions(self):
        """Get the FactionRepository instance."""
        if self._factions is None:
            from src.repositories.faction_repository import FactionRepository
            self._factions = FactionRepository(self.session)
        return self._factions

    @property
    def quests(self):
        """Get the QuestRepository instance."""
        if self._quests is None:
            from src.repositories.quest_repository import QuestRepository
            self._quests = QuestRepository(self.session)
        return self._quests

    def commit(self):
        """Commit all pending changes."""
        self.session.commit()

    def rollback(self):
        """Rollback all pending changes."""
        self.session.rollback()

    def flush(self):
        """Flush pending changes without committing."""
        self.session.flush()


@contextmanager
def unit_of_work() -> Generator[UnitOfWork, None, None]:
    """Context manager for transactional operations.

    Automatically commits on success, rolls back on exception.

    Example:
        with unit_of_work() as uow:
            player = uow.players.get_by_id(player_id)
            player.data.health_status = "rested"
            # Auto-commits when exiting the context

    Yields:
        UnitOfWork instance with active session.
    """
    uow = UnitOfWork()
    try:
        uow.__enter__()
        yield uow
        uow.commit()
    except Exception:
        uow.rollback()
        raise
    finally:
        uow.__exit__(None, None, None)
