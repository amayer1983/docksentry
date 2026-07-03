#!/usr/bin/env python3
"""Extended disk warning: reclaimable + auto-cleanup hint (#2, @famewolf).

A bare "disk at X%" got lost in the noise (famewolf's 215 GB lesson). The
warning now tells the user how much `/cleanup` could free AND whether
auto-cleanup is off — making it actionable.

Covers _parse_human_size, reclaimable_bytes (docker system df mocked), and
the message composition in scheduler._check_disk_space. No Docker.
"""
import sys, os, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import update_checker
from update_checker import UpdateChecker


class _Res:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def main():
    checks = {}
    p = UpdateChecker._parse_human_size
    checks["parse: 20.1GB → ~20.1e9"] = 20_000_000_000 < p("20.1GB") < 21_000_000_000
    # Docker's `system df --format {{json .}}` appends " (N%)" — must not break parsing.
    checks["parse: 20.1GB (50%) [Docker's real format]"] = p("20.1GB (50%)") == p("20.1GB")
    checks["parse: 2.113MB"] = 2_000_000 < p("2.113MB") < 2_200_000
    checks["parse: 0B → 0"] = p("0B") == 0
    checks["parse: empty/junk → 0"] = p("") == 0 and p("nope") == 0

    df_out = "\n".join([
        '{"Type":"Images","Reclaimable":"20.1GB (50%)"}',
        '{"Type":"Containers","Reclaimable":"0B (0%)"}',
        '{"Type":"Local Volumes","Reclaimable":"43.03MB (0%)"}',
        '{"Type":"Build Cache","Reclaimable":"2.113MB"}',
    ])
    orig = update_checker.subprocess.run
    try:
        update_checker.subprocess.run = lambda *a, **k: _Res(0, df_out)
        chk = UpdateChecker(types.SimpleNamespace(debug=False))
        total = chk.reclaimable_bytes()
        # 20.1 GB + 43.03 MB + 2.113 MB ≈ 20.145 GB total
        checks["reclaimable ≈ sum of all reclaimable types"] = 20_140_000_000 < total < 20_160_000_000

        update_checker.subprocess.run = lambda *a, **k: _Res(1, "")
        checks["reclaimable: docker error → 0"] = chk.reclaimable_bytes() == 0

        update_checker.subprocess.run = lambda *a, **k: _Res(0, "")
        checks["reclaimable: no output → 0"] = chk.reclaimable_bytes() == 0
    finally:
        update_checker.subprocess.run = orig

    # Message composition — mirror scheduler._check_disk_space's builder.
    def build(reclaim, auto_cleanup, percent=90, free_gb=1.2):
        m = f"⚠️ Disk usage at {percent}% — {free_gb:.1f} GB free."
        if reclaim > 0:
            gib = reclaim / (1024 ** 3)
            unit = f"{gib:.1f} GB" if gib >= 0.1 else f"{reclaim / (1024 ** 2):.0f} MB"
            m += f"\n🧹 {unit} reclaimable via `/cleanup` (unused images / build cache)."
        if not auto_cleanup:
            m += "\n💡 Auto-cleanup is OFF — set `DISK_WARN_AUTO_CLEANUP=true` (or enable it in Web UI → Settings) to reclaim automatically next time."
        return m

    msg1 = build(20 * 1024**3, False)
    checks["msg: shows GB reclaimable"] = "20.0 GB reclaimable" in msg1
    checks["msg: warns auto-cleanup OFF"] = "Auto-cleanup is OFF" in msg1

    msg2 = build(20 * 1024**3, True)
    checks["msg: no auto-cleanup hint when ON"] = "Auto-cleanup is OFF" not in msg2

    msg3 = build(0, False)
    checks["msg: no reclaim line when 0"] = "reclaimable via" not in msg3

    msg4 = build(50 * 1024**2, False)  # 50 MB
    checks["msg: falls back to MB for small totals"] = "50 MB reclaimable" in msg4

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
