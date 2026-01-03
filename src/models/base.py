"""Database setup and base model."""

from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Global engine and session factory
_engine = None
_SessionLocal = None


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


def init_db(db_path: str | Path = "data/game.db") -> None:
    """Initialize the database and create all tables.

    Args:
        db_path: Path to the SQLite database file.
    """
    global _engine, _SessionLocal

    # Ensure directory exists
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create engine
    _engine = create_engine(f"sqlite:///{db_path}", echo=False)
    _SessionLocal = sessionmaker(bind=_engine)

    # Create all tables
    Base.metadata.create_all(_engine)


def get_session() -> Session:
    """Get a database session.

    Returns:
        A new database session.

    Raises:
        RuntimeError: If database has not been initialized.
    """
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _SessionLocal()


def get_engine():
    """Get the database engine.

    Returns:
        The SQLAlchemy engine.

    Raises:
        RuntimeError: If database has not been initialized.
    """
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine
