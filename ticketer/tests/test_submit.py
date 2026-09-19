"""Submission tests. A fake 311 service stands in for the portal: no test ever reaches the city."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.sac311 import PreparedCase, Sac311Error, SubmissionResult, SubmissionUncertain, describe
from app.sac311 import Reporter, VehicleReport
from app.submit import Submitter, postal_from
from app.worker import Pipeline
from test_api import ALICE, BOB, new_batch
from test_extraction import FakeExtractor, FakePlates, FakeGeocoder, drain, extraction, get, queued_batch


class FakePortal:
    """Records what it was asked to do. `prepare` is a read; `submit` is the only write."""

    def __init__(self, prepare_error=None, submit_error=None):
        self.prepare_error = prepare_error
        self.submit_error = submit_error
        self.prepared: list[VehicleReport] = []
        self.submitted: list[tuple[PreparedCase, tuple | None]] = []
        self.case_seq = 0

    def prepare(self, report, reporter):
        if self.prepare_error:
            raise self.prepare_error
        self.prepared.append(report)
        return PreparedCase(
            case_record={"Address__c": report.address,
                         "Case_Question__r": {"totalSize": 5, "done": True, "records": [
                             {"Question__c": "License Plate Number", "Answer__c": report.plate},
                             {"Question__c": "Vehicle Color", "Answer__c": report.color},
                             {"Question__c": "Vehicle Make", "Answer__c": report.make},
                             {"Question__c": "Vehicle Model", "Answer__c": report.model}]},
                         "Anonymous_Contact__c": reporter.anonymous},
            summary=describe(report), matched_address=report.address,
            lat=38.5, lon=-121.5, council_district="4", warnings=["inferred payload"])

    def submit(self, prepared, photo=None):
        if self.submit_error:
            raise self.submit_error
        self.submitted.append((prepared, photo))
        self.case_seq += 1
        return SubmissionResult(case_number=f"CASE-{self.case_seq:04d}", case_id="500x", raw={})

    def check_contract(self):
        return []


@pytest.fixture
def make_client(tmp_path):
    def make(portal=None, dry_run=True, reporter_email="owner@example.com"):
        settings = Settings(data_dir=tmp_path, run_worker=False, submit_dry_run=dry_run,
                            reporter_first_name="Pat", reporter_last_name="Resident",
                            reporter_email=reporter_email)
        pipeline = Pipeline(extractor=FakeExtractor(*[extraction() for _ in range(5)]),
                            plate_reader=FakePlates(), geocoder=FakeGeocoder())
        app = create_app(settings, pipeline=pipeline, sac311=portal or FakePortal())
        return TestClient(app)
    return make


def ready_draft(client, headers=ALICE):
    """A batch extracted and marked Report, ready to submit."""
    batch_id, _ = queued_batch(client)
    drain(client)
    capture = get(client, batch_id)["captures"][0]
    r = client.patch(f"/api/batches/{batch_id}/captures/{capture['id']}/draft",
                     json={"version": capture["draft"]["review"]["version"], "plate_checked": True,
                           "decision": "report"}, headers=headers)
    assert r.status_code == 200, r.text
    return batch_id, r.json()


def send(client, batch_id, capture, dry_run=True, headers=ALICE, version=None):
    return client.post(f"/api/batches/{batch_id}/submit", headers=headers, json={
        "dry_run": dry_run,
        "drafts": [{"capture_id": capture["id"],
                    "version": version if version is not None else capture["draft"]["review"]["version"]}]})


def run_submitter(client) -> None:
    while client.app.state.submitter.run_once():
        pass


def submission_of(client, batch_id):
    return get(client, batch_id)["captures"][0]["submission"]


# ---- dry run ----

def test_dry_run_assembles_the_payload_without_sending_anything(make_client):
    portal = FakePortal()
    with make_client(portal) as client:
        batch_id, capture = ready_draft(client)
        assert send(client, batch_id, capture).status_code == 200
        run_submitter(client)
        submission = submission_of(client, batch_id)

    assert submission["status"] == "prepared"
    assert submission["dry_run"] is True
    assert "8ABC123" in submission["description"]
    assert submission["warnings"] == ["inferred payload"]
    assert submission["case_number"] is None
    assert portal.prepared and not portal.submitted  # read yes, write no


def test_a_dry_run_server_downgrades_a_real_request(make_client):
    """Asking for a real send against a dry-run server still only prepares."""
    portal = FakePortal()
    with make_client(portal, dry_run=True) as client:
        batch_id, capture = ready_draft(client)
        r = send(client, batch_id, capture, dry_run=False)
        run_submitter(client)
        assert r.json()["dry_run"] is True
        assert submission_of(client, batch_id)["status"] == "prepared"
    assert not portal.submitted


# ---- real submission ----

def test_real_submission_records_the_case_number_and_attaches_the_photo(make_client):
    portal = FakePortal()
    with make_client(portal, dry_run=False) as client:
        batch_id, capture = ready_draft(client)
        assert send(client, batch_id, capture, dry_run=False).json()["dry_run"] is False
        run_submitter(client)
        submission = submission_of(client, batch_id)

    assert submission["status"] == "sent" and submission["case_number"] == "CASE-0001"
    assert submission["photo_attached"] is True
    prepared, photo = portal.submitted[0]
    assert photo is not None and photo[0].endswith(".jpg") and len(photo[1]) > 0
    assert prepared.case_record["Anonymous_Contact__c"] is False


def test_the_report_carries_the_reviewed_values_not_the_extracted_ones(make_client):
    portal = FakePortal()
    with make_client(portal, dry_run=False) as client:
        batch_id, capture = ready_draft(client)
        edited = client.patch(f"/api/batches/{batch_id}/captures/{capture['id']}/draft",
                              json={"version": capture["draft"]["review"]["version"],
                                    "make": "Lexus", "address": "1200 Example St"}, headers=ALICE).json()
        send(client, batch_id, edited, dry_run=False)
        run_submitter(client)

    report = portal.prepared[0]
    assert (report.make, report.address) == ("Lexus", "1200 Example St")
    assert report.model == "Camry"  # untouched fields still come from extraction


def test_anonymous_when_no_reporter_is_configured(make_client):
    portal = FakePortal()
    with make_client(portal, dry_run=False, reporter_email=None) as client:
        # Settings with no name either: build one directly.
        client.app.state.submitter.config.reporter = Reporter()
        batch_id, capture = ready_draft(client)
        send(client, batch_id, capture, dry_run=False)
        run_submitter(client)
    assert portal.submitted[0][0].case_record["Anonymous_Contact__c"] is True


# ---- approval and safety ----

def test_submitting_needs_report_the_right_version_and_the_owner(make_client):
    with make_client() as client:
        batch_id, capture = ready_draft(client)
        assert send(client, batch_id, capture, headers=BOB).status_code == 403
        assert send(client, batch_id, capture, version=capture["draft"]["review"]["version"] + 5).status_code == 409

        other_batch, other = ready_draft(client)
        undecided = client.patch(f"/api/batches/{other_batch}/captures/{other['id']}/draft",
                                 json={"version": other["draft"]["review"]["version"], "decision": None},
                                 headers=ALICE).json()
        assert send(client, other_batch, undecided).status_code == 409  # not marked Report

        assert client.post(f"/api/batches/{batch_id}/submit", json={"drafts": []},
                           headers=ALICE).status_code == 422


def test_a_sent_draft_is_not_sent_again(make_client):
    portal = FakePortal()
    with make_client(portal, dry_run=False) as client:
        batch_id, capture = ready_draft(client)
        send(client, batch_id, capture, dry_run=False)
        run_submitter(client)
        again = get(client, batch_id)["captures"][0]
        r = send(client, batch_id, again, dry_run=False, version=again["draft"]["review"]["version"])
        assert r.status_code == 409 and "already sent" in r.json()["detail"]
        # A dry run over the same draft is still fine: it sends nothing.
        assert send(client, batch_id, again, version=again["draft"]["review"]["version"]).status_code == 200
    assert len(portal.submitted) == 1


def test_an_uncertain_result_is_never_retried(make_client):
    portal = FakePortal(submit_error=SubmissionUncertain("no confirmation"))
    with make_client(portal, dry_run=False) as client:
        batch_id, capture = ready_draft(client)
        send(client, batch_id, capture, dry_run=False)
        run_submitter(client)
        submission = submission_of(client, batch_id)
        assert submission["status"] == "unknown"
        assert client.app.state.submitter.run_once() is False  # nothing requeued

        again = get(client, batch_id)["captures"][0]
        r = send(client, batch_id, again, dry_run=False, version=again["draft"]["review"]["version"])
        assert r.status_code == 409  # unknown counts as live: the owner checks open data first


def test_a_failed_submission_can_be_sent_again(make_client):
    portal = FakePortal(submit_error=Sac311Error("the portal rejected the payload"))
    with make_client(portal, dry_run=False) as client:
        batch_id, capture = ready_draft(client)
        send(client, batch_id, capture, dry_run=False)
        run_submitter(client)
        assert submission_of(client, batch_id)["status"] == "failed"

        portal.submit_error = None
        again = get(client, batch_id)["captures"][0]
        assert send(client, batch_id, again, dry_run=False,
                    version=again["draft"]["review"]["version"]).status_code == 200
        run_submitter(client)
        assert submission_of(client, batch_id)["status"] == "sent"


def test_a_batch_that_went_to_311_cannot_be_deleted(make_client):
    with make_client(FakePortal(), dry_run=False) as client:
        batch_id, capture = ready_draft(client)
        send(client, batch_id, capture, dry_run=False)
        run_submitter(client)
        r = client.delete(f"/api/batches/{batch_id}", headers=ALICE)
        assert r.status_code == 409 and "record of what was sent" in r.json()["detail"]


def test_a_dry_run_does_not_block_deleting_a_batch(make_client):
    with make_client(FakePortal()) as client:
        batch_id, capture = ready_draft(client)
        send(client, batch_id, capture)
        run_submitter(client)
        assert client.delete(f"/api/batches/{batch_id}", headers=ALICE).status_code == 204


def test_editing_a_draft_after_submitting_marks_the_submission_stale(make_client):
    with make_client(FakePortal(), dry_run=False) as client:
        batch_id, capture = ready_draft(client)
        send(client, batch_id, capture, dry_run=False)
        run_submitter(client)
        assert submission_of(client, batch_id)["stale"] is False
        current = get(client, batch_id)["captures"][0]
        client.patch(f"/api/batches/{batch_id}/captures/{capture['id']}/draft",
                     json={"version": current["draft"]["review"]["version"], "color": "silver"}, headers=ALICE)
        assert submission_of(client, batch_id)["stale"] is True


def test_a_draft_missing_required_fields_fails_before_any_call(make_client):
    portal = FakePortal()
    with make_client(portal, dry_run=False) as client:
        batch_id, capture = ready_draft(client)
        # Clear a required field straight in the database: the API would refuse it.
        with client.app.state.db.connect() as conn:
            conn.execute("UPDATE drafts SET color = NULL, edits = NULL WHERE capture_id = ?", (capture["id"],))
        send(client, batch_id, capture, dry_run=False)
        run_submitter(client)
        assert "missing fields" in submission_of(client, batch_id)["error"]
    assert not portal.prepared and not portal.submitted


def test_postal_from():
    assert postal_from("1200 Example St, Sacramento, California, 95814") == "95814"
    assert postal_from("1200 Example St") is None
    assert postal_from(None) is None
