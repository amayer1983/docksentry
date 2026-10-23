#!/usr/bin/env python3
"""Whatever CI runs, the local suite runs too.

CI is one step: `python3 scripts/pre-commit-check.py`. That file is not
named `test_*`, so every local sweep over `scripts/test_*.py` walked
straight past it — and on 23.10.2026 a change that was green across 211
local procedures turned CI red, twice, for a reason no local run could
have surfaced: a new string existed in `en` and `de` and in none of the
other fourteen language files.

Nothing was wrong with the check. It was simply the one gate that only
ran somewhere else, which is the same shape as the queue bug this
release fixes: a thing that works, reachable from only one direction.

So it runs here as well. This procedure is a thin wrapper on purpose —
it owns no rules of its own, it just makes the existing gate
unforgettable. If `pre-commit-check.py` grows a new check, this picks it
up for free.
"""

import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
GATE = os.path.join(ROOT, "scripts", "pre-commit-check.py")

checks = {}

checks["the gate CI runs exists"] = os.path.isfile(GATE)

# It has to be the same file CI invokes, named the same way. A wrapper
# pointed at a copy would pass while the real gate rotted.
ci = os.path.join(ROOT, ".github", "workflows", "ci.yml")
checks["CI still invokes it by that path"] = (
    os.path.isfile(ci)
    and "scripts/pre-commit-check.py" in open(ci).read())

if checks["the gate CI runs exists"]:
    r = subprocess.run([sys.executable, GATE], cwd=ROOT,
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                       timeout=900)
    checks["…and it passes"] = r.returncode == 0
    if r.returncode != 0:
        # Its own output, not a summary of it: whoever reads a red run
        # here needs the same lines CI would have shown them.
        tail = (r.stdout + r.stderr).strip().splitlines()
        print("\n  ── pre-commit-check.py sagt ──")
        for line in tail[-25:]:
            print("  " + line)
        print("  ──────────────────────────────\n")
else:
    checks["…and it passes"] = False

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
