"""SQLite storage for capture batches and drafts. One short-lived connection per operation."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,   -- client-generated UUID
    created_by      TEXT NOT NULL,      -- Tailscale login
    created_by_name TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,      -- capturing | queued | processing | ready
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

-- One draft per capture, filled in by the extraction worker.
CREATE TABLE IF NOT EXISTS drafts (
    capture_id            TEXT PRIMARY KEY REFERENCES captures(id) ON DELETE CASCADE,
    batch_id              TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    status                TEXT NOT NULL,        -- pending | done | error
    attempts              INTEGER NOT NULL DEFAULT 0,
    next_attempt_at       TEXT,
    error                 TEXT,
    created_at            TEXT NOT NULL,
    extracted_at          TEXT,
    -- extractor (Claude)
    plate_text            TEXT,
    plate_state           TEXT,
    plate_confidence      TEXT,                 -- high | medium | low
    color                 TEXT,
    make                  TEXT,
    model                 TEXT,
    make_model_confidence TEXT,
    notes                 TEXT,
    extractor_model       TEXT,
    request_id            TEXT,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cost_usd              REAL,
    -- local plate reader (fast-alpr)
    alpr_text             TEXT,
    alpr_confidence       REAL,
    alpr_box              TEXT,                 -- JSON [x1, y1, x2, y2]
    alpr_error            TEXT,
    plates_agree          INTEGER,              -- 1 / 0, NULL when either reading is missing
    -- reverse geocode of the GPS fix
    address               TEXT,
    address_full          TEXT,
    address_match         TEXT,                 -- PointAddress | StreetAddress
    address_lat           REAL,
    address_lon           REAL,
    address_distance_m    REAL,
    geocode_error         TEXT
);

CREATE INDEX IF NOT EXISTS drafts_by_status ON drafts (status, next_attempt_at);
"""

# Address columns on captures (v4), settled on the phone while the photo is taken so the address
# doesn't have to be remembered back home. When set, the extraction worker uses these instead of
# reverse geocoding the GPS fix. address_full and address_match describe the geocoder's match, so
# they are only filled in while the address is still the one it returned.
CAPTURE_ADDRESS_COLUMNS = {
    "address": "TEXT",
    "address_full": "TEXT",
    "address_source": "TEXT",  # geocoded (accepted as looked up) | picked (a neighbour) | typed
    "address_match": "TEXT",   # PointAddress | StreetAddress
}

# Review columns (v3), added with ALTER TABLE so databases from earlier versions upgrade in place.
DRAFT_REVIEW_COLUMNS = {
    "version": "INTEGER NOT NULL DEFAULT 1",  # bumped on every change a submission would send
    "decision": "TEXT",                       # NULL (undecided) | report | skip
    "edits": "TEXT",                          # JSON {field: value} overriding extracted values
    "plate_checked": "INTEGER NOT NULL DEFAULT 0",  # reviewer confirmed the plate against the photo
    "reviewed_by": "TEXT",
    "reviewed_at": "TEXT",
}


class Database:
    def __init__(self, path: Path):
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            for table, columns in (("drafts", DRAFT_REVIEW_COLUMNS), ("captures", CAPTURE_ADDRESS_COLUMNS)):
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
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
