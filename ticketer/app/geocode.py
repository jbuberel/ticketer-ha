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

CITY_311_GEOCODER = (
    "https://utility.arcgis.com/usrsvcs/servers/3f594920d25340bcb7108f137a28cda1/rest/services/World/GeocodeServer"
)
MAX_BUILDING_DISTANCE_M = 40  # beyond this, an address along the block is the better guess
NEARBY_STEPS = 2       # house numbers offered either side of the matched one
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


class ArcGisReverseGeocoder:
    def __init__(self, url: str = CITY_311_GEOCODER, timeout: float = 15.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _call(self, lat: float, lon: float, feature_type: str | None = None) -> dict:
        params = {"location": f"{lon},{lat}", "outSR": "4326", "f": "json"}
        if feature_type:
            params["featureTypes"] = feature_type
        request = urllib.request.Request(
            f"{self.url}/reverseGeocode?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": "Mozilla/5.0 (ticketer Home Assistant app)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except (OSError, ValueError) as e:
            raise GeocodeError(f"Geocoder unavailable: {e}") from e

    def reverse(self, lat: float, lon: float) -> Address | None:
        building = parse_reverse_geocode(self._call(lat, lon, "PointAddress"), lat, lon)
        if building and building.distance_m <= MAX_BUILDING_DISTANCE_M:
            return building
        return parse_reverse_geocode(self._call(lat, lon), lat, lon) or building


def nearby_addresses(street: str, steps: int = NEARBY_STEPS) -> list[str]:
    """The matched address plus its neighbours, for picking the right house from the sidewalk.

    A fix taken beside a parked car resolves to whichever house is nearest, which is often a
    door or two off. Stepping the number by 2 stays on the same side of the street, and staying
    inside the hundred block keeps the list from naming a house around the corner.
    """
    match = re.match(r"(\d+)(\s.*)$", street)
    if not match:
        return [street]  # no leading house number to step: offer what was matched
    number, rest = int(match[1]), match[2]
    numbers = {number + HOUSE_NUMBER_STEP * step for step in range(-steps, steps + 1)}
    return [f"{n}{rest}" for n in sorted(numbers) if n > 0 and n // 100 == number // 100]
