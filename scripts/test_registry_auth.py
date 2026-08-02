#!/usr/bin/env python3
"""Registry credential matching, and compose files behind one label.

Both came out of sweeping the issue history of comparable tools
(watchtower#376, dockcheck#27) and both were reproduced here before being
fixed — the tests below encode the reproductions, not the hypotheses.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker, _auth_host


def _config(auths):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".docker"), exist_ok=True)
    with open(os.path.join(d, ".docker", "config.json"), "w") as f:
        json.dump({"auths": auths}, f)
    return os.path.join(d, ".docker")


def main():
    checks = {}

    # ── host normalisation ────────────────────────────────────────
    # Docker Hub answers to four names: `docker login` writes the legacy v1
    # index URL while image refs resolve to registry-1. Without folding
    # them together, the one credential nearly everyone has is never found.
    for entry in ("docker.io", "index.docker.io", "registry-1.docker.io",
                  "https://index.docker.io/v1/", "registry.hub.docker.com"):
        checks[f"hub alias: {entry}"] = _auth_host(entry) == "registry-1.docker.io"
    checks["scheme is stripped"] = _auth_host("https://ghcr.io") == "ghcr.io"
    checks["path is stripped"] = _auth_host("ghcr.io/owner") == "ghcr.io"
    checks["case is folded"] = _auth_host("GHCR.io") == "ghcr.io"
    # And the half that matters most: two hosts that merely share a suffix
    # are NOT the same host.
    checks["eu.gcr.io is not gcr.io"] = _auth_host("eu.gcr.io") != _auth_host("gcr.io")

    # ── the leak, as it was measured ──────────────────────────────
    # Before the fix, `registry in key or key in registry` handed one
    # registry's Basic-Auth header to a different operator entirely.
    auth_dir = _config({
        "eu.gcr.io": {"auth": "RVUtT05MWTpTRUNSRVQ="},          # EU-ONLY:SECRET
        "myregistry.example.com": {"auth": "UFJJVkFURTpQVw=="},  # PRIVATE:PW
        "https://index.docker.io/v1/": {"auth": "SFVCOlRPS0VO"}, # HUB:TOKEN
    })
    os.environ["DOCKER_CONFIG"] = auth_dir
    o = UpdateChecker.__new__(UpdateChecker)
    get = lambda r: UpdateChecker._get_docker_credentials(o, r)

    checks["own registry still authenticates"] = get("eu.gcr.io") == "RVUtT05MWTpTRUNSRVQ="
    # These two are the leak. A parent domain must never inherit a
    # subdomain's credentials, in either direction.
    checks["gcr.io does NOT get eu.gcr.io's secret"] = get("gcr.io") is None
    checks["example.com does NOT get the private registry's secret"] = (
        get("example.com") is None)
    checks["an unrelated registry gets nothing"] = get("ghcr.io") is None
    # And the case the old substring match was written for, which it never
    # actually handled: neither string contains the other.
    checks["docker hub resolves through its aliases"] = (
        get("registry-1.docker.io") == "SFVCOlRPS0VO")
    checks["docker.io resolves too"] = get("docker.io") == "SFVCOlRPS0VO"

    # An entry with no `auth` must not shadow anything.
    os.environ["DOCKER_CONFIG"] = _config({"ghcr.io": {}})
    checks["credsStore-style empty entry yields nothing"] = (
        UpdateChecker._get_docker_credentials(o, "ghcr.io") is None)

    # ── compose files behind one label ────────────────────────────
    # Docker joins multiple compose files into ONE comma-separated label
    # value. Treated as a single path it fails isfile(), and the stack
    # silently drops out of the compose path into `docker run` recreate —
    # on exactly the override-file setup the Compose docs recommend.
    f = UpdateChecker._compose_files
    d = tempfile.mkdtemp()
    a = os.path.join(d, "docker-compose.yml")
    b = os.path.join(d, "docker-compose.override.yml")
    for p in (a, b):
        open(p, "w").close()
    checks["single file is unchanged"] = f(a) == [a]
    checks["two files are split"] = f(f"{a},{b}") == [a, b]
    checks["order is preserved"] = f(f"{b},{a}") == [b, a]
    checks["whitespace around commas survives"] = f(f"{a} , {b}") == [a, b]
    checks["empty label yields nothing"] = f("") == []
    # A path may legitimately contain a comma and the label format cannot
    # say so. Splitting then would deploy from the wrong file — worse than
    # the fallback it replaces — so an unresolvable split is not trusted.
    comma = os.path.join(d, "we,ird.yml")
    open(comma, "w").close()
    checks["a comma inside a real path is not split"] = f(comma) == [comma]
    checks["a partly-missing split is not trusted"] = (
        f(f"{a},{d}/does-not-exist.yml") == [f"{a},{d}/does-not-exist.yml"])

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
