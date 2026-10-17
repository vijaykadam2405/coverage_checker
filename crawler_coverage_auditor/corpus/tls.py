"""Verified Corpus TLS; an operator-supplied CA never disables hostname checks."""

import os
import ssl
from pathlib import Path

from .errors import CorpusUnavailable

ROOT = Path(__file__).resolve().parents[2]


def verification_context() -> bool | ssl.SSLContext:
    bundle = os.getenv("CRAWLSCOPE_CORPUS_CA_BUNDLE", "").strip()
    if not bundle:
        return True
    path = Path(bundle).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        # Add the explicitly trusted PEM to the system defaults. Never learn trust
        # by downloading a certificate from an unverified server during a scan.
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=str(path))
    except (OSError, ValueError, ssl.SSLError):
        raise CorpusUnavailable(
            "Corpus CA bundle could not be loaded. Set CRAWLSCOPE_CORPUS_CA_BUNDLE "
            "to a trusted PEM certificate bundle supplied by your API administrator."
        ) from None
    return context
