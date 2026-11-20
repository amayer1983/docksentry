#!/usr/bin/env python3
"""A queued update waits for the whole scheduled pass, not its first host.

Since Q13 every release of the update lock kicks the engine's drainer.
The scheduler's pass is not one claim, though: the automatic
self-update check claims and releases, then `handle_autoupdates` claims
and releases once per host, then the post-update cleanup and the
disk-warning cleanup claim and release again. Each of those releases
kicked the drainer, and a queued update (one somebody tapped while the
pass held the lock) took the lock in the gap on its own thread. The
next host's `handle_autoupdates` then failed its claim and printed
"Auto-update skipped: another update flow is running" — every host
after the first lost its unattended updates until the next cron match,
usually a day later (Q13 review, measured on d165e6e: 6 of 6 runs).

So the scheduler holds the drainer back for the length of a pass: kicks
that land meanwhile are kept, and the end of the pass (`_after_tick`)
lets them go. Driven here through the real `_run` with a real
TelegramBot and engine; stood in for are the wall clock and the sleep,
the hosts' `check_all`, the container work (`_process_update_batch`),
the queued update's work (`_drain_one`) and the docker calls behind
the cleanup and the disk check.

- three hosts, a tap queued during the first host's batch: both hosts
  with updates run, nothing says "skipped", both cleanups run, and the
  tap runs once, after all of it;
- a tap during the last host's batch: the post-update cleanup and the
  disk-warning cleanup both still run, and the tap after them;
- the same with the queue filled during the automatic self-update
  check, which releases first;
- an applied self-update returns from `_run` without `_after_tick`, and
  leaves no hold behind: the tap still runs;
- the deferred check after a self-update restart walks the hosts the
  same way, on its own thread, and holds the drainer back too;
- a pass that raises between its hold and `_after_tick` lets the hold
  go on its way out, and later releases kick as before;
- the other direction: outside a pass, a release still starts the
  drainer at once, and so does a hold that is let go.

Exits non-zero on any failure.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timedelta

HERE = os.path.dirname(__file__)
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

import notify_retry                                          # noqa: E402
import scheduler                                             # noqa: E402
import weekly_report                                         # noqa: E402
from container_store import ContainerStore                   # noqa: E402
from telegram_bot import TelegramBot                         # noqa: E402

checks = {}


def check(label, ok, why=""):
    checks[label] = bool(ok)
    if not ok and why:
        print(f"      → {label}: {why}")


#: How long a host's `check_all` and the docker df behind the disk check
#: take. Long enough that a drainer started by the release before it has
#: claimed the lock by then (it did on the old code in every run), short
#: enough to keep the procedure quick.
LATENCY = 0.15
#: How long the queued update's own work takes: longer than the last
#: host's check and the disk check together, so a drainer started at the
#: last host's release would still hold the lock at both cleanups.
DRAIN_TIME = 0.6


def _world(auto_selfupdate=False, tap_on="web"):
    d = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(
        bot_token="x", chat_id="1", language="en", debug=False,
        container_cli="docker", auto_update_all=False, update_policy="all",
        channel_telegram_enabled=True, bot_label="", telegram_topic_id="",
        quiet_hours_start="", quiet_hours_end="",
        data_dir=d, pending_file=os.path.join(d, "p.json"),
        history_file=os.path.join(d, "h.json"),
        maintenance_file=os.path.join(d, "maintenance.json"),
        deferred_check_file=os.path.join(d, "deferred.json"),
        cron_schedule="* * * * *", monitor_enabled=False,
        disk_check_interval_seconds=300, monitor_interval_seconds=60,
        auto_selfupdate=auto_selfupdate, auto_cleanup=True,
        disk_warn_auto_cleanup=True)
    for n in ("pinned", "autoupdate", "update_windows", "ask_before_major",
              "trust_running", "cooldown", "protect_stop", "major_pending",
              "groups", "notes", "links"):
        setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
    store = ContainerStore(cfg)
    bot = TelegramBot(cfg, store)
    bot.send_message = lambda text, *a, **k: None
    bot.announce = lambda text, *a, **k: None
    bot.notify_updates = lambda upd, **k: None
    bot.notifier = None
    bot.link_resolver = types.SimpleNamespace(
        resolve_container_link=lambda *a, **k: "")
    eng = bot.engine
    w = types.SimpleNamespace(cfg=cfg, bot=bot, eng=eng, events=[],
                              ev_lock=threading.Lock(), tapped=False)

    def ev(name):
        with w.ev_lock:
            w.events.append(name)
    w.ev = ev

    def tap():
        # Somebody taps a container while the pass holds the lock.
        if not w.tapped:
            w.tapped = True
            claimed, _busy, _p = eng.claim_or_queue("tapped")
            ev("queued" if not claimed else "tap claimed?!")

    w.tap = tap

    def drain_one(key):
        ev(f"drain:{key}")
        time.sleep(DRAIN_TIME)
        return None
    eng._drain_one = drain_one

    def batch(updates, checker, *, auto):
        names = [u["name"] for u in updates]
        ev("batch:" + ",".join(names))
        if names == [tap_on]:
            tap()
        return ([f"ok {n}" for n in names], len(names), [])
    bot._process_update_batch = batch

    for n in ("web", "db"):
        store.toggle_auto(n)

    class _Host:
        def __init__(self, name, updates):
            self.name, self.updates = name, updates

        def check_all(self):
            time.sleep(LATENCY)
            ev(f"check:{self.name}")
            return [dict(u) for u in self.updates]

        def get_container_labels(self, name):
            return {}

        def label_bool(self, labels, key):
            return None

    class _Local:
        def check_disk_usage(self):
            time.sleep(LATENCY)
            return "warn", 95, 1.0

        def reclaimable_bytes(self):
            return 0

        def cleanup_images(self):
            ev("cleanup")
            return True, "✅ cleaned"

    w.hosts = [(_Host("a", [{"name": "web", "image": "nginx:1",
                             "host": "local"}]), "a"),
               (_Host("b", [{"name": "db", "image": "pg:1",
                             "host": "local"}]), "b"),
               (_Host("c", []), "c")]
    s = scheduler.Scheduler(cfg, _Local(), bot, engine=eng)
    s._checkers = lambda: list(w.hosts)
    w.s = s
    return w


def _settle(w, timeout=5.0):
    """Wait until the drainer has finished (or never started)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if getattr(w.eng, "_drainer", None) is None:
            time.sleep(0.05)
            if getattr(w.eng, "_drainer", None) is None:
                return True
        time.sleep(0.02)
    return False


_BASE = datetime(2026, 11, 20, 3, 0, 0)


def run_pass(w):
    """One pass of the real `_run` in a cron minute; returns the log."""
    clk = {"t": 1000.0}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _BASE + timedelta(seconds=clk["t"] - 1000.0)

    def fake_sleep(sec):
        # What the pass left behind at its own sleep, before `_run`'s
        # way out has had a say: a pass must let its hold go itself.
        w.at_sleep = dict(
            holds=getattr(w.eng, "_drain_holds", None),
            drainer=getattr(w.eng, "_drainer", None) is not None
            or "drain:tapped" in w.events)
        clk["t"] += sec
        w.s.running = False

    saved = (scheduler.time, weekly_report.maybe_send_weekly_report,
             notify_retry.flush, scheduler.datetime)
    scheduler.datetime = _Clock
    scheduler.time = types.SimpleNamespace(
        monotonic=lambda: clk["t"], sleep=fake_sleep, time=lambda: clk["t"])
    weekly_report.maybe_send_weekly_report = lambda *a, **k: False
    notify_retry.flush = lambda *a, **k: (0, 0)
    w.s.running = True
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            w.s._run()
            _settle(w)
    finally:
        (scheduler.time, weekly_report.maybe_send_weekly_report,
         notify_retry.flush, scheduler.datetime) = saved
    return log.getvalue()


def _skips(log):
    return [line for line in log.splitlines() if "skipped" in line]


# ── three hosts, a tap during the first host's batch ─────────────────
w = _world()
log = run_pass(w)
ev = list(w.events)
check("every host with updates runs its unattended batch",
      "batch:web" in ev and "batch:db" in ev, f"events {ev}")
check("…and nothing in the pass says it skipped",
      not _skips(log), f"skips {_skips(log)}")
check("both cleanups run: after the updates and on the disk warning",
      ev.count("cleanup") == 2, f"events {ev}")
check("the tapped update runs once, after the whole pass",
      ev.count("drain:tapped") == 1 and ev[-1] == "drain:tapped"
      and ev.index("drain:tapped") > ev.index("check:c"),
      f"events {ev}")
check("…and the queue is empty afterwards",
      w.eng.queued_updates() == [], f"left {w.eng.queued_updates()}")
# The pass ends at its sleep, and the loop goes round; `_run`'s own way
# out only comes when the scheduler stops. So the kept kick has to be
# gone by the sleep, or the next passes would all run under the hold.
_at = getattr(w, "at_sleep", {})
check("…and the pass let its hold go and started the drainer before its "
      "sleep, not only on the way out of _run",
      _at.get("holds") == 0 and _at.get("drainer"), f"at sleep {_at}")

# ── a tap during the last host's batch, and the two cleanups ─────────
# The last host's release is the one right before the cleanups. On the
# old code it started the drainer, which held the lock through both of
# them, and each said it skipped.
w = _world(tap_on="db")
log = run_pass(w)
ev = list(w.events)
check("a tap during the last host's batch leaves both cleanups to run",
      ev.count("cleanup") == 2 and not _skips(log),
      f"events {ev}, skips {_skips(log)}")
check("…and runs once after them",
      ev.count("drain:tapped") == 1 and ev[-1] == "drain:tapped",
      f"events {ev}")

# ── the queue filled during the automatic self-update check ──────────
w = _world(auto_selfupdate=True)


def _su_locked(defer_check=False):
    w.ev("selfupdate")
    w.tap()
    return False


w.bot._check_selfupdate_auto_locked = _su_locked
log = run_pass(w)
ev = list(w.events)
check("a tap during the self-update check does not take the first "
      "host's batch",
      ev[:2] == ["selfupdate", "queued"] and "batch:web" in ev
      and "batch:db" in ev and not _skips(log),
      f"events {ev}, skips {_skips(log)}")
check("…and runs once after the pass",
      ev.count("drain:tapped") == 1 and ev[-1] == "drain:tapped",
      f"events {ev}")

# ── an applied self-update leaves no hold behind ─────────────────────
w = _world(auto_selfupdate=True)


def _su_applied(defer_check=False):
    w.ev("selfupdate")
    w.tap()
    return True


w.bot._check_selfupdate_auto_locked = _su_applied
log = run_pass(w)
ev = list(w.events)
check("an applied self-update leaves the pass before any host",
      "batch:web" not in ev and "Self-update applied" in log,
      f"events {ev}")
check("…and the drainer is not held back after it",
      ev.count("drain:tapped") == 1, f"events {ev}")

# ── the deferred check after a restart ───────────────────────────────
w = _world()
with open(w.cfg.deferred_check_file, "w", encoding="utf-8") as f:
    json.dump({"trigger_time": datetime.now().isoformat()}, f)
started = []
_real_threading, _real_time = scheduler.threading, scheduler.time


class _Thread(threading.Thread):
    def start(self):
        started.append(self)
        super().start()


scheduler.threading = types.SimpleNamespace(Thread=_Thread)
scheduler.time = types.SimpleNamespace(sleep=lambda s: None,
                                       monotonic=time.monotonic,
                                       time=time.time)
log = io.StringIO()
try:
    with contextlib.redirect_stdout(log):
        w.s._resume_deferred_check()
        for t in started:
            t.join(5)
        _settle(w)
finally:
    scheduler.threading, scheduler.time = _real_threading, _real_time
ev = list(w.events)
check("the deferred check runs every host with updates",
      len(started) == 1 and "batch:web" in ev and "batch:db" in ev
      and not _skips(log.getvalue()),
      f"threads {len(started)}, events {ev}, skips {_skips(log.getvalue())}")
check("…and the tap runs once, after its last host",
      ev.count("drain:tapped") == 1 and ev[-1] == "drain:tapped",
      f"events {ev}")

# ── a pass that raises between its hold and _after_tick ──────────────
# The hold is the pass's, and so is letting it go. Measured on cbd4790:
# a host list that raised after the first host left `_run` with the hold
# still counted, and from then on no release anywhere (Telegram, Web,
# Discord) started a drainer — the queue stayed frozen for good. Whether
# the error then ends `_run` is not what this pins; that the hold does
# not outlive the pass is.
w = _world()
_hosts = list(w.hosts)


def _raising_hosts():
    yield _hosts[0]
    raise RuntimeError("host list blew up")


w.s._checkers = _raising_hosts
try:
    log = run_pass(w)
except RuntimeError:
    log = ""
_settle(w)
ev = list(w.events)
check("a pass that raises after its first host still lets the queue go",
      ev.count("drain:tapped") == 1 and ev[-1] == "drain:tapped",
      f"events {ev}")
check("…and leaves no hold behind on the engine",
      getattr(w.eng, "_drain_holds", 0) == 0
      and not getattr(w.s, "_queue_held", False),
      f"holds {getattr(w.eng, '_drain_holds', None)}, "
      f"pass holds {getattr(w.s, '_queue_held', None)}")
with contextlib.redirect_stdout(io.StringIO()):
    w.eng.claim_update()
    w.eng.claim_or_queue("tapped2")
    w.eng.release_update()
    kicked = getattr(w.eng, "_drainer", None) is not None
    _settle(w)
check("…so a later release outside a pass starts the drainer again",
      kicked and w.events.count("drain:tapped2") == 1
      and w.eng.queued_updates() == [],
      f"started {kicked}, events {w.events}, "
      f"left {w.eng.queued_updates()}")

# ── the other direction: outside a pass a release kicks at once ──────
w = _world()
eng = w.eng
eng.claim_update()
eng.claim_or_queue("tapped")
with contextlib.redirect_stdout(io.StringIO()):
    eng.release_update()
    kicked = getattr(eng, "_drainer", None) is not None
    _settle(w)
check("outside a pass a release starts the drainer at once",
      kicked and w.events.count("drain:tapped") == 1,
      f"started {kicked}, events {w.events}")

w = _world()
eng = w.eng
hold = getattr(eng, "hold_drain", None)
let_go = getattr(eng, "let_drain_go", None)
if hold is not None and let_go is not None:
    with contextlib.redirect_stdout(io.StringIO()):
        hold()
        eng.claim_update()
        eng.claim_or_queue("tapped")
        eng.release_update()
        held = getattr(eng, "_drainer", None) is None
        let_go()
        went = getattr(eng, "_drainer", None) is not None
        _settle(w)
else:
    held = went = False
check("a release while the drainer is held starts nothing",
      held, "no hold_drain/let_drain_go" if hold is None else "")
check("…and letting the hold go starts the drainer it kept",
      went and w.events.count("drain:tapped") == 1,
      f"started {went}, events {w.events}")

w = _world()
eng = w.eng
if hold is not None:
    with contextlib.redirect_stdout(io.StringIO()):
        eng.hold_drain()
        eng.hold_drain()
        eng.claim_update()
        eng.claim_or_queue("tapped")
        eng.release_update()
        eng.let_drain_go()
        still = getattr(eng, "_drainer", None) is None
        eng.let_drain_go()
        went = getattr(eng, "_drainer", None) is not None
        _settle(w)
else:
    still = went = False
check("two holds (a pass and a deferred check) need both let go",
      still and went and w.events.count("drain:tapped") == 1,
      f"held after one {still}, started after two {went}, "
      f"events {w.events}")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
