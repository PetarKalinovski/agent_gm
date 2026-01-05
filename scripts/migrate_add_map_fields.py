#!/usr/bin/env python3
"""Migration script to add map-related fields to existing database.

Usage:
    python scripts/migrate_add_map_fields.py
    python scripts/migrate_add_map_fields.py --db data/my_world.db
"""

import argparse
import sqlite3
from pathlib import Path


def migrate(db_path: str = "data/sw.db"):
    """Add map-related columns to locations and connections tables."""

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get existing columns in locations table
    cursor.execute("PRAGMA table_info(locations)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    # New columns to add to locations
    location_columns = [
        ("display_type", "VARCHAR(20) DEFAULT 'pin'"),
        ("is_map_container", "BOOLEAN DEFAULT 0"),
        ("map_image_path", "VARCHAR(500)"),
        ("map_width", "INTEGER DEFAULT 1000"),
        ("map_height", "INTEGER DEFAULT 1000"),
        ("pin_icon", "VARCHAR(100) DEFAULT 'circle'"),
        ("pin_color", "VARCHAR(20) DEFAULT '#3388ff'"),
        ("pin_size", "FLOAT DEFAULT 15.0"),
    ]

    print(f"Migrating database: {db_path}")
    print(f"Existing columns in locations: {len(existing_columns)}")

    for col_name, col_def in location_columns:
        if col_name not in existing_columns:
            try:
                sql = f"ALTER TABLE locations ADD COLUMN {col_name} {col_def}"
                cursor.execute(sql)
                print(f"  Added column: locations.{col_name}")
            except sqlite3.OperationalError as e:
                print(f"  Error adding {col_name}: {e}")
        else:
            print(f"  Column already exists: locations.{col_name}")

    # Get existing columns in connections table
    cursor.execute("PRAGMA table_info(connections)")
    existing_conn_columns = {row[1] for row in cursor.fetchall()}

    # New columns to add to connections
    connection_columns = [
        ("difficulty", "INTEGER DEFAULT 0"),
        ("description", "TEXT DEFAULT ''"),
    ]

    for col_name, col_def in connection_columns:
        if col_name not in existing_conn_columns:
            try:
                sql = f"ALTER TABLE connections ADD COLUMN {col_name} {col_def}"
                cursor.execute(sql)
                print(f"  Added column: connections.{col_name}")
            except sqlite3.OperationalError as e:
                print(f"  Error adding {col_name}: {e}")
        else:
            print(f"  Column already exists: connections.{col_name}")

    conn.commit()
    conn.close()

    print("\nMigration complete!")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add map fields to database")
    parser.add_argument("--db", type=str, default="data/sw.db", help="Database path")
    args = parser.parse_args()

    migrate(args.db)
