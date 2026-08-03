#!/usr/bin/env python3
"""An unhealthy alert carries what the probe said (#2, @famewolf).

His report was a question, and a fair one: two alerts, byparr turned
unhealthy and recovered, neither with any diagnostic. "Normally there is a
last log when a container goes unhealthy?"

The first suspect was us. His alerts come from a remote host
(`🖥 dockmox.lan`), and per-host plumbing is the trap this project has
fallen into four times. Measured instead of assumed: `_tail_logs` against
the remote podman host returned 173 characters of real nginx output. That
path works, and the remote-host theory was wrong.

The actual cause: for a health flip we attach `docker logs`, and `docker
logs` frequently has nothing to say. What explains the flip is the probe's
own output, and Docker keeps it — `State.Health.Log[].Output`. Measured on
a container whose healthcheck printed "connection refused: upstream
10.0.0.5:8080":

    docker logs           (completely empty)
    State.Health.Log      ExitCode=1, Output="connection refused: …"

We read that field nowhere. The snapshot already inspects every container,
so picking it up costs no extra call.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from monitor import ContainerMonitor

H = ContainerMonitor._health_output


def _state(log, status="unhealthy"):
    return {"Status": "running", "Health": {"Status": status, "Log": log}}


def main():
    checks = {}

    # ── picking the right probe ──────────────────────────────────
    failing = {"ExitCode": 1, "Output": "connection refused: upstream\n"}
    passing = {"ExitCode": 0, "Output": "OK\n"}
    checks["a failing probe is reported"] = (
        H(_state([failing])) == "connection refused: upstream")
    # After a recovery the last entry passed, but what is worth reading is
    # what it said while it was down — that is the message being explained.
    checks["the failing probe wins over a later pass"] = (
        H(_state([failing, passing], "healthy")) == "connection refused: upstream")
    # With nothing failing there is still context in the last probe.
    checks["a passing probe is used as a fallback"] = H(_state([passing])) == "OK"
    # Most recent failure, not the first.
    old = {"ExitCode": 1, "Output": "old failure"}
    checks["the newest failure wins"] = (
        H(_state([old, failing])) == "connection refused: upstream")

    # ── nothing to say ───────────────────────────────────────────
    checks["no healthcheck yields nothing"] = H({"Status": "running"}) == ""
    checks["an empty log yields nothing"] = H(_state([])) == ""
    checks["a silent probe yields nothing"] = (
        H(_state([{"ExitCode": 1, "Output": "   \n"}])) == "")
    checks["a missing Health key is safe"] = H({}) == ""

    # ── a probe that dumps a page is capped ──────────────────────
    # A curl healthcheck against an HTML error page is the realistic worst
    # case. The log tail already spends 1500 of Telegram's 4096.
    big = H(_state([{"ExitCode": 1, "Output": "x" * 4000}]))
    checks["a long output is capped"] = len(big) <= 501
    checks["…and marked as cut"] = big.endswith("…")

    # ── it reaches the snapshot ──────────────────────────────────
    m = ContainerMonitor.__new__(ContainerMonitor)
    m.backend = types.SimpleNamespace(
        ps=lambda **kw: types.SimpleNamespace(returncode=0, stdout="web\n"),
        inspect=lambda names, **kw: types.SimpleNamespace(
            returncode=0,
            stdout='[{"Name":"/web","State":{"Status":"running","Health":'
                   '{"Status":"unhealthy","Log":[{"ExitCode":1,'
                   '"Output":"probe said no"}]}},"Config":{"Labels":{}}}]'))
    snap = m.snapshot()
    checks["the snapshot carries the probe output"] = (
        snap["web"]["health_output"] == "probe said no")
    # And the fields it already had are untouched.
    checks["the snapshot keeps its other fields"] = (
        snap["web"]["health"] == "unhealthy" and snap["web"]["status"] == "running")

    # ── the alert uses it, and only where it applies ─────────────
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "monitor.py"), encoding="utf-8").read()
    notify = src[src.index("def _notify"):]
    checks["the alert reads health_output"] = "health_output" in notify
    # It is shown as well as the log tail, not instead: one says why the
    # probe failed, the other what the container was doing.
    checks["the log tail is still attached"] = "_tail_logs" in notify
    # A crash has no probe to quote; asking for one would be noise.
    seg = notify[notify.index("health_output") - 260:notify.index("health_output")]
    checks["only health flips look for a probe"] = '"unhealthy", "recovered"' in seg

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
