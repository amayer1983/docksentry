#!/usr/bin/env python3
"""Image-reference parsing and platform selection (cross-tool audit).

1. Digest-pinned references (`repo@sha256:...`) are the user explicitly
   freezing an image. Naive parsing split at the digest's colon, producing a
   garbage repository/tag whose registry call failed every cycle — the
   container looked like a permanently unreachable registry instead of a
   deliberate pin. They must parse as "not update-checkable".
2. Multi-arch version metadata used to hardcode linux/amd64; on ARM hosts the
   displayed "new version" came from the amd64 config. The platform manifest
   is now chosen by the daemon's own os/arch.

Pure logic plus one `docker version` call (skipped without a daemon).
Exits non-zero on any failure.
"""
import sys, os, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker


def main():
    checks = {}
    uc = UpdateChecker.__new__(UpdateChecker)

    p = uc._parse_image

    # ── digest pins: not update-checkable, never garbage ──
    checks["hub digest pin -> skipped"] = p("nginx@sha256:abc123") == (None, None, None)
    checks["ghcr digest pin -> skipped"] = p("ghcr.io/u/app@sha256:abc") == (None, None, None)
    checks["tag+digest pin -> skipped"] = p("redis:7@sha256:abc") == (None, None, None)
    checks["bare image ID -> skipped"] = p("sha256:deadbeef") == (None, None, None)

    # ── normal references still parse exactly as before ──
    checks["bare official"] = p("nginx") == ("registry-1.docker.io", "library/nginx", "latest")
    checks["user/repo:tag"] = p("linuxserver/radarr:latest") == (
        "registry-1.docker.io", "linuxserver/radarr", "latest")
    checks["ghcr"] = p("ghcr.io/u/app:1.2") == ("ghcr.io", "u/app", "1.2")
    checks["port registry"] = p("registry:5000/team/app:1.2.3") == (
        "registry:5000", "team/app", "1.2.3")
    checks["localhost registry"] = p("localhost/app:dev") == ("localhost", "app", "dev")

    # ── platform is per HOST, not per process (#7, @LeeNX) ──────────
    # This cache used to be one process-wide value. Fine with one daemon,
    # silently wrong with two: the first host to ask cached its arch and
    # every other host reused it, so an arm64 box in a mixed fleet got
    # compared against amd64 digests and the wrong verdict on every
    # multi-arch image.
    import types as _t
    from update_checker import UpdateChecker as _UC

    class _CP:
        def __init__(self, out):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    _outs = {"x86": "linux/amd64", "pi": "linux/arm64", "quiet": ""}

    class _B:
        def __init__(self, k):
            self.name = k

        def run(self, args, **kw):
            return _CP(_outs[self.name])

    _cfg = _t.SimpleNamespace(debug=False, container_cli="docker")
    _UC._host_platform_cache = {}
    _x = _UC(_cfg, backend=_B("x86"))._host_platform()
    _p = _UC(_cfg, backend=_B("pi"))._host_platform()
    checks["platform: each host gets its own"] = _x != _p
    checks["platform: amd64 host reads amd64"] = _x == ("linux", "amd64")
    checks["platform: arm64 host reads arm64"] = _p == ("linux", "arm64")
    checks["platform: still cached per host"] = (
        _UC(_cfg, backend=_B("pi"))._host_platform() is _p)
    # A daemon that says nothing still falls back rather than crashing.
    checks["platform: silent daemon falls back"] = (
        _UC(_cfg, backend=_B("quiet"))._host_platform() == ("linux", "amd64"))
    _UC._host_platform_cache = {}

    # ── host platform: sane shape, cached, daemon-agreeing when available ──
    plat = uc._host_platform()
    checks["platform is (os, arch) pair"] = (
        isinstance(plat, tuple) and len(plat) == 2 and all(plat))
    checks["platform cached (same object)"] = uc._host_platform() is plat
    r = subprocess.run(["docker", "version", "--format",
                        "{{.Server.Os}}/{{.Server.Arch}}"],
                       capture_output=True, text=True)
    if r.returncode == 0 and "/" in r.stdout:
        want = tuple(r.stdout.strip().split("/"))
        checks["platform matches daemon"] = plat == want
    else:
        checks["platform falls back to linux/amd64"] = plat == ("linux", "amd64")

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
