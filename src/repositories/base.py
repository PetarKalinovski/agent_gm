"""Base repository with common operations."""

from abc import ABC, abstractmethod
from typing import TypeVar, Generic, Type, Any
from sqlalchemy.orm import Session

from src.core.results import Result

T = TypeVar('T')


class BaseRepository(ABC, Generic[T]):
    """Abstract base class for repositories.

    Provides common database operations with Result-based error handling.
    All concrete repositories should inherit from this class.

    Example:
        class PlayerRepository(BaseRepository[Player]):
            model_class = Player

            def get_by_id(self, player_id: str) -> Result[Player]:
                return self._get_by_id(player_id)
    """

    model_class: Type[T] = None
    not_found_code: str = "ENTITY_NOT_FOUND"
    not_found_message: str = "Entity not found"

    def __init__(self, session: Session):
        """Initialize repository with a database session.

        Args:
            session: SQLAlchemy session for database operations.
        """
        self.session = session

    def _get_by_id(self, entity_id: str) -> Result[T]:
        """Get entity by ID with Result wrapper.

        Args:
            entity_id: The entity's ID.

        Returns:
            Result containing the entity or error.
        """
        entity = self.session.get(self.model_class, entity_id)
        if not entity:
            return Result.fail(self.not_found_message, self.not_found_code)
        return Result.ok(entity)

    def _get_all(self, **filters) -> Result[list[T]]:
        """Get all entities with optional filters.

        Args:
            **filters: Column=value filters to apply.

        Returns:
            Result containing list of entities.
        """
        query = self.session.query(self.model_class)
        for column, value in filters.items():
            if value is not None:
                query = query.filter(getattr(self.model_class, column) == value)
        return Result.ok(query.all())

    def _save(self, entity: T) -> Result[T]:
        """Save (add or update) an entity.

        Args:
            entity: The entity to save.

        Returns:
            Result containing the saved entity.
        """
        try:
            self.session.add(entity)
            self.session.flush()  # Get ID without committing
            return Result.ok(entity)
        except Exception as e:
            return Result.fail(str(e), "SAVE_ERROR")

    def _delete(self, entity_id: str) -> Result[bool]:
        """Delete an entity by ID.

        Args:
            entity_id: The entity's ID.

        Returns:
            Result indicating success or failure.
        """
        entity = self.session.get(self.model_class, entity_id)
        if not entity:
            return Result.fail(self.not_found_message, self.not_found_code)
        try:
            self.session.delete(entity)
            self.session.flush()
            return Result.ok(True)
        except Exception as e:
            return Result.fail(str(e), "DELETE_ERROR")

    @abstractmethod
    def get_by_id(self, entity_id: str) -> Result[T]:
        """Get entity by ID. Must be implemented by subclasses."""
        pass

    def save(self, entity: T) -> Result[T]:
        """Save entity. Can be overridden for custom behavior."""
        return self._save(entity)

    def delete(self, entity_id: str) -> Result[bool]:
        """Delete entity. Can be overridden for custom behavior."""
        return self._delete(entity_id)
