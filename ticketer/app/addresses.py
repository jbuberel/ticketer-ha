"""Sacramento County's address list, kept on this server so addresses are looked up locally.

The county publishes every address point it maintains as a public ArcGIS layer, edited daily and
dedicated to the public domain (CC0). Inside the City of Sacramento that is about 255,000 points,
one per apartment or suite as well as one per building. Once a month the city's points are
downloaded, collapsed to one row per street address, and written to `addresses.db`, which
`geocode.CountyGeocoder` reads.

The download says nothing about the owner: it asks for every address in the city, never for any
one place. GPS fixes are looked up here and never leave the server.

A new download replaces the old file only once it is complete and looks like the last one, so a
failed or truncated download leaves lookups working from last month's list.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .tls import verified_context

log = logging.getLogger("ticketer.addresses")

ADDRESS_FILE = "addresses.db"
COUNTY_LAYER = "https://services1.arcgis.com/5NARefyPVtAeuJPU/arcgis/rest/services/Address/FeatureServer/0"
CITY = "SACRAMENTO"  # the layer's Jurisdiction for the city; the rest of the county has its own
FIELDS = ("OBJECTID", "Address_Number", "Address_Number_Suffix", "Full_Street", "Unit_Number",
          "Zip_Code", "Latitude_Y", "Longitude_X")
PAGE_SIZE = 2000             # the layer's maxRecordCount
PAGE_PAUSE_SECONDS = 0.5     # a monthly download has no reason to hurry the county's server
USER_AGENT = "Mozilla/5.0 (ticketer Home Assistant app)"

REFRESH_AFTER = timedelta(days=30)
CHECK_SECONDS = 6 * 3600
RETRY_SECONDS = 3600         # after a failed download

# A point outside this box is not in the city, whatever the row says.
LAT_RANGE, LON_RANGE = (38.40, 38.80), (-121.60, -121.30)
# What a download has to look like before it replaces the list in use.
MIN_RECEIVED = 0.99          # of the points the layer says it has
MAX_SKIPPED = 0.01           # of the points received
MIN_OF_PREVIOUS = 0.90       # of the street addresses in the list being replaced

POINTS = """
CREATE TEMP TABLE points (number INTEGER, suffix TEXT, street TEXT, zip TEXT, has_unit INTEGER,
                          lat REAL, lon REAL);
"""
# One row per street address. An apartment building has a point per unit as well as one for the
# building, usually in the same place but not always; the building's own point wins, and an
# address that only has unit points gets the middle of them.
ADDRESSES = """
CREATE TABLE addresses (number INTEGER NOT NULL, suffix TEXT NOT NULL, street TEXT NOT NULL,
                        zip TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL);
INSERT INTO addresses
SELECT number, suffix, street, zip,
       COALESCE(AVG(CASE WHEN has_unit = 0 THEN lat END), AVG(lat)),
       COALESCE(AVG(CASE WHEN has_unit = 0 THEN lon END), AVG(lon))
FROM temp.points GROUP BY number, suffix, street, zip;
CREATE INDEX addresses_by_position ON addresses (lat, lon);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class AddressDataError(Exception):
    pass


def read_only(path: Path) -> sqlite3.Connection:
    """A connection that can't change the list, and that keeps reading the file it opened even
    if a refresh replaces it meanwhile."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def stored(path: Path) -> dict | None:
    """What the list on disk holds, or None when there is no usable list yet."""
    if not path.is_file():
        return None
    try:
        conn = read_only(path)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
        return {"downloaded_at": datetime.fromisoformat(meta["downloaded_at"]), "rows": int(meta["rows"])}
    except (sqlite3.Error, KeyError, ValueError):
        return None


def clean(point: dict) -> tuple | None:
    """A `points` row from one of the layer's records, or None for a record that can't be
    placed: no number, no street, or a position outside the city."""
    try:
        number = int(str(point["Address_Number"]).strip())
        lat, lon = float(point["Latitude_Y"]), float(point["Longitude_X"])
    except (KeyError, TypeError, ValueError):
        return None
    street = " ".join(str(point.get("Full_Street") or "").split()).upper()
    if number <= 0 or not street:
        return None
    if not (LAT_RANGE[0] < lat < LAT_RANGE[1] and LON_RANGE[0] < lon < LON_RANGE[1]):
        return None
    suffix = " ".join(str(point.get("Address_Number_Suffix") or "").split())  # " " means none
    zip_code = str(point.get("Zip_Code") or "").strip()
    has_unit = bool(str(point.get("Unit_Number") or "").strip())
    return (number, suffix, street, zip_code if re.fullmatch(r"\d{5}", zip_code) else "",
            int(has_unit), lat, lon)


class Source(Protocol):
    def count(self) -> int: ...
    def pages(self) -> Iterator[list[dict]]: ...


class CountySource:
    """The county's address layer, read through its public query API a page at a time."""

    def __init__(self, url: str = COUNTY_LAYER, timeout: float = 60.0, pause: float = PAGE_PAUSE_SECONDS):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.pause = pause

    def _get(self, params: dict) -> dict:
        request = urllib.request.Request(f"{self.url}/query?{urllib.parse.urlencode(params)}",
                                         headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=verified_context()) as response:
                payload = json.load(response)
        except (OSError, ValueError) as e:
            raise AddressDataError(f"The county's address layer is unavailable: {e}") from e
        if "error" in payload:
            raise AddressDataError(f"The county's address layer refused the query: "
                                   f"{payload['error'].get('message', payload['error'])}")
        return payload

    def count(self) -> int:
        payload = self._get({"where": f"Jurisdiction = '{CITY}'", "returnCountOnly": "true", "f": "json"})
        try:
            return int(payload["count"])
        except (KeyError, TypeError, ValueError) as e:
            raise AddressDataError(f"The county's address layer gave no count: {payload}") from e

    def pages(self) -> Iterator[list[dict]]:
        # Paged by OBJECTID rather than by offset, so a record edited mid-download can't shift
        # every later page by one.
        last = 0
        while True:
            payload = self._get({"where": f"Jurisdiction = '{CITY}' AND OBJECTID > {last}",
                                 "outFields": ",".join(FIELDS), "returnGeometry": "false",
                                 "orderByFields": "OBJECTID", "resultRecordCount": str(PAGE_SIZE), "f": "json"})
            points = [feature["attributes"] for feature in payload.get("features") or []]
            if points:
                yield points
            if not points or not payload.get("exceededTransferLimit"):
                return
            if points[-1]["OBJECTID"] <= last:
                raise AddressDataError("The county's address layer repeated a page")
            last = points[-1]["OBJECTID"]
            time.sleep(self.pause)


def build(source: Source, path: Path, now: datetime | None = None) -> int:
    """Download the city's addresses and swap them in at `path`. Returns how many street
    addresses the new list holds. Raises AddressDataError, leaving the old list in place, when
    the download is incomplete or doesn't look like the list it would replace."""
    expected = source.count()
    if expected <= 0:
        raise AddressDataError("The county's address layer says it has no addresses in the city")
    previous = stored(path)
    temp = path.with_name(f"{path.name}.new")
    temp.unlink(missing_ok=True)
    try:
        conn = sqlite3.connect(temp)
        try:
            conn.executescript(POINTS)
            received = skipped = 0
            for page in source.pages():
                rows = [row for row in map(clean, page) if row]
                received += len(page)
                skipped += len(page) - len(rows)
                conn.executemany("INSERT INTO temp.points VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            if received < MIN_RECEIVED * expected:
                raise AddressDataError(f"Only {received:,} of {expected:,} address points arrived")
            if skipped > MAX_SKIPPED * received:
                raise AddressDataError(f"{skipped:,} of {received:,} address points were unusable")
            conn.executescript(ADDRESSES)
            count = conn.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
            if previous and count < MIN_OF_PREVIOUS * previous["rows"]:
                raise AddressDataError(f"The new list has {count:,} addresses where the current one has "
                                       f"{previous['rows']:,}")
            conn.executemany("INSERT INTO meta VALUES (?, ?)", [
                ("downloaded_at", (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")),
                ("rows", str(count)), ("points", str(received)), ("source", COUNTY_LAYER)])
            conn.commit()
        finally:
            conn.close()
        os.replace(temp, path)  # atomic: a lookup sees the old list or the new one, never half
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return count


class AddressRefresher:
    """Keeps `addresses.db` current: downloads it when the app first starts, then monthly."""

    def __init__(self, path: Path, source: Source | None = None, max_age: timedelta = REFRESH_AFTER):
        self.path = path
        self.source = source or CountySource()
        self.max_age = max_age
        self.refreshing = False
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="addresses", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)  # a download in progress is abandoned; its temp file goes next time
            self._thread = None

    def due(self, now: datetime | None = None) -> bool:
        current = stored(self.path)
        return current is None or (now or datetime.now(timezone.utc)) - current["downloaded_at"] >= self.max_age

    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = CHECK_SECONDS
            if self.due():
                try:
                    self.refresh()
                except Exception:
                    log.exception("address list download failed; trying again in %d min", RETRY_SECONDS // 60)
                    wait = RETRY_SECONDS
            self._stop.wait(wait)

    def refresh(self) -> int:
        started = time.monotonic()
        self.refreshing = True
        try:
            count = build(self.source, self.path)
        except Exception as e:
            self.last_error = str(e)
            raise
        finally:
            self.refreshing = False
        self.last_error = None
        log.info("address list: %d street addresses in %.0f s", count, time.monotonic() - started)
        return count

    def status(self) -> dict:
        current = stored(self.path)
        return {"ready": current is not None,
                "addresses": current and current["rows"],
                "as_of": current and current["downloaded_at"].isoformat(),
                "refreshing": self.refreshing,
                "error": self.last_error}
