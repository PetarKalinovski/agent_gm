"""Database setup and base model."""

from pathlib import Path
from sqlalchemy import create_engine, inspect, text
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

    print(f"[init_db] Initializing database: {db_path}")

    # Create engine
    _engine = create_engine(f"sqlite:///{db_path}", echo=False)
    _SessionLocal = sessionmaker(bind=_engine)

    # Create all tables (only creates new tables, doesn't add columns)
    Base.metadata.create_all(_engine)

    # Add any missing columns to existing tables
    _migrate_missing_columns(_engine)


def _migrate_missing_columns(engine) -> None:
    """Add any columns defined in models but missing from existing tables.

    This is a lightweight alternative to Alembic for simple column additions.
    Only handles adding new TEXT columns with empty string defaults.
    """
    insp = inspect(engine)
    for table_name, table in Base.metadata.tables.items():
        if not insp.has_table(table_name):
            continue
        existing = {col["name"] for col in insp.get_columns(table_name)}
        for col in table.columns:
            if col.name not in existing:
                col_type = col.type.compile(engine.dialect)
                default = "''" if hasattr(col.default, 'arg') and col.default.arg == "" else "NULL"
                with engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {col_type} DEFAULT {default}'
                    ))
                print(f"[migrate] Added column {table_name}.{col.name}")


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


def reset_engine() -> None:
    """Reset the database engine to allow switching databases.

    This must be called before init_db() when switching to a different database.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
