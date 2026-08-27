#!/usr/bin/env python3
"""A compose recreate waits long enough for the grace it grants (#2).

beta.21 gave the compose recreate `--timeout <docker_stop_timeout>` so a
slow container would stop on its own terms instead of being SIGKILLed
after Compose's 10s default. The grace was right. What it forgot is that
the subprocess wait around it stayed a flat 120 seconds — so the budget
left for pull, create and start fell from 110s to 60s, and a stack whose
container has to rejoin a VPN network namespace ran out of it:

    Command 'docker compose -f … -f … -p gluetun up -d --no-deps
    --force-recreate sabnzbd' timed out after 120 seconds

Measured against real Docker while fixing this: a container that ignores
SIGTERM spends the full 60s stopping under `--timeout 60`, and 10s
without it. The grace does not come out of thin air — it comes out of
the same 120 seconds.

The standalone path never had this problem because it waits
`effective_stop + 30` rather than a fixed number. This is the same idea:
whatever grace we grant is added to the wait, never subtracted from the
work.
"""
import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}
src = open(os.path.join(APP, "update_checker.py"), encoding="utf-8").read()

i = src.index("# Recreate service via compose.")
block = src[i:i + 2000]

checks["the compose recreate still grants the stop grace"] = (
    '"--timeout", str(stop_grace)' in block)
checks["…and the grace comes from docker_stop_timeout"] = (
    'stop_grace = int(getattr(self.config, "docker_stop_timeout"' in src)
checks["the wait around it is no longer a flat 120s"] = (
    "timeout=120)" not in block)
checks["…it contains the grace it granted"] = (
    "timeout=stop_grace + 120" in block)

# The arithmetic, stated as the property rather than the number: for any
# configured grace, the wait must exceed it by the working budget the
# path had before the grace existed.
m = re.search(r"timeout=stop_grace \+ (\d+)", block)
checks["the working budget survives"] = bool(m) and int(m.group(1)) >= 110

for grace in (10, 60, 300, 3600):
    wait = grace + int(m.group(1))
    checks[f"grace {grace}s leaves {wait - grace}s to work with"] = (
        wait - grace >= 110)

# The standalone path is where the shape came from; if it ever goes back
# to a fixed number this test should say so too.
checks["the standalone stop still sizes its own wait"] = (
    "subprocess_timeout = effective_stop + 30" in src)

for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
