"""Ticketer API: capture sessions (batches of GPS-tagged photos) and the drafts extracted from them."""

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, BinaryIO, Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, ConfigDict

from .db import Database
from .extract import ClaudeExtractor
from .geocode import CITY_311_GEOCODER, ArcGisReverseGeocoder
from .plates import FastAlprReader, normalize_plate
from .worker import Pipeline, Worker, plate_crop_path

VERSION = os.environ.get("TICKETER_VERSION", "dev")
STATIC_DIR = Path(__file__).parent / "static"
PHOTO_FORMATS = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png")}
EXIF_ORIENTATION = 0x0112
DEFAULT_EXTRACTOR_MODEL = "claude-sonnet-5"

mimetypes.add_type("application/manifest+json", ".webmanifest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    dev_user: str | None = None  # login to assume without a Tailscale header; local dev only
    max_photo_bytes: int = 30 * 1024 * 1024
    anthropic_api_key: str | None = None
    extractor_model: str = DEFAULT_EXTRACTOR_MODEL
    geocoder_url: str = CITY_311_GEOCODER
    run_worker: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            dev_user=os.environ.get("TICKETER_DEV_USER") or None,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            extractor_model=os.environ.get("TICKETER_EXTRACTOR_MODEL") or DEFAULT_EXTRACTOR_MODEL,
            geocoder_url=os.environ.get("TICKETER_GEOCODER_URL") or CITY_311_GEOCODER,
        )


def default_pipeline(settings: Settings) -> Pipeline:
    return Pipeline(
        extractor=(ClaudeExtractor(settings.extractor_model, settings.anthropic_api_key)
                   if settings.anthropic_api_key else None),
        plate_reader=FastAlprReader(),
        geocoder=ArcGisReverseGeocoder(settings.geocoder_url),
    )


@dataclass(frozen=True)
class User:
    login: str
    name: str | None


class BatchCreate(BaseModel):
    id: uuid.UUID


class DraftUpdate(BaseModel):
    """A review change. Only the fields sent are changed; `version` must be the draft's current one."""

    model_config = ConfigDict(extra="forbid")
    version: int
    decision: Literal["report", "skip"] | None = None
    plate_checked: bool | None = None
    plate_text: str | None = None
    plate_state: str | None = None
    color: str | None = None
    make: str | None = None
    model: str | None = None
    address: str | None = None


EDITABLE_FIELDS = ("plate_text", "plate_state", "color", "make", "model", "address")
REQUIRED_TO_REPORT = {"plate_text": "plate", "color": "color", "make": "make", "model": "model", "address": "address"}
TEXT_LIMITS = {"color": 40, "make": 40, "model": 60, "address": 200}
MAX_PLATE_LENGTH = 8


def clean_field(field: str, value: str | None) -> str | None:
    if field == "plate_text":
        plate = normalize_plate(value)
        if plate and len(plate) > MAX_PLATE_LENGTH:
            raise HTTPException(422, f"A plate has at most {MAX_PLATE_LENGTH} letters and digits")
        return plate
    value = " ".join((value or "").split()) or None
    if field == "plate_state":
        if value and not re.fullmatch(r"[A-Za-z]{2}", value):
            raise HTTPException(422, "State must be a 2-letter code, e.g. CA")
        return value.upper() if value else None
    if value and len(value) > TEXT_LIMITS[field]:
        raise HTTPException(422, f"{field} is longer than {TEXT_LIMITS[field]} characters")
    return value


def plate_needs_check(extracted: Mapping, edits: dict, plate_checked: bool) -> bool:
    """A plate is trusted only when Claude is confident and the local reader agrees. Any other plate must
    be typed in or confirmed against the photo by the reviewer before the draft can be reported."""
    trusted = extracted["plate_confidence"] == "high" and bool(extracted["plates_agree"])
    return not (trusted or "plate_text" in edits or plate_checked)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def save_limited(src: BinaryIO, dest: Path, limit: int) -> tuple[str, int]:
    """Copy src to dest, returning (sha256, size). Raises 413 past `limit` bytes."""
    digest = hashlib.sha256()
    size = 0
    with dest.open("wb") as out:
        while chunk := src.read(1 << 20):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"Photo is larger than {limit // (1024 * 1024)} MB")
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest(), size


def inspect_image(path: Path) -> tuple[str, int, int]:
    """Return (format, width, height) with EXIF rotation applied to the dimensions."""
    try:
        with Image.open(path) as im:
            fmt, (width, height) = im.format, im.size
            if im.getexif().get(EXIF_ORIENTATION) in (5, 6, 7, 8):
                width, height = height, width
            im.verify()
    except Exception as e:
        raise HTTPException(422, "Photo is not a readable image") from e
    if fmt not in PHOTO_FORMATS:
        raise HTTPException(415, f"Unsupported image format {fmt}; send JPEG or PNG")
    return fmt, width, height


CAPTURE_FIELDS = ("id", "captured_at", "lat", "lon", "accuracy_m", "heading", "speed_mps", "fix_at",
                  "width", "height", "bytes", "content_type", "received_at")
DRAFT_FIELDS = ("status", "error", "attempts", "plate_text", "plate_state", "plate_confidence", "color", "make",
                "model", "make_model_confidence", "notes", "extractor_model", "cost_usd", "alpr_text",
                "alpr_confidence", "alpr_error", "plates_agree", "address", "address_full", "address_match",
                "address_distance_m", "geocode_error", "extracted_at")
REVIEW_FIELDS = ("version", "decision", "edits", "plate_checked", "reviewed_by", "reviewed_at")
CAPTURE_WITH_DRAFT_SELECT = (
    "SELECT c.*, d.capture_id AS d_capture_id, d.alpr_box AS d_alpr_box, "  # extracted_at is in DRAFT_FIELDS
    + ", ".join(f"d.{f} AS d_{f}" for f in DRAFT_FIELDS + REVIEW_FIELDS)
    + " FROM captures c LEFT JOIN drafts d ON d.capture_id = c.id"
)
CAPTURES_WITH_DRAFTS = CAPTURE_WITH_DRAFT_SELECT + " WHERE c.batch_id = ? ORDER BY c.captured_at"
CAPTURE_WITH_DRAFT = CAPTURE_WITH_DRAFT_SELECT + " WHERE c.batch_id = ? AND c.id = ?"


def capture_json(row: sqlite3.Row) -> dict:
    base = f"/api/batches/{row['batch_id']}/captures/{row['id']}"
    out = {k: row[k] for k in CAPTURE_FIELDS} | {"photo_url": f"{base}/photo", "plate_crop_url": None, "draft": None}
    if "d_capture_id" in row.keys() and row["d_capture_id"]:
        draft = {f: row[f"d_{f}"] for f in DRAFT_FIELDS}
        if draft["plates_agree"] is not None:
            draft["plates_agree"] = bool(draft["plates_agree"])
        edits = json.loads(row["d_edits"] or "{}")
        plate_checked = bool(row["d_plate_checked"])
        draft["review"] = {
            "version": row["d_version"],
            "decision": row["d_decision"],
            "edits": edits,
            "values": {f: edits.get(f, draft[f]) for f in EDITABLE_FIELDS},  # what a submission would send
            "plate_checked": plate_checked,
            "plate_needs_check": plate_needs_check(draft, edits, plate_checked),
            "reviewed_by": row["d_reviewed_by"],
            "reviewed_at": row["d_reviewed_at"],
        }
        out["draft"] = draft
        if row["d_alpr_box"]:
            # The close-up is rewritten when a draft is re-run; a new URL keeps phones from showing a cached one.
            version = hashlib.sha1(f"{row['d_extracted_at']}{row['d_alpr_box']}".encode()).hexdigest()[:12]
            out["plate_crop_url"] = f"{base}/plate?v={version}"
    return out


def batch_json(conn: sqlite3.Connection, row: sqlite3.Row, with_captures: bool = False) -> dict:
    captures = conn.execute(CAPTURES_WITH_DRAFTS, (row["id"],)).fetchall()
    counts = {"pending": 0, "done": 0, "error": 0}
    review = {"report": 0, "skip": 0, "undecided": 0}
    cost = 0.0
    for capture in captures:
        if capture["d_status"] in counts:
            counts[capture["d_status"]] += 1
        if capture["d_status"] in ("done", "error"):
            review[capture["d_decision"] or "undecided"] += 1
        cost += capture["d_cost_usd"] or 0.0
    out = dict(row) | {"capture_count": len(captures), "drafts": counts, "review": review,
                       "cost_usd": round(cost, 4)}
    if with_captures:
        out["captures"] = [capture_json(c) for c in captures]
    return out


def get_batch(conn: sqlite3.Connection, batch_id: uuid.UUID) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (str(batch_id),)).fetchone()
    if row is None:
        raise HTTPException(404, "Batch not found")
    return row


def require_owner(batch: sqlite3.Row, user: User) -> None:
    if batch["created_by"] != user.login:
        raise HTTPException(403, "Only the person who started this capture session can change it")


def require_capturing(batch: sqlite3.Row) -> None:
    if batch["status"] != "capturing":
        raise HTTPException(409, f"Batch is {batch['status']} and no longer accepts changes")


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.data_dir / "ticketer.db")
    photos_dir = settings.data_dir / "photos"
    worker = Worker(db, settings.data_dir, pipeline or default_pipeline(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        photos_dir.mkdir(parents=True, exist_ok=True)
        db.init()
        if settings.run_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(title="Ticketer", version=VERSION, lifespan=lifespan)
    app.state.db = db
    app.state.worker = worker

    def current_user(request: Request) -> User:
        # Tailscale Serve sets these headers; the API itself only listens on localhost.
        login = request.headers.get("tailscale-user-login") or settings.dev_user
        if not login:
            raise HTTPException(401, "No Tailscale identity: open the app at its tailnet HTTPS address")
        return User(login=login, name=request.headers.get("tailscale-user-name"))

    CurrentUser = Annotated[User, Depends(current_user)]

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/whoami")
    def whoami(user: CurrentUser) -> dict:
        return {"version": VERSION, "server_time": utc_now(), "user_login": user.login,
                "user_name": user.name, "extraction_enabled": worker.extraction_enabled}

    @app.post("/api/batches", status_code=201)
    def create_batch(body: BatchCreate, user: CurrentUser, response: Response) -> dict:
        """Start a capture session. The phone picks the id, so retries are safe."""
        with db.connect() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO batches (id, created_by, created_by_name, created_at, status)"
                " VALUES (?, ?, ?, ?, 'capturing')",
                (str(body.id), user.login, user.name, utc_now()),
            ).rowcount
            batch = get_batch(conn, body.id)
            if not inserted:
                if batch["created_by"] != user.login:
                    raise HTTPException(409, "Batch id already in use")
                response.status_code = 200
            return batch_json(conn, batch)

    @app.get("/api/batches")
    def list_batches(user: CurrentUser, limit: int = 50) -> dict:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (min(limit, 200),)
            ).fetchall()
            return {"batches": [batch_json(conn, row) for row in rows]}

    @app.get("/api/batches/{batch_id}")
    def get_batch_detail(batch_id: uuid.UUID, user: CurrentUser) -> dict:
        with db.connect() as conn:
            return batch_json(conn, get_batch(conn, batch_id), with_captures=True)

    @app.delete("/api/batches/{batch_id}", status_code=204)
    def discard_batch(batch_id: uuid.UUID, user: CurrentUser) -> None:
        """Delete a batch with its photos, close-ups and drafts, in any state. Deleting a missing batch
        succeeds. If the worker is mid-photo, its result finds no draft row and is dropped."""
        with db.connect(immediate=True) as conn:
            try:
                batch = get_batch(conn, batch_id)
            except HTTPException:
                return
            require_owner(batch, user)
            conn.execute("DELETE FROM batches WHERE id = ?", (str(batch_id),))  # captures and drafts cascade
        shutil.rmtree(photos_dir / str(batch_id), ignore_errors=True)

    @app.put("/api/batches/{batch_id}/captures/{capture_id}", status_code=201)
    def put_capture(
        batch_id: uuid.UUID,
        capture_id: uuid.UUID,
        user: CurrentUser,
        response: Response,
        photo: UploadFile,
        captured_at: Annotated[datetime, Form()],
        lat: Annotated[float | None, Form(ge=-90, le=90)] = None,
        lon: Annotated[float | None, Form(ge=-180, le=180)] = None,
        accuracy_m: Annotated[float | None, Form(ge=0)] = None,
        heading: Annotated[float | None, Form(ge=0, le=360)] = None,
        speed_mps: Annotated[float | None, Form(ge=0)] = None,
        fix_at: Annotated[datetime | None, Form()] = None,
    ) -> dict:
        """Store one photo with the phone's GPS fix. Re-sending the same photo is safe."""
        if (lat is None) != (lon is None):
            raise HTTPException(422, "lat and lon must be sent together")
        with db.connect() as conn:
            require_owner(get_batch(conn, batch_id), user)

        batch_dir = photos_dir / str(batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)
        tmp = batch_dir / f".{capture_id}.{uuid.uuid4().hex}.upload"  # unique: retries can overlap
        try:
            sha256, size = save_limited(photo.file, tmp, settings.max_photo_bytes)
            fmt, width, height = inspect_image(tmp)
            ext, content_type = PHOTO_FORMATS[fmt]
            with db.connect(immediate=True) as conn:
                batch = get_batch(conn, batch_id)
                existing = conn.execute(
                    "SELECT * FROM captures WHERE id = ?", (str(capture_id),)
                ).fetchone()
                if existing:
                    if existing["batch_id"] != str(batch_id) or existing["sha256"] != sha256:
                        raise HTTPException(409, "Capture id already used for a different photo")
                    response.status_code = 200
                    return capture_json(existing)
                require_capturing(batch)

                photo_path = f"photos/{batch_id}/{capture_id}{ext}"
                conn.execute(
                    "INSERT INTO captures (id, batch_id, photo_path, content_type, bytes, sha256,"
                    " width, height, captured_at, lat, lon, accuracy_m, heading, speed_mps, fix_at,"
                    " received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(capture_id), str(batch_id), photo_path, content_type, size, sha256,
                     width, height, to_utc(captured_at), lat, lon, accuracy_m, heading, speed_mps,
                     to_utc(fix_at) if fix_at else None, utc_now()),
                )
                os.replace(tmp, settings.data_dir / photo_path)  # after the insert, so a failed insert leaves no file
                row = conn.execute("SELECT * FROM captures WHERE id = ?", (str(capture_id),)).fetchone()
                return capture_json(row)
        finally:
            tmp.unlink(missing_ok=True)

    @app.delete("/api/batches/{batch_id}/captures/{capture_id}", status_code=204)
    def delete_capture(batch_id: uuid.UUID, capture_id: uuid.UUID, user: CurrentUser) -> None:
        with db.connect() as conn:
            batch = get_batch(conn, batch_id)
            require_owner(batch, user)
            require_capturing(batch)
            row = conn.execute(
                "SELECT photo_path FROM captures WHERE id = ? AND batch_id = ?",
                (str(capture_id), str(batch_id)),
            ).fetchone()
            if row is None:
                return
            conn.execute("DELETE FROM captures WHERE id = ?", (str(capture_id),))
            (settings.data_dir / row["photo_path"]).unlink(missing_ok=True)

    @app.post("/api/batches/{batch_id}/process")
    def process_batch(batch_id: uuid.UUID, user: CurrentUser) -> dict:
        """End the capture session and queue it for extraction."""
        with db.connect() as conn:
            batch = get_batch(conn, batch_id)
            require_owner(batch, user)
            if batch["status"] == "capturing":
                count = conn.execute(
                    "SELECT COUNT(*) FROM captures WHERE batch_id = ?", (str(batch_id),)
                ).fetchone()[0]
                if count == 0:
                    raise HTTPException(409, "Batch has no photos; discard it instead")
                conn.execute(
                    "UPDATE batches SET status = 'queued', closed_at = ? WHERE id = ?",
                    (utc_now(), str(batch_id)),
                )
                batch = get_batch(conn, batch_id)
            result = batch_json(conn, batch, with_captures=True)
        worker.wake()
        return result

    @app.post("/api/batches/{batch_id}/retry")
    def retry_extraction(batch_id: uuid.UUID, user: CurrentUser, rerun_all: bool = False) -> dict:
        """Run extraction again for photos whose extraction failed, or for every photo (rerun_all)."""
        with db.connect() as conn:
            require_owner(get_batch(conn, batch_id), user)  # clears review decisions
        worker.retry(str(batch_id), include_done=rerun_all)
        with db.connect() as conn:
            return batch_json(conn, get_batch(conn, batch_id), with_captures=True)

    @app.patch("/api/batches/{batch_id}/captures/{capture_id}/draft")
    def review_draft(batch_id: uuid.UUID, capture_id: uuid.UUID, body: DraftUpdate, user: CurrentUser) -> dict:
        """Edit a draft's fields, confirm its plate, or decide report/skip. A stale `version` gets 409,
        so a decision always applies to the exact values the reviewer was looking at."""
        changes = {f: clean_field(f, getattr(body, f)) for f in EDITABLE_FIELDS if f in body.model_fields_set}
        with db.connect(immediate=True) as conn:
            batch = get_batch(conn, batch_id)
            require_owner(batch, user)
            row = conn.execute(CAPTURE_WITH_DRAFT, (str(batch_id), str(capture_id))).fetchone()
            if row is None or not row["d_capture_id"]:
                raise HTTPException(404, "Draft not found")
            if batch["status"] != "ready":
                raise HTTPException(409, "Drafts can be reviewed once extraction has finished")
            if body.version != row["d_version"]:
                raise HTTPException(409, "This draft changed since it was loaded")

            extracted = {f: row[f"d_{f}"] for f in DRAFT_FIELDS}
            old = (json.loads(row["d_edits"] or "{}"), row["d_decision"], bool(row["d_plate_checked"]))
            edits = dict(old[0])
            for field, value in changes.items():
                if value == extracted[field]:
                    edits.pop(field, None)  # set back to what extraction found
                else:
                    edits[field] = value
            decision = body.decision if "decision" in body.model_fields_set else old[1]
            plate_checked = bool(body.plate_checked) if "plate_checked" in body.model_fields_set else old[2]

            if decision == "report":
                values = {f: edits.get(f, extracted[f]) for f in EDITABLE_FIELDS}
                missing = [label for f, label in REQUIRED_TO_REPORT.items() if not values[f]]
                if missing:
                    raise HTTPException(422, f"To report, fill in: {', '.join(missing)}")
                if plate_needs_check(extracted, edits, plate_checked):
                    raise HTTPException(422, "Check the plate against the photo before reporting")

            if (edits, decision, plate_checked) != old:
                conn.execute(
                    "UPDATE drafts SET edits = ?, decision = ?, plate_checked = ?, version = version + 1,"
                    " reviewed_by = ?, reviewed_at = ? WHERE capture_id = ?",
                    (json.dumps(edits) if edits else None, decision, int(plate_checked), user.login, utc_now(),
                     str(capture_id)),
                )
                row = conn.execute(CAPTURE_WITH_DRAFT, (str(batch_id), str(capture_id))).fetchone()
            return capture_json(row)

    @app.get("/api/batches/{batch_id}/captures/{capture_id}/photo")
    def capture_photo(batch_id: uuid.UUID, capture_id: uuid.UUID, user: CurrentUser) -> FileResponse:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT photo_path, content_type FROM captures WHERE id = ? AND batch_id = ?",
                (str(capture_id), str(batch_id)),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Capture not found")
        return FileResponse(settings.data_dir / row["photo_path"], media_type=row["content_type"],
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/batches/{batch_id}/captures/{capture_id}/plate")
    def capture_plate_crop(batch_id: uuid.UUID, capture_id: uuid.UUID, user: CurrentUser) -> FileResponse:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT photo_path FROM captures WHERE id = ? AND batch_id = ?",
                (str(capture_id), str(batch_id)),
            ).fetchone()
        path = settings.data_dir / plate_crop_path(row["photo_path"]) if row else None
        if path is None or not path.is_file():
            raise HTTPException(404, "No plate close-up for this photo")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
