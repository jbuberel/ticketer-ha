"""Review tests: editing drafts, report/skip decisions, versions, and upgrading older databases."""

import sqlite3

from app.db import SCHEMA, SCHEMA_VERSION, Database
from app.extract import ExtractionError
from test_api import ALICE, BOB
from test_extraction import FakeExtractor, FakePlates, drain, extraction, get, make_client, queued_batch  # noqa: F401


def extracted_capture(client, batch_id=None) -> tuple[str, dict]:
    """Extract a one-photo batch (or re-read one) and return (batch id, its capture)."""
    if batch_id is None:
        batch_id, _ = queued_batch(client)
        drain(client)
    return batch_id, get(client, batch_id)["captures"][0]


def review(client, batch_id, capture, headers=ALICE, **changes):
    return client.patch(f"/api/batches/{batch_id}/captures/{capture['id']}/draft",
                        json={"version": capture["draft"]["review"]["version"], **changes}, headers=headers)


def test_edits_override_extracted_values(make_client):
    with make_client(FakeExtractor(extraction())) as client:
        batch_id, capture = extracted_capture(client)
        r = review(client, batch_id, capture, plate_text="1tst 234", color="  dark   blue ", plate_state="")

    assert r.status_code == 200, r.text
    draft = r.json()["draft"]
    assert draft["plate_text"] == "8ABC123"  # what extraction found is kept
    assert draft["review"]["edits"] == {"plate_text": "1TST234", "color": "dark blue", "plate_state": None}
    assert draft["review"]["values"] == {"plate_text": "1TST234", "plate_state": None, "color": "dark blue",
                                         "make": "Toyota", "model": "Camry", "address": "100 Example St"}
    assert draft["review"]["version"] == capture["draft"]["review"]["version"] + 1
    assert draft["review"]["reviewed_by"] == "alice@example.com"


def test_setting_a_field_back_to_the_extracted_value_drops_the_edit(make_client):
    with make_client(FakeExtractor(extraction())) as client:
        batch_id, capture = extracted_capture(client)
        edited = review(client, batch_id, capture, make="Lexus").json()
        restored = review(client, batch_id, edited, make="Toyota").json()["draft"]["review"]
    assert restored["edits"] == {} and restored["values"]["make"] == "Toyota"


def test_stale_version_is_rejected_and_a_no_op_keeps_the_version(make_client):
    with make_client(FakeExtractor(extraction())) as client:
        batch_id, capture = extracted_capture(client)
        assert review(client, batch_id, capture, decision="skip").status_code == 200
        assert review(client, batch_id, capture, decision="report").status_code == 409  # made against the old version

        _, current = extracted_capture(client, batch_id)
        again = review(client, batch_id, current, decision="skip").json()
    assert again["draft"]["review"]["version"] == current["draft"]["review"]["version"]


def test_trusted_plate_can_be_reported_directly(make_client):
    with make_client(FakeExtractor(extraction())) as client:  # plate high, and the local reader agrees
        batch_id, capture = extracted_capture(client)
        assert capture["draft"]["review"]["plate_needs_check"] is False
        r = review(client, batch_id, capture, decision="report")
        batch = get(client, batch_id)
    assert r.status_code == 200 and r.json()["draft"]["review"]["decision"] == "report"
    assert batch["review"] == {"report": 1, "skip": 0, "undecided": 0}


def test_untrusted_plate_must_be_checked_before_reporting(make_client):
    with make_client(FakeExtractor(extraction(plate_confidence="medium"))) as client:
        batch_id, capture = extracted_capture(client)
        assert capture["draft"]["review"]["plate_needs_check"] is True
        refused = review(client, batch_id, capture, decision="report")
        assert refused.status_code == 422 and "Check the plate" in refused.json()["detail"]

        r = review(client, batch_id, capture, decision="report", plate_checked=True)
    assert r.status_code == 200
    assert r.json()["draft"]["review"]["plate_checked"] is True


def test_differing_readings_need_a_check_and_typing_the_plate_counts(make_client):
    with make_client(FakeExtractor(extraction()), plates=FakePlates(text="3ABC123")) as client:
        batch_id, capture = extracted_capture(client)
        assert capture["draft"]["review"]["plate_needs_check"] is True
        assert review(client, batch_id, capture, decision="skip").status_code == 200  # skipping needs no check

        _, capture = extracted_capture(client, batch_id)
        r = review(client, batch_id, capture, plate_text="3ABC123", decision="report")
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["review"]["plate_needs_check"] is False


def test_report_needs_every_field(make_client):
    with make_client(FakeExtractor(extraction(model=None))) as client:
        batch_id, capture = extracted_capture(client)
        refused = review(client, batch_id, capture, decision="report", address="")
        assert refused.status_code == 422 and refused.json()["detail"] == "To report, fill in: model, address"
        _, unchanged = extracted_capture(client, batch_id)
        assert unchanged["draft"]["review"] == capture["draft"]["review"]  # nothing half-applied

        reported = review(client, batch_id, capture, decision="report", model="Corolla")
        assert reported.status_code == 200
        assert review(client, batch_id, reported.json(), make=None).status_code == 422  # can't empty it now


def test_failed_extraction_can_be_filled_in_by_hand(make_client):
    with make_client(FakeExtractor(ExtractionError("Anthropic API key rejected", retryable=False))) as client:
        batch_id, capture = extracted_capture(client)
        assert capture["draft"]["status"] == "error"
        r = review(client, batch_id, capture, plate_text="1TST234", plate_state="ca", color="Red", make="Honda",
                   model="Civic", address="100 Example St", decision="report")
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["review"]["values"]["plate_state"] == "CA"


def test_invalid_changes_are_rejected(make_client):
    with make_client(FakeExtractor(extraction())) as client:
        batch_id, capture = extracted_capture(client)
        for change in ({"plate_text": "ABCDEFGHJ"}, {"plate_state": "Cal"}, {"color": "x" * 41},
                       {"decision": "maybe"}, {"notes": "not editable"}):
            assert review(client, batch_id, capture, **change).status_code == 422, change


def test_only_the_creator_reviews_and_only_once_extraction_is_done(make_client):
    with make_client(FakeExtractor(extraction())) as client:
        batch_id, _ = queued_batch(client)
        client.app.state.worker.run_once()  # draft done, batch still processing
        _, capture = extracted_capture(client, batch_id)
        assert capture["draft"]["status"] == "done" and get(client, batch_id)["status"] == "processing"
        assert review(client, batch_id, capture, decision="skip").status_code == 409

        drain(client)
        _, capture = extracted_capture(client, batch_id)
        assert review(client, batch_id, capture, headers=BOB, decision="skip").status_code == 403
        assert client.post(f"/api/batches/{batch_id}/retry?rerun_all=true", headers=BOB).status_code == 403


def test_rerun_keeps_edits_but_clears_decisions(make_client):
    with make_client(FakeExtractor(extraction(), extraction(plate_confidence="low"))) as client:
        batch_id, capture = extracted_capture(client)
        reported = review(client, batch_id, capture, address="102 Example St", decision="report").json()

        rerun = client.post(f"/api/batches/{batch_id}/retry?rerun_all=true", headers=ALICE).json()
        assert rerun["captures"][0]["draft"]["review"]["decision"] is None
        assert review(client, batch_id, reported, decision="skip").status_code == 409  # re-running
        drain(client)
        batch = get(client, batch_id)

    after = batch["captures"][0]["draft"]["review"]
    assert after["edits"] == {"address": "102 Example St"}
    assert after["decision"] is None and after["plate_checked"] is False
    assert after["plate_needs_check"] is True  # the new run said low
    assert after["version"] > reported["draft"]["review"]["version"]
    assert batch["review"] == {"report": 0, "skip": 0, "undecided": 1}


def test_older_database_gains_review_columns(tmp_path):
    path = tmp_path / "ticketer.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)  # the tables as version 2 created them
    conn.execute("INSERT INTO batches VALUES ('b', 'alice@example.com', NULL, 't', 'ready', NULL)")
    conn.execute("INSERT INTO captures (id, batch_id, photo_path, content_type, bytes, sha256, width, height,"
                 " captured_at, received_at) VALUES ('c', 'b', 'p.jpg', 'image/jpeg', 1, 'x', 1, 1, 't', 't')")
    conn.execute("INSERT INTO drafts (capture_id, batch_id, status, created_at) VALUES ('c', 'b', 'done', 't')")
    conn.commit()
    conn.close()

    db = Database(path)
    db.init()
    db.init()  # every restart runs it again
    with db.connect() as conn:
        row = conn.execute("SELECT version, decision, edits, plate_checked FROM drafts").fetchone()
        assert tuple(row) == (1, None, None, 0)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
