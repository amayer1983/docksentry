#!/usr/bin/env python3
"""Docksentry is PID 1, so it inherits every orphan — and must reap them.

Measured on a live four-host install running 2.17.9: **12074 defunct
`ssh` processes**, 12073 of them parented to our own `python3` as PID 1,
9839 of them accumulated in a single hour. The container could no longer
`fork()`, so every host failed at once with

    could not list containers (rc=2): runtime/cgo: pthread_create
    failed: Resource temporarily unavailable

including the two `tcp://` hosts and the local socket, which have
nothing to do with ssh. Nothing was wrong with those hosts; the side
asking them had run out of process slots.

Where the orphans come from: `ControlPersist` puts an ssh master into
the background so the next `docker -H ssh://…` reuses the connection.
When the docker client exits, that master is reparented to PID 1 — to
us — and Python never waits for a child it did not start. One zombie per
connection, held forever, each holding a PID.

Why an init and not a handler of our own: a reaper calling
`waitpid(-1)` races `subprocess.run()` for its children and can take an
exit status out from under it. An update would then report a success it
never had, which is a worse failure than the one being fixed.

Measured against this image, 200 backgrounded orphans: without the init
200 zombies remained, with it 0.
"""
import os
import re
import sys

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")
DOCKERFILE = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()

entry = re.search(r'^ENTRYPOINT\s+(\[.*\])\s*$', DOCKERFILE, re.M)
checks["the image has an ENTRYPOINT"] = entry is not None
line = entry.group(1) if entry else ""

# PID 1 has to be something whose job is reaping. Ours is not: it is a
# Python process busy talking to Docker.
checks["PID 1 is an init, not the app"] = "tini" in line
checks["…and it comes first in the entrypoint"] = (
    line.index("tini") < line.index("python3") if
    ("tini" in line and "python3" in line) else False)
# `--` so tini treats the rest as the command, not as its own flags.
checks["…and hands the rest over as the command"] = '"--"' in line
checks["the app is still what runs"] = "/app/main.py" in line

# An entrypoint naming a binary the image does not install fails at
# start, which is the one failure mode worse than the leak.
checks["the init is actually installed"] = re.search(
    r'^RUN apk add[^\n]*\btini\b', DOCKERFILE, re.M) is not None

# The reason has to survive in the file, or the next person removes the
# init as an unexplained extra hop.
checks["why it is there is written down"] = (
    "orphan" in DOCKERFILE.lower() and "12074" in DOCKERFILE)

# And the counterpart: nobody adds a signal handler that fights
# subprocess for the same children.
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
