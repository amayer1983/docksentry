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
    # The rule this file is about now lives in `merge_pending`, a
    # function, so it can be run instead of described. It used to be
    # eleven lines inside `check_all` that no test could reach: the old
    # version of this file greped the source for `others + updates +
    # carried` and then, below, re-typed that same expression and
    # asserted against its own copy. Both halves stayed green with the
    # carry-over deleted.
    from update_checker import merge_pending
    host_of = lambda e: e.get("host") or "local"                # noqa: E731
    prev = [
        {"name": "nas-app", "host": "nas", "image": "a:1"},
        {"name": "kept", "host": "local", "image": "b:1"},
        {"name": "gone", "host": "local", "image": "c:1"},
    ]
    merged, carried = merge_pending(prev, [], {"kept"}, "local", host_of)
    after = {u["name"] for u in merged}
    checks["another host's entries survive a scan of this one"] = "nas-app" in after
    checks["an entry we could not re-verify is carried over"] = "kept" in after
    checks["…and counted, so the log can say how many"] = carried == 1
    checks["an entry the scan verified as gone is dropped"] = "gone" not in after

    # A container that WAS re-verified this run must not be resurrected by
    # also appearing in the failed set — otherwise its badge never clears.
    m2, _ = merge_pending(prev, [{"name": "kept", "host": "local", "image": "b:2"}],
                          {"kept"}, "local", host_of)
    checks["an entry found this run is not double-added"] = (
        [u["name"] for u in m2].count("kept") == 1)
    checks["…and it is the fresh one that survives"] = (
        [u for u in m2 if u["name"] == "kept"][0]["image"] == "b:2")

    # Nothing failed: the file is exactly this host's findings plus the
    # other hosts, with no leftovers.
    m3, c3 = merge_pending(prev, [], set(), "local", host_of)
    checks["with nothing failing, nothing is carried"] = (
        c3 == 0 and {u["name"] for u in m3} == {"nas-app"})

    # Junk in the file is skipped rather than crashing the write.
    m4, _ = merge_pending([None, "x", {"name": "kept", "host": "local"}],
                          [], {"kept"}, "local", host_of)
    checks["a corrupt entry does not take the merge down"] = (
        [u["name"] for u in m4] == ["kept"])

    # And the caller still goes through it.
    import inspect as _i
    checks["check_all writes what the merge returns"] = (
        "merge_pending(" in _i.getsource(UpdateChecker.check_all))

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
