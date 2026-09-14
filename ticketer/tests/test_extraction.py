"""Extraction worker tests with a fake extractor, plate reader and geocoder (no network, no models)."""

import pytest
from fastapi.testclient import TestClient

from app.extract import Extraction, ExtractionError, VehicleReport
from app.geocode import Address, GeocodeError, parse_reverse_geocode
from app.main import Settings, create_app
from app.plates import PlateRead
from app.worker import Pipeline
from test_api import ALICE, jpeg, new_batch, upload


def extraction(**overrides) -> Extraction:
    fields = dict(plate_text="8abc-123", plate_state="ca", plate_confidence="high", color="white",
                  make="Toyota", model="Camry", make_model_confidence="medium", notes="Rear plate, daylight.")
    return Extraction(report=VehicleReport(**(fields | overrides)), model="fake-model",
                      input_tokens=1500, output_tokens=120, cost_usd=0.0042)


class FakeExtractor:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def extract(self, image):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakePlates:
    def __init__(self, text="8ABC123", error=None):
        self.text, self.error = text, error

    def read(self, image):
        if self.error:
            raise self.error
        return PlateRead(text=self.text, ocr_confidence=0.93, detection_confidence=0.81, box=(5, 5, 30, 20))


class FakeGeocoder:
    def __init__(self, error=None):
        self.error = error

    def reverse(self, lat, lon):
        if self.error:
            raise self.error
        return Address(street="100 Example St", full="100 Example St, Sacramento, California, 95814",
                       city="Sacramento", postal="95814", match_type="PointAddress",
                       lat=lat, lon=lon + 0.0001, distance_m=8.7)


@pytest.fixture
def make_client(tmp_path):
    def make(extractor, plates=None, geocoder=None):
        pipeline = Pipeline(extractor=extractor, plate_reader=plates or FakePlates(),
                            geocoder=geocoder or FakeGeocoder())
        return TestClient(create_app(Settings(data_dir=tmp_path, run_worker=False), pipeline=pipeline))
    return make


def queued_batch(client, photos=1, with_gps=True):
    batch_id = new_batch(client)
    gps = {"lat": 37.7749, "lon": -122.4194, "accuracy_m": 9} if with_gps else {}
    capture_ids = [upload(client, batch_id, photo=jpeg(), **gps)[0] for _ in range(photos)]
    assert client.post(f"/api/batches/{batch_id}/process", headers=ALICE).status_code == 200
    return batch_id, capture_ids


def drain(client) -> None:
    while client.app.state.worker.run_once():
        pass


def get(client, batch_id) -> dict:
    return client.get(f"/api/batches/{batch_id}", headers=ALICE).json()


def test_extracts_a_queued_batch(make_client):
    with make_client(FakeExtractor(extraction(), extraction(plate_text=None, plate_confidence="low"))) as client:
        batch_id, _ = queued_batch(client, photos=2)
        drain(client)
        batch = get(client, batch_id)

    assert batch["status"] == "ready"
    assert batch["drafts"] == {"pending": 0, "done": 2, "error": 0}
    assert batch["cost_usd"] == pytest.approx(0.0084)
    first, second = (c["draft"] for c in batch["captures"])
    assert (first["plate_text"], first["plate_state"], first["plate_confidence"]) == ("8ABC123", "CA", "high")
    assert (first["color"], first["make"], first["model"]) == ("white", "Toyota", "Camry")
    assert first["alpr_text"] == "8ABC123" and first["plates_agree"] is True
    assert (first["address"], first["address_match"], first["address_distance_m"]) == ("100 Example St", "PointAddress", 8.7)
    assert second["plate_text"] is None and second["plates_agree"] is None  # nothing to compare


def test_plate_close_up_is_served(make_client):
    with make_client(FakeExtractor(extraction())) as client:
        batch_id, _ = queued_batch(client)
        drain(client)
        capture = get(client, batch_id)["captures"][0]
        crop = client.get(capture["plate_crop_url"], headers=ALICE)
    assert crop.status_code == 200 and crop.headers["content-type"] == "image/jpeg"


def test_plate_disagreement_is_flagged(make_client):
    with make_client(FakeExtractor(extraction()), plates=FakePlates(text="3ABC123")) as client:
        batch_id, _ = queued_batch(client)
        drain(client)
        draft = get(client, batch_id)["captures"][0]["draft"]
    assert draft["plates_agree"] is False


def test_retryable_error_waits_then_succeeds(make_client):
    extractor = FakeExtractor(ExtractionError("Anthropic API rate limit reached", retryable=True), extraction())
    with make_client(extractor) as client:
        batch_id, _ = queued_batch(client)
        drain(client)
        batch = get(client, batch_id)
        assert batch["status"] == "processing"
        assert batch["captures"][0]["draft"]["status"] == "pending"
        assert batch["captures"][0]["draft"]["error"] == "Anthropic API rate limit reached"

        with client.app.state.db.connect() as conn:  # fast-forward the backoff
            conn.execute("UPDATE drafts SET next_attempt_at = NULL")
        drain(client)
        batch = get(client, batch_id)
    assert batch["status"] == "ready" and batch["captures"][0]["draft"]["status"] == "done"
    assert extractor.calls == 2


def test_permanent_error_then_retry_failed(make_client):
    extractor = FakeExtractor(ExtractionError("Anthropic API key rejected", retryable=False), extraction())
    with make_client(extractor) as client:
        batch_id, _ = queued_batch(client)
        drain(client)
        batch = get(client, batch_id)
        assert batch["status"] == "ready" and batch["drafts"]["error"] == 1
        assert batch["captures"][0]["draft"]["error"] == "Anthropic API key rejected"

        retried = client.post(f"/api/batches/{batch_id}/retry", headers=ALICE).json()
        assert retried["status"] == "processing" and retried["drafts"]["pending"] == 1
        drain(client)
        assert get(client, batch_id)["drafts"] == {"pending": 0, "done": 1, "error": 0}


def test_batches_wait_while_no_api_key_is_configured(make_client):
    # After upgrading, the app starts before the key can be entered: queued batches must not fail.
    with make_client(None) as client:
        batch_id, _ = queued_batch(client)
        assert client.app.state.worker.run_once() is False
        batch = get(client, batch_id)
        me = client.get("/api/whoami", headers=ALICE).json()
    assert batch["status"] == "queued" and batch["captures"][0]["draft"] is None
    assert me["extraction_enabled"] is False


def test_unexpected_extractor_exception_does_not_stall_the_worker(make_client):
    with make_client(FakeExtractor(RuntimeError("boom"), extraction())) as client:
        batch_id, _ = queued_batch(client, photos=2)
        drain(client)
        batch = get(client, batch_id)
    assert batch["status"] == "ready"
    assert sorted(c["draft"]["status"] for c in batch["captures"]) == ["done", "error"]


def test_plate_reader_and_geocoder_failures_do_not_block_extraction(make_client):
    plates = FakePlates(error=RuntimeError("onnx exploded"))
    geocoder = FakeGeocoder(error=GeocodeError("Geocoder unavailable: timed out"))
    with make_client(FakeExtractor(extraction()), plates=plates, geocoder=geocoder) as client:
        batch_id, _ = queued_batch(client)
        drain(client)
        capture = get(client, batch_id)["captures"][0]
    draft = capture["draft"]
    assert draft["status"] == "done" and draft["plate_text"] == "8ABC123"
    assert draft["alpr_error"] == "onnx exploded" and draft["geocode_error"].startswith("Geocoder unavailable")
    assert capture["plate_crop_url"] is None


def test_capture_without_gps_skips_geocoding(make_client):
    with make_client(FakeExtractor(extraction()), geocoder=FakeGeocoder(error=AssertionError("not called"))) as client:
        batch_id, _ = queued_batch(client, with_gps=False)
        drain(client)
        draft = get(client, batch_id)["captures"][0]["draft"]
    assert draft["status"] == "done" and draft["address"] is None and draft["geocode_error"] is None


# A real response from the geocoder for Sacramento City Hall (a public landmark).
CITY_HALL = {
    "address": {"Match_addr": "915-999 I St, Sacramento, California, 95814", "ShortLabel": "915-999 I St",
                "Addr_type": "StreetAddress", "AddNum": "967", "Address": "967 I St", "City": "Sacramento",
                "Postal": "95814"},
    "location": {"x": -121.493318337979, "y": 38.581538437996, "spatialReference": {"wkid": 4326}},
}


def test_parse_reverse_geocode():
    address = parse_reverse_geocode(CITY_HALL, 38.5816, -121.4933)
    assert (address.street, address.match_type, address.postal) == ("967 I St", "StreetAddress", "95814")
    assert address.full == "915-999 I St, Sacramento, California, 95814"
    assert 0 < address.distance_m < 10

    no_match = {"error": {"code": 400, "message": "Cannot perform query. Invalid query parameters.",
                          "details": ["Unable to find address for the specified location."]}}
    assert parse_reverse_geocode(no_match, 38.5816, -121.4933) is None
    with pytest.raises(GeocodeError):
        parse_reverse_geocode({"error": {"code": 498, "message": "Invalid token."}}, 38.5816, -121.4933)
