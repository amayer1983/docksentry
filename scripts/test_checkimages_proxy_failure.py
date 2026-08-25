#!/usr/bin/env python3
"""A host whose `system df` is blocked reports "could not check", not "clean".

`/checkimages` (dry-run for `/cleanup`) reads `docker system df`. A
tecnativa socket-proxy — the RECOMMENDED remote anbindung — blocks
`/system/df` by default (needs SYSTEM=1) and answers HTTP 403, so the CLI
exits 1. The old path swallowed that to `{}` → 0 bytes → "nothing to
reclaim", a positive false claim on a host it never actually measured
(measured live on a real proxy host, 2026-08-25).

Now `reclaimable_breakdown` RAISES on a failed `system df`, so the
human-facing `container_flags.reclaimable` reports the host as unreadable
(`host_check_failed`, ok=False) — while `reclaimable_bytes`, which feeds
the auto-cleanup disk warning, still swallows it back to 0 ("don't act").
A run that succeeds with nothing reclaimable is still a real, honest 0.
"""
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import update_checker                               # noqa: E402
from update_checker import UpdateChecker            # noqa: E402
import container_flags                              # noqa: E402


class _Res:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


checks = {}

# ── UpdateChecker level: raise vs swallow ─────────────────────────────────
orig = update_checker.subprocess.run
try:
    chk = UpdateChecker(types.SimpleNamespace(debug=False))

    # 403 from a socket-proxy: CLI exits 1 with a forbidden body.
    update_checker.subprocess.run = lambda *a, **k: _Res(
        1, "", "Error response from daemon: 403 Forbidden")
    raised = False
    try:
        chk.reclaimable_breakdown()
    except Exception as e:
        raised = True
        # the message points at the actual cause (proxy / SYSTEM=1)
        checks["breakdown raise mentions the proxy hint"] = "SYSTEM=1" in str(e)
    checks["breakdown RAISES on a failed system df"] = raised
    # …but the auto-cleanup figure still degrades to 0 (don't act), no raise
    checks["reclaimable_bytes swallows the failure to 0"] = (
        chk.reclaimable_bytes() == 0)

    # A genuine success with nothing reclaimable stays an honest 0, no raise.
    update_checker.subprocess.run = lambda *a, **k: _Res(
        0, '{"Type":"Images","Reclaimable":"0B (0%)"}')
    checks["success-with-zero does NOT raise"] = (
        chk.reclaimable_breakdown().get("images", 0) == 0)
finally:
    update_checker.subprocess.run = orig


# ── container_flags.reclaimable: failure → host_check_failed, not "none" ──
class BlockedChecker:
    """`system df` blocked (socket-proxy 403)."""
    def reclaimable_bytes(self):
        return 0                                     # swallowed, as in prod
    def reclaimable_breakdown(self):
        raise RuntimeError("`system df` blocked (HTTP 403) — needs SYSTEM=1")


class OkChecker:
    def __init__(self, images):
        self._img = images
    def reclaimable_bytes(self):
        return self._img
    def reclaimable_breakdown(self):
        return {"images": self._img}


# blocked host → ok=False host_check_failed (NOT chan_reclaim_none)
replies, total = container_flags.reclaimable(
    [None], checker_for=lambda h: BlockedChecker())
checks["blocked host: exactly one reply"] = len(replies) == 1
r = replies[0]
checks["blocked host: reply is host_check_failed"] = r.key == "host_check_failed"
checks["blocked host: reply is not ok"] = (r.ok is False)
checks["blocked host: NOT reported as 'nothing to reclaim'"] = (
    r.key != "chan_reclaim_none")

# real reclaimable → chan_reclaim_some, ok=True, correct figure
replies2, total2 = container_flags.reclaimable(
    [None], checker_for=lambda h: OkChecker(23 * 1000 * 1000))
r2 = replies2[0]
checks["reclaimable host: chan_reclaim_some"] = r2.key == "chan_reclaim_some"
checks["reclaimable host: ok"] = (r2.ok is not False)
checks["reclaimable host: figure carried through"] = total2 == 23 * 1000 * 1000

# genuine zero still reads as 'nothing', honestly (ok=True)
replies3, _ = container_flags.reclaimable(
    [None], checker_for=lambda h: OkChecker(0))
r3 = replies3[0]
checks["genuine zero: chan_reclaim_none"] = r3.key == "chan_reclaim_none"
checks["genuine zero: still ok (not a failure)"] = (r3.ok is not False)


for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
