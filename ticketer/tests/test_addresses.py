"""The local address list: downloading it, swapping it in safely, and looking fixes up in it.

No test reaches the county: a fake source stands in for the layer. Every address is invented, and
placed by metres from an arbitrary point inside the city's bounding box.
"""

import itertools
import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.addresses import ADDRESS_FILE, AddressDataError, AddressRefresher, CountySource, build, stored
from app.geocode import CountyGeocoder, GeocodeError, street_line, street_name
from app.main import Settings, create_app
from app.worker import Pipeline
from test_api import ALICE

BASE_LAT, BASE_LON = 38.5700, -121.4700
_ids = itertools.count(1)


def at(north_m: float, east_m: float) -> tuple[float, float]:
    return (BASE_LAT + north_m / 111_320,
            BASE_LON + east_m / (111_320 * math.cos(math.radians(BASE_LAT))))


def point(number, street, north_m=0.0, east_m=0.0, suffix="", unit="", zip_code="95814") -> dict:
    """One record as the county's layer returns it."""
    lat, lon = at(north_m, east_m)
    return {"OBJECTID": next(_ids), "Address_Number": str(number), "Address_Number_Suffix": suffix,
            "Full_Street": street, "Unit_Number": unit, "Zip_Code": zip_code,
            "Latitude_Y": lat, "Longitude_X": lon}


def block() -> list[dict]:
    """Example St runs north, odd numbers on its east side and even on its west, 12 m apart.
    Sample St is the street behind, 60 m east. C St crosses just south of 1201 and has a house
    on the corner."""
    points = [point(1201 + 2 * i, "EXAMPLE ST", 12 * i, 15) for i in range(5)]      # 1201-1209
    points += [point(1200 + 2 * i, "EXAMPLE ST", 12 * i, -15) for i in range(4)]    # 1200-1206
    points += [point(1200 + 2 * i, "SAMPLE ST", 12 * i, 75) for i in range(5)]      # behind
    points.append(point(2901, "C ST", -25, 15))
    return points


class FakeSource:
    def __init__(self, points, count=None, page_size=3):
        self.points = list(points)
        self._count = count
        self.page_size = page_size

    def count(self):
        return len(self.points) if self._count is None else self._count

    def pages(self):
        for i in range(0, len(self.points), self.page_size):
            yield self.points[i:i + self.page_size]


def built(tmp_path, points=None):
    path = tmp_path / ADDRESS_FILE
    build(FakeSource(block() if points is None else points), path)
    return path


# ---- looking up ----

def test_the_nearest_real_address_is_the_match(tmp_path):
    fix = at(24, 8)  # on the east sidewalk, in front of 1205
    address = CountyGeocoder(built(tmp_path)).reverse(*fix)
    assert address.street == "1205 Example St"
    assert address.full == "1205 Example St, Sacramento, California, 95814"
    assert (address.postal, address.match_type) == ("95814", "PointAddress")
    assert 6 < address.distance_m < 8


def test_the_picker_offers_both_sides_of_the_street_in_number_order(tmp_path):
    geocoder = CountyGeocoder(built(tmp_path), limit=4)
    # 1205 is nearest, 1203 and 1207 flank it, and 1204 is straight across the street.
    assert geocoder.candidates(*at(24, 8)) == [
        "1203 Example St", "1204 Example St", "1205 Example St", "1207 Example St"]


def test_a_corner_puts_the_nearest_street_first(tmp_path):
    geocoder = CountyGeocoder(built(tmp_path), limit=4)
    fix = at(-18, 8)
    assert geocoder.reverse(*fix).street == "2901 C St"
    assert geocoder.candidates(*fix) == ["2901 C St", "1200 Example St", "1201 Example St", "1203 Example St"]


def test_nothing_is_offered_far_from_any_address(tmp_path):
    geocoder = CountyGeocoder(built(tmp_path))
    assert geocoder.reverse(*at(500, 0)) is None
    assert geocoder.candidates(*at(500, 0)) == []


def test_lookups_say_so_until_the_list_has_downloaded(tmp_path):
    geocoder = CountyGeocoder(tmp_path / ADDRESS_FILE)
    with pytest.raises(GeocodeError, match="still downloading"):
        geocoder.reverse(*at(0, 0))


@pytest.mark.parametrize("county, written", [
    ("12TH ST", "12th St"),
    ("21ST ST", "21st St"),
    ("I ST", "I St"),
    ("MCCLATCHY WAY", "McClatchy Way"),
    ("EL CAMINO AVE", "El Camino Ave"),
])
def test_street_names_are_written_as_people_write_them(county, written):
    assert street_name(county) == written


def test_a_half_number_keeps_its_suffix():
    assert street_line(1201, "1/2", "12TH ST") == "1201 1/2 12th St"


# ---- building the list ----

def test_units_collapse_to_one_address_at_the_building(tmp_path):
    points = block() + [
        point(1300, "EXAMPLE ST", 100, 15),                     # the building's own point
        point(1300, "EXAMPLE ST", 130, 15, unit="A"),           # its flats, further back
        point(1300, "EXAMPLE ST", 130, 15, unit="B"),
        point(1310, "EXAMPLE ST", 100, -20, unit="1"),          # flats with no building point
        point(1310, "EXAMPLE ST", 110, -20, unit="2"),
    ]
    geocoder = CountyGeocoder(built(tmp_path, points), limit=10)
    assert geocoder.reverse(*at(100, 15)).distance_m < 1       # the building's point, not the flats'
    assert geocoder.reverse(*at(105, -20)).street == "1310 Example St"
    assert geocoder.reverse(*at(105, -20)).distance_m < 1      # the middle of its flats
    assert geocoder.candidates(*at(100, 0)).count("1300 Example St") == 1


def test_a_blank_suffix_is_no_suffix_and_a_half_is_kept(tmp_path):
    points = block() + [point(1400, "EXAMPLE ST", 300, 15, suffix=" "),
                        point(1400, "EXAMPLE ST", 306, 15, suffix="1/2")]
    assert CountyGeocoder(built(tmp_path, points)).candidates(*at(303, 15)) == [
        "1400 Example St", "1400 1/2 Example St"]


def test_unplaceable_points_are_skipped(tmp_path):
    good = [point(1000 + 2 * i, "LONG EXAMPLE ST", 12 * i, 15) for i in range(300)]
    bad = [point("", "LONG EXAMPLE ST"), point(1, "LONG EXAMPLE ST", north_m=200_000)]
    path = built(tmp_path, good + bad)
    assert stored(path)["rows"] == 300


def test_too_many_unplaceable_points_keeps_the_old_list(tmp_path):
    path = built(tmp_path)
    before = path.read_bytes()
    broken = block() + [point("", "EXAMPLE ST") for _ in range(5)]
    with pytest.raises(AddressDataError, match="unusable"):
        build(FakeSource(broken), path)
    assert path.read_bytes() == before
    assert not path.with_name(f"{ADDRESS_FILE}.new").exists()


def test_a_short_download_keeps_the_old_list(tmp_path):
    path = built(tmp_path)
    before = path.read_bytes()
    with pytest.raises(AddressDataError, match="arrived"):
        build(FakeSource(block(), count=1000), path)
    assert path.read_bytes() == before


def test_a_much_smaller_list_does_not_replace_the_current_one(tmp_path):
    path = built(tmp_path)
    with pytest.raises(AddressDataError, match="where the current one has"):
        build(FakeSource(block()[:5]), path)
    assert stored(path)["rows"] == len(block())


def test_a_new_list_is_picked_up_by_the_next_lookup(tmp_path):
    geocoder = CountyGeocoder(built(tmp_path))
    assert geocoder.reverse(*at(0, 15)).street == "1201 Example St"
    renumbered = [p | {"Address_Number": str(int(p["Address_Number"]) + 1000)} for p in block()]
    build(FakeSource(renumbered), tmp_path / ADDRESS_FILE)
    assert geocoder.reverse(*at(0, 15)).street == "2201 Example St"


def test_the_county_source_pages_by_objectid_until_the_layer_is_done(monkeypatch):
    source = CountySource(pause=0)
    pages = [{"features": [{"attributes": {"OBJECTID": 5}}, {"attributes": {"OBJECTID": 9}}],
              "exceededTransferLimit": True},
             {"features": [{"attributes": {"OBJECTID": 12}}]}]
    asked = []
    monkeypatch.setattr(source, "_get", lambda params: asked.append(params["where"]) or pages[len(asked) - 1])
    assert [len(page) for page in source.pages()] == [2, 1]
    assert asked == ["Jurisdiction = 'SACRAMENTO' AND OBJECTID > 0",
                     "Jurisdiction = 'SACRAMENTO' AND OBJECTID > 9"]


def test_the_county_source_refuses_a_repeated_page(monkeypatch):
    source = CountySource(pause=0)
    same = {"features": [{"attributes": {"OBJECTID": 0}}], "exceededTransferLimit": True}
    monkeypatch.setattr(source, "_get", lambda params: same)
    with pytest.raises(AddressDataError, match="repeated"):
        list(source.pages())


# ---- keeping it current ----

def test_the_refresher_downloads_a_missing_or_old_list(tmp_path):
    refresher = AddressRefresher(tmp_path / ADDRESS_FILE, source=FakeSource(block()))
    assert refresher.due() and refresher.status()["ready"] is False
    refresher.refresh()
    assert not refresher.due()
    assert refresher.due(now=datetime.now(timezone.utc) + timedelta(days=31))
    status = refresher.status()
    assert (status["ready"], status["addresses"], status["error"]) == (True, len(block()), None)


def test_a_failed_refresh_is_reported_and_keeps_the_list(tmp_path):
    path = built(tmp_path)
    refresher = AddressRefresher(path, source=FakeSource(block(), count=1000))
    with pytest.raises(AddressDataError):
        refresher.refresh()
    status = refresher.status()
    assert status["ready"] is True and "arrived" in status["error"]


def test_the_home_screen_can_ask_whether_the_list_is_ready(tmp_path):
    pipeline = Pipeline(extractor=None, plate_reader=None, geocoder=CountyGeocoder(tmp_path / ADDRESS_FILE))
    with TestClient(create_app(Settings(data_dir=tmp_path, run_worker=False), pipeline=pipeline)) as client:
        assert client.get("/api/addresses").status_code == 401
        assert client.get("/api/addresses", headers=ALICE).json()["ready"] is False
        build(FakeSource(block()), tmp_path / ADDRESS_FILE)
        status = client.get("/api/addresses", headers=ALICE).json()
    assert status["ready"] is True and status["addresses"] == len(block())
