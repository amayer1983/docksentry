#!/usr/bin/env python3
"""Maintenance mode stops the scheduled run, and the monitor with it.

README promises that `/maintenance` pauses auto-updates, and
docs/monitoring.md that alerts respect maintenance mode. Both live in
`Scheduler._run`, and the audit for 2.19.1 measured that nothing held
either: with `if _maint_active(self.config):` turned into `if False:`
every procedure stayed green, and so did they with the monitor's
`and not _maint_active2(self.config)` removed. test_boot_clock.py does
drive this loop, but with a cron that never fires and without
maintenance.

So this runs the real loop. What is swapped out is only the environment:
the clock (`scheduler.datetime` answers a fixed minute that the cron
matches, `scheduler.time` counts passes instead of sleeping and ends the
loop after a few), the engine behind it (a bot that records whether
self-update and auto-update were asked for, a checker that records
whether a check ran, a monitor that records its ticks) and the
maintenance file, which sits in a temp directory and is switched on with
the real `maintenance.enable`. The decision under test — whether the
tick goes ahead — is the scheduler's own code, untouched.

Both directions each time: with maintenance on nothing of the update
work happens; with it off all of it does. Exits non-zero on any failure.
"""
import contextlib
import io
import os
import sys
import tempfile
import types
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import maintenance                                           # noqa: E402
import monitor                                               # noqa: E402
import notify_retry                                          # noqa: E402
import scheduler                                             # noqa: E402
import weekly_report                                         # noqa: E402

checks = {}

# A Wednesday, 03:00 — handed in, never read from the machine.
TICK = datetime(2026, 11, 18, 3, 0, 0)
EVERY_NIGHT = "0 3 * * *"
NEVER = "0 0 31 2 *"


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return TICK


def run(maint_on, cron, passes=6):
    """Drive `passes` rounds of the real loop. Returns what got done."""
    done = {"selfupdate": 0, "check_all": 0, "autoupdate": [],
            "monitor": 0, "disk": 0}
    tmp = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(
        cron_schedule=cron,
        auto_selfupdate=True,
        auto_cleanup=False,
        monitor_enabled=True,
        monitor_interval_seconds=60,
        disk_check_interval_seconds=300,
        maintenance_file=os.path.join(tmp, "maintenance.json"),
        deferred_check_file=os.path.join(tmp, "deferred.json"),
        language="en")
    if maint_on:
        maintenance.enable(cfg)          # "forever": no wall clock involved

    class Checker:
        def check_all(self):
            done["check_all"] += 1
            return [{"name": "web", "image": "nginx:1.27"}]

    class Bot:
        enabled = False
        notifier = None
        broadcast = None

        def t(self, key, **kw):
            return key

        def check_selfupdate_auto(self, defer_check=False):
            done["selfupdate"] += 1
            return False

        def handle_autoupdates(self, updates, checker):
            done["autoupdate"].append([u["name"] for u in updates])
            return 0

    class FakeMonitor:
        def __init__(self, *a, host_name="", **k):
            self.host_name = host_name

        def tick(self):
            done["monitor"] += 1

    s = scheduler.Scheduler(cfg, Checker(), Bot())
    s._check_disk_space = lambda: done.__setitem__("disk", done["disk"] + 1)
    clk = {"t": 1000.0, "n": 0}

    def fake_sleep(sec):
        clk["t"] += sec
        clk["n"] += 1
        if clk["n"] >= passes:
            s.running = False

    saved = (scheduler.time, scheduler.datetime, monitor.ContainerMonitor,
             weekly_report.maybe_send_weekly_report, notify_retry.flush)
    scheduler.time = types.SimpleNamespace(
        monotonic=lambda: clk["t"], sleep=fake_sleep, time=lambda: clk["t"])
    scheduler.datetime = FixedClock
    monitor.ContainerMonitor = FakeMonitor
    weekly_report.maybe_send_weekly_report = lambda *a, **k: False
    notify_retry.flush = lambda *a, **k: None
    s.running = True
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            s._run()
    finally:
        (scheduler.time, scheduler.datetime, monitor.ContainerMonitor,
         weekly_report.maybe_send_weekly_report, notify_retry.flush) = saved
    return done


# ── the cron tick ────────────────────────────────────────────────────
on = run(maint_on=True, cron=EVERY_NIGHT)
off = run(maint_on=False, cron=EVERY_NIGHT)

check = checks.__setitem__
check("maintenance on: the scheduled tick does not ask for a self-update",
      on["selfupdate"] == 0)
check("maintenance on: no host is checked", on["check_all"] == 0)
check("maintenance on: nothing is handed to auto-update",
      on["autoupdate"] == [])
# Counter-direction: the same minute without maintenance does all of it,
# once — so the zeros above are the gate, not a tick that never fired.
check("maintenance off: the same tick asks for a self-update once",
      off["selfupdate"] == 1)
check("maintenance off: the host is checked once", off["check_all"] == 1)
check("maintenance off: its update goes to auto-update",
      off["autoupdate"] == [["web"]])

# ── the monitor, on its own cadence ──────────────────────────────────
# No cron tick here, so the maintenance short-circuit above cannot be
# what keeps the monitor quiet; only the monitor's own gate is left.
mon_on = run(maint_on=True, cron=NEVER, passes=6)
mon_off = run(maint_on=False, cron=NEVER, passes=6)
check("maintenance on: the monitor does not tick", mon_on["monitor"] == 0)
# 6 passes of 30 s at a 60 s interval: offsets 0, 60, 120 → 3 ticks.
check("maintenance off: the monitor ticks every 60 s",
      mon_off["monitor"] == 3)
check("…and with no cron tick nothing of the update work runs either way",
      mon_on["check_all"] == mon_off["check_all"] == 0)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
if bad:
    print(f"      on={on} off={off} mon_on={mon_on} mon_off={mon_off}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
