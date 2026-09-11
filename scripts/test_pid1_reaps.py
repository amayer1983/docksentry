#!/usr/bin/env python3
"""We are PID 1 and do not reap — knowingly, for now.

Running as PID 1 means inheriting every orphaned process in the
container, and the ssh masters that `ControlPersist` backgrounds are
exactly that. Measured on a live four-host install: **12074 defunct
`ssh` processes**, 9839 of them in a single hour, until it could not
fork and every host failed at once with `pthread_create failed:
Resource temporarily unavailable` — the `tcp://` ones included.

An init as ENTRYPOINT fixes it, and 2.17.10 shipped that. It had to be
taken back out: changing ENTRYPOINT was the first time a Docksentry
image ever did, and the update path could not survive it (see
test_entrypoint_survives_update.py). The container came back running a
bare `python3` and restarted forever.

So the leak is still here, on purpose, and this file is what keeps that
from being forgotten. It comes back once people are ON a version whose
update survives an ENTRYPOINT change — never in the same release that
teaches it to, because the update INTO that release runs the old code.

Affects `ssh://` endpoints only. `tcp://`, `context://` and single-host
installs never create the orphans.
"""
import os
import re
import sys

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")
DOCKERFILE = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()

# The state we are deliberately in, asserted so that changing it is a
# decision someone makes rather than something that drifts.
entry = re.search(r'^ENTRYPOINT\s+(\[.*\])\s*$', DOCKERFILE, re.M)
checks["the image has an ENTRYPOINT"] = entry is not None
line = entry.group(1) if entry else ""
checks["it starts the app directly, with no init in front"] = (
    '"python3"' in line and "tini" not in line)
checks["…and the reason that is so is written down"] = (
    "tini" in DOCKERFILE and "12074" in DOCKERFILE)

# The one thing that must never be the answer instead. A reaper calling
# waitpid(-1) races `subprocess.run()` for its children and can take an
# exit status out from under it — an update would then report a success
# it never had, which is worse than the leak it set out to fix.
app = os.path.join(ROOT, "app")
loose = []
for name in sorted(os.listdir(app)):
    if not name.endswith(".py"):
        continue
    src = open(os.path.join(app, name), encoding="utf-8").read()
    if "SIGCHLD" in src or re.search(r"waitpid\(\s*-1", src):
        loose.append(name)
checks["no home-made reaper races subprocess for its children"] = loose == []

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
