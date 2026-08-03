#!/usr/bin/env python3
"""A registry behind the operator's own CA (wud#604, wud#111, wud#52).

Three reports of the same shape in one corpus: a private registry with a
real certificate signed by a CA the operator runs. `docker pull` works —
the daemon reads `/etc/docker/certs.d/<host>/ca.crt` — and the checker
said "TLS error" forever. That is the confusing pair of facts this project
has hit before: the pull side works, the check side does not, and nothing
says why.

It turns out nothing was missing. Python honours `SSL_CERT_FILE`, so the
setup already worked — nobody could discover it. The message stopped at
"TLS error", which reads as *unsupported*, and the one setting the docs
did offer, `INSECURE_REGISTRIES`, is actively the wrong answer here: it
drops TLS, so against a TLS-only port it fails outright and against a port
that does answer HTTP it puts the Basic credentials on the wire in clear.

Measured end to end against a self-signed `registry:2` before this was
written:

    without SSL_CERT_FILE   TLS error (CERTIFICATE_VERIFY_FAILED)
    with INSECURE_REGISTRIES  HTTP 400   (downgraded to http, TLS-only port)
    with SSL_CERT_FILE      digest sha256:7dc2e94… + tags ['latest']

So this asserts the part that failed: that the error names the remedy, and
that `INSECURE_REGISTRIES` is not silently treated as one.
"""

import os
import ssl
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker, registry_scheme


def main():
    checks = {}
    d = UpdateChecker._describe_registry_error

    # ── the message points somewhere ──────────────────────────────
    verify_fail = urllib.error.URLError(
        ssl.SSLCertVerificationError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self-signed certificate (_ssl.c:1000)"))
    msg = d(verify_fail)
    checks["a verify failure names SSL_CERT_FILE"] = "SSL_CERT_FILE" in msg
    # Naming the remedy is only half of it. Someone reading "TLS error" next
    # to a documented INSECURE_REGISTRIES setting reaches for that setting,
    # and it is both ineffective here and a downgrade to clear text.
    checks["…and warns off INSECURE_REGISTRIES"] = "INSECURE_REGISTRIES" in msg

    # Other TLS failures are not verify failures and must not claim a CA
    # would fix them — a handshake or protocol error has a different cause.
    other = d(urllib.error.URLError(ssl.SSLError("[SSL] wrong version number")))
    checks["an unrelated TLS error keeps its own message"] = (
        "SSL_CERT_FILE" not in other and "TLS" in other)

    # And the neighbouring cases stay put.
    for exc, want in (
        (_http(429), "rate limited"),
        (_http(401), "not authorised"),
        (_http(404), "not found"),
    ):
        checks[f"HTTP {exc.code} unchanged"] = want in d(exc)

    # ── INSECURE_REGISTRIES is still only what it says ───────────
    # It must not have grown into a general "TLS problems" escape hatch:
    # it drops encryption, and reaching for it to solve a *trust* problem
    # is the mistake the message now heads off.
    checks["a private-CA host is not implicitly insecure"] = (
        registry_scheme("registry.internal", []) == "https")
    checks["only a named host goes plain"] = (
        registry_scheme("registry.internal", ["registry.internal"]) == "http")

    # ── the trust store the docs promise ─────────────────────────
    # The docs say a private CA in SSL_CERT_FILE does not cost you the
    # public roots, because the hashed CApath is still consulted. If that
    # ever stops being true the advice becomes actively harmful, so it is
    # asserted rather than believed.
    paths = ssl.get_default_verify_paths()
    checks["a hashed CA directory exists to fall back on"] = bool(paths.capath)

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    if failed:
        print(f"    message was: {msg}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


def _http(code):
    return urllib.error.HTTPError("https://reg/v2/", code, "x", {}, None)


if __name__ == "__main__":
    sys.exit(main())
