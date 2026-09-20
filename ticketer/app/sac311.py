"""Client for the City of Sacramento 311 portal.

The portal (https://311.cityofsacramento.org/s/) is a Salesforce Experience Cloud site. Its
request form calls guest-accessible Apex methods over the standard Aura endpoint, and its
address map is a Visualforce page using JavaScript remoting. There is no documented API.

The payload shape here was taken from a real submission captured from the portal, not guessed:
the vehicle details are structured question answers, the GIS block is a list of records, and no
Description is sent at all.

Everything except `upload_photo` and `save_service` is a read. `prepare()` does all the reads and
assembles the payload; `submit()` is the only thing that writes, and the caller decides whether
to run it.
"""

import http.cookiejar
import json
import mimetypes
import re
import uuid
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from .tls import verified_context

# Every report is about a Sacramento street, so times are written in the city's own zone
# whatever the server is set to.
CITY_TZ = ZoneInfo("America/Los_Angeles")

PORTAL = "https://311.cityofsacramento.org"
AURA_PATH = "/s/sfsites/aura"
REMOTING_PATH = "/apexremote"
MAP_PAGE = "/apex/Sac311_Portal_ServiceAddressMap"
UPLOAD_PATH = "/chatter/handlers/file/body"   # only a fallback: the portal names it at run time
USER_AGENT = "Mozilla/5.0 (ticketer; Home Assistant app)"

# Catalog ids, confirmed against the live portal on 2026-09-19. `check_contract()` re-checks
# them, because a portal redeploy can change them without warning.
SERVICE_TYPE_ID = "a0W1U0000029WFqUAM"       # Parking
SUB_SERVICE_TYPE_ID = "a0W1U0000029WFtUAM"   # Enforcement Request
CONCERN_QUESTION_ID = "a0V1U00000Jbq1uUAB"   # "Please select a concern" (required picklist)
CONCERN_JUNCTION_ID = "a0U1U000003OkQFUA0"   # its "Parked without Permit" option
CONCERN_LABEL = "Parked without Permit"
GUEST_FILE_PARENT_ID = "a0S1U000004KY6vUAG"  # placeholder record guest uploads attach to
PUBLISHER_CONTROLLER = ("serviceComponent://ui.chatter.components.aura.components.forceChatter"
                        ".chatter.PublisherFileAttachmentController")

# getAdditionalInfo returns the parcel layer (37) plus street-segment layers. The portal
# forwards the street layers whole, but only these nine parcel fields; the rest are padded
# duplicates of routes other layers already carry. Taken from a captured submission.
# The downtown parking flag is a layer you either fall inside or you don't, so the map returns
# no value when you don't. The portal sends that absence as "No" rather than an empty row, and on
# a parking case it is worth matching.
FLAG_WHEN_ABSENT = {"DTPR_FLAG": "No"}

PARCEL_LAYER = "37"
PARCEL_FIELDS = ("FULLADDRESS", "APN", "NAME", "MAIL_ADDRE", "MAIL_CITY", "MAIL_STATE", "MAIL_ZIP",
                 "SITUSADDRESS", "GARBAGE_DAY")

# The enforcement form is five required questions, not one. The vehicle details are structured
# answers, not free text: a captured submission carries no Description at all. Questions are
# answered by name, because the ids move between portal releases but the labels have not.
CONCERN_QUESTION_LABEL = "Please select a concern"
ANSWER_FOR_QUESTION = {
    CONCERN_QUESTION_LABEL: "concern",
    "Vehicle Color": "color",
    "Vehicle Make": "make",
    "Vehicle Model": "model",
    "License Plate Number": "plate",
}

DEFAULT_TIMEOUT = 30.0
MAX_SUMMARY = 32000


class Sac311Error(Exception):
    """Something went wrong talking to the portal. `retryable` is False when a retry would only
    repeat the same failure (a rejected payload), rather than a transient network problem."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class SubmissionUncertain(Sac311Error):
    """The write may or may not have created a case. Never retry on this: check the city's open
    data first, or the owner ends up with two officers dispatched for one car."""


@dataclass(frozen=True)
class Reporter:
    """Who the request is filed as. The portal accepts a guest submission with no contact
    details at all (`Anonymous_Contact__c`), or with a name, email and phone sent inline."""

    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None

    @property
    def anonymous(self) -> bool:
        return not any((self.first_name, self.last_name, self.email, self.phone))

    def contact(self) -> dict | None:
        if self.anonymous:
            return None
        return {k: v for k, v in (("FirstName", self.first_name), ("LastName", self.last_name),
                                  ("Phone", self.phone), ("Email", self.email)) if v}


@dataclass(frozen=True)
class VehicleReport:
    """One reviewed draft, in the terms the 311 form asks for."""

    plate: str
    plate_state: str | None
    color: str
    make: str
    model: str
    address: str          # street line only, e.g. "1200 Example St"
    city: str = "Sacramento"
    postal: str | None = None
    lat: float | None = None
    lon: float | None = None
    seen_at: str | None = None  # ISO 8601, when the photo was taken


@dataclass(frozen=True)
class PreparedCase:
    """Everything `submit()` would send, assembled and inspectable. Building one performs only
    reads, so a dry run can show the owner the exact payload without touching anything."""

    case_record: dict
    summary: str          # prose for the owner to check; the portal is sent structured answers
    matched_address: str
    lat: float
    lon: float
    council_district: str | None
    warnings: list[str] = field(default_factory=list)

    def redacted(self) -> dict:
        """The payload with the GIS block summarised instead of inlined. Those rows carry the
        names of city staff assigned to the block and the property owner's name and mailing
        address, none of which belongs on a phone screen."""
        shown = dict(self.case_record)
        gis = shown.get("CaseGISFields__r")
        if gis:
            shown["CaseGISFields__r"] = f"<{len(gis['records'])} GIS rows>"
        return shown


@dataclass(frozen=True)
class SubmissionResult:
    case_number: str
    case_id: str | None
    raw: dict


class Sac311Service(Protocol):
    def prepare(self, report: VehicleReport, reporter: Reporter) -> PreparedCase: ...

    def submit(self, prepared: PreparedCase, photo: tuple[str, bytes] | None) -> SubmissionResult: ...

    def check_contract(self) -> list[str]:
        """Read-only check that the hardcoded ids still exist. Returns what has drifted."""
        ...


def local_time(iso: str) -> str:
    """A timestamp an officer can read, in Sacramento time. Falls back to the raw string rather
    than dropping the information if it isn't a timestamp we recognise."""
    try:
        when = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso
    return when.astimezone(CITY_TZ).strftime("%-I:%M %p on %A %-d %B %Y")


def describe(report: VehicleReport) -> str:
    """A one-line summary for the owner to check before sending. This is NOT sent to 311: the
    form takes colour, make, model and plate as separate answers."""
    vehicle = " ".join(w for w in (report.color, report.make, report.model) if w)
    plate = f"plate {report.plate}" + (f" ({report.plate_state})" if report.plate_state else "")
    lines = [f"{vehicle}, {plate}, parked without a residential parking permit at {report.address}."]
    if report.seen_at:
        lines.append(f"Seen at {local_time(report.seen_at)}.")
    return " ".join(lines)[:MAX_SUMMARY]


def new_session() -> urllib.request.OpenerDirector:
    """An opener that keeps cookies, so one run looks like one visit to the portal.

    Nothing in the flow is known to depend on a cookie -- a captured guest upload carried only
    consent and analytics ones -- but the portal sets them, and sending them back is what a
    browser would do."""
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=verified_context()),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def _send(session, url: str, data: bytes | None, headers: dict, timeout: float) -> bytes:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with session.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as e:  # a 5xx is worth another go; a 4xx is our payload
        raise Sac311Error(f"Portal returned HTTP {e.code}", retryable=e.code >= 500) from e
    except (OSError, ValueError) as e:
        raise Sac311Error(f"Can't reach the portal: {e}", retryable=True) from e


JSON_HIJACK_GUARD = "while(1);"


def unhijack(raw: bytes) -> dict:
    """Decode a response Salesforce prefixes with `while(1);` to spoil cross-site script-tag theft."""
    text = raw.decode("utf-8", errors="replace").lstrip()
    if text.startswith(JSON_HIJACK_GUARD):
        text = text[len(JSON_HIJACK_GUARD):]
    try:
        return json.loads(text)
    except ValueError as e:
        raise Sac311Error(f"The portal's upload gave an answer we can't read: {e}") from e


def multipart(before: dict, file_part: tuple[str, str, str, bytes], after: dict) -> tuple[bytes, str]:
    """Encode a multipart/form-data body, keeping the portal's own field order.

    Written out by hand rather than with email.mime because the file part is raw bytes and the
    order of the fields around it is part of what was captured.
    """
    boundary = f"----ticketer{uuid.uuid4().hex}"
    name, filename, content_type, data = file_part
    safe = re.sub(r'[\r\n"\\]', "_", filename)    # a quote or newline here would split the body

    chunks: list[bytes] = []
    def field(key: str, value: str) -> None:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                      f"{value}\r\n".encode())

    for key, value in before.items():
        field(key, value)
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                  f'filename="{safe}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    chunks += [data, b"\r\n"]
    for key, value in after.items():
        field(key, value)
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def parse_aura_context(html: str) -> dict:
    """Pull the Aura framework id and app hash out of the homepage.

    They sit URL-encoded inside the `/s/sfsites/l/<json>/...js` script paths and change on every
    portal redeploy, so they are read per run rather than pinned.
    """
    for blob in re.findall(r"/s/sfsites/l/([^/]+)/", html):
        try:
            loaded = json.loads(urllib.parse.unquote(blob))
        except ValueError:
            continue
        if "fwuid" in loaded and "loaded" in loaded:
            return {"mode": "PROD", "fwuid": loaded["fwuid"], "app": "siteforce:communityApp",
                    "loaded": loaded["loaded"], "dn": [], "globals": {}, "uad": True}
    raise Sac311Error("The portal homepage no longer carries an Aura context; it may have been rebuilt")


def parse_remoting(html: str) -> dict:
    """Pull the Visualforce remoting descriptors out of the address-map page. Each method comes
    with its own CSRF token and signed authorization, both short-lived."""
    marker = "RemotingProviderImpl("
    if marker not in html:
        raise Sac311Error("The address map page no longer carries remoting descriptors")
    config, _ = json.JSONDecoder().raw_decode(html[html.index(marker) + len(marker):])
    methods = config.get("actions", {}).get("Sac311_ArcGisMapCtrl", {}).get("ms", [])
    return {"vid": config["vf"]["vid"], "methods": {m["name"]: m for m in methods}}


def gis_records(attributes: list[dict]) -> list[dict]:
    """Turn the map's attribute list into the CaseGISFields__r rows the portal sends.

    A captured submission keeps a row even when the layer had no value for it, dropping only the
    `Value__c` key, so that is what happens here too.
    """
    records = []
    for attribute in attributes:
        name = attribute.get("fieldNameInService")
        value = attribute.get("value")
        if value in (None, ""):
            value = FLAG_WHEN_ABSENT.get(name)
        record = {"Label__c": attribute.get("label")}
        if value not in (None, ""):
            record["Value__c"] = str(value)   # omitted entirely when the layer had nothing
        record |= {"Field_Name_in_Service__c": name, "Layer_Id__c": str(attribute.get("layerId"))}
        records.append(record)
    return records


def question_records(questions: list[dict], answers: dict[str, str]) -> list[dict]:
    """The Case_Question__r rows, in the form's own order. Each question's `caseTypeQuestion`
    object is exactly the shape the portal sends, so it is used verbatim with the answer added."""
    records = []
    for entry in sorted(questions, key=lambda q: (q.get("serviceTypeQuestion") or {}).get("Sequence__c") or 0):
        template = entry.get("caseTypeQuestion") or {}
        field_name = ANSWER_FOR_QUESTION.get(template.get("Question__c"))
        if field_name is None:
            continue  # a question this app doesn't know how to answer; check_contract reports it
        records.append(dict(template) | {"Answer__c": answers[field_name]})
    return records


class Sac311Portal:
    """Talks to the live portal. Construct one per submission run: the Aura context and the
    remoting tokens are fetched lazily and are only good for a short while."""

    def __init__(self, base_url: str = PORTAL, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._aura_context: dict | None = None
        self._remoting: dict | None = None
        self._action_id = 0
        self.session = new_session()

    # ---- transport ----

    def aura_context(self) -> dict:
        if self._aura_context is None:
            self._aura_context = parse_aura_context(
                _send(self.session, f"{self.base_url}/s/", None, {}, self.timeout).decode())
        return self._aura_context

    def remoting(self) -> dict:
        if self._remoting is None:
            self._remoting = parse_remoting(
                _send(self.session, f"{self.base_url}{MAP_PAGE}", None, {}, self.timeout).decode())
        return self._remoting

    def aura(self, descriptor: str, params: dict) -> dict:
        self._action_id += 1
        body = urllib.parse.urlencode({
            "message": json.dumps({"actions": [{"id": f"{self._action_id};a", "descriptor": descriptor,
                                                "callingDescriptor": "UNKNOWN", "params": params}]}),
            "aura.context": json.dumps(self.aura_context()),
            "aura.pageURI": "/s/new-service",
            "aura.token": "null",
        }).encode()
        payload = json.loads(_send(
            self.session, f"{self.base_url}{AURA_PATH}", body,
            {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}, self.timeout))
        actions = payload.get("actions") or []
        if not actions:
            raise Sac311Error(f"The portal gave no answer for {descriptor}", retryable=True)
        action = actions[0]
        if action.get("state") != "SUCCESS":
            errors = action.get("error") or []
            detail = "; ".join(str(e.get("message", e)) for e in errors) or action.get("state", "unknown")
            raise Sac311Error(f"{descriptor.rsplit('$', 1)[-1]} failed: {detail}")
        return action.get("returnValue")

    def remote(self, method: str, *args) -> dict:
        tokens = self.remoting()
        spec = tokens["methods"].get(method)
        if spec is None:
            raise Sac311Error(f"The address map no longer offers {method}")
        body = json.dumps({
            "action": "Sac311_ArcGisMapCtrl", "method": method, "data": list(args), "type": "rpc",
            "tid": self._action_id + 100,
            "ctx": {"csrf": spec["csrf"], "vid": tokens["vid"], "ns": spec["ns"],
                    "ver": int(spec["ver"]), "authorization": spec["authorization"]},
        }).encode()
        payload = json.loads(_send(
            self.session, f"{self.base_url}{REMOTING_PATH}", body,
            {"Content-Type": "application/json", "Referer": f"{self.base_url}{MAP_PAGE}"}, self.timeout))
        entry = payload[0] if isinstance(payload, list) and payload else {}
        if entry.get("statusCode") != 200:
            raise Sac311Error(f"{method} failed: {entry.get('message') or entry.get('statusCode')}")
        return entry.get("result") or {}

    # ---- reads ----

    def questions(self) -> list[dict]:
        payload = self.aura("apex://Sac311_Portal_ServiceQuestionsCtrl/ACTION$getQuestionsList",
                            {"serviceTypeId": SERVICE_TYPE_ID, "subServiceTypeId": SUB_SERVICE_TYPE_ID,
                             "caseId": None})
        return (payload or {}).get("records") or []

    def check_contract(self) -> list[str]:
        """Confirm the ids and questions this module relies on still exist in the portal's own
        catalog. The interface is undocumented, so a redeploy can move them; this is how we find
        out before a submission does."""
        drift = []
        catalog = self.aura("apex://Sac311_Portal_ServicesCtrl/ACTION$GetServiceDetails", {})
        services = {s["Id"]: s for s in (catalog or {}).get("ServiceTypes", [])}
        parking = services.get(SERVICE_TYPE_ID)
        if parking is None:
            drift.append(f"Parking service type {SERVICE_TYPE_ID} is gone from the catalog")
        elif not any(s["Id"] == SUB_SERVICE_TYPE_ID for s in parking.get("Sac311_Service_Types__r", [])):
            drift.append(f"Enforcement Request sub-type {SUB_SERVICE_TYPE_ID} is gone from Parking")

        records = self.questions()
        asked = {(r.get("caseTypeQuestion") or {}).get("Question__c") for r in records}
        for label in ANSWER_FOR_QUESTION:
            if label not in asked:
                drift.append(f"The form no longer asks {label!r}")
        concern = next((r for r in records
                        if (r.get("serviceTypeQuestion") or {}).get("Id") == CONCERN_QUESTION_ID), None)
        if concern is None:
            drift.append(f"Concern question {CONCERN_QUESTION_ID} is gone")
        elif concern.get("optionById", {}).get(CONCERN_JUNCTION_ID) != CONCERN_LABEL:
            drift.append(f"Option {CONCERN_JUNCTION_ID} is no longer {CONCERN_LABEL!r}")
        return drift

    def validate_address(self, street: str, city: str = "Sacramento", postal: str | None = None) -> dict:
        """Ask the city's own map whether the address is real and inside the city, and collect
        the GIS attributes the case form normally fills in from the map iframe.

        The street has to go in its own field: passing only the combined `address` string comes
        back `hasAddress: true` with every attribute blank. `hasAddress` is not the signal at
        all — an address outside the city, or one that doesn't exist, also returns true. A real
        match is one that came back with coordinates and a geocoder name.
        """
        combined = ", ".join(p for p in (street, city, postal) if p)
        result = self.remote("validateAddress",
                             {"street": street, "city": city or "", "zip": postal or "", "address": combined})
        if result.get("isSystemUp") is False:
            raise Sac311Error("The city's GIS system is down", retryable=True)
        matched = result.get("address") or {}
        if result.get("X") in (None, "") or not (matched.get("attributes") or {}).get("Loc_name"):
            raise Sac311Error(
                f"The city's map can't place {combined!r}. It has to be a real address inside Sacramento.")
        return result

    def parcel_info(self, user_fld: str) -> list[dict]:
        """Parcel attributes for the matched address (layer 37), narrowed to the ones the portal
        actually forwards. A captured submission includes these; see the note in prepare()."""
        attributes = (self.remote("getAdditionalInfo", user_fld) or {}).get("attributes") or []
        return [a for a in attributes
                if str(a.get("layerId")) != PARCEL_LAYER
                or a.get("fieldNameInService") in PARCEL_FIELDS]

    # ---- payload ----

    def prepare(self, report: VehicleReport, reporter: Reporter) -> PreparedCase:
        """Do every read and assemble the case. Nothing here writes to the city."""
        warnings: list[str] = []
        validated = self.validate_address(report.address, report.city, report.postal)
        matched = validated["address"]
        attributes = matched.get("attributes", {})

        gis = list(validated.get("attributes") or [])
        # The portal enriches the case with parcel data before submitting, and the city is both
        # the source and the recipient of it, so matching keeps our case shaped like theirs. It
        # does mean the payload carries the property owner's name and mailing address.
        user_fld = attributes.get("User_fld")
        if user_fld:
            try:
                gis += self.parcel_info(user_fld)
            except Sac311Error as e:
                warnings.append(f"No parcel details for this address ({e})")
        else:
            warnings.append("The map returned no parcel reference for this address")

        council = next((str(a.get("value")) for a in gis
                        if str(a.get("layerId")) == "8" and a.get("fieldNameInService") == "DISTNUM"), None)
        if council is None:
            warnings.append("The map returned no council district for this address")

        # Keep the map's own strings: they carry a digit more than a Python float round-trips,
        # and the portal forwards them untouched.
        lat_text, lon_text = str(validated["Y"]), str(validated["X"])
        lat, lon = float(lat_text), float(lon_text)
        if report.lat is not None and abs(lat - report.lat) + abs(lon - report.lon) > 0.01:
            warnings.append("The matched address is far from where the photo was taken")

        answers = {"concern": CONCERN_LABEL, "color": report.color, "make": report.make,
                   "model": report.model, "plate": report.plate}
        questions = question_records(self.questions(), answers)
        answered = {q.get("Question__c") for q in questions}
        missing = [label for label in ANSWER_FOR_QUESTION if label not in answered]
        if missing:
            raise Sac311Error(f"The 311 form no longer asks for: {', '.join(missing)}."
                              " The form has changed and this app needs updating.")

        case = {
            "Service_Type__c": SERVICE_TYPE_ID,
            "Sub_Service_Type__c": SUB_SERVICE_TYPE_ID,
            "Service_Type__r": {"Id": SERVICE_TYPE_ID, "Name": "Parking",
                                "Case_Record_Type_Developer_Name__c": "Sac311_Parking"},
            "Sub_Service_Type__r": {"Id": SUB_SERVICE_TYPE_ID, "Name": "Enforcement Request"},
            "Address__c": matched.get("address") or report.address,
            # The portal sends the matched point as strings and the map pin as numbers.
            "Address_Geolocation__Latitude__s": lat_text,
            "Address_Geolocation__Longitude__s": lon_text,
            "Address_X__c": attributes.get("X"),
            "Address_Y__c": attributes.get("Y"),
            "Pin_Drop_Location__Latitude__s": report.lat if report.lat is not None else lat,
            "Pin_Drop_Location__Longitude__s": report.lon if report.lon is not None else lon,
            "Council_District__c": council,
            "CaseGISFields__r": {"totalSize": len(gis), "done": True, "records": gis_records(gis)},
            "Case_Question__r": {"totalSize": len(questions), "done": True, "records": questions},
            "Case_Source__c": "Mobile Web",
        }
        contact = reporter.contact()
        if contact:
            case["Contact"] = contact  # the portal omits Anonymous_Contact__c entirely in this case
        else:
            case["Anonymous_Contact__c"] = True
            warnings.append("Filed anonymously: 311 will not send a confirmation email")
        return PreparedCase(case_record=case, summary=describe(report),
                            matched_address=case["Address__c"], lat=lat, lon=lon,
                            council_district=council, warnings=warnings)

    # ---- writes: the only two calls in this module that change anything at the city ----

    def upload_photo(self, name: str, data: bytes) -> str:
        """Attach a photo as a guest and return its ContentDocument id.

        Three steps, all taken from a captured submission rather than inferred:

        1. `getFileUploaderParams` names the upload URL and says whether the portal is in
           "decoupled" mode, where the bytes and the record that owns them are saved separately.
        2. A multipart POST of the bytes to that URL -- *not* to /s/sfsites/aura, which is why it
           never appears when filtering the network log for "aura". It answers with a content
           body id. Guests send the literal string "undefined" as `token`: the portal issues a
           guest no CSRF token and does not ask for one here.
        3. `saveContentVersion` turns that id into a ContentDocument owned by the portal's
           placeholder parent record. Its `docid` is what `saveService` wants in `fileIds`.
        """
        params = self.aura(f"{PUBLISHER_CONTROLLER}/ACTION$getFileUploaderParams", {}) or {}
        if str(params.get("isDecoupledMode", "")).lower() != "true":
            raise Sac311Error(
                "The portal has changed how it accepts file uploads, so the photo can't be "
                "attached. Submit this one through the website, or turn photos off.")
        url = f"{self.base_url}{params.get('decoupledFileUploaderUrl') or UPLOAD_PATH}"

        body, boundary = multipart(
            {"token": "undefined", "fromUITier": "true"},
            ("file", name, mimetypes.guess_type(name)[0] or "application/octet-stream", data),
            {"target": "ContentVersion"})
        answer = unhijack(_send(self.session, url, body,
                                {"Content-Type": f"multipart/form-data; boundary={boundary}",
                                 "Referer": f"{self.base_url}/s/new-service"}, self.timeout))
        body_id = answer.get("content_body_id")
        if not body_id:
            raise Sac311Error(f"The portal took the photo but named no content body: {answer}")

        saved = self.aura(f"{PUBLISHER_CONTROLLER}/ACTION$saveContentVersion",
                          {"contentBodyId": body_id,
                           "firstPublishLocationId": GUEST_FILE_PARENT_ID,
                           "pathOnClient": name,
                           "title": name.rsplit(".", 1)[0]}) or {}
        document_id = saved.get("docid")
        if not document_id:
            raise Sac311Error(f"The portal stored the photo but named no document: {saved}")
        return document_id

    def submit(self, prepared: PreparedCase, photo: tuple[str, bytes] | None = None) -> SubmissionResult:
        """Create the case. This dispatches a city parking officer, so it runs only for a draft
        the owner has approved, and never automatically.

        A network failure after the request is sent raises SubmissionUncertain, because the case
        may exist. The caller must check the city's open data rather than retrying.
        """
        file_ids: list[str] = []
        if photo is not None:
            file_ids.append(self.upload_photo(*photo))

        params = {"paramMap": {"caseRecord": prepared.case_record}, "fileIds": json.dumps(file_ids)}
        try:
            result = self.aura("apex://Sac311_Portal_ServiceConfirmationCtrl/ACTION$saveService", params)
        except Sac311Error as e:
            if e.retryable:  # the request went out; we just never heard back
                raise SubmissionUncertain(
                    f"No confirmation from 311: a case may or may not have been created ({e})") from e
            raise
        case = (result or {}).get("CaseRecord") or {}
        number = case.get("CaseNumber")
        if not number:
            raise SubmissionUncertain("311 accepted the request but returned no case number")
        return SubmissionResult(case_number=number, case_id=case.get("Id"), raw=case)
