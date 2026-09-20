"""Trust settings for the city's hosts.

The 311 portal serves only its leaf certificate. The intermediate that signed it ("Go Daddy
Secure Certificate Authority - G2") is never sent, so a client has to already hold that
certificate to build a chain up to the root. Browsers hide the omission by fetching the missing
certificate from the URL inside the leaf, and macOS quietly reuses intermediates it has seen
before. Python's ssl module does neither, so inside the container verification fails with
"unable to get local issuer certificate" and no request ever reaches 311.

The missing intermediate is shipped in `certs/` and added to the default trust store here.
Nothing is loosened by this: certificates are still verified, hostnames still checked, and the
certificate being added is a public CA that already chains to a root every trust store carries.
It expires 2031-05-03; `tests/test_tls.py` fails well before then.
"""

import functools
import ssl
from pathlib import Path

CERT_DIR = Path(__file__).parent / "certs"


@functools.cache
def verified_context() -> ssl.SSLContext:
    """A normal verifying context, plus the intermediates the city's servers don't send."""
    context = ssl.create_default_context()
    try:
        import certifi  # arrives with the Anthropic client; the container may have no system store
        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    for pem in sorted(CERT_DIR.glob("*.pem")):
        context.load_verify_locations(cafile=str(pem))
    return context
