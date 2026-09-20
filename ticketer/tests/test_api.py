"""API tests for capture sessions. Run from ticketer/: python -m pytest"""

import io
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import Settings, create_app

ALICE = {"Tailscale-User-Login": "alice@example.com", "Tailscale-User-Name": "Alice"}
BOB = {"Tailscale-User-Login": "bob@example.com", "Tailscale-User-Name": "Bob"}


def jpeg(width=40, height=30, orientation=None, color="red") -> bytes:
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG", exif=exif)
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, max_photo_bytes=200_000, run_worker=False)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def new_batch(client, headers=ALICE) -> str:
    batch_id = str(uuid.uuid4())
    r = client.post("/api/batches", json={"id": batch_id}, headers=headers)
    assert r.status_code == 201, r.text
    return batch_id


def upload(client, batch_id, capture_id=None, photo=None, headers=ALICE, **fields):
    capture_id = capture_id or str(uuid.uuid4())
    r = client.put(
        f"/api/batches/{batch_id}/captures/{capture_id}",
        files={"photo": ("photo.jpg", photo or jpeg(), "image/jpeg")},
        data={"captured_at": "2026-09-13T22:00:00Z", **fields},
        headers=headers,
    )
    return capture_id, r


def test_requires_tailscale_identity(client):
    assert client.get("/api/batches").status_code == 401


def test_dev_user_fallback(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, dev_user="dev@example.com", run_worker=False))) as c:
        assert c.get("/api/whoami").json()["user_login"] == "dev@example.com"


def test_serves_the_app_shell(client):
    r = client.get("/")
    assert r.status_code == 200 and "app.js" in r.text


def test_create_batch_is_idempotent_for_its_creator(client):
    batch_id = new_batch(client)
    assert client.post("/api/batches", json={"id": batch_id}, headers=ALICE).status_code == 200
    assert client.post("/api/batches", json={"id": batch_id}, headers=BOB).status_code == 409


def test_upload_stores_photo_and_location(client, settings):
    batch_id = new_batch(client)
    capture_id, r = upload(client, batch_id, photo=jpeg(40, 30, orientation=6),
                           lat=37.7749, lon=-122.4194, accuracy_m=8, fix_at="2026-09-13T21:59:58Z")
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["width"], body["height"]) == (30, 40)  # EXIF rotation applied
    assert (body["lat"], body["lon"], body["accuracy_m"]) == (37.7749, -122.4194, 8)
    assert body["fix_at"].startswith("2026-09-13T21:59:58")
    assert (settings.data_dir / "photos" / batch_id / f"{capture_id}.jpg").is_file()

    photo = client.get(body["photo_url"], headers=ALICE)
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"


def test_upload_without_location(client):
    _, r = upload(client, new_batch(client))
    assert r.status_code == 201 and r.json()["lat"] is None


def test_rejects_half_a_location(client):
    _, r = upload(client, new_batch(client), lat=37.7749)
    assert r.status_code == 422


def test_upload_retry_is_idempotent(client):
    batch_id = new_batch(client)
    photo = jpeg()
    capture_id, first = upload(client, batch_id, photo=photo)
    _, again = upload(client, batch_id, capture_id, photo=photo)
    _, different = upload(client, batch_id, capture_id, photo=jpeg(color="blue"))
    assert (first.status_code, again.status_code, different.status_code) == (201, 200, 409)
    assert client.get(f"/api/batches/{batch_id}", headers=ALICE).json()["capture_count"] == 1


def test_concurrent_retries_of_the_same_photo(client, settings):
    # Two open copies of the app (or a retry that overlaps a slow first attempt) can send the
    # same capture at once. Exactly one stores it; the rest see it as already stored.
    batch_id = new_batch(client)
    capture_id, photo = str(uuid.uuid4()), jpeg(2000, 1500)
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = sorted(pool.map(lambda _: upload(client, batch_id, capture_id, photo=photo)[1].status_code, range(6)))
    assert codes == [200, 200, 200, 200, 200, 201]
    assert [p.name for p in (settings.data_dir / "photos" / batch_id).iterdir()] == [f"{capture_id}.jpg"]


def test_rejects_non_images_and_oversized_uploads(client, settings):
    batch_id = new_batch(client)
    assert upload(client, batch_id, photo=b"not an image")[1].status_code == 422
    assert upload(client, batch_id, photo=b"\xff" * 300_000)[1].status_code == 413
    assert [p.name for p in (settings.data_dir / "photos" / batch_id).iterdir()] == []


def test_only_the_creator_can_change_a_batch(client):
    batch_id = new_batch(client)
    assert upload(client, batch_id, headers=BOB)[1].status_code == 403
    assert client.delete(f"/api/batches/{batch_id}", headers=BOB).status_code == 403
    assert client.post(f"/api/batches/{batch_id}/process", headers=BOB).status_code == 403
    assert client.get(f"/api/batches/{batch_id}", headers=BOB).status_code == 200


def test_delete_capture(client, settings):
    batch_id = new_batch(client)
    capture_id, _ = upload(client, batch_id)
    url = f"/api/batches/{batch_id}/captures/{capture_id}"
    assert client.delete(url, headers=ALICE).status_code == 204
    assert client.delete(url, headers=ALICE).status_code == 204  # retry-safe
    assert list((settings.data_dir / "photos" / batch_id).iterdir()) == []
    assert client.get(f"/api/batches/{batch_id}", headers=ALICE).json()["capture_count"] == 0


def test_process_closes_the_batch(client):
    batch_id = new_batch(client)
    process = f"/api/batches/{batch_id}/process"
    assert client.post(process, headers=ALICE).status_code == 409  # nothing to process

    photo = jpeg()
    capture_id, _ = upload(client, batch_id, photo=photo)
    r = client.post(process, headers=ALICE)
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["closed_at"]
    assert client.post(process, headers=ALICE).json()["status"] == "queued"  # idempotent

    assert upload(client, batch_id)[1].status_code == 409  # no new photos
    assert upload(client, batch_id, capture_id, photo=photo)[1].status_code == 200  # lost response retried
    assert client.delete(f"/api/batches/{batch_id}/captures/{capture_id}", headers=ALICE).status_code == 409


def test_discard_batch_removes_photos(client, settings):
    batch_id = new_batch(client)
    upload(client, batch_id)
    assert client.delete(f"/api/batches/{batch_id}", headers=ALICE).status_code == 204
    assert not (settings.data_dir / "photos" / batch_id).exists()
    assert client.get(f"/api/batches/{batch_id}", headers=ALICE).status_code == 404
    assert client.delete(f"/api/batches/{batch_id}", headers=ALICE).status_code == 204


def test_list_batches_newest_first_with_counts(client):
    mine = new_batch(client)
    upload(client, mine)
    theirs = new_batch(client, BOB)
    batches = client.get("/api/batches", headers=ALICE).json()["batches"]
    assert {b["id"]: b["capture_count"] for b in batches} == {mine: 1, theirs: 0}
    assert batches[0]["created_by"] in {"alice@example.com", "bob@example.com"}


def test_app_files_are_revalidated_so_a_phone_cannot_run_stale_code(client):
    """Without this the browser caches app.js heuristically: the home screen reports the new
    version (that comes from the API) while the page still runs the old JavaScript."""
    for path in ("/", "/app.js", "/index.html"):
        r = client.get(path, headers=ALICE)
        assert r.status_code == 200, path
        assert "no-cache" in r.headers.get("cache-control", ""), f"{path} may be cached blind"
