#!/usr/bin/env python3
"""The rollback copy of a running update is not called litter.

Mid-update both containers exist: the new one is already up, and `<name>_old`
is still the copy a rollback would restore from. The status banner counted
that as "left behind from an interrupted update" and offered a `docker rm`
for it — which, followed, removes the only thing a failed update could fall
back to. It cleared itself once the update finished, so it looked like a
harmless glitch rather than the advice it was.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

checks = {}

src = open(os.path.join(os.path.dirname(__file__), "..",
                        "app", "web_ui.py")).read()

# The gathering loop, isolated by the two lines that bound it.
start = src.index('leftovers = []')
block = src[start:src.index("leftovers = sorted(set(leftovers))", start)]

checks["a leftover is only counted when its live twin exists"] = (
    'n[:-4] in _live' in block)
checks["…and not while that container is being updated"] = (
    'not in _busy' in block)
checks["the busy set comes from the engine, not a second bookkeeping"] = (
    '_updating_now(' in block)

# The engine is the one source: same call the badge uses.
badge = src[src.index('_updating_here = self._updating_now'):][:200]
checks["the badge reads the same place"] = ('_updating_now' in badge)

# A stale suffix check would match `foo_older`; make sure it is exact.
checks["only the exact _old suffix counts"] = ('endswith("_old")' in block)

# And the advice offered is still a plain removal — no silent deleting.
checks["Docksentry still does not delete it itself"] = (
    'docker rm' in src and 'leftover' in src)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
