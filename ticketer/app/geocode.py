"""Reverse geocoding: a GPS fix -> the real street addresses nearest it.

Looked up on this server in Sacramento County's own address list (see addresses.py), so a fix
never leaves it. Every address offered is one the county has a point for: nothing is estimated
along the block, which is how an earlier geocoder came to offer a number with no house.
"""

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .addresses import read_only

NEARBY_RADIUS_M = 100  # further than this, a house is likelier the next block than the one in front
NEARBY_LIMIT = 6       # the picker's list: both sides of the street near the fix, and a corner
METERS_PER_DEGREE = 111_320


@dataclass(frozen=True)
class Address:
    street: str  # "800 10th St"
    full: str  # "800 10th St, Sacramento, California, 95814"
    city: str | None
    postal: str | None
    match_type: str | None  # PointAddress: a real address point (earlier drafts also have StreetAddress)
    lat: float
    lon: float
    distance_m: float  # from the GPS fix to the matched location


class Geocoder(Protocol):
    def reverse(self, lat: float, lon: float) -> Address | None: ...
    def candidates(self, lat: float, lon: float) -> list[str]: ...


class GeocodeError(Exception):
    pass


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def street_name(street: str) -> str:
    """The county's capitals as people write a street: "12TH ST" -> "12th St"."""
    def word(token: str) -> str:
        if token[:1].isdigit():
            return token.lower()  # "12TH" -> "12th", "1/2" stays
        if token.startswith("MC") and len(token) > 2 and token.isalpha():
            return "Mc" + token[2:].capitalize()  # "MCCLATCHY" -> "McClatchy"
        return token.capitalize()
    return " ".join(word(token) for token in street.split())


def street_line(number: int, suffix: str, street: str) -> str:
    return " ".join(part for part in (str(number), suffix, street_name(street)) if part)


class CountyGeocoder:
    """Looks fixes up in `addresses.db`, which addresses.AddressRefresher keeps current."""

    def __init__(self, path: Path, radius_m: float = NEARBY_RADIUS_M, limit: int = NEARBY_LIMIT):
        self.path = path
        self.radius_m = radius_m
        self.limit = limit

    def _nearest(self, lat: float, lon: float) -> list[tuple]:
        """(distance, number, suffix, street, zip, lat, lon) for the addresses within reach of the
        fix, nearest first."""
        if not self.path.is_file():
            raise GeocodeError("The address list is still downloading")
        dlat = self.radius_m / METERS_PER_DEGREE
        dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
        try:
            conn = read_only(self.path)
            try:
                rows = conn.execute(
                    "SELECT number, suffix, street, zip, lat, lon FROM addresses"
                    " WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                    (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise GeocodeError(f"Can't read the address list: {e}") from e
        near = sorted((distance_m(lat, lon, row[4], row[5]), *row) for row in rows)
        return [row for row in near if row[0] <= self.radius_m][:self.limit]

    def reverse(self, lat: float, lon: float) -> Address | None:
        near = self._nearest(lat, lon)
        if not near:
            return None
        distance, number, suffix, street, zip_code, a_lat, a_lon = near[0]
        line = street_line(number, suffix, street)
        return Address(street=line, full=", ".join(filter(None, (line, "Sacramento", "California", zip_code))),
                       city="Sacramento", postal=zip_code or None, match_type="PointAddress",
                       lat=a_lat, lon=a_lon, distance_m=round(distance, 1))

    def candidates(self, lat: float, lon: float) -> list[str]:
        """The picker's list. The nearest address's street comes first, then any other street in
        the order it comes near, each in house-number order: the houses opposite sit among their
        neighbours, and a corner house, or the street behind when the fix drifted, comes after."""
        near = self._nearest(lat, lon)
        rank: dict[str, int] = {}
        for row in near:  # nearest first, so each street is ranked by its nearest address
            rank.setdefault(row[3], len(rank))
        ordered = sorted(near, key=lambda row: (rank[row[3]], row[1], row[2]))
        # dict.fromkeys: an address listed under two ZIP codes is still one choice
        return list(dict.fromkeys(street_line(number, suffix, street) for _, number, suffix, street, *_ in ordered))
