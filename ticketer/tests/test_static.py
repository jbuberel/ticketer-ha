"""The phone UI's own files. Nothing else loads its JavaScript, so a broken reference fails only on
the phone, as a blank screen; these catch the ones that can be caught without a browser."""

import json
import re

from app.main import STATIC_DIR

RELATIVE_IMPORT = re.compile(r"""(?:from|import)\s+["'](\./[^"']+)["']""")


def test_every_module_a_page_imports_is_there():
    checked = 0
    for path in STATIC_DIR.glob("*.js"):
        for target in RELATIVE_IMPORT.findall(path.read_text()):
            assert (STATIC_DIR / target).is_file(), f"{path.name} imports {target}, which doesn't exist"
            checked += 1
    assert checked


def test_every_screen_the_router_names_is_defined():
    """A route naming an element no module defines shows an empty page for that address."""
    routed = set(re.findall(r"""route\(\s*"[^"]+",\s*"([a-z-]+)\"""", (STATIC_DIR / "app.js").read_text()))
    defined = {name for path in STATIC_DIR.glob("*.js")
               for name in re.findall(r"""customElements\.define\(\s*"([a-z-]+)\"""", path.read_text())}
    assert routed == {"page-home", "page-capture", "page-batch"}
    assert routed <= defined, f"routed but never defined: {routed - defined}"


def test_installed_app_colours_suit_dark_mode():
    """Dark on purpose, though the manifest can't say so. With 3-button navigation, Android fills
    the strip behind the Back / Home / Recents buttons with the manifest's background_color, baked
    into the installed app: Chrome never lets the page draw there, and no longer reads a dark
    variant, so the one colour serves both themes. The Pixel it runs on stays in dark mode, whose
    buttons are light -- on 0.9.0's white they vanished -- so it is the dark toolbar's own grey."""
    manifest = json.loads((STATIC_DIR / "manifest.webmanifest").read_text())
    assert manifest["background_color"] == manifest["theme_color"] == "#1f1f1f"
