"""Schema init helper — exposes DB_PATH + SCHEMA + init_db().

Pulled out of hr_live.py so that modules which don't import bleak
(like hr_live_0ab.py) can still get the schema bootstrapped.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "health" / "sr16.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def init_db() -> None:
    """Create sr16.db and apply schema.sql if it doesn't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    conn.close()