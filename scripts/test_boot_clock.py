#!/usr/bin/env python3
"""A host that just booted is watched like any other.

`time.monotonic()` counts from system boot on Linux. Every "when did this
last happen" that started at 0 instead of "never" read as "it happened
at boot" — harmless on a box that has been up for weeks, wrong for the
first minutes after a reboot, which is exactly when a monitor matters.

16b84f1 fixed the alert cooldown for that and pinned it for the
twelve-container host-down digest. The audit for 2.19.1 then reverted
the same fix in the branch that carries health alerts (`others`) and
every monitor procedure stayed green: the suite ran on a machine up 119
days, where 0 and "never" give the same answer. Measured here with an
injected clock before the fix stood alone: at uptimes of 5, 40 and
1799 s a confirmed unhealthy container sent 0 alerts with the revert,
1 without it.

The scheduler had the same thing on its own cadence: driven through
`Scheduler._run` with `scheduler.time` swapped for a fake clock, a host
up 5 s got its first disk check 300 s later and, at a 600 s monitor
interval, its first monitor pass 600 s later. On a host up 100000 s
both ran on the first pass.

And the event watcher: its burst cache started at (0.0, ""), so a death
in the first two seconds of uptime was handed an empty memory picture
without one ever being taken. Measured: snapshot_fn called 0 times at
uptimes 0.5 and 1.9 s, once at 2.1 s.

Not here, on purpose: the `_bump_streak` gap. Reverting it to
`.get(key, 0)` was driven at 5, 40, 1799 and 100000 s and gave the same
alert sequence each time — the streak and `_last_sent` are written
together, so the mutation has nothing to change. The backoff at boot is
still checked below, and it goes red when the cooldown gate is reverted.

Every clock is injected, so none of this depends on the uptime of the
machine running it. Exits non-zero on any failure.
"""
import contextlib
import io
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import event_watcher                                         # noqa: E402
import monitor                                               # noqa: E402
import notify_retry                                          # noqa: E402
import scheduler                                             # noqa: E402
import weekly_report                                         # noqa: E402
from monitor import ContainerMonitor                         # noqa: E402
from update_checker import UpdateChecker                     # noqa: E402

checks = {}


def check(label, ok, why=""):
    checks[label] = bool(ok)
    if not ok and why:
        print(f"      → {why}")


# ── the monitor, driven through tick() ──────────────────────────────
def c(status="running", health="", code=0, oom=False):
    return {"status": status, "health": health, "exit_code": code,
            "oom": oom, "restarts": 0, "labels": {}, "started_at": ""}


def make_monitor():
    m = ContainerMonitor.__new__(ContainerMonitor)
    m.config = types.SimpleNamespace(monitor_enabled=True,
                                     exclude_containers=[],
                                     monitor_mass_stop_enabled=True)
    m.host_name = ""
    m.checker = types.SimpleNamespace(
        label_bool=UpdateChecker.label_bool,
        _own_container_name=lambda: "docksentry",
        _tail_logs=lambda name, lines=10, none_on_error=False: "",
    )
    m._memory_snapshot = lambda top=3: ""
    m._cpu_snapshot = lambda top=3: ""
    m._host_memory = lambda: ""
    m._host_load = lambda: ""
    m._event_snapshot = lambda: {}
    m._ensure_watcher = lambda: None
    m.sent = []
    m.bot = types.SimpleNamespace(
        update_running=False, t=lambda k, **kw: k,
        send_message=lambda msg, **kw: m.sent.append(msg),
        send_document=lambda *a, **k: None, notifier=None)
    m._prev = None
    m._last_sent = {}
    m._alert_streak = {}
    m._health_pending = {}
    m._alerted_unhealthy = set()
    m._snap_cache = None
    # The event log is a file; the decision under test does not live there.
    m.recorded = []
    m._record = lambda kind, *a, **k: m.recorded.append(kind)
    return m


def tick(m, snap):
    m.snapshot = lambda: snap
    with contextlib.redirect_stdout(io.StringIO()):
        return m.tick() or []


def flip_unhealthy(m):
    """healthy → unhealthy → still unhealthy: the debounce confirms on the
    third pass. Returns how many unhealthy alerts went out."""
    n = 0
    for h in ("healthy", "unhealthy", "unhealthy"):
        n += sum(1 for k, _, _ in tick(m, {"web": c(health=h)})
                 if k == "unhealthy")
    return n


clock = {"t": 0.0}
_real_monotonic = monitor.time.monotonic
try:
    monitor.time.monotonic = lambda: clock["t"]

    # A container turns unhealthy minutes after the host came up.
    for up in (5.0, 40.0, 1799.0):
        clock["t"] = up
        m = make_monitor()
        got = flip_unhealthy(m)
        check(f"an unhealthy container {int(up)}s after boot is reported",
              got == 1, f"uptime {up}s sent {got} unhealthy alert(s)")

    # A single OOM kill — one death, so the per-container path, not the
    # host digest test_mass_stop.py drives.
    for up in (5.0, 40.0, 1799.0):
        clock["t"] = up
        m = make_monitor()
        tick(m, {"db": c()})
        got = [k for k, _, _ in tick(m, {"db": c("exited", code=137, oom=True)})]
        check(f"an OOM kill {int(up)}s after boot is reported", got == ["oom"],
              f"uptime {up}s sent {got}")

    # The counter-direction: "never" must not turn into "always". A repeat
    # inside the window is still held back, at boot as anywhere else, and
    # the wait still grows — first half an hour, then an hour.
    base = 5.0
    m = make_monitor()
    clock["t"] = base
    first = flip_unhealthy(m)
    clock["t"] = base + 1799
    held = flip_unhealthy(m)
    clock["t"] = base + 1801
    through = flip_unhealthy(m)
    check("the first unhealthy alert after boot goes out", first == 1,
          f"sent {first}")
    check("…a repeat 1799 s later is held back", held == 0, f"sent {held}")
    check("…and one 1801 s later is let through", through == 1,
          f"sent {through}")
    check("…counting as the second of the incident, not the third",
          m._alert_streak.get(("web", "unhealthy")) == 2,
          f"streak {m._alert_streak}")
    t2 = base + 1801
    clock["t"] = t2 + 3599
    held2 = flip_unhealthy(m)
    clock["t"] = t2 + 3601
    through2 = flip_unhealthy(m)
    check("…after which the wait has doubled: held at 3599 s", held2 == 0,
          f"sent {held2}")
    check("…and let through at 3601 s", through2 == 1, f"sent {through2}")
finally:
    monitor.time.monotonic = _real_monotonic


# ── the scheduler's own cadence, driven through _run() ──────────────
def run_scheduler(uptime, mon_interval, loops=40):
    """Run the scheduler loop for `loops` passes of its 30 s sleep on a
    host that has been up `uptime` seconds. Returns the offsets (from loop
    start) of every disk check and every monitor pass."""
    clk = {"t": uptime}
    disk, mon = [], []
    s = scheduler.Scheduler.__new__(scheduler.Scheduler)
    passes = {"n": 0}

    def fake_sleep(sec):
        clk["t"] += sec
        passes["n"] += 1
        if passes["n"] >= loops:
            s.running = False

    class FakeMonitor:
        def __init__(self, *a, host_name="", **k):
            self.host_name = host_name

        def tick(self):
            mon.append(clk["t"] - uptime)

    tmp = tempfile.mkdtemp()
    s.config = types.SimpleNamespace(
        cron_schedule="0 0 31 2 *",          # never: no cron tick interferes
        monitor_enabled=True,
        disk_check_interval_seconds=300,
        monitor_interval_seconds=mon_interval,
        maintenance_file=os.path.join(tmp, "maintenance.json"),
        language="en")
    s.checker = object()
    s.bot = object()
    s.hosts = None
    s._weekly_sent_date = None
    s._disk_warned_at = None
    s._checkers = lambda: [(s.checker, "")]
    s._check_disk_space = lambda: disk.append(clk["t"] - uptime)
    s.running = True

    saved = (scheduler.time, monitor.ContainerMonitor,
             weekly_report.maybe_send_weekly_report, notify_retry.flush)
    scheduler.time = types.SimpleNamespace(
        monotonic=lambda: clk["t"], sleep=fake_sleep, time=lambda: 0.0)
    monitor.ContainerMonitor = FakeMonitor
    weekly_report.maybe_send_weekly_report = lambda *a, **k: False
    notify_retry.flush = lambda *a, **k: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            s._run()
    finally:
        (scheduler.time, monitor.ContainerMonitor,
         weekly_report.maybe_send_weekly_report, notify_retry.flush) = saved
    return disk, mon


for up in (5.0, 40.0, 250.0):
    disk, mon = run_scheduler(up, 600)
    check(f"{int(up)}s after boot the first disk check runs on the first pass",
          disk[:1] == [0.0], f"disk checks at {disk[:3]}")
    check(f"{int(up)}s after boot the first monitor pass runs on the first pass",
          mon[:1] == [0.0], f"monitor passes at {mon[:3]}")
    # Counter-direction: "never" must not mean "every pass".
    check(f"…and after that the disk check keeps its 300 s ({int(up)}s)",
          disk[:4] == [0.0, 300.0, 600.0, 900.0], f"disk checks at {disk[:4]}")
    check(f"…and the monitor its 600 s ({int(up)}s)",
          mon[:2] == [0.0, 600.0], f"monitor passes at {mon[:3]}")

disk, mon = run_scheduler(5.0, 60)
check("a 60 s monitor interval passes every 60 s from the first pass on",
      mon[:4] == [0.0, 60.0, 120.0, 180.0], f"monitor passes at {mon[:4]}")
disk, mon = run_scheduler(100000.0, 600)
check("a host up for days still starts both on the first pass",
      disk[:1] == [0.0] and mon[:1] == [0.0],
      f"disk {disk[:2]}, monitor {mon[:2]}")


# ── the event watcher's first snapshot ──────────────────────────────
_real_monotonic = event_watcher.time.monotonic
try:
    for up in (0.5, 1.9):
        event_watcher.time.monotonic = lambda u=up: u
        taken = []
        w = event_watcher.EventWatcher(
            None, lambda: (taken.append(1), "MEMORY PICTURE")[1],
            log=lambda msg: None)
        snap = w._snapshot()
        check(f"a death {up}s after boot gets a memory picture taken",
              snap == "MEMORY PICTURE" and len(taken) == 1,
              f"snapshot {snap!r}, taken {len(taken)}x")
    # Counter-direction: a burst still shares one picture.
    clk = {"t": 1.0}
    event_watcher.time.monotonic = lambda: clk["t"]
    taken = []
    w = event_watcher.EventWatcher(
        None, lambda: (taken.append(1), f"PICTURE {len(taken)}")[1],
        log=lambda msg: None)
    a = w._snapshot()
    clk["t"] = 1.0 + 1.5
    b = w._snapshot()
    taken_in_burst = len(taken)
    clk["t"] = 1.0 + 1.5 + 2.5
    d = w._snapshot()
    check("…a second death inside the burst window shares it",
          a == b == "PICTURE 1" and taken_in_burst == 1, f"{a!r}, {b!r}")
    check("…and one after the window gets its own", d == "PICTURE 2",
          f"{d!r}")
finally:
    event_watcher.time.monotonic = _real_monotonic


bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
