"""311 client tests: parsing the portal's tokens, and assembling a case. No network.

The payload shape asserted here was taken from a real submission captured from the portal on
2026-09-19 and diffed field by field. All values below are invented.
"""

import json

import pytest

from app import sac311
from app.sac311 import (ANSWER_FOR_QUESTION, CONCERN_LABEL, CONCERN_QUESTION_ID,
                        GUEST_FILE_PARENT_ID, SERVICE_TYPE_ID, SUB_SERVICE_TYPE_ID, PreparedCase,
                        Reporter, Sac311Error, Sac311Portal, VehicleReport, describe, gis_records,
                        local_time, multipart, parse_aura_context, parse_remoting,
                        question_records, unhijack)

HOMEPAGE = (
    '<html><script src="/s/sfsites/l/%7B%22mode%22%3A%22PROD%22%2C%22app%22%3A%22siteforce%3Acommunity'
    'App%22%2C%22fwuid%22%3A%22FW123%22%2C%22loaded%22%3A%7B%22APPLICATION%40markup%3A%2F%2Fsiteforce%3A'
    'communityApp%22%3A%22APP456%22%7D%7D/inline.js"></script></html>'
)
MAP_PAGE = (
    'var x = Visualforce.remoting.Manager.add(new $VFRM.RemotingProviderImpl({"vf":{"vid":"066xyz"},'
    '"actions":{"Sac311_ArcGisMapCtrl":{"ms":[{"name":"validateAddress","len":1,"ns":"","ver":46.0,'
    '"csrf":"CSRF1","authorization":"AUTH1"}]}},"service":"apexremote"}));'
)

MATCH = {
    "hasAddress": True, "isSystemUp": True, "X": "-121.491234567890123", "Y": "38.581234567890123",
    "address": {"address": "1200 EXAMPLE ST, SACRAMENTO 95814",
                "attributes": {"Loc_name": "ADDRESS_UNIT_C", "User_fld": "928399",
                               "X": "6700000.1234560000", "Y": "1970000.123456"}},
    "attributes": [
        {"fieldNameInService": "DTPR_FLAG", "layerId": "0", "label": "Downtown Parking Restricted"},
        {"fieldNameInService": "DISTNUM", "layerId": "8", "label": "Council District", "value": "4"},
        {"fieldNameInService": "MAINTSUP", "layerId": "25", "label": "Park Supervisor", "value": ""},
    ],
}
PARCEL = [
    {"fieldNameInService": "APN", "layerId": "37", "label": "APN", "value": "01234567890000"},
    {"fieldNameInService": "NAME", "layerId": "37", "label": "Owner Name", "value": "EXAMPLE OWNER"},
    # The portal drops the parcel layer's padded duplicates of routes other layers already carry.
    {"fieldNameInService": "ROUTE", "layerId": "37", "label": "Recycle Route", "value": "9-999Z   "},
    # ...but forwards the street-segment layers whole.
    {"fieldNameInService": "C1STRNAME", "layerId": "23", "label": "Cross Street", "value": "EXAMPLE WAY"},
]
QUESTIONS = [
    {"serviceTypeQuestion": {"Id": CONCERN_QUESTION_ID, "Sequence__c": 1},
     "caseTypeQuestion": {"Question__c": "Please select a concern",
                          "Portal_Question_Label__c": "Please select a concern and provide a detailed summary"
                                                      " in the additional notes section.",
                          "Available_for_Portal__c": True},
     "optionById": {"a0U1U000003OkQFUA0": CONCERN_LABEL}},
    {"serviceTypeQuestion": {"Id": "q3", "Sequence__c": 3},
     "caseTypeQuestion": {"Question__c": "Vehicle Make", "Portal_Question_Label__c": "Vehicle Make",
                          "Available_for_Portal__c": True}},
    {"serviceTypeQuestion": {"Id": "q2", "Sequence__c": 2},
     "caseTypeQuestion": {"Question__c": "Vehicle Color", "Portal_Question_Label__c": "Vehicle Color",
                          "Available_for_Portal__c": True}},
    {"serviceTypeQuestion": {"Id": "q4", "Sequence__c": 4},
     "caseTypeQuestion": {"Question__c": "Vehicle Model", "Portal_Question_Label__c": "Vehicle Model",
                          "Integration_Type__c": "CSERVE", "Available_for_Portal__c": True}},
    {"serviceTypeQuestion": {"Id": "q5", "Sequence__c": 5},
     "caseTypeQuestion": {"Question__c": "License Plate Number",
                          "Portal_Question_Label__c": "License Plate Number",
                          "Integration_Type__c": "CSERVE", "Available_for_Portal__c": True}},
]
REPORT = VehicleReport(plate="1TST234", plate_state="CA", color="Grey", make="Honda", model="Civic",
                       address="1200 Example St", postal="95814", lat=38.5812, lon=-121.4912,
                       seen_at="2026-09-17T18:23:00+00:00")


class StubPortal(Sac311Portal):
    """A portal whose transport is replaced; nothing leaves the process."""

    def __init__(self, validate=MATCH, parcel=None, questions=None):
        super().__init__()
        self._validate = validate
        self._parcel = PARCEL if parcel is None else parcel
        self._questions = QUESTIONS if questions is None else questions
        self.remote_calls = []

    def remote(self, method, *args):
        self.remote_calls.append((method, args))
        return self._validate if method == "validateAddress" else {"attributes": self._parcel}

    def questions(self):
        return self._questions


# ---- parsing ----

def test_parse_aura_context():
    ctx = parse_aura_context(HOMEPAGE)
    assert ctx["fwuid"] == "FW123"
    assert ctx["loaded"]["APPLICATION@markup://siteforce:communityApp"] == "APP456"


def test_parse_aura_context_complains_when_the_portal_changes():
    with pytest.raises(Sac311Error, match="no longer carries an Aura context"):
        parse_aura_context("<html>nothing here</html>")


def test_parse_remoting():
    tokens = parse_remoting(MAP_PAGE)
    assert tokens["vid"] == "066xyz"
    assert tokens["methods"]["validateAddress"]["csrf"] == "CSRF1"


# ---- building the blocks ----

def test_gis_records_match_the_portal_s_shape():
    records = gis_records(MATCH["attributes"])
    # A layer with no value keeps its row and simply drops Value__c...
    assert records[2] == {"Label__c": "Park Supervisor", "Field_Name_in_Service__c": "MAINTSUP",
                          "Layer_Id__c": "25"}
    # ...except the downtown parking flag, which the portal sends as "No" when absent.
    assert records[0]["Value__c"] == "No"
    assert records[1] == {"Label__c": "Council District", "Value__c": "4",
                          "Field_Name_in_Service__c": "DISTNUM", "Layer_Id__c": "8"}


def test_question_records_follow_the_form_s_own_order_and_shape():
    answers = {"concern": CONCERN_LABEL, "color": "Grey", "make": "Honda", "model": "Civic",
               "plate": "1TST234"}
    records = question_records(QUESTIONS, answers)
    assert [r["Question__c"] for r in records] == [
        "Please select a concern", "Vehicle Color", "Vehicle Make", "Vehicle Model", "License Plate Number"]
    assert records[4] == {"Question__c": "License Plate Number",
                          "Portal_Question_Label__c": "License Plate Number",
                          "Integration_Type__c": "CSERVE", "Available_for_Portal__c": True,
                          "Answer__c": "1TST234"}


def test_question_records_skip_questions_the_app_cannot_answer():
    extra = QUESTIONS + [{"serviceTypeQuestion": {"Id": "q9", "Sequence__c": 9},
                          "caseTypeQuestion": {"Question__c": "Something New"}}]
    records = question_records(extra, {k: "x" for k in ANSWER_FOR_QUESTION.values()})
    assert "Something New" not in {r["Question__c"] for r in records}


# ---- address validation ----

def test_validate_address_sends_the_street_as_its_own_field():
    portal = StubPortal()
    portal.validate_address("1200 Example St", "Sacramento", "95814")
    method, (params,) = portal.remote_calls[0]
    assert method == "validateAddress"
    assert params["street"] == "1200 Example St"  # the combined string alone matches nothing
    assert params["address"] == "1200 Example St, Sacramento, 95814"


@pytest.mark.parametrize("bad", [
    # The portal answers hasAddress=True for an address that doesn't exist or is outside the
    # city; the real signal is coordinates plus a geocoder name.
    {"hasAddress": True, "isSystemUp": True, "address": {"attributes": {"Loc_name": ""}}},
    {"hasAddress": True, "isSystemUp": True, "X": "", "address": {"attributes": {"Loc_name": "X"}}},
])
def test_validate_address_rejects_a_match_with_no_coordinates(bad):
    with pytest.raises(Sac311Error, match="can't place"):
        StubPortal(bad).validate_address("99999 Nowhere Ave")


def test_validate_address_reports_the_gis_system_being_down_as_retryable():
    with pytest.raises(Sac311Error) as caught:
        StubPortal({"isSystemUp": False}).validate_address("1200 Example St")
    assert caught.value.retryable


def test_parcel_info_keeps_street_layers_but_trims_the_parcel_layer():
    fields = [(a["layerId"], a["fieldNameInService"]) for a in StubPortal().parcel_info("928399")]
    assert ("37", "APN") in fields and ("37", "NAME") in fields
    assert ("23", "C1STRNAME") in fields          # street layers pass through whole
    assert ("37", "ROUTE") not in fields          # padded duplicate the portal drops


# ---- the assembled case ----

def test_prepare_matches_the_captured_payload_shape():
    case = StubPortal().prepare(REPORT, Reporter(first_name="Pat", last_name="Resident",
                                                 phone="9165550100", email="pat@example.com")).case_record
    assert case["Service_Type__c"] == SERVICE_TYPE_ID
    assert case["Sub_Service_Type__r"] == {"Id": SUB_SERVICE_TYPE_ID, "Name": "Enforcement Request"}
    assert case["Address__c"] == "1200 EXAMPLE ST, SACRAMENTO 95814"  # the city's spelling, not ours
    # The map's own strings, which carry a digit more than a float round-trips.
    assert case["Address_Geolocation__Latitude__s"] == "38.581234567890123"
    assert case["Address_Geolocation__Longitude__s"] == "-121.491234567890123"
    assert (case["Pin_Drop_Location__Latitude__s"], case["Pin_Drop_Location__Longitude__s"]) == (38.5812, -121.4912)
    assert (case["Address_X__c"], case["Address_Y__c"]) == ("6700000.1234560000", "1970000.123456")
    assert case["Council_District__c"] == "4"
    assert case["Case_Question__r"]["totalSize"] == 5
    assert case["CaseGISFields__r"]["totalSize"] == len(case["CaseGISFields__r"]["records"])
    assert case["Contact"] == {"FirstName": "Pat", "LastName": "Resident",
                               "Phone": "9165550100", "Email": "pat@example.com"}
    # The portal omits this key entirely when contact details are given.
    assert "Anonymous_Contact__c" not in case
    # The vehicle goes in the structured answers; the portal sends no Description at all.
    assert "Description" not in case
    assert "1TST234" in json.dumps(case["Case_Question__r"])


def test_prepare_without_contact_details_files_anonymously():
    prepared = StubPortal().prepare(REPORT, Reporter())
    assert prepared.case_record["Anonymous_Contact__c"] is True
    assert "Contact" not in prepared.case_record
    warning = next(w for w in prepared.warnings if "anonymously" in w)
    # The setting is in the add-on's options, which the phone can't reach: say where it is.
    assert "reporter_first_name" in warning and "Configuration" in warning


def test_prepare_fails_when_the_form_stops_asking_for_the_vehicle():
    only_concern = [q for q in QUESTIONS if q["caseTypeQuestion"]["Question__c"].startswith("Please")]
    with pytest.raises(Sac311Error, match="no longer asks for"):
        StubPortal(questions=only_concern).prepare(REPORT, Reporter())


def test_prepare_warns_when_the_match_is_far_from_the_photo():
    away = VehicleReport(**{**REPORT.__dict__, "lat": 38.9, "lon": -121.9})
    assert any("far from where the photo" in w for w in StubPortal().prepare(away, Reporter()).warnings)


def test_redacted_summarises_the_gis_rows():
    # Those rows carry city staff names and the property owner's name; keep them off the phone.
    prepared = StubPortal().prepare(REPORT, Reporter())
    assert prepared.redacted()["CaseGISFields__r"].endswith("GIS rows>")
    assert "EXAMPLE OWNER" not in json.dumps(prepared.redacted())


# ---- the owner-facing summary (not sent to 311) ----

def test_describe_summarises_for_the_owner():
    text = describe(REPORT)
    assert "Grey Honda Civic" in text and "1TST234" in text and "(CA)" in text
    assert "Seen at 11:23 AM on Thursday 17 September 2026." in text


@pytest.mark.parametrize("iso, expected", [
    ("2026-09-18T02:13:50+00:00", "7:13 PM on Thursday 17 September 2026"),  # previous day in Sacramento
    ("2026-01-05T20:00:00+00:00", "12:00 PM on Monday 5 January 2026"),      # standard time
    ("not a timestamp", "not a timestamp"),                                  # kept rather than dropped
])
def test_local_time(iso, expected):
    assert local_time(iso) == expected


# ---- attaching the photo ----
#
# The chain below (uploader params -> multipart POST -> saveContentVersion) and every literal in
# it come from a submission captured from the portal on 2026-09-19. The ids are invented.

UPLOADER_PARAMS = {"decoupledFileUploaderUrl": "/chatter/handlers/file/body",
                   "fileUploaderUrl": "/chatter/handlers/chatterfile",
                   "isDecoupledMode": "true", "isFileUploadChunkingEnabled": "true"}
CONTENT_BODY = b'while(1);\n{"content_body_id":"05TTEST00001DDDAA"}'


class UploadPortal(StubPortal):
    """A portal that records the Aura actions an upload makes and answers them as the portal does."""

    def __init__(self, params=UPLOADER_PARAMS, saved=None):
        super().__init__()
        self._params = params
        self._saved = {"docid": "069TEST00001AAABB", "versionId": "068TEST00001BBBCC",
                       "isGuestUserFileUpload": "true"} if saved is None else saved
        self.aura_calls = []

    def aura(self, descriptor, params):
        self.aura_calls.append((descriptor.rsplit("$", 1)[-1], params))
        if descriptor.endswith("getFileUploaderParams"):
            return self._params
        if descriptor.endswith("saveContentVersion"):
            return self._saved
        if descriptor.endswith("saveService"):
            return {"CaseRecord": {"CaseNumber": "260101-1234567", "Id": "500TEST00001CCCDD"}}
        raise AssertionError(f"unexpected action {descriptor}")


@pytest.fixture
def uploads(monkeypatch):
    """Capture the one non-Aura POST an upload makes, and answer it the way the portal does."""
    calls = []

    def fake_send(session, url, data, headers, timeout):
        calls.append({"url": url, "body": data, "headers": headers})
        return CONTENT_BODY

    monkeypatch.setattr(sac311, "_send", fake_send)
    return calls


def test_unhijack_strips_the_guard_salesforce_prefixes_its_json_with():
    assert unhijack(CONTENT_BODY) == {"content_body_id": "05TTEST00001DDDAA"}
    assert unhijack(b'{"plain":1}') == {"plain": 1}


def test_unhijack_reports_an_unreadable_answer():
    with pytest.raises(Sac311Error, match="can't read"):
        unhijack(b"<html>login</html>")


def test_multipart_keeps_the_portal_s_own_field_order():
    body, boundary = multipart({"token": "undefined"}, ("file", "a.jpg", "image/jpeg", b"\xff\xd8"),
                               {"target": "ContentVersion"})
    assert body.index(b"token") < body.index(b"filename") < body.index(b"target")
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_multipart_neutralises_a_filename_that_would_split_the_body():
    body, _ = multipart({}, ("file", 'ev"il\r\nX: y.jpg', "image/jpeg", b"x"), {})
    assert b'filename="ev_il__X: y.jpg"' in body


def test_upload_photo_walks_the_captured_three_step_chain(uploads):
    portal = UploadPortal()
    assert portal.upload_photo("shot.jpg", b"\xff\xd8jpeg-bytes") == "069TEST00001AAABB"
    assert [name for name, _ in portal.aura_calls] == ["getFileUploaderParams", "saveContentVersion"]

    post = uploads[0]
    assert post["url"] == "https://311.cityofsacramento.org/chatter/handlers/file/body"
    assert post["headers"]["Content-Type"].startswith("multipart/form-data; boundary=----ticketer")
    assert b'name="token"\r\n\r\nundefined' in post["body"]    # a guest is issued no CSRF token
    assert b'name="fromUITier"\r\n\r\ntrue' in post["body"]
    assert b'name="target"\r\n\r\nContentVersion' in post["body"]
    assert b'filename="shot.jpg"' in post["body"] and b"Content-Type: image/jpeg" in post["body"]
    assert b"\xff\xd8jpeg-bytes" in post["body"]

    assert portal.aura_calls[1][1] == {"contentBodyId": "05TTEST00001DDDAA",
                                       "firstPublishLocationId": GUEST_FILE_PARENT_ID,
                                       "pathOnClient": "shot.jpg", "title": "shot"}


def test_upload_photo_stops_before_sending_if_the_portal_leaves_decoupled_mode(uploads):
    portal = UploadPortal(params={"isDecoupledMode": "false"})
    with pytest.raises(Sac311Error, match="changed how it accepts file uploads"):
        portal.upload_photo("shot.jpg", b"x")
    assert uploads == []          # the bytes never left


def test_upload_photo_complains_when_no_content_body_comes_back(monkeypatch):
    monkeypatch.setattr(sac311, "_send", lambda *a, **k: b'while(1);\n{}')
    with pytest.raises(Sac311Error, match="named no content body"):
        UploadPortal().upload_photo("shot.jpg", b"x")


def test_upload_photo_complains_when_no_document_comes_back(uploads):
    with pytest.raises(Sac311Error, match="named no document"):
        UploadPortal(saved={"versionId": "068TEST00001BBBCC"}).upload_photo("shot.jpg", b"x")


def test_submit_sends_the_uploaded_document_id_as_file_ids(uploads):
    portal = UploadPortal()
    prepared = PreparedCase(case_record={"Address__c": "1200 EXAMPLE ST, SACRAMENTO 95814"},
                            summary="summary", matched_address="1200 EXAMPLE ST, SACRAMENTO 95814",
                            lat=38.58, lon=-121.49, council_district="4")
    result = portal.submit(prepared, photo=("shot.jpg", b"\xff\xd8"))

    assert [name for name, _ in portal.aura_calls][-1] == "saveService"
    assert portal.aura_calls[-1][1]["fileIds"] == '["069TEST00001AAABB"]'
    assert result.case_number == "260101-1234567"


def test_submit_without_a_photo_sends_an_empty_file_list(uploads):
    portal = UploadPortal()
    prepared = PreparedCase(case_record={}, summary="s", matched_address="a", lat=0.0, lon=0.0,
                            council_district=None)
    portal.submit(prepared, photo=None)
    assert portal.aura_calls[-1][1]["fileIds"] == "[]"
    assert uploads == []
