"""Session management for game sessions with expiration."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src.agents.factory import AgentFactory


@dataclass
class GameSessionState:
    """State for a single game session.

    Tracks player session data including agents and activity.
    """
    player_id: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    dm_agent: Any = None
    tool_tracker: Any = None
    extra_data: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, timeout_minutes: int = 60) -> bool:
        """Check if the session has expired.

        Args:
            timeout_minutes: Minutes of inactivity before expiration.

        Returns:
            True if session is expired.
        """
        return datetime.utcnow() - self.last_activity > timedelta(minutes=timeout_minutes)

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = datetime.utcnow()

    def get_age_minutes(self) -> float:
        """Get session age in minutes."""
        return (datetime.utcnow() - self.created_at).total_seconds() / 60

    def get_idle_minutes(self) -> float:
        """Get minutes since last activity."""
        return (datetime.utcnow() - self.last_activity).total_seconds() / 60


class SessionManager:
    """Manages game sessions with automatic expiration.

    Provides centralized session management for the web server
    with automatic cleanup of expired sessions.

    Example:
        session = SessionManager.get_or_create("player_123", callback_handler=tracker)
        session.touch()  # Update activity

        # Later, cleanup expired sessions
        SessionManager.cleanup_expired()
    """

    _sessions: dict[str, GameSessionState] = {}
    SESSION_TIMEOUT_MINUTES: int = 60

    @classmethod
    def get_or_create(
        cls,
        player_id: str,
        callback_handler: Any = None,
    ) -> GameSessionState:
        """Get existing session or create a new one.

        Args:
            player_id: The player's ID.
            callback_handler: Optional callback handler for tool tracking.

        Returns:
            GameSessionState for the player.
        """
        cls._cleanup_expired()

        if player_id in cls._sessions:
            session = cls._sessions[player_id]
            session.touch()

            # Update callback handler if provided
            if callback_handler and session.tool_tracker != callback_handler:
                session.tool_tracker = callback_handler

            return session

        # Create new session
        dm = AgentFactory.create_dm(player_id, callback_handler=callback_handler)

        session = GameSessionState(
            player_id=player_id,
            dm_agent=dm,
            tool_tracker=callback_handler,
        )
        cls._sessions[player_id] = session

        return session

    @classmethod
    def get(cls, player_id: str) -> GameSessionState | None:
        """Get session without creating.

        Args:
            player_id: The player's ID.

        Returns:
            GameSessionState or None if not found or expired.
        """
        session = cls._sessions.get(player_id)
        if session and not session.is_expired(cls.SESSION_TIMEOUT_MINUTES):
            return session
        return None

    @classmethod
    def end_session(cls, player_id: str) -> bool:
        """Explicitly end a session.

        Args:
            player_id: The player's ID.

        Returns:
            True if session was found and ended.
        """
        if player_id in cls._sessions:
            # Clear agent cache for this player
            AgentFactory.clear_cache(player_id)
            del cls._sessions[player_id]
            return True
        return False

    @classmethod
    def _cleanup_expired(cls) -> int:
        """Remove expired sessions.

        Returns:
            Number of sessions cleaned up.
        """
        expired = [
            pid for pid, session in cls._sessions.items()
            if session.is_expired(cls.SESSION_TIMEOUT_MINUTES)
        ]

        for pid in expired:
            AgentFactory.clear_cache(pid)
            del cls._sessions[pid]

        return len(expired)

    @classmethod
    def cleanup_expired(cls) -> int:
        """Public method to cleanup expired sessions.

        Returns:
            Number of sessions cleaned up.
        """
        return cls._cleanup_expired()

    @classmethod
    def get_all_sessions(cls) -> list[dict[str, Any]]:
        """Get info about all active sessions.

        Returns:
            List of session info dictionaries.
        """
        cls._cleanup_expired()

        return [
            {
                "player_id": session.player_id,
                "created_at": session.created_at.isoformat(),
                "last_activity": session.last_activity.isoformat(),
                "age_minutes": session.get_age_minutes(),
                "idle_minutes": session.get_idle_minutes(),
            }
            for session in cls._sessions.values()
        ]

    @classmethod
    def get_stats(cls) -> dict[str, Any]:
        """Get session manager statistics.

        Returns:
            Dictionary with session statistics.
        """
        cls._cleanup_expired()

        return {
            "active_sessions": len(cls._sessions),
            "timeout_minutes": cls.SESSION_TIMEOUT_MINUTES,
            "sessions": cls.get_all_sessions(),
            "agent_cache_stats": AgentFactory.get_cache_stats(),
        }

    @classmethod
    def clear_all(cls) -> int:
        """Clear all sessions (for shutdown).

        Returns:
            Number of sessions cleared.
        """
        count = len(cls._sessions)
        AgentFactory.clear_cache()
        cls._sessions.clear()
        return count

    @classmethod
    def set_timeout(cls, minutes: int) -> None:
        """Set session timeout in minutes.

        Args:
            minutes: New timeout value.
        """
        cls.SESSION_TIMEOUT_MINUTES = minutes
