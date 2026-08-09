"""SQLite storage. One connection, used only from the main/UI thread.

The serial worker never touches the database; it hands packets to the UI thread via
Qt signals, and persistence happens there. So check_same_thread stays at its safe default.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Union

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);

CREATE TABLE IF NOT EXISTS telemetry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    received_at     TEXT NOT NULL,
    arduino_time_ms INTEGER NOT NULL,
    ay              INTEGER NOT NULL,
    shock           INTEGER NOT NULL,
    distance_cm     REAL,
    status          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    received_at     TEXT NOT NULL,
    arduino_time_ms INTEGER NOT NULL,
    ay              INTEGER NOT NULL,
    shock           INTEGER NOT NULL,
    distance_cm     REAL,
    status          TEXT NOT NULL,
    notes           TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_telemetry_session ON telemetry(session_id);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
"""


class Database:
    def __init__(self, path: Optional[Union[str, Path]] = None):
        if path is None:
            path = paths.database_path()
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
