"""
reset_db.py – Wipe all job records from the SQLite database.

Deletes every row in the `jobs` table and resets the auto-increment counter,
leaving the table structure and indexes intact so the application starts fresh.

Usage:
    python reset_db.py                     # uses DB_PATH from .env
    python reset_db.py --db ./data/jobs.db # explicit path
    python reset_db.py --drop              # drop and recreate the table instead

The --drop flag is useful when you want to remove any orphaned columns from
old schema experiments.  It calls database.init_db() after dropping, which
recreates the current schema and runs all migrations cleanly.
"""

import argparse
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()


def _confirm(prompt: str) -> bool:
    """Prompt the user for explicit confirmation before destructive actions."""
    answer = input(f"{prompt} [yes/no]: ").strip().lower()
    return answer in ("yes", "y")


def reset_rows(db_path: str) -> None:
    """Delete all rows and reset the auto-increment sequence."""
    if not os.path.exists(db_path):
        print(f"Database not found at '{db_path}'. Nothing to reset.")
        return

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        print(f"Found {count} job record(s) in '{db_path}'.")

        if count == 0:
            print("Database is already empty.")
            return

        if not _confirm(f"Permanently delete all {count} job record(s)?"):
            print("Aborted.")
            return

        conn.execute("DELETE FROM jobs")
        # Reset the AUTOINCREMENT counter so IDs start from 1 again.
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'jobs'")
        conn.execute("VACUUM")
        conn.commit()
        print(f"Done. All {count} records deleted and auto-increment counter reset.")
    finally:
        conn.close()


def drop_and_recreate(db_path: str) -> None:
    """Drop the jobs table entirely and recreate it with the current schema."""
    if not os.path.exists(db_path):
        print(f"Database not found at '{db_path}'. Nothing to drop.")
        return

    print(f"This will DROP the entire jobs table in '{db_path}' and recreate it.")
    if not _confirm("Are you absolutely sure?"):
        print("Aborted.")
        return

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS jobs")
        conn.commit()
        print("Table dropped.")
    finally:
        conn.close()

    # Recreate using the current schema + migrations
    import database
    database.init_db(db_path)
    print(f"Table recreated with the current schema at '{db_path}'.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset the JobPortalScraper SQLite database."
    )
    parser.add_argument(
        "--db",
        default=os.getenv("DB_PATH", "./data/jobs.db"),
        help="Path to the SQLite database file (default: DB_PATH from .env)",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Drop and fully recreate the jobs table instead of just deleting rows.",
    )
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    print(f"Target database: {db_path}")

    if args.drop:
        drop_and_recreate(db_path)
    else:
        reset_rows(db_path)


if __name__ == "__main__":
    main()
