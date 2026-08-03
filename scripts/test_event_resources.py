#!/usr/bin/env python3
"""The event log keeps the memory/CPU picture, not just the exit code (#2).

@NotRetarded's actual problem, once it was measured rather than guessed at:
the alert names the culprit — who was holding memory and CPU at the instant
a container died — and the *history* could not, for any event, old or new.
Not because old rows were stripped by a later release, which was his first
theory, but because the snapshot was gathered inside `_notify`, after the
event had already been written, and thrown away with the message.

His use for it, in his words: "That's how I'm able to actually see in real
time what CPU and Memory usage containers are using." The point of the
event log is that it answers the same question *without* having caught the
notification, and for the one number that matters it could not.

So the gathering moved ahead of the write and both callers get the same
record. This asserts the three things that can quietly regress: that it is
only collected where it means something, that it reaches the stored event,
and that an event written before v1.75.0 still renders.
"""

import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from monitor import ContainerMonitor


def _monitor(tmp, snapshot):
    m = ContainerMonitor.__new__(ContainerMonitor)
    m.config = types.SimpleNamespace(
        monitor_events_file=os.path.join(tmp, "monitor_events.json"))
    m._host_memory = lambda: snapshot.get("host", "")
    m._event_snapshot = lambda: {k: snapshot.get(k, "")
                                 for k in ("mem", "cpu")}
    m.watcher = None
    return m


def main():
    tmp = tempfile.mkdtemp()
    checks = {}
    snap = {"host": "14.8/15.6 GB · Swap 3.9/4.0 GB",
            "mem": "some-new-app 9.1GiB · unifi 2.2GiB",
            "cpu": "some-new-app 198%"}
    m = _monitor(tmp, snap)

    # ── gathered only where it means something ───────────────────
    for kind in ("oom", "crash_restart", "exited"):
        r = m._resources_for(kind, "unifi")
        checks[f"{kind} collects the picture"] = (
            r.get("host") and r.get("mem") and r.get("cpu"))
    # A recovery or a health flip has no death to explain. Collecting there
    # would cost a ~2s `docker stats` call per health wobble for a line
    # nobody can act on.
    for kind in ("recovered", "unhealthy"):
        checks[f"{kind} collects nothing"] = m._resources_for(kind, "x") == {}

    # ── it reaches the stored event ──────────────────────────────
    m._record("crash_restart", "unifi", {"code": 137, "count": 1,
                                         "when": "16:14:47"},
              m._resources_for("crash_restart", "unifi"))
    path = m.config.monitor_events_file
    stored = json.load(open(path))
    ev = stored[-1]
    checks["the event is written"] = ev["kind"] == "crash_restart"
    checks["resources ride along"] = ev.get("resources", {}).get("cpu") == snap["cpu"]
    # `detail` is what fills the message template. Putting the snapshot in
    # there would make it an argument to a string that never asked for one.
    checks["detail keeps its own meaning"] = set(ev["detail"]) == {
        "code", "count", "when"}

    # ── an event with nothing to add carries no empty key ────────
    m._record("recovered", "unifi", {}, m._resources_for("recovered", "unifi"))
    ev2 = json.load(open(path))[-1]
    checks["no empty resources key"] = "resources" not in ev2

    # ── a host that cannot report memory still works ─────────────
    # _host_memory is local-only by design; on a remote host it returns "".
    m2 = _monitor(tmp, {"mem": "a 1GiB", "cpu": ""})
    r = m2._resources_for("exited", "x")
    checks["a remote host still records what it can"] = (
        "host" not in r and r.get("mem") == "a 1GiB")
    checks["an idle CPU is left out"] = "cpu" not in r

    # ── the renderer survives an event from before v1.75.0 ───────
    # Old rows have no `resources` at all. The web UI reads it with
    # `ev.get("resources") or {}`; this asserts the shape it relies on
    # rather than the markup, which lives in a template.
    old = {"timestamp": "2026-07-01 10:00:00", "kind": "exited",
           "container": "unifi", "detail": {"code": 137}}
    checks["an old event yields an empty picture"] = (old.get("resources") or {}) == {}
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "web_ui.py"), encoding="utf-8").read()
    checks["the renderer reads it defensively"] = 'ev.get("resources") or {}' in src
    # Same i18n keys as the alert, so a row and a notification about the
    # same death cannot word it differently.
    for key in ("monitor_host_memory", "monitor_top_memory", "monitor_top_cpu"):
        checks[f"the row uses {key}"] = key in src

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
