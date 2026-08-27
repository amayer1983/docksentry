#!/usr/bin/env python3
"""The compose recreate is not bounded by a flat two minutes (#2).

`docker compose up -d --force-recreate` ran under a fixed 120-second
wait. That wait exists to stop a hung command from blocking the update
loop forever — it was never meant to bound normal work, and at 120
seconds it was doing the second job badly: the number never scaled with
anything, and a service that has to rejoin a VPN network namespace on
start runs past it.

@famewolf's gluetun stack reported `timed out after 120 seconds`. That
is OUR message, not Docker's — the subprocess timeout we set. Which is
what makes this measurable rather than a theory: whatever else was slow,
the thing that ended the run was our own number.

The pull beside it has always been allowed 1800s for the same reason a
recreate now gets more than 120: a slow operation is not a stuck one.
"""
import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}
src = open(os.path.join(APP, "update_checker.py"), encoding="utf-8").read()

# The whole compose update method, not just the recreate: the pull sits
# ABOVE the recreate in it, and a window that started at the recreate
# would miss the precedent it is compared against.
_i = src.index("    def _update_compose(")
block = src[_i:src.index("\n    def ", _i + 10)]

checks["the compose recreate was found"] = "--force-recreate" in block
checks["it is no longer bounded by a flat 120s"] = "timeout=120)" not in block

m = re.search(r"result = self\.backend\.run\(up_cmd\[1:\], timeout=(\d+)\)", block)
checks["the wait is a number we can read"] = bool(m)
checks["…and it is generous enough to be about hangs, not work"] = (
    bool(m) and int(m.group(1)) >= 300)

# The pull next to it is the precedent, not an outlier: this file has
# always allowed a slow pull far more than two minutes.
checks["a compose pull keeps its own generous wait"] = "timeout=1800" in block

# Deliberately NOT here: the stop grace `--timeout` the 2.18 line adds.
# That changes how long a container gets to stop — behaviour, not a
# fix — and a patch on a stable release is not where it belongs.
checks["no stop grace is smuggled into a patch release"] = (
    '"--timeout"' not in block)

for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
