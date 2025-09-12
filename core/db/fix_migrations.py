#!/usr/bin/env python
import os
import sqlite3
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def get_latest_revision(alembic_cfg: Config) -> Optional[str]:
    """Get the most recent revision from available migration files."""
    script = ScriptDirectory.from_config(alembic_cfg)
    if script.get_heads():
        return script.get_heads()[0]
    return None


def fix_alembic_version(db_path: str, version: str) -> None:
    """Manually update alembic_version table to the specified version."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE alembic_version SET version_num = ?", (version,))
        conn.commit()
        print(f"Successfully updated alembic_version to {version}")
    except sqlite3.Error as e:
        print(f"Error updating database: {e}")
        raise
    finally:
        conn.close()


def main():
    # Get the project root directory (where core/ is located)
    project_root = Path(__file__).parent.parent.parent

    # Configure alembic
    alembic_ini = os.path.join(project_root, "core", "db", "alembic.ini")
    if not os.path.exists(alembic_ini):
        print(f"Error: Could not find alembic.ini at {alembic_ini}")
        return 1

    alembic_cfg = Config(alembic_ini)

    # Get the database path from alembic.ini
    db_url = alembic_cfg.get_main_option("sqlalchemy.url")
    if not db_url.startswith("sqlite:///"):
        print("Error: This script only works with SQLite databases")
        return 1

    db_path = db_url.replace("sqlite:///", "")
    db_path = os.path.join(project_root, db_path)

    if not os.path.exists(db_path):
        print(f"Database file not found at {db_path}")
        create_new = input("Would you like to create a new database? (y/n): ")
        if create_new.lower() == "y":
            print("Creating new database and running migrations...")
            command.upgrade(alembic_cfg, "head")
            print("Done!")
            return 0
        return 1

    # Get the latest available revision
    latest_revision = get_latest_revision(alembic_cfg)
    if not latest_revision:
        print("Error: No migration versions found")
        return 1

    print(f"Latest available revision: {latest_revision}")

    try:
        # Update the version in the database
        fix_alembic_version(db_path, latest_revision)

        # Run migrations to ensure database schema is up to date
        print("Running migrations to ensure database schema is current...")
        command.upgrade(alembic_cfg, "head")

        print("Database successfully fixed and upgraded!")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
