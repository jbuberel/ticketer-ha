"""The trust settings that let the app reach the city's servers.

No network: these check the shipped certificate and the context built from it. The portal's own
server omits the intermediate that signs its certificate, so the app has to carry it.
"""

import datetime
import ssl

import pytest

from app.tls import CERT_DIR, verified_context

SHIPPED = sorted(CERT_DIR.glob("*.pem"))
GODADDY_G2 = "Go Daddy Secure Certificate Authority - G2"


def common_name(cert: dict) -> str:
    return dict(x for rdn in cert.get("subject", ()) for x in rdn).get("commonName", "")


def test_the_missing_intermediate_is_shipped():
    assert SHIPPED, f"no certificates in {CERT_DIR}; the portal can't be verified without them"


@pytest.mark.parametrize("pem", SHIPPED, ids=lambda p: p.name)
def test_shipped_certificate_is_not_near_expiry(pem):
    """A CA certificate that lapses takes 311 submission down with it. Fail with time to spare."""
    loaded = ssl._ssl._test_decode_cert(str(pem))
    expires = datetime.datetime.strptime(loaded["notAfter"], "%b %d %H:%M:%S %Y %Z")
    assert expires - datetime.datetime.now() > datetime.timedelta(days=90), (
        f"{pem.name} expires {expires:%Y-%m-%d}: replace it from the CA Issuers URL in the "
        f"portal's own certificate")


def test_context_trusts_the_intermediate_the_portal_omits():
    names = [common_name(c) for c in verified_context().get_ca_certs()]
    assert GODADDY_G2 in names


def test_context_still_verifies():
    """The intermediate is added to a verifying context, never in place of verification."""
    context = verified_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
