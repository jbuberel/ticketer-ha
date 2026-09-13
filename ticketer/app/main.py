"""Ticketer hello app: proves the phone -> Tailscale HTTPS -> Home Assistant app path works."""

import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.staticfiles import StaticFiles

VERSION = os.environ.get("TICKETER_VERSION", "dev")
STATIC_DIR = Path(__file__).parent / "static"

mimetypes.add_type("application/manifest+json", ".webmanifest")

app = FastAPI(title="Ticketer", version=VERSION)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/whoami")
def whoami(request: Request) -> dict:
    # Tailscale Serve sets these for requests from (untagged) user devices.
    return {
        "version": VERSION,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "user_login": request.headers.get("tailscale-user-login"),
        "user_name": request.headers.get("tailscale-user-name"),
    }


@app.post("/api/probe")
async def probe(
    photo: UploadFile,
    lat: Annotated[float | None, Form()] = None,
    lon: Annotated[float | None, Form()] = None,
    accuracy_m: Annotated[float | None, Form()] = None,
) -> dict:
    """Accept a test upload, count its bytes, and discard it. Nothing is stored."""
    size = 0
    while chunk := await photo.read(1 << 20):
        size += len(chunk)
    return {
        "filename": photo.filename,
        "content_type": photo.content_type,
        "bytes": size,
        "lat": lat,
        "lon": lon,
        "accuracy_m": accuracy_m,
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
