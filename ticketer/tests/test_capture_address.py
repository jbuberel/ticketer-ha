"""The address is settled on the phone as each photo is taken, not from memory back home."""

import uuid

import pytest

from app.geocode import GeocodeError, nearby_addresses
from test_api import ALICE, BOB, jpeg, new_batch, upload
from test_extraction import FakeExtractor, FakeGeocoder, drain, extraction, get, make_client  # noqa: F401

FIX = {"lat": 37.7749, "lon": -122.4194, "accuracy_m": 9}
FIELD_ADDRESS = {"address": "1301 Example St", "address_source": "picked"}


@pytest.fixture
def client(make_client):  # noqa: F811
    with make_client(FakeExtractor(extraction())) as c:
        yield c


def capture_of(client, batch_id) -> dict:
    return get(client, batch_id)["captures"][0]


@pytest.mark.parametrize("street, expected", [
    # Stepping by 2 keeps the same side of the street; the block boundary is not crossed.
    ("1211 Example St", ["1207 Example St", "1209 Example St", "1211 Example St",
                         "1213 Example St", "1215 Example St"]),
    ("1203 Example St", ["1201 Example St", "1203 Example St", "1205 Example St", "1207 Example St"]),
    ("1211A Example St", ["1211A Example St"]),  # no plain house number to step
    ("Example St", ["Example St"]),
])
def test_nearby_addresses(street, expected):
    assert nearby_addresses(street) == expected


def test_geocode_offers_the_match_and_its_neighbours(client):
    r = client.get("/api/geocode", params={"lat": 37.7749, "lon": -122.4194}, headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["address"] == "100 Example St" and body["address_match"] == "PointAddress"
    # 96 and 98 are in the previous hundred block, so they are left out.
    assert body["candidates"] == ["100 Example St", "102 Example St", "104 Example St"]


def test_geocode_needs_an_identity_and_a_sane_fix(client):
    assert client.get("/api/geocode", params={"lat": 37.7, "lon": -122.4}).status_code == 401
    assert client.get("/api/geocode", params={"lat": 137.7, "lon": -122.4}, headers=ALICE).status_code == 422


def test_geocode_reports_a_failing_geocoder(make_client):  # noqa: F811
    with make_client(FakeExtractor(), geocoder=FakeGeocoder(error=GeocodeError("Geocoder unavailable"))) as client:
        r = client.get("/api/geocode", params={"lat": 37.7749, "lon": -122.4194}, headers=ALICE)
    assert r.status_code == 502 and "Geocoder unavailable" in r.json()["detail"]


def test_upload_keeps_the_address_settled_on_the_street(client):
    batch_id = new_batch(client)
    _, r = upload(client, batch_id, **FIX, **FIELD_ADDRESS)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["address"], body["address_source"]) == ("1301 Example St", "picked")
    # The geocoder's match line described the address it returned, not this one.
    assert (body["address_full"], body["address_match"]) == (None, None)


def test_upload_keeps_the_match_details_of_an_untouched_lookup(client):
    batch_id = new_batch(client)
    _, r = upload(client, batch_id, **FIX, address=" 100  Example St ", address_source="geocoded",
                  address_full="100 Example St, Sacramento, California, 95814", address_match="PointAddress")
    body = r.json()
    assert body["address"] == "100 Example St"  # whitespace collapsed like every other text field
    assert body["address_match"] == "PointAddress"
    assert body["address_full"].startswith("100 Example St, Sacramento")


def test_upload_rejects_an_address_without_its_source(client):
    batch_id = new_batch(client)
    assert upload(client, batch_id, **FIX, address="1301 Example St")[1].status_code == 422
    assert upload(client, batch_id, **FIX, address_source="picked")[1].status_code == 422
    assert upload(client, batch_id, **FIX, address="1301 Example St", address_source="guessed")[1].status_code == 422


def test_address_can_be_corrected_while_the_session_is_open(client):
    batch_id = new_batch(client)
    capture_id, _ = upload(client, batch_id, **FIX, address="100 Example St", address_source="geocoded",
                           address_full="100 Example St, Sacramento, California, 95814",
                           address_match="PointAddress")
    r = client.patch(f"/api/batches/{batch_id}/captures/{capture_id}",
                     json={"address": "104 Example St", "address_source": "picked"}, headers=ALICE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["address"], body["address_source"]) == ("104 Example St", "picked")
    assert (body["address_full"], body["address_match"]) == (None, None)  # no longer the geocoder's match


def test_correcting_an_address_is_the_creator_s_and_only_until_the_session_closes(client):
    batch_id = new_batch(client)
    capture_id, _ = upload(client, batch_id, **FIX, **FIELD_ADDRESS)
    change = {"address": "1305 Example St", "address_source": "picked"}
    patch = f"/api/batches/{batch_id}/captures/{capture_id}"
    assert client.patch(patch, json=change, headers=BOB).status_code == 403
    assert client.patch(patch, json={"address": "  ", "address_source": "typed"}, headers=ALICE).status_code == 422
    missing = client.patch(f"/api/batches/{batch_id}/captures/{uuid.uuid4()}", json=change, headers=ALICE)
    assert missing.status_code == 404

    client.post(f"/api/batches/{batch_id}/process", headers=ALICE)
    assert client.patch(patch, json=change, headers=ALICE).status_code == 409


def test_extraction_uses_the_field_address_instead_of_geocoding(make_client):  # noqa: F811
    geocoder = FakeGeocoder(error=AssertionError("the fix must not be geocoded again"))
    with make_client(FakeExtractor(extraction(), extraction()), geocoder=geocoder) as client:
        batch_id = new_batch(client)
        upload(client, batch_id, photo=jpeg(), **FIX, **FIELD_ADDRESS)
        client.post(f"/api/batches/{batch_id}/process", headers=ALICE)
        drain(client)
        draft = capture_of(client, batch_id)["draft"]
        assert draft["status"] == "done"
        assert draft["address"] == "1301 Example St" and draft["review"]["values"]["address"] == "1301 Example St"
        assert draft["address_full"] == "1301 Example St"  # no fuller form: the geocoder never saw it
        assert draft["geocode_error"] is None

        # Re-running extraction still starts from the address settled on the street.
        client.post(f"/api/batches/{batch_id}/retry?rerun_all=true", headers=ALICE)
        drain(client)
        assert capture_of(client, batch_id)["draft"]["address"] == "1301 Example St"


def test_extraction_still_geocodes_a_photo_with_no_field_address(client):
    batch_id = new_batch(client)
    upload(client, batch_id, photo=jpeg(), **FIX)
    client.post(f"/api/batches/{batch_id}/process", headers=ALICE)
    drain(client)
    capture = capture_of(client, batch_id)
    assert capture["address"] is None and capture["address_source"] is None
    assert capture["draft"]["address"] == "100 Example St"  # the worker's own lookup, as before
    assert capture["draft"]["address_distance_m"] == 8.7
