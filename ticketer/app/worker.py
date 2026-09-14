"""Background extraction: turns queued batches into drafts, one photo at a time."""

import json
import logging
import re
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
from .plates import PlateReader

log = logging.getLogger("ticketer.worker")

MAX_ATTEMPTS = 5
POLL_SECONDS = 10


@dataclass
class Pipeline:
    extractor: Extractor | None  # None when no API key is configured
    plate_reader: PlateReader | None
    geocoder: Geocoder | None


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def normalize_plate(text: str | None) -> str | None:
    cleaned = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    return cleaned or None


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
                "SELECT d.capture_id, d.attempts, c.photo_path, c.lat, c.lon"
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

    def retry_failed(self, batch_id: str) -> None:
        with self.db.connect(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE drafts SET status = 'pending', attempts = 0, error = NULL, next_attempt_at = NULL"
                " WHERE batch_id = ? AND status = 'error'",
                (batch_id,),
            ).rowcount
            if changed:
                conn.execute("UPDATE batches SET status = 'processing' WHERE id = ? AND status = 'ready'", (batch_id,))
        self.wake()

    def _process(self, row: sqlite3.Row) -> None:
        capture_id = row["capture_id"]
        fields: dict = {}
        try:
            with Image.open(self.data_dir / row["photo_path"]) as original:
                image = ImageOps.exif_transpose(original).convert("RGB")
        except Exception as e:
            return self._finish(capture_id, error=f"Can't read the photo: {e}")

        if self.pipeline.plate_reader:
            try:
                plate = self.pipeline.plate_reader.read(image)
                if plate:
                    fields |= {"alpr_text": normalize_plate(plate.text), "alpr_confidence": plate.ocr_confidence,
                               "alpr_box": json.dumps(plate.box)}
                    self._save_crop(image, plate.box, row["photo_path"])
            except Exception as e:
                log.exception("plate reader failed for capture %s", capture_id)
                fields["alpr_error"] = str(e)

        if self.pipeline.geocoder and row["lat"] is not None:
            try:
                address = self.pipeline.geocoder.reverse(row["lat"], row["lon"])
                if address:
                    fields |= {"address": address.street, "address_full": address.full,
                               "address_match": address.match_type, "address_lat": address.lat,
                               "address_lon": address.lon, "address_distance_m": address.distance_m}
            except Exception as e:
                fields["geocode_error"] = str(e)

        if self.pipeline.extractor is None:
            return self._finish(capture_id, fields, error="No Anthropic API key is configured (app Configuration tab)")
        try:
            result = self.pipeline.extractor.extract(image)
        except ExtractionError as e:
            attempts = row["attempts"] + 1
            if e.retryable and attempts < MAX_ATTEMPTS:
                return self._retry_later(capture_id, attempts, str(e))
            return self._finish(capture_id, fields, error=str(e))

        report = result.report
        plate_text = normalize_plate(report.plate_text)
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
            conn.execute(f"UPDATE drafts SET {assignments}, attempts = attempts + 1 WHERE capture_id = ?",
                         (*values.values(), capture_id))
