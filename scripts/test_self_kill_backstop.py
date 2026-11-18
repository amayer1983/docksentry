#!/usr/bin/env python3
"""An update that names Docksentry's own container stops nothing (#16).

`get_running_containers` drops Docksentry from every scan, so the
scheduled check never offers to update it. But a caller that does not
start from a scan — the Web UI's single-update button, a bot callback,
anything added later — goes straight to `update_container` with a name.
For that, `update_container` has a second guard: it compares full
container IDs and refuses before anything is stopped.

The audit for 2.19.1 measured that this second guard held nothing in the
suite: with `if self._would_kill_self(name):` turned into `if False:`,
every procedure stayed green. Thirteen procedures mentioned
`update_container` before this one (grep): eleven define a stand-in for
it, test_compose_fallback_precise.py reads its source, and only
test_monitor_only.py calls the real one — with `_would_kill_self`
replaced by `lambda n: False`.

Driven here through the path such a caller takes: the real
`UpdateEngine._process_update_batch` with `auto=False`, a real
`UpdateChecker`, and only the daemon stood in for — a `DockerBackend`
whose `run()` records every argv instead of starting `docker`. It knows
two containers by ID, answers the pull and the container inspect, and
refuses to stop anything, so a recreate that gets past the guard goes
as far as issuing `stop` and `kill`, and no further. Our own ID is
handed in the way `/proc/self/cgroup` would give it.

Counter-direction: the same single update for another container does
reach `stop` — so "nothing was stopped" above is the guard, not a
stand-in that never gets that far. Exits non-zero on any failure.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import container_backend                                    # noqa: E402
from config import Config                                    # noqa: E402
from container_store import ContainerStore                   # noqa: E402
from update_checker import UpdateChecker                     # noqa: E402
from update_engine import UpdateEngine                       # noqa: E402

OWN = "a" * 64          # our container's full ID
OTHER = "b" * 64
IDS = {"docksentry": OWN, "web": OTHER}
TOUCHING = ("stop", "kill", "rm", "rename", "create", "run", "start")

checks = {}


class Daemon(container_backend.DockerBackend):
    """`docker`, without the daemon: records, answers, never stops."""

    def __init__(self):
        self.calls = []

    def run(self, args, **kw):
        args = list(args)
        self.calls.append(args)

        def ok(out=""):
            return subprocess.CompletedProcess(args, 0, out, "")

        if args[:3] == ["inspect", "--format", "{{.Id}}"] and args[3] in IDS:
            return ok(IDS[args[3]] + "\n")
        if args[:1] == ["pull"]:
            return ok("pulled")
        if args[:1] == ["inspect"] and len(args) == 2 and args[1] in IDS:
            return ok(json.dumps([{
                "Name": "/" + args[1], "Id": IDS[args[1]],
                "Image": "sha256:" + "c" * 64,
                "Config": {"Image": "img:1", "Env": [], "Labels": {}},
                "HostConfig": {"NetworkMode": "bridge",
                               "RestartPolicy": {"Name": "no"}},
                "NetworkSettings": {"Networks": {}}, "Mounts": [],
                "State": {"Status": "running"}}]))
        return subprocess.CompletedProcess(args, 1, "", "stand-in: refused")


def single_update(name):
    """One manual update of `name`, the way the Web UI button runs it."""
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    try:
        cfg = Config.from_env()
    finally:
        os.environ.pop("DATA_DIR", None)
    daemon = Daemon()
    checker = UpdateChecker(cfg, backend=daemon)
    checker._own_id_cache = OWN
    engine = UpdateEngine(cfg, ContainerStore(cfg))
    with contextlib.redirect_stdout(io.StringIO()):
        results, ok_count, _major = engine._process_update_batch(
            [{"name": name, "image": "img:1", "host": "local"}],
            checker, auto=False)
    touched = [c for c in daemon.calls if c[:1] and c[0] in TOUCHING]
    pulled = [c for c in daemon.calls if c[:1] == ["pull"]]
    return results, ok_count, touched, pulled


res, n, touched, pulled = single_update("docksentry")
checks["its own container: nothing is stopped, killed, renamed or removed"] = (
    touched == [])
checks["…not even pulled for"] = pulled == []
checks["…and the answer says why and where to go instead"] = (
    n == 0 and any("/selfupdate" in r for r in res))

res2, n2, touched2, pulled2 = single_update("web")
checks["another container: the same single update reaches the stop"] = (
    any(c[:1] == ["stop"] and c[-1] == "web" for c in touched2))
checks["…after pulling its image"] = pulled2 == [["pull", "img:1"]]
checks["…and nothing it did was aimed at Docksentry"] = not any(
    "docksentry" in c for c in touched2)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
if bad:
    print(f"      own: {res} touched={touched} pulled={pulled}")
    print(f"      web: {res2} touched={touched2}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
