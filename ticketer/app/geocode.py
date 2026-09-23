"""Reverse geocoding: a GPS fix -> the nearest street address.

Uses the ArcGIS World GeocodeServer that the City of Sacramento 311 portal's address map calls.
"""

import json
import math
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .tls import verified_context

CITY_311_GEOCODER = (
    "https://utility.arcgis.com/usrsvcs/servers/3f594920d25340bcb7108f137a28cda1/rest/services/World/GeocodeServer"
)
MAX_BUILDING_DISTANCE_M = 40  # beyond this, an address along the block is the better guess
NEARBY_STEPS = 2       # real neighbours wanted on each side of the matched one
MAX_SEARCH_STEPS = 8   # how far out to look for them before giving up
HOUSE_NUMBER_STEP = 2  # one side of a street is all odd or all even


@dataclass(frozen=True)
class Address:
    street: str  # "800 10th St"
    full: str  # "800 10th St, Sacramento, California, 95814"
    city: str | None
    postal: str | None
    match_type: str | None  # PointAddress (a building) or StreetAddress (interpolated along the block)
    lat: float
    lon: float
    distance_m: float  # from the GPS fix to the matched location


class Geocoder(Protocol):
    def reverse(self, lat: float, lon: float) -> Address | None: ...
    def is_real_address(self, street: str, city: str | None = None) -> bool: ...


class GeocodeError(Exception):
    pass


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def parse_reverse_geocode(payload: dict, lat: float, lon: float) -> Address | None:
    if "error" in payload:
        error = payload["error"]
        text = " ".join([str(error.get("message", "")), *map(str, error.get("details") or [])])
        if "unable to find" in text.lower():
            return None
        raise GeocodeError(f"Geocoder error: {text.strip()}")
    address, location = payload.get("address") or {}, payload.get("location") or {}
    street = address.get("Address") or address.get("ShortLabel")
    if not street or "x" not in location:
        return None
    return Address(
        street=street,
        full=address.get("Match_addr") or street,
        city=address.get("City") or None,
        postal=address.get("Postal") or None,
        match_type=address.get("Addr_type") or None,
        lat=location["y"],
        lon=location["x"],
        distance_m=round(distance_m(lat, lon, location["y"], location["x"]), 1),
    )


REAL_ADDRESS_MIN_SCORE = 80  # forward-geocode confidence; every exact match observed scores 100


def parse_forward_geocode(payload: dict) -> tuple[str | None, float]:
    """The (Addr_type, score) of a forward geocode's best candidate, or (None, 0) for no match.

    `PointAddress` is a real parcel; `StreetAddress` is a number interpolated along the block
    that may not correspond to any building at all.
    """
    if "error" in payload:
        error = payload["error"]
        text = " ".join([str(error.get("message", "")), *map(str, error.get("details") or [])])
        raise GeocodeError(f"Geocoder error: {text.strip()}")
    candidates = payload.get("candidates") or []
    if not candidates:
        return None, 0
    best = candidates[0]
    return best.get("attributes", {}).get("Addr_type"), best.get("score", 0)


class ArcGisReverseGeocoder:
    def __init__(self, url: str = CITY_311_GEOCODER, timeout: float = 15.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _get(self, endpoint: str, params: dict) -> dict:
        request = urllib.request.Request(
            f"{self.url}/{endpoint}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": "Mozilla/5.0 (ticketer Home Assistant app)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=verified_context()) as response:
                return json.load(response)
        except (OSError, ValueError) as e:
            raise GeocodeError(f"Geocoder unavailable: {e}") from e

    def _call(self, lat: float, lon: float, feature_type: str | None = None) -> dict:
        params = {"location": f"{lon},{lat}", "outSR": "4326", "f": "json"}
        if feature_type:
            params["featureTypes"] = feature_type
        return self._get("reverseGeocode", params)

    def reverse(self, lat: float, lon: float) -> Address | None:
        building = parse_reverse_geocode(self._call(lat, lon, "PointAddress"), lat, lon)
        if building and building.distance_m <= MAX_BUILDING_DISTANCE_M:
            return building
        return parse_reverse_geocode(self._call(lat, lon), lat, lon) or building

    def is_real_address(self, street: str, city: str | None = None) -> bool:
        text = f"{street}, {city}" if city else street
        payload = self._get("findAddressCandidates", {
            "SingleLine": text, "outFields": "Addr_type", "f": "json", "maxLocations": "1",
        })
        addr_type, score = parse_forward_geocode(payload)
        return addr_type == "PointAddress" and score >= REAL_ADDRESS_MIN_SCORE


def verified_candidates(geocoder: Geocoder, address: Address, wanted: int = NEARBY_STEPS,
                         max_steps: int = MAX_SEARCH_STEPS) -> list[str]:
    """The matched address plus up to `wanted` confirmed real neighbours on each side.

    Stepping by two assumes a house every two numbers, which often isn't true -- a driveway, a
    lot split, or two houses sharing one address all leave a gap, sometimes several numbers wide.
    This walks outward confirming each candidate against the geocoder's own parcel data, and
    keeps going past a gap instead of stopping at the first one. Still bounded by the hundred
    block and by `max_steps`, so one sparse side can't hang the picker on an empty block.
    """
    match = re.match(r"(\d+)(\s.*)$", address.street)
    if not match:
        return [address.street]  # no leading house number to step: offer what was matched
    number, rest = int(match[1]), match[2]
    hundred_block = number // 100

    def real_neighbours(direction: int) -> list[int]:
        found = []
        for step in range(1, max_steps + 1):
            n = number + HOUSE_NUMBER_STEP * step * direction
            if n <= 0 or n // 100 != hundred_block:
                break  # off the block: a guess this far out is more likely someone else's
            try:
                if geocoder.is_real_address(f"{n}{rest}", address.city):
                    found.append(n)
                    if len(found) == wanted:
                        break
            except GeocodeError:
                pass  # can't confirm it exists -- leave it out rather than offer a guess
        return found

    numbers = real_neighbours(-1) + [number] + real_neighbours(1)
    return [f"{n}{rest}" for n in sorted(numbers)]
