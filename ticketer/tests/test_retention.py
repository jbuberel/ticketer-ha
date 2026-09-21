"""Retention: expired batches are deleted, photos and all, and a filed case leaves a stub behind.

Time is moved by rewriting the stored timestamps rather than by waiting: every clock these
queries read is a column.
"""

import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db import Database
from app.main import Settings, create_app
from app.retention import Reaper, RetentionPolicy

ALICE = {"Tailscale-User-Login": "alice@example.com", "Tailscale-User-Name": "Alice"}


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, max_photo_bytes=200_000, run_worker=False)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def db(settings):
    return Database(settings.data_dir / "ticketer.db")


@pytest.fixture
def reaper(db, settings):
    return Reaper(db, settings.data_dir / "photos", settings.retention())


def hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def make_batch(client, photos: int = 1) -> tuple[str, list[str]]:
    batch_id = str(uuid.uuid4())
    assert client.post("/api/batches", json={"id": batch_id}, headers=ALICE).status_code == 201
    captures = []
    for _ in range(photos):
        capture_id = str(uuid.uuid4())
        r = client.put(
            f"/api/batches/{batch_id}/captures/{capture_id}",
            files={"photo": ("photo.jpg", jpeg(), "image/jpeg")},
            data={"captured_at": "2026-09-20T22:00:00Z"},
            headers=ALICE,
        )
        assert r.status_code == 201, r.text
        captures.append(capture_id)
    return batch_id, captures


def age_batch(db, batch_id: str, hours: float) -> None:
    """Move a batch and its photos that many hours into the past."""
    with db.connect() as conn:
        conn.execute("UPDATE batches SET created_at = ? WHERE id = ?", (hours_ago(hours), batch_id))
        conn.execute("UPDATE captures SET received_at = ? WHERE batch_id = ?", (hours_ago(hours), batch_id))


def add_submission(db, batch_id: str, capture_id: str, *, hours: float, status: str = "sent",
                   dry_run: int = 0, case_number: str | None = "260101-1234567") -> str:
    submission_id = str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO submissions (id, capture_id, batch_id, draft_version, dry_run, status,"
            " payload, description, case_number, photo_attached, requested_by, created_at, completed_at)"
            " VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, 1, 'alice@example.com', ?, ?)",
            (submission_id, capture_id, batch_id, dry_run, status,
             '{"plate": "1TST234"}', "Blue Honda Civic, plate 1TST234", case_number,
             hours_ago(hours), hours_ago(hours)),
        )
    return submission_id


def batch_ids(db) -> set[str]:
    with db.connect() as conn:
        return {row["id"] for row in conn.execute("SELECT id FROM batches")}


# ---- the unsubmitted clock ----


def test_fresh_batch_is_kept(client, db, reaper):
    batch_id, _ = make_batch(client)
    assert reaper.sweep() == 0
    assert batch_id in batch_ids(db)


def test_unsubmitted_batch_expires_after_eight_hours(client, db, reaper, settings):
    batch_id, _ = make_batch(client)
    photos = settings.data_dir / "photos" / batch_id
    age_batch(db, batch_id, hours=7.5)
    assert reaper.sweep() == 0, "still inside the window"

    age_batch(db, batch_id, hours=8.5)
    assert reaper.sweep() == 1
    assert batch_id not in batch_ids(db)
    assert not photos.exists(), "the photos go with the batch"


def test_captures_and_drafts_go_with_the_batch(client, db, reaper):
    batch_id, captures = make_batch(client, photos=2)
    with db.connect() as conn:
        for capture_id in captures:
            conn.execute("INSERT INTO drafts (capture_id, batch_id, status, created_at)"
                         " VALUES (?, ?, 'done', ?)", (capture_id, batch_id, hours_ago(9)))
    age_batch(db, batch_id, hours=9)
    reaper.sweep()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_a_still_open_session_expires_too(client, db, reaper):
    """A session left open on a phone is still a folder of photos of other people's cars."""
    batch_id, _ = make_batch(client)
    age_batch(db, batch_id, hours=9)
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()[0] == "capturing"
    assert reaper.sweep() == 1


def test_the_clock_runs_from_the_last_photo(client, db, reaper):
    """A long session isn't cut short: it is the newest photo that starts the countdown."""
    batch_id, captures = make_batch(client, photos=2)
    age_batch(db, batch_id, hours=9)
    with db.connect() as conn:
        conn.execute("UPDATE captures SET received_at = ? WHERE id = ?", (hours_ago(1), captures[-1]))
    assert reaper.sweep() == 0
    assert batch_id in batch_ids(db)


def test_a_dry_run_does_not_extend_the_clock(client, db, reaper):
    """A dry run creates nothing at the city, so it buys the batch no extra time."""
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=9)
    add_submission(db, batch_id, captures[0], hours=0, status="prepared", dry_run=1, case_number=None)
    assert reaper.sweep() == 1


# ---- the submitted clock ----


def test_a_submitted_batch_lives_a_day(client, db, reaper):
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=30)  # well past the unsubmitted window
    add_submission(db, batch_id, captures[0], hours=20)
    assert reaper.sweep() == 0, "the submitted clock is the later one"
    assert batch_id in batch_ids(db)


def test_a_submitted_batch_goes_after_twenty_four_hours(client, db, reaper, settings):
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=30)
    add_submission(db, batch_id, captures[0], hours=25)
    assert reaper.sweep() == 1
    assert not (settings.data_dir / "photos" / batch_id).exists()


def test_a_submission_in_flight_holds_the_batch(client, db, reaper):
    """Whatever the clocks say: the submitter may be reading the photo right now."""
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=40)
    add_submission(db, batch_id, captures[0], hours=30, status="sending", case_number=None)
    assert reaper.sweep() == 0
    assert batch_id in batch_ids(db)


# ---- what survives ----


def test_a_filed_case_leaves_a_stub(client, db, reaper):
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=30)
    submission_id = add_submission(db, batch_id, captures[0], hours=25)
    reaper.sweep()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM cases").fetchall()
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
    assert len(rows) == 1
    case = rows[0]
    assert case["submission_id"] == submission_id
    assert case["case_number"] == "260101-1234567"
    assert case["status"] == "sent"
    assert case["photo_attached"] == 1
    assert case["requested_by"] == "alice@example.com"
    assert case["purged_at"]


def test_the_stub_carries_no_personal_data(client, db, reaper):
    """The payload holds the plate, the address and a neighbour's name and mailing address.
    None of it may outlive the batch."""
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=30)
    add_submission(db, batch_id, captures[0], hours=25)
    reaper.sweep()
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(cases)")}
        stored = " ".join(str(v) for v in dict(conn.execute("SELECT * FROM cases").fetchone()).values())
    assert not columns & {"payload", "description", "warnings", "error"}
    assert "1TST234" not in stored


def test_an_uncertain_submission_is_kept_as_a_case(client, db, reaper):
    """`unknown` means a case may exist and submit.py will never retry it, so the owner needs
    the record to check the city's open data against."""
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=30)
    add_submission(db, batch_id, captures[0], hours=25, status="unknown", case_number=None)
    reaper.sweep()
    with db.connect() as conn:
        case = conn.execute("SELECT * FROM cases").fetchone()
    assert case["status"] == "unknown"
    assert case["case_number"] is None


def test_a_dry_run_leaves_no_case(client, db, reaper):
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=9)
    add_submission(db, batch_id, captures[0], hours=9, status="prepared", dry_run=1, case_number=None)
    reaper.sweep()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_cases_endpoint_lists_the_stubs(client, db, reaper):
    batch_id, captures = make_batch(client)
    age_batch(db, batch_id, hours=30)
    add_submission(db, batch_id, captures[0], hours=25)
    reaper.sweep()
    body = client.get("/api/cases", headers=ALICE).json()
    assert [c["case_number"] for c in body["cases"]] == ["260101-1234567"]
    assert body["cases"][0]["photo_attached"] is True


# ---- orphaned files ----


def test_orphaned_photo_directories_are_removed(client, db, reaper, settings):
    """A purge that died between the delete and the rmtree leaves a directory with no batch."""
    orphan = settings.data_dir / "photos" / str(uuid.uuid4())
    orphan.mkdir(parents=True)
    (orphan / "photo.jpg").write_bytes(jpeg())
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    os.utime(orphan, (old, old))

    reaper.sweep()
    assert not orphan.exists()


def test_a_new_directory_is_left_alone(client, db, reaper, settings):
    """An upload may be writing into it right now."""
    orphan = settings.data_dir / "photos" / str(uuid.uuid4())
    orphan.mkdir(parents=True)
    reaper.sweep()
    assert orphan.exists()


def test_a_live_batchs_photos_survive_the_orphan_sweep(client, reaper, settings):
    batch_id, _ = make_batch(client)
    photos = settings.data_dir / "photos" / batch_id
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    os.utime(photos, (old, old))
    reaper.sweep()
    assert photos.exists()


# ---- what the phone is told ----


def test_the_api_says_when_a_batch_expires(client, db):
    batch_id, _ = make_batch(client)
    age_batch(db, batch_id, hours=2)
    batch = client.get(f"/api/batches/{batch_id}", headers=ALICE).json()
    expires = datetime.fromisoformat(batch["expires_at"])
    assert expires.tzinfo is not None, "a bare timestamp would be read as local time on the phone"
    hours_left = (expires - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 5.9 < hours_left < 6.1


def test_a_submission_pushes_the_expiry_out(client, db):
    batch_id, captures = make_batch(client)
    add_submission(db, batch_id, captures[0], hours=0)
    batch = client.get(f"/api/batches/{batch_id}", headers=ALICE).json()
    hours_left = (datetime.fromisoformat(batch["expires_at"]) - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 23.9 < hours_left < 24.1


def test_whoami_reports_the_policy(client):
    retention = client.get("/api/whoami", headers=ALICE).json()["retention"]
    assert retention == {"unsubmitted_hours": 8, "submitted_hours": 24}


# ---- configuration ----


def test_the_windows_are_configurable(tmp_path):
    settings = Settings(data_dir=tmp_path, run_worker=False,
                        retain_unsubmitted_hours=2, retain_submitted_hours=3)
    assert settings.retention().params == ("+2 hours", "+3 hours")


def test_out_of_range_windows_are_clamped_not_fatal():
    """A typo in the app's options shouldn't stop it booting, but it can't switch retention off."""
    assert RetentionPolicy(unsubmitted_hours=0, submitted_hours=10_000) == RetentionPolicy(1, 24 * 7)


def test_a_short_window_is_honoured(client, db, settings, tmp_path):
    batch_id, _ = make_batch(client)
    age_batch(db, batch_id, hours=3)
    quick = Reaper(db, tmp_path / "photos", RetentionPolicy(unsubmitted_hours=2, submitted_hours=3))
    assert quick.sweep() == 1
