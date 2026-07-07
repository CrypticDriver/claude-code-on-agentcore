"""Shared SSL context for the deploy scripts' stdlib HTTP calls.

python.org builds of Python on macOS ship without CA certs wired up (until
the user runs "Install Certificates.command"), so ssl.create_default_context()
verifies against an empty store and every TLS call fails. When that happens,
fall back to certifi's bundle or the one botocore ships — boto3 is a hard
prerequisite, so at least one is always available.
"""
from __future__ import annotations

import os
import ssl


def make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()["x509_ca"] > 0:
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    import botocore
    bundle = os.path.join(os.path.dirname(botocore.__file__), "cacert.pem")
    if os.path.exists(bundle):
        return ssl.create_default_context(cafile=bundle)
    return ctx


SSL_CONTEXT = make_ssl_context()
