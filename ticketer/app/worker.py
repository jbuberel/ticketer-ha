"""Background extraction: turns queued batches into drafts, one photo at a time."""

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps

from .db import Database
from .extract import ExtractionError, Extractor
from .geocode import Geocoder
from .plates import PlateRead, PlateReader, normalize_plate

log = logging.getLogger("ticketer.worker")

MAX_ATTEMPTS = 5
POLL_SECONDS = 10

# Draft columns written by a run; cleared before re-running a draft.
RESULT_COLUMNS = (
    "error", "extracted_at", "plate_text", "plate_state", "plate_confidence", "color", "make", "model",
    "make_model_confidence", "notes", "extractor_model", "request_id", "input_tokens", "output_tokens", "cost_usd",
    "alpr_text", "alpr_confidence", "alpr_box", "alpr_error", "plates_agree", "address", "address_full",
    "address_match", "address_lat", "address_lon", "address_distance_m", "geocode_error",
)


@dataclass
class Pipeline:
    extractor: Extractor | None  # None when no API key is configured
    plate_reader: PlateReader | None
    geocoder: Geocoder | None


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def plate_crop_path(photo_path: str) -> str:
    return str(Path(photo_path).with_suffix(".plate.jpg"))


class Worker:
    def __init__(self, db: Database, data_dir: Path, pipeline: Pipeline):
        self.db = db
        self.data_dir = data_dir
        self.pipeline = pipeline
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned_unconfigured = False

    @property
    def extraction_enabled(self) -> bool:
        return self.pipeline.extractor is not None

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="extraction", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
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
                log.exception("extraction worker error")
                busy = False
            if not busy:
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()

    def run_once(self) -> bool:
        """Process one pending photo. Returns False when nothing is due."""
        if not self.extraction_enabled:
            # Leave batches queued so they run once a key is configured, instead of failing them all.
            if not self._warned_unconfigured:
                log.warning("no Anthropic API key configured; queued batches will wait")
                self._warned_unconfigured = True
            return False
        now = utc_iso()
        with self.db.connect(immediate=True) as conn:
            for batch in conn.execute("SELECT id FROM batches WHERE status = 'queued'").fetchall():
                conn.execute(
                    "INSERT OR IGNORE INTO drafts (capture_id, batch_id, status, created_at)"
                    " SELECT id, batch_id, 'pending', ? FROM captures WHERE batch_id = ?",
                    (now, batch["id"]),
                )
                conn.execute("UPDATE batches SET status = 'processing' WHERE id = ?", (batch["id"],))
            row = conn.execute(
                "SELECT d.capture_id, d.attempts, c.photo_path, c.lat, c.lon,"
                " c.address, c.address_full, c.address_match"
                " FROM drafts d JOIN captures c ON c.id = d.capture_id JOIN batches b ON b.id = d.batch_id"
                " WHERE d.status = 'pending' AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)"
                " ORDER BY b.created_at, c.captured_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "UPDATE batches SET status = 'ready' WHERE status = 'processing' AND NOT EXISTS"
                    " (SELECT 1 FROM drafts d WHERE d.batch_id = batches.id AND d.status = 'pending')"
                )
                return False

        started = time.monotonic()
        try:
            self._process(row)
        except Exception as e:  # a bug rather than a transient failure: don't loop on this photo
            log.exception("unexpected extraction failure for capture %s", row["capture_id"])
            self._finish(row["capture_id"], error=f"Unexpected error: {e}")
        log.info("capture %s processed in %.1f s", row["capture_id"][:8], time.monotonic() - started)
        return True

    def retry(self, batch_id: str, include_done: bool = False) -> int:
        """Queue a batch's failed drafts (or all of them) to run again. Returns how many.
        Reviewer edits are kept, but report/skip decisions and plate checks are cleared: they were
        made against the old results."""
        statuses = ("error", "done") if include_done else ("error",)
        reset = ", ".join(f"{column} = NULL" for column in RESULT_COLUMNS)
        with self.db.connect(immediate=True) as conn:
            changed = conn.execute(
                f"UPDATE drafts SET status = 'pending', attempts = 0, next_attempt_at = NULL, {reset},"
                " decision = NULL, plate_checked = 0, version = version + 1"
                f" WHERE batch_id = ? AND status IN ({', '.join('?' for _ in statuses)})",
                (batch_id, *statuses),
            ).rowcount
            if changed:
                conn.execute("UPDATE batches SET status = 'processing' WHERE id = ? AND status = 'ready'", (batch_id,))
        self.wake()
        return changed

    def _process(self, row: sqlite3.Row) -> None:
        capture_id, photo_path = row["capture_id"], row["photo_path"]
        fields: dict = {}
        try:
            with Image.open(self.data_dir / photo_path) as original:
                image = ImageOps.exif_transpose(original).convert("RGB")
        except Exception as e:
            return self._finish(capture_id, error=f"Can't read the photo: {e}")

        local_plates: list[PlateRead] = []
        if self.pipeline.plate_reader:
            try:
                local_plates = self.pipeline.plate_reader.read(image)
            except Exception as e:
                log.exception("plate reader failed for capture %s", capture_id)
                fields["alpr_error"] = str(e)

        if row["address"]:
            # Settled on the phone in front of the house, which beats anything a fix can be
            # resolved to afterwards. address_lat/lon/distance_m stay empty: they described the
            # geocoder's own match, and this address may not be it.
            fields |= {"address": row["address"], "address_full": row["address_full"] or row["address"],
                       "address_match": row["address_match"]}
        elif self.pipeline.geocoder and row["lat"] is not None:
            try:
                address = self.pipeline.geocoder.reverse(row["lat"], row["lon"])
                if address:
                    fields |= {"address": address.street, "address_full": address.full,
                               "address_match": address.match_type, "address_lat": address.lat,
                               "address_lon": address.lon, "address_distance_m": address.distance_m}
            except Exception as e:
                fields["geocode_error"] = str(e)

        if self.pipeline.extractor is None:
            fields |= self._local_plate(image, local_plates, None, photo_path)
            return self._finish(capture_id, fields, error="No Anthropic API key is configured (app Configuration tab)")
        try:
            result = self.pipeline.extractor.extract(image)
        except ExtractionError as e:
            attempts = row["attempts"] + 1
            if e.retryable and attempts < MAX_ATTEMPTS:
                return self._retry_later(capture_id, attempts, str(e))
            fields |= self._local_plate(image, local_plates, None, photo_path)
            return self._finish(capture_id, fields, error=str(e))

        report = result.report
        plate_text = normalize_plate(report.plate_text)
        fields |= self._local_plate(image, local_plates, plate_text, photo_path)
        alpr_text = fields.get("alpr_text")
        fields |= {
            "plate_text": plate_text,
            "plate_state": (report.plate_state or "").strip().upper() or None,
            "plate_confidence": report.plate_confidence,
            "color": report.color,
            "make": report.make,
            "model": report.model,
            "make_model_confidence": report.make_model_confidence,
            "notes": report.notes,
            "extractor_model": result.model,
            "request_id": result.request_id,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "plates_agree": None if not (plate_text and alpr_text) else int(plate_text == alpr_text),
        }
        self._finish(capture_id, fields)

    def _local_plate(self, image: Image.Image, plates: list[PlateRead], extracted: str | None,
                     photo_path: str) -> dict:
        """Pick the local reading to show and save its close-up. Street photos often include other
        cars' plates, so prefer the reading that matches the extracted plate, else the most confident."""
        if not plates:
            return {}
        chosen = next((p for p in plates if extracted and normalize_plate(p.text) == extracted), plates[0])
        self._save_crop(image, chosen.box, photo_path)
        return {"alpr_text": normalize_plate(chosen.text), "alpr_confidence": chosen.ocr_confidence,
                "alpr_box": json.dumps(chosen.box)}

    def _save_crop(self, image: Image.Image, box: tuple[int, int, int, int], photo_path: str) -> None:
        x1, y1, x2, y2 = box
        pad_x, pad_y = (x2 - x1) * 0.3, (y2 - y1) * 0.6
        crop = image.crop((max(0, x1 - pad_x), max(0, y1 - pad_y),
                           min(image.width, x2 + pad_x), min(image.height, y2 + pad_y)))
        crop.thumbnail((640, 640))
        crop.save(self.data_dir / plate_crop_path(photo_path), "JPEG", quality=90)

    def _retry_later(self, capture_id: str, attempts: int, error: str) -> None:
        delay = timedelta(seconds=30 * 2 ** (attempts - 1))  # 30 s, 1 min, 2 min, 4 min
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE drafts SET attempts = ?, error = ?, next_attempt_at = ? WHERE capture_id = ?",
                (attempts, error, utc_iso(datetime.now(timezone.utc) + delay), capture_id),
            )

    def _finish(self, capture_id: str, fields: dict | None = None, error: str | None = None) -> None:
        values = dict(fields or {}) | {"status": "error" if error else "done", "error": error,
                                       "extracted_at": utc_iso(), "next_attempt_at": None}
        assignments = ", ".join(f"{column} = ?" for column in values)  # column names come from this module
        with self.db.connect() as conn:
            conn.execute(f"UPDATE drafts SET {assignments}, attempts = attempts + 1, version = version + 1"
                         " WHERE capture_id = ?",
                         (*values.values(), capture_id))
