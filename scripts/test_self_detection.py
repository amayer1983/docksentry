#!/usr/bin/env python3
"""Robust self-container detection (#41, @NotRetarded).

On some hosts (e.g. QNAP Container Station) $HOSTNAME is not a reference
`docker inspect` can resolve — it reports "no such object". That broke
self-detection (Docksentry checked/updated itself via the regular flow) and
the self-update paths. resolve_own_id() must then fall back to scanning
running containers for one whose Config.Hostname matches $HOSTNAME.

subprocess is mocked — no Docker required. Exits non-zero on any failure.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import update_checker
from update_checker import UpdateChecker

FULL = "a" * 64           # our container's real full id
HOST = "e936e5995aac"     # $HOSTNAME — NOT inspect-resolvable in the QNAP case


class _Res:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _is_id_inspect(cmd):
    return cmd[:3] == ["docker", "inspect", "--format"] and cmd[3] == "{{.Id}}"


def _is_scan_inspect(cmd):
    return (cmd[:3] == ["docker", "inspect", "--format"]
            and "Config.Hostname" in cmd[3])


def main():
    os.environ["HOSTNAME"] = HOST
    orig = update_checker.subprocess.run
    checks = {}

    # ── Scenario A: QNAP — inspect-by-$HOSTNAME fails; fall back to scan ──
    def fake_qnap(cmd, **kw):
        if _is_id_inspect(cmd):
            return _Res(1, "")  # "no such object"
        if cmd[:2] == ["docker", "ps"]:
            return _Res(0, FULL + "\n" + ("b" * 64) + "\n")
        if _is_scan_inspect(cmd):
            return _Res(0, f"{'b'*64}|other\n{FULL}|{HOST}\n")
        return _Res(1, "")
    update_checker.subprocess.run = fake_qnap
    checks["QNAP: resolves via Config.Hostname fallback"] = UpdateChecker.resolve_own_id() == FULL

    # ── Scenario B: normal host — inspect-by-$HOSTNAME resolves directly ──
    def fake_normal(cmd, **kw):
        if _is_id_inspect(cmd):
            return _Res(0, FULL + "\n")
        raise AssertionError("normal host must not need the fallback scan")
    update_checker.subprocess.run = fake_normal
    checks["normal: resolves directly (no scan)"] = UpdateChecker.resolve_own_id() == FULL

    # ── Scenario C: no container matches → "" ──
    def fake_none(cmd, **kw):
        if cmd[:2] == ["docker", "ps"]:
            return _Res(0, ("c" * 64) + "\n")
        if _is_scan_inspect(cmd):
            return _Res(0, f"{'c'*64}|somethingelse\n")
        return _Res(1, "")
    update_checker.subprocess.run = fake_none
    checks["no match -> empty string"] = UpdateChecker.resolve_own_id() == ""

    update_checker.subprocess.run = orig

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
