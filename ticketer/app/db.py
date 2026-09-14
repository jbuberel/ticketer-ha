"""SQLite storage for capture batches. One short-lived connection per request."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,   -- client-generated UUID
    created_by      TEXT NOT NULL,      -- Tailscale login
    created_by_name TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,      -- capturing | queued
    closed_at       TEXT
);

CREATE TABLE IF NOT EXISTS captures (
    id           TEXT PRIMARY KEY,      -- client-generated UUID
    batch_id     TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    photo_path   TEXT NOT NULL,         -- relative to the data dir
    content_type TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    width        INTEGER NOT NULL,      -- as displayed (EXIF rotation applied)
    height       INTEGER NOT NULL,
    captured_at  TEXT NOT NULL,         -- phone clock, when the photo came back from the camera
    lat          REAL,
    lon          REAL,
    accuracy_m   REAL,
    heading      REAL,
    speed_mps    REAL,
    fix_at       TEXT,                  -- phone clock, when the GPS fix was taken
    received_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS captures_by_batch ON captures (batch_id, captured_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def connect(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """immediate=True takes the write lock up front, so check-then-insert blocks run one at a time."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
