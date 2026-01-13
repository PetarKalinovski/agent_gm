#!/usr/bin/env python3
"""
Migration script to sync database schema with the latest
Player, NPC, and Location visual/position models.
"""

import argparse
import sqlite3
import json
from pathlib import Path


def migrate(db_path: str = "data/sw.db"):
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    def add_columns(table_name, columns):
        cursor.execute(f"PRAGMA table_info({table_name})")
        existing = {row[1] for row in cursor.fetchall()}

        print(f"\nChecking table: {table_name}")
        for col_name, col_def in columns:
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
                    print(f"  [+] Added: {col_name}")
                except Exception as e:
                    print(f"  [!] Error: {e}")
            else:
                print(f"  [.] Exists: {col_name}")

    # --- 1. NPC Updates ---
    npc_cols = [
        ("sprite_path", "VARCHAR(500)"),
        ("portrait_path", "VARCHAR(500)"),
        ("position_x", "FLOAT DEFAULT 50.0"),
        ("position_y", "FLOAT DEFAULT 50.0"),
    ]
    add_columns("npcs", npc_cols)

    # --- 2. Location Updates ---
    # Default JSON for walkable bounds
    default_bounds = json.dumps({"x_min": 10, "x_max": 90, "y_min": 20, "y_max": 80})
    loc_cols = [
        ("background_image_path", "VARCHAR(500)"),
        ("collision_mask_path", "VARCHAR(500)"),
        ("walkable_bounds", f"TEXT DEFAULT '{default_bounds}'"),
        ("display_type", "VARCHAR(20) DEFAULT 'pin'"),
        ("is_map_container", "BOOLEAN DEFAULT 0"),
    ]
    add_columns("locations", loc_cols)

    # --- 3. Player Updates ---
    player_cols = [
        ("position_x", "FLOAT DEFAULT 50.0"),
        ("position_y", "FLOAT DEFAULT 50.0"),
        ("facing_direction", "VARCHAR(10) DEFAULT 'front'"),
        ("sprite_base_path", "VARCHAR(500)"),
        ("portrait_path", "VARCHAR(500)"),
    ]
    add_columns("players", player_cols)

    conn.commit()
    conn.close()
    print("\nMigration complete! All visual and position fields are synced.")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=str, default="data/sw.db")
    args = parser.parse_args()
    migrate(args.db)