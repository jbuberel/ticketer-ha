"""Retention: expired capture sessions are deleted, photos and all.

A photo of a car parked where it shouldn't be is useful for a few hours. After that it is a
picture of someone's car with their plate beside it, sitting on a disk with nothing left to do.
The hard rules ask for a retention policy; this is it, and it runs on its own.

Two clocks, whichever falls later:

- an unsubmitted session expires `unsubmitted_hours` after its last photo arrived (default 8).
  These vehicles are parked for six to eight hours at most, so a report that hasn't been sent by
  then can no longer be sent usefully -- the car has gone.
- a session with a real submission expires `submitted_hours` after the last one was sent
  (default 24). The city auto-closes a request it hasn't acted on within a day, so nothing here
  has a reason to outlive that either.

A submission still queued or in flight holds its batch back whatever the clocks say, so a purge
can't delete a photo out from under the submitter thread.

The clocks read server timestamps (`captures.received_at`, `batches.created_at`), never the
phone's `captured_at`: a phone whose clock is wrong would otherwise expire a session on arrival
or never expire it at all. Uploads happen as the photos are taken, so the two are seconds apart.

What survives a purge is one row per real submission in `cases`: the city's case number, the
status, when it was filed. No plate, no address, no photo, and not the payload -- which carries a
neighbour's name and mailing address from `getAdditionalInfo` and is the most sensitive thing
stored here. The case number is kept so a filed request can still be followed in the city's
public `SalesForce311_View` layer after its batch is gone, including one left `unknown`, which
`submit.py` deliberately never retries.
"""

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import IN_FLIGHT_SUBMISSION_STATUSES, LIVE_SUBMISSION_STATUSES, Database

log = logging.getLogger("ticketer.retention")

POLL_SECONDS = 300  # nothing here is urgent to the minute
MIN_HOURS, MAX_HOURS = 1, 24 * 7
# A photo directory with no batch row is left alone this long first, so a purge racing an upload
# can't delete the directory a capture is being written into.
ORPHAN_GRACE_SECONDS = 3600


def _hours(name: str, value: int) -> int:
    bounded = max(MIN_HOURS, min(MAX_HOURS, value))
    if bounded != value:
        log.warning("%s of %s h is outside %d-%d h; using %d h", name, value, MIN_HOURS, MAX_HOURS, bounded)
    return bounded


@dataclass(frozen=True)
class RetentionPolicy:
    unsubmitted_hours: int = 8
    submitted_hours: int = 24

    def __post_init__(self) -> None:
        # Clamped rather than rejected: a typo in the app's options shouldn't stop it booting.
        object.__setattr__(self, "unsubmitted_hours", _hours("retain_unsubmitted_hours", self.unsubmitted_hours))
        object.__setattr__(self, "submitted_hours", _hours("retain_submitted_hours", self.submitted_hours))

    @property
    def params(self) -> tuple[str, str]:
        """The two SQLite date modifiers the queries below take, in order."""
        return (f"+{self.unsubmitted_hours} hours", f"+{self.submitted_hours} hours")


# When batch `b` is due for deletion. Takes RetentionPolicy.params. Everything is normalised
# through datetime(), so the comparisons are against SQLite's own UTC format rather than the
# stored ISO strings. The '' keeps MAX() from going NULL when a batch was never submitted.
EXPIRES_AT = """
    MAX(
        datetime(COALESCE((SELECT MAX(received_at) FROM captures WHERE batch_id = b.id),
                          b.created_at), ?),
        COALESCE((SELECT datetime(MAX(COALESCE(completed_at, created_at)), ?) FROM submissions
                  WHERE batch_id = b.id AND dry_run = 0), '')
    )
"""

BATCH_EXPIRES_AT = f"SELECT {EXPIRES_AT} AS expires_at FROM batches b WHERE b.id = ?"

EXPIRED_BATCHES = f"""
SELECT id, expires_at FROM (
    SELECT b.id AS id, {EXPIRES_AT} AS expires_at,
           EXISTS (SELECT 1 FROM submissions WHERE batch_id = b.id AND status IN ({
               ', '.join('?' for _ in IN_FLIGHT_SUBMISSION_STATUSES)})) AS in_flight
    FROM batches b
) WHERE expires_at <= datetime('now') AND NOT in_flight
"""

# Copied out before the batch goes, because `cases` has no foreign key to hold it to one.
RECORD_CASES = f"""
INSERT OR REPLACE INTO cases
    (submission_id, case_number, case_id, status, photo_attached, requested_by, filed_at, purged_at)
SELECT id, case_number, case_id, status, photo_attached, requested_by,
       COALESCE(completed_at, created_at), ?
FROM submissions
WHERE batch_id = ? AND dry_run = 0 AND status IN ({', '.join('?' for _ in LIVE_SUBMISSION_STATUSES)})
"""


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_iso(sqlite_datetime: str | None) -> str | None:
    """SQLite's "YYYY-MM-DD HH:MM:SS" (always UTC here) as ISO 8601, so a browser doesn't read it
    as local time."""
    return f"{sqlite_datetime.replace(' ', 'T')}+00:00" if sqlite_datetime else None


def expires_at(conn, batch_id: str, policy: RetentionPolicy) -> str | None:
    row = conn.execute(BATCH_EXPIRES_AT, (*policy.params, batch_id)).fetchone()
    return as_iso(row["expires_at"]) if row else None


class Reaper:
    """Sweeps expired batches off disk on a timer. Deliberately has no API: retention that only
    runs when someone remembers to press a button is not a retention policy."""

    def __init__(self, db: Database, photos_dir: Path, policy: RetentionPolicy):
        self.db = db
        self.photos_dir = photos_dir
        self.policy = policy
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="retention", daemon=True)
        self._thread.start()
        log.info("retention: unsubmitted %d h, submitted %d h",
                 self.policy.unsubmitted_hours, self.policy.submitted_hours)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:
                log.exception("retention sweep failed")
            self._stop.wait(POLL_SECONDS)

    def sweep(self) -> int:
        """Delete every expired batch and any photo directory left behind. Returns how many
        batches went."""
        purged = self._purge_expired()
        self._remove_orphan_dirs()
        return purged

    def _purge_expired(self) -> int:
        with self.db.connect(immediate=True) as conn:
            rows = conn.execute(
                EXPIRED_BATCHES, (*self.policy.params, *IN_FLIGHT_SUBMISSION_STATUSES)
            ).fetchall()
            for row in rows:
                conn.execute(RECORD_CASES, (utc_iso(), row["id"], *LIVE_SUBMISSION_STATUSES))
                # captures, drafts and submissions cascade; `cases` has no key into any of them.
                conn.execute("DELETE FROM batches WHERE id = ?", (row["id"],))
        for row in rows:
            # After the commit: a directory left behind is tidied by the orphan sweep below,
            # whereas a row with no photos would be a batch the app can't show.
            shutil.rmtree(self.photos_dir / row["id"], ignore_errors=True)
            log.info("retention: purged batch %s, due %s", row["id"][:8], row["expires_at"])
        return len(rows)

    def _remove_orphan_dirs(self) -> None:
        """Photo directories with no batch: a purge that died between the delete and the rmtree,
        or a batch deleted by hand the same way."""
        if not self.photos_dir.is_dir():
            return
        with self.db.connect() as conn:
            known = {row["id"] for row in conn.execute("SELECT id FROM batches")}
        cutoff = time.time() - ORPHAN_GRACE_SECONDS
        for entry in self.photos_dir.iterdir():
            try:
                if not entry.is_dir() or entry.name in known or entry.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            log.info("retention: removed orphaned photo directory %s", entry.name[:8])
