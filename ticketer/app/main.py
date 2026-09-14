"""Ticketer API: capture sessions (batches of GPS-tagged photos) that are processed later."""

import hashlib
import mimetypes
import os
import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, BinaryIO

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from .db import Database

VERSION = os.environ.get("TICKETER_VERSION", "dev")
STATIC_DIR = Path(__file__).parent / "static"
PHOTO_FORMATS = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png")}
EXIF_ORIENTATION = 0x0112

mimetypes.add_type("application/manifest+json", ".webmanifest")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    dev_user: str | None = None  # login to assume without a Tailscale header; local dev only
    max_photo_bytes: int = 30 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            dev_user=os.environ.get("TICKETER_DEV_USER") or None,
        )


@dataclass(frozen=True)
class User:
    login: str
    name: str | None


class BatchCreate(BaseModel):
    id: uuid.UUID


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


def capture_json(row: sqlite3.Row) -> dict:
    fields = ("id", "captured_at", "lat", "lon", "accuracy_m", "heading", "speed_mps", "fix_at",
              "width", "height", "bytes", "content_type", "received_at")
    return {k: row[k] for k in fields} | {
        "photo_url": f"/api/batches/{row['batch_id']}/captures/{row['id']}/photo",
    }


def batch_json(conn: sqlite3.Connection, row: sqlite3.Row, with_captures: bool = False) -> dict:
    captures = conn.execute(
        "SELECT * FROM captures WHERE batch_id = ? ORDER BY captured_at", (row["id"],)
    ).fetchall()
    out = dict(row) | {"capture_count": len(captures)}
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.data_dir / "ticketer.db")
    photos_dir = settings.data_dir / "photos"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        photos_dir.mkdir(parents=True, exist_ok=True)
        db.init()
        yield

    app = FastAPI(title="Ticketer", version=VERSION, lifespan=lifespan)

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
        return {"version": VERSION, "server_time": utc_now(),
                "user_login": user.login, "user_name": user.name}

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
        """Discard an unfinished session and its photos. Deleting a missing batch succeeds."""
        with db.connect() as conn:
            try:
                batch = get_batch(conn, batch_id)
            except HTTPException:
                return
            require_owner(batch, user)
            require_capturing(batch)
            conn.execute("DELETE FROM batches WHERE id = ?", (str(batch_id),))
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
        tmp = batch_dir / f".{capture_id}.upload"
        try:
            sha256, size = save_limited(photo.file, tmp, settings.max_photo_bytes)
            fmt, width, height = inspect_image(tmp)
            ext, content_type = PHOTO_FORMATS[fmt]
            with db.connect() as conn:
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
                os.replace(tmp, settings.data_dir / photo_path)
                conn.execute(
                    "INSERT INTO captures (id, batch_id, photo_path, content_type, bytes, sha256,"
                    " width, height, captured_at, lat, lon, accuracy_m, heading, speed_mps, fix_at,"
                    " received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(capture_id), str(batch_id), photo_path, content_type, size, sha256,
                     width, height, to_utc(captured_at), lat, lon, accuracy_m, heading, speed_mps,
                     to_utc(fix_at) if fix_at else None, utc_now()),
                )
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
        """End the capture session and queue it. Extraction is a later step."""
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
            return batch_json(conn, batch, with_captures=True)

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

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
