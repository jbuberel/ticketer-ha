"""Sends approved drafts to 311, one at a time, in the background.

Safety rules this module exists to enforce:

- Nothing is sent that the owner has not approved as a specific draft version.
- Dry run is the default. A dry run does every read and assembles the payload, then stops: it
  never uploads a photo and never creates a case.
- One request at a time, with a pause between them. A batch of photos must not arrive at the
  city as a burst.
- No automatic retry, ever. If we don't know whether a case was created, the row is left
  `unknown` for the owner to check against the city's open data. Retrying would risk sending two
  officers to one car.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import Database
from .sac311 import Reporter, Sac311Error, Sac311Service, SubmissionUncertain, VehicleReport

log = logging.getLogger("ticketer.submit")

POLL_SECONDS = 10
PAUSE_BETWEEN_SECONDS = 5.0  # keep the volume human-scale, as a person filling the form would


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SubmitConfig:
    reporter: Reporter
    dry_run: bool = True
    attach_photo: bool = True


class Submitter:
    def __init__(self, db: Database, data_dir: Path, service: Sac311Service | None,
                 config: SubmitConfig, pause: float = PAUSE_BETWEEN_SECONDS):
        self.db = db
        self.data_dir = data_dir
        self.service = service
        self.config = config
        self.pause = pause
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="submission", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:
                log.exception("submission worker error")
                busy = False
            if busy:
                self._stop.wait(self.pause)  # space the requests out
            else:
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()

    def run_once(self) -> bool:
        """Send one queued submission. Returns False when nothing is waiting."""
        with self.db.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT s.*, c.photo_path, c.content_type FROM submissions s"
                " JOIN captures c ON c.id = s.capture_id"
                " WHERE s.status = 'queued' ORDER BY s.created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            # Claim it before releasing the lock, so a second worker can't pick up the same one.
            conn.execute("UPDATE submissions SET status = 'sending' WHERE id = ?", (row["id"],))

        try:
            self._send(row)
        except Exception as e:  # a bug, not a portal failure: don't leave the row claimed
            log.exception("unexpected failure sending submission %s", row["id"])
            self._finish(row["id"], "failed", error=f"Unexpected error: {e}")
        return True

    def _send(self, row: sqlite3.Row) -> None:
        if self.service is None:
            return self._finish(row["id"], "failed", error="311 submission is not configured")
        dry_run = bool(row["dry_run"])
        report = self._report_for(row)
        if report is None:
            return self._finish(row["id"], "failed",
                                error="This draft is missing fields a 311 request needs")
        try:
            prepared = self.service.prepare(report, self.config.reporter)
        except Sac311Error as e:
            return self._finish(row["id"], "failed", error=str(e))

        fields = {"payload": json.dumps(prepared.case_record), "description": prepared.summary,
                  "warnings": json.dumps(prepared.warnings)}
        if dry_run:
            # Everything above was a read. Stop here: no photo upload, no case.
            return self._finish(row["id"], "prepared", **fields)

        photo = self._photo(row) if self.config.attach_photo else None
        try:
            result = self.service.submit(prepared, photo)
        except SubmissionUncertain as e:
            # Do not retry. The owner checks the city's open data for a matching case.
            log.error("submission %s is uncertain: %s", row["id"], e)
            return self._finish(row["id"], "unknown", error=str(e), **fields)
        except Sac311Error as e:
            return self._finish(row["id"], "failed", error=str(e), **fields)

        log.info("submitted capture %s as case %s", row["capture_id"][:8], result.case_number)
        self._finish(row["id"], "sent", case_number=result.case_number, case_id=result.case_id,
                     photo_attached=int(photo is not None), **fields)

    def _report_for(self, row: sqlite3.Row) -> VehicleReport | None:
        """Read the draft the owner approved and turn it into what 311 asks for. Values are the
        extracted ones overlaid with the reviewer's edits, exactly as the review screen showed."""
        with self.db.connect() as conn:
            draft = conn.execute(
                "SELECT d.*, c.captured_at, c.lat, c.lon, c.address AS capture_address,"
                " c.address_full AS capture_address_full"
                " FROM drafts d JOIN captures c ON c.id = d.capture_id WHERE d.capture_id = ?",
                (row["capture_id"],),
            ).fetchone()
        if draft is None:
            return None
        edits = json.loads(draft["edits"] or "{}")

        def value(field: str):
            return edits.get(field, draft[field])

        required = {f: value(f) for f in ("plate_text", "color", "make", "model", "address")}
        if not all(required.values()):
            return None
        return VehicleReport(
            plate=required["plate_text"],
            plate_state=value("plate_state"),
            color=required["color"],
            make=required["make"],
            model=required["model"],
            address=required["address"],
            postal=postal_from(draft["capture_address_full"] or draft["address_full"]),
            lat=draft["lat"],
            lon=draft["lon"],
            seen_at=draft["captured_at"],
        )

    def _photo(self, row: sqlite3.Row) -> tuple[str, bytes] | None:
        path = self.data_dir / row["photo_path"]
        if not path.is_file():
            return None
        suffix = ".png" if row["content_type"] == "image/png" else ".jpg"
        return (f"{row['capture_id']}{suffix}", path.read_bytes())

    def _finish(self, submission_id: str, status: str, **fields) -> None:
        values = dict(fields) | {"status": status, "completed_at": utc_iso()}
        assignments = ", ".join(f"{column} = ?" for column in values)  # column names are ours
        with self.db.connect() as conn:
            conn.execute(f"UPDATE submissions SET {assignments} WHERE id = ?",
                         (*values.values(), submission_id))


def postal_from(address_full: str | None) -> str | None:
    """The ZIP out of a geocoder match line like "1200 Example St, Sacramento, California, 95814"."""
    if not address_full:
        return None
    tail = address_full.rsplit(",", 1)[-1].strip()
    return tail if tail.isdigit() and len(tail) == 5 else None


def new_submission_id() -> str:
    return str(uuid.uuid4())


def queue(conn: sqlite3.Connection, capture_id: str, batch_id: str, draft_version: int,
          dry_run: bool, requested_by: str) -> str:
    submission_id = new_submission_id()
    conn.execute(
        "INSERT INTO submissions (id, capture_id, batch_id, draft_version, dry_run, status,"
        " requested_by, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
        (submission_id, capture_id, batch_id, draft_version, int(dry_run), requested_by, utc_iso()),
    )
    return submission_id
