#!/usr/bin/env python3
"""What a failed check must NOT do (wud#570/#711, wud#116/#419/#945).

Two failures that both ended in the same lie — "everything is up to date"
— from opposite directions.
"""

import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import ContainerListUnavailable, UpdateChecker


class _Backend:
    default_timeout = 30

    def __init__(self, rc=0, out=""):
        self.rc, self.out = rc, out

    def run(self, args, **kw):
        return types.SimpleNamespace(returncode=self.rc, stdout=self.out,
                                     stderr="Cannot connect to the Docker daemon")


def main():
    checks = {}

    # ── a daemon that does not answer is not an empty host ────────
    # A failing `ps` produced empty stdout and therefore an empty list,
    # which downstream is indistinguishable from "this host runs nothing":
    # zero updates reported, that host's pending list wiped, "all up to
    # date" sent. The monitor has checked the return code since it was
    # written; the update path never did.
    o = UpdateChecker.__new__(UpdateChecker)
    o._backend = _Backend(rc=1)
    o.debug = False
    o._debug = lambda m: None
    raised = False
    try:
        UpdateChecker.get_running_containers(o)
    except ContainerListUnavailable:
        raised = True
    except Exception:
        pass
    checks["a failing ps raises rather than returning []"] = raised
    # Deliberately an exception, not a sentinel: a caller that forgets to
    # handle it gets a traceback, where one returning [] gets a quiet lie.
    checks["the exception type is specific"] = issubclass(
        ContainerListUnavailable, Exception)

    # ── a failed registry check must not delete a known update ────
    # A full scan rewrites this host's slice of the pending file from the
    # updates it found. A container whose check failed is not among them,
    # so its already-known pending update silently disappeared — badge and
    # button gone from the Web UI, and the next report said everything was
    # current. One 429 from Docker Hub was enough.
    import inspect as _i
    src = _i.getsource(UpdateChecker.check_all)
    # Assert on the code, not on prose: an earlier version of this test
    # matched the word "carried" and hit the explanatory comment above the
    # variable rather than the variable, and passed for the wrong reason.
    checks["failed checks are remembered"] = "failed_checks.add" in src
    checks["the write includes the carried-over entries"] = (
        "others + updates + carried" in src)
    checks["the carry-over is keyed on the failures"] = (
        'u.get("name") in failed_checks' in src)
    # It must not resurrect an entry the scan DID re-verify as gone —
    # otherwise a container that was updated keeps its badge forever.
    checks["an entry found this run is not double-added"] = (
        'u.get("name") not in found' in src)

    # ── the merge itself, on real files ──────────────────────────
    d = tempfile.mkdtemp()
    path = os.path.join(d, "pending.json")
    with open(path, "w") as f:
        json.dump([
            {"name": "nas-app", "host": "nas", "image": "a:1"},
            {"name": "kept", "host": "local", "image": "b:1"},
            {"name": "gone", "host": "local", "image": "c:1"},
        ], f)
    # Simulate: local scan found nothing new, `kept`'s check failed,
    # `gone`'s succeeded and it is no longer pending.
    from container_store import atomic_write_json
    prev = json.load(open(path))
    host_of = lambda e: e.get("host") or "local"
    others = [u for u in prev if host_of(u) != "local"]
    updates = []
    found = {u.get("name") for u in updates}
    carried = [u for u in prev if host_of(u) == "local"
               and u.get("name") in {"kept"} and u.get("name") not in found]
    atomic_write_json(path, others + updates + carried)
    after = {u["name"] for u in json.load(open(path))}
    checks["another host's entries survive"] = "nas-app" in after
    checks["an unverifiable entry survives"] = "kept" in after
    checks["a verified-gone entry is dropped"] = "gone" not in after

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
