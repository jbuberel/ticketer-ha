"""Fetch the browser libraries the phone UI loads, pinned and checksum-verified.

The phone loads nothing from outside the tailnet, so these are served by the app itself. They are
not committed: the image build runs this, and a local checkout runs it once before serving.

    python3 vendor.py [dest]      # dest defaults to app/static/vendor

Each package lands in a directory named for its version (vendor/ionic-9.0.4/...), so a file's URL
never changes content and the server can let phones cache it for good. Bumping a version here
means updating the pages that reference the old directory too; tests/test_vendor.py checks.
"""

import base64
import hashlib
import io
import shutil
import sys
import tarfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_DEST = Path(__file__).parent / "app" / "static" / "vendor"
DONE_MARKER = ".complete"


@dataclass(frozen=True)
class Package:
    name: str  # on the npm registry
    short: str  # what its directory is called here
    version: str
    integrity: str  # the registry's dist.integrity for this exact version
    pick: Callable[[str], str | None]  # path inside the tarball -> path to write, or None to skip

    @property
    def directory(self) -> str:
        return f"{self.short}-{self.version}"

    @property
    def tarball(self) -> str:
        return f"https://registry.npmjs.org/{self.name}/-/{self.name.rsplit('/', 1)[-1]}-{self.version}.tgz"


def ionic_files(path: str) -> str | None:
    """The self-loading browser build (components load on first use, icons included) and its CSS."""
    if path == "package/LICENSE":
        return "LICENSE"
    if path.endswith(".map"):
        return None
    for prefix, to in (("package/dist/ionic/", ""), ("package/css/", "css/")):
        if path.startswith(prefix):
            return to + path.removeprefix(prefix)
    return None


def lit_html_files(path: str) -> str | None:
    """The production modules: the library itself and its directives, not the development build."""
    rest = path.removeprefix("package/")
    if rest == "LICENSE":
        return rest
    if rest.endswith(".js") and ("/" not in rest or rest.startswith("directives/")):
        return rest
    return None


PACKAGES = (
    # 9.0.4 rather than the newest release: a version is left a few days before it's taken up.
    Package("@ionic/core", "ionic", "9.0.4",
            "sha512-tNYTxBt8+Tla61B8OwAeEHuTWuJdJn6+buftCNikhPHi5TW9MbduIPsWn+D8KtlWhZKPVshVyvItfWcYxMN3JA==",
            ionic_files),
    Package("lit-html", "lit-html", "3.3.3",
            "sha512-el8M6jK2o3RXBnrSHX3ZKrsN8zEV63pSExTO1wYJz7QndGYZ8353e2a5PPX+qHe2aGayfnchQmkAojaWAREOIA==",
            lit_html_files),
)


def verify(data: bytes, integrity: str) -> None:
    algorithm, _, expected = integrity.partition("-")
    actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
    if actual != expected:
        raise SystemExit(f"checksum mismatch: expected {integrity}, got {algorithm}-{actual}")


def install(package: Package, dest: Path) -> None:
    target = dest / package.directory
    if (target / DONE_MARKER).exists():
        return
    print(f"vendor: fetching {package.name} {package.version}", file=sys.stderr)
    with urllib.request.urlopen(package.tarball, timeout=60) as response:
        data = response.read()
    verify(data, package.integrity)

    shutil.rmtree(target, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            relative = package.pick(member.name) if member.isfile() else None
            if relative is None:
                continue
            if ".." in PurePosixPath(relative).parts:
                raise SystemExit(f"refusing to write outside {target}: {member.name}")
            out = target / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(tar.extractfile(member).read())
    (target / DONE_MARKER).touch()


def main() -> None:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DEST
    for package in PACKAGES:
        install(package, dest)


if __name__ == "__main__":
    main()
