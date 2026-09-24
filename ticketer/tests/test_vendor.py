"""The vendored browser libraries: what vendor.py fetches, and how the app serves it."""

import base64
import hashlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vendor
from app.main import STATIC_DIR, AppStatic


def test_pages_load_the_versions_vendor_py_fetches():
    """A version bumped in vendor.py but not in a page would leave the page loading a directory
    the image no longer has -- a blank screen on the phone, and nothing failing at build time."""
    fetched = {package.directory for package in vendor.PACKAGES}
    loaded = set()
    for path in STATIC_DIR.glob("*.*"):
        if path.suffix not in (".html", ".js"):
            continue
        for directory in re.findall(r"/vendor/([^/\"'`]+)/", path.read_text()):
            assert directory in fetched, f"{path.name} loads /vendor/{directory}/, which vendor.py doesn't fetch"
            loaded.add(directory)
    assert loaded == fetched, f"fetched but never loaded: {fetched - loaded}"


def test_pinned_libraries_are_cached_and_app_files_revalidated(tmp_path):
    (tmp_path / "vendor" / "lib-1.2.3").mkdir(parents=True)
    (tmp_path / "vendor" / "lib-1.2.3" / "lib.js").write_text("export {};")
    (tmp_path / "review.js").write_text("export {};")
    app = FastAPI()
    app.mount("/", AppStatic(directory=tmp_path, html=True))
    with TestClient(app) as client:
        pinned = client.get("/vendor/lib-1.2.3/lib.js")
        assert pinned.status_code == 200
        assert "immutable" in pinned.headers["cache-control"]
        assert client.get("/review.js").headers["cache-control"] == "no-cache"
        # A missing file mustn't be cached as missing for a year.
        assert "immutable" not in client.get("/vendor/lib-1.2.3/gone.js").headers.get("cache-control", "")


def test_a_tampered_download_is_refused():
    data = b"package contents"
    good = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    vendor.verify(data, good)
    with pytest.raises(SystemExit, match="checksum mismatch"):
        vendor.verify(data + b"!", good)


@pytest.mark.parametrize(("pick", "path", "expected"), [
    (vendor.ionic_files, "package/dist/ionic/ionic.esm.js", "ionic.esm.js"),
    (vendor.ionic_files, "package/dist/ionic/svg/car-outline.svg", "svg/car-outline.svg"),
    (vendor.ionic_files, "package/css/palettes/dark.system.css", "css/palettes/dark.system.css"),
    (vendor.ionic_files, "package/css/ionic.bundle.css.map", None),
    (vendor.ionic_files, "package/dist/esm/index.js", None),
    (vendor.lit_html_files, "package/lit-html.js", "lit-html.js"),
    (vendor.lit_html_files, "package/directives/live.js", "directives/live.js"),
    (vendor.lit_html_files, "package/development/lit-html.js", None),
    (vendor.lit_html_files, "package/lit-html.d.ts", None),
])
def test_only_the_browser_builds_are_kept(pick, path, expected):
    assert pick(path) == expected
