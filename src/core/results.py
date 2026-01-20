"""Result type for standardized tool and operation responses."""

from dataclasses import dataclass, field
from typing import TypeVar, Generic, Any

T = TypeVar('T')


@dataclass
class Result(Generic[T]):
    """Standard result type for all operations.

    Provides a consistent pattern for returning success/failure states
    with associated data or error information.

    Examples:
        >>> Result.ok({"name": "Player1"})
        Result(success=True, data={'name': 'Player1'}, error=None, error_code=None)

        >>> Result.fail("Player not found", "PLAYER_NOT_FOUND")
        Result(success=False, data=None, error='Player not found', error_code='PLAYER_NOT_FOUND')
    """
    success: bool
    data: T | None = None
    error: str | None = None
    error_code: str | None = None

    @classmethod
    def ok(cls, data: T) -> "Result[T]":
        """Create a successful result with data.

        Args:
            data: The data to return on success.

        Returns:
            Result with success=True and the provided data.
        """
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, error: str, code: str = "UNKNOWN") -> "Result[T]":
        """Create a failed result with error information.

        Args:
            error: Human-readable error message.
            code: Machine-readable error code for programmatic handling.

        Returns:
            Result with success=False and error information.
        """
        return cls(success=False, error=error, error_code=code)

    def to_tool_response(self) -> dict[str, Any]:
        """Convert to dictionary format for tool return values.

        This maintains compatibility with existing tool response patterns
        while providing a consistent structure.

        Returns:
            Dictionary with success/data or success/error fields.
        """
        if self.success:
            if isinstance(self.data, dict):
                return {"success": True, **self.data}
            return {"success": True, "data": self.data}
        return {
            "success": False,
            "error": self.error,
            "error_code": self.error_code
        }

    def map(self, func) -> "Result":
        """Transform the data if successful.

        Args:
            func: Function to apply to the data.

        Returns:
            New Result with transformed data, or unchanged if failed.
        """
        if self.success and self.data is not None:
            return Result.ok(func(self.data))
        return self

    def unwrap(self) -> T:
        """Get the data, raising an exception if failed.

        Returns:
            The data if successful.

        Raises:
            ValueError: If the result is a failure.
        """
        if not self.success:
            raise ValueError(f"Cannot unwrap failed result: {self.error} ({self.error_code})")
        return self.data

    def unwrap_or(self, default: T) -> T:
        """Get the data or a default value if failed.

        Args:
            default: Value to return if the result is a failure.

        Returns:
            The data if successful, otherwise the default.
        """
        if self.success:
            return self.data
        return default

    def __bool__(self) -> bool:
        """Allow using Result in boolean context."""
        return self.success


# Common error codes for consistency
class ErrorCodes:
    """Standard error codes used across the application."""

    # Entity not found errors
    PLAYER_NOT_FOUND = "PLAYER_NOT_FOUND"
    NPC_NOT_FOUND = "NPC_NOT_FOUND"
    LOCATION_NOT_FOUND = "LOCATION_NOT_FOUND"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    FACTION_NOT_FOUND = "FACTION_NOT_FOUND"
    QUEST_NOT_FOUND = "QUEST_NOT_FOUND"

    # State errors
    INVALID_STATE = "INVALID_STATE"
    NPC_UNAVAILABLE = "NPC_UNAVAILABLE"
    LOCATION_INACCESSIBLE = "LOCATION_INACCESSIBLE"

    # Validation errors
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_REQUIRED = "MISSING_REQUIRED"

    # Transaction errors
    TRANSACTION_FAILED = "TRANSACTION_FAILED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INVENTORY_FULL = "INVENTORY_FULL"

    # Agent errors
    AGENT_ERROR = "AGENT_ERROR"
    TOOL_ERROR = "TOOL_ERROR"

    # Generic
    UNKNOWN = "UNKNOWN"
