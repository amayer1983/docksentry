#!/usr/bin/env python3
"""Queued work must not depend on who happens to hold the lock.

Telegram's `_drain_update_queue` used to run in exactly one place: at
the end of a Telegram single update, after it had released the lock.
The lock is taken in eleven places — the scheduled run, the Web UI, a
self-update and six other Telegram paths among them. Queue something
while one of THOSE held it and the entry waited for the next Telegram
single update to finish, however long that was.

Measured on the maintainer's own instance on 23.10.2026. Three buttons
at 19:15:17, :21 and :22 (audit log, three presses, not four). The
second answered "queued — position 2" and the third "already in the
queue" — neither of which can be true for a first press. The queue had
been holding an entry since an earlier day; both containers were then
updated inside the same minute, once an unrelated 2.3 GB update
happened to end. Nothing was lost. It sat there, and the person
pressing the button was told something that read like a correction.

Two fixes, both driven here: a sweep on the poll loop that picks up
whatever nobody is going to drain, and an answer that says how long the
entry has been waiting instead of implying you already pressed it.

And since Q13 the third, which the sweep was standing in for: the
engine's one drainer (`kick_drain`). Every release of the lock kicks
it, both sweeps hand it in — the poll loop's and the scheduler's
`_after_tick`, which is the only one an install without Telegram has —
and at most one runs at a time. Its rows are below, the guard with an
entry stand-in, the reporting and the headless install in the callback
matrix's world.

The clock is stood in for, because a procedure that waits 27 hours
measures patience. The queue itself is the real one.
"""

import os
import sys
import types

import srcslice

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import update_engine  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402

checks = {}


class _Clock:
    """A clock that only moves when told to."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _engine(clock):
    e = UpdateEngine.__new__(UpdateEngine)
    import threading
    e._update_queue = []
    e._update_queue_lock = threading.Lock()
    e._queued_at = {}
    e._updating = {}
    update_engine.time.time = clock
    return e


clock = _Clock()
_real_time = update_engine.time.time
eng = _engine(clock)

# ── the queue remembers when, not just what ──────────────────────────

checks["a first queue gets position 1"] = eng.enqueue_update("srv40/portainer") == 1
checks["a second gets position 2"] = eng.enqueue_update("srv30/portainer") == 2
# The two hosts must not collide: same container name, different host.
checks["…because the two hosts are told apart"] = (
    len(eng.queued_updates()) == 2)
checks["re-queueing the same key is refused"] = (
    eng.enqueue_update("srv40/portainer") == 0)

clock.advance(27 * 3600)
waited = eng.queued_waiting_seconds("srv40/portainer")
checks["the wait is measured from when it was queued"] = (
    waited is not None and abs(waited - 27 * 3600) < 1)
checks["…and a key that is not queued has no wait"] = (
    eng.queued_waiting_seconds("srv40/nothing") is None)

snap = dict((k, w) for k, w in eng.queued_updates())
checks["the snapshot keeps the queue order"] = (
    [k for k, _ in eng.queued_updates()]
    == ["srv40/portainer", "srv30/portainer"])
checks["…and carries each entry's wait"] = (
    abs(snap["srv40/portainer"] - 27 * 3600) < 1)
# A snapshot that is the live list would change under a half-built
# message. Mutating what came back must not touch the queue.
_snap = eng.queued_updates()
_snap.clear()
checks["…and is a copy, not the queue itself"] = len(eng.queued_updates()) == 2

checks["has_queued_updates says yes while full"] = eng.has_queued_updates()

# Taking an entry forgets its timestamp — otherwise the map grows for
# the life of the process and a re-queued key reports the OLD wait.
taken = eng.take_queued_update()
checks["taking returns them in order"] = taken == "srv40/portainer"
checks["…and forgets its timestamp"] = eng._queued_at.get(taken) is None
eng.enqueue_update("srv40/portainer")
checks["…so a re-queued key starts its wait again"] = (
    eng.queued_waiting_seconds("srv40/portainer") < 1)

# Dropping does the same.
eng.drop_queued_updates()
checks["dropping empties the queue"] = not eng.has_queued_updates()
checks["…and leaves no timestamps behind"] = eng._queued_at == {}

import threading  # noqa: E402

# ── the same rules, on the engine ────────────────────────────────────
# The rules live in `UpdateEngine.sweep_queue(drain)` since Q9; since
# Q13 everyone hands it the engine's `kick_drain`. Driven here with a
# drain that records
# that it was reached, so a connection without Telegram's drain can rely
# on the same three answers: empty → nothing, held → nothing, never
# raises.

eng3 = _engine(clock)
eng3._update_lock = threading.Lock()
_esweep = srcslice.attr(eng3, "sweep_queue", label="UpdateEngine.sweep_queue")
_ecalls = []


def esweep(drain):
    if _esweep is not None:
        return _esweep(drain)


def _drain_rec():
    _ecalls.append("drained")


esweep(_drain_rec)
checks["engine: an empty queue does not reach the drain"] = (
    _esweep is not None and _ecalls == [])

eng3.enqueue_update("srv40/portainer")
esweep(_drain_rec)
checks["engine: queued work with a free lock is drained"] = (
    _ecalls == ["drained"])
# The sweep only decides; working the queue off is the drain's job.
checks["engine: …and the sweep leaves the queue to the drain"] = (
    _esweep is not None and [k for k, _ in eng3.queued_updates()] == ["srv40/portainer"])

_ecalls.clear()
eng3._update_lock.acquire()
try:
    esweep(_drain_rec)
    checks["engine: a held lock is left alone"] = (
        _esweep is not None and _ecalls == [])
finally:
    eng3._update_lock.release()
esweep(_drain_rec)
checks["engine: …and the next sweep takes it"] = _ecalls == ["drained"]


def _drain_boom():
    raise RuntimeError("drain broke")


_eraised = None
try:
    esweep(_drain_boom)
except Exception as e:
    _eraised = e
checks["engine: a failing drain does not escape the sweep"] = (
    _esweep is not None and _eraised is None)

# ── one drainer, on every install (Q13) ──────────────────────────────
# The drain used to run where a Telegram path happened to let go of the
# lock: at the end of a single update and in the poll loop. An install
# without Telegram never worked its queue off, and the Web UI and Discord
# cannot queue into nothing. So every release of the lock, and both
# sweeps, kick the engine's one drainer: a thread of its own, at most one
# at a time, running each entry through `run_keys` and saying what
# happened through the broadcast seam.
#
# `_drain_one` (one entry's update) is stood in for in the rows about the
# guard: what is measured there is how many drainers run and in which
# order the entries go, not what an update does. The rows after it run
# the real one in the callback matrix's world.
import contextlib as _cl  # noqa: E402
import io as _io  # noqa: E402
import json  # noqa: E402
import time as _time  # noqa: E402


def _qengine():
    e = _engine(clock)
    e._update_lock = threading.Lock()
    e._queued_selfupdate = None
    e._swap_in_flight = False
    return e


def _wait_until(cond, seconds=5.0):
    end = _time.monotonic() + seconds
    while _time.monotonic() < end:
        if cond():
            return True
        _time.sleep(0.01)
    return cond()


def _join_drainer(e, seconds=10.0):
    """Wait for the engine's drainer, and any it started on its way out."""
    end = _time.monotonic() + seconds
    while _time.monotonic() < end:
        t = getattr(e, "_drainer", None)
        if t is None:
            return True
        try:
            t.join(0.05)
        except RuntimeError:              # not started: a mutated kick
            _time.sleep(0.05)
    return getattr(e, "_drainer", None) is None


eng5 = _qengine()
_kick = srcslice.attr(eng5, "kick_drain", label="UpdateEngine.kick_drain")
_gate = threading.Event()
_ran5, _threads5 = [], []


def _one5(key):
    _threads5.append(threading.current_thread())
    _ran5.append((key, eng5._update_lock.locked()))
    if key == "a":
        _gate.wait(10)
    return None


eng5._drain_one = _one5
for _k in ("a", "b"):
    eng5.enqueue_update(_k)
_barrier = threading.Barrier(2)
_got = []


def _kicker():
    _barrier.wait()
    _got.append(_kick() if _kick is not None else None)


_ks = [threading.Thread(target=_kicker) for _ in range(2)]
for _t in _ks:
    _t.start()
for _t in _ks:
    _t.join(5)
_wait_until(lambda: _ran5[:1] == [("a", True)])
checks["two kicks at once start one drainer"] = (
    _kick is not None and len(_got) == 2
    and sum(1 for g in _got if g is not None) == 1)
eng5.enqueue_update("c")
_third = _kick() if _kick is not None else "no kick_drain"
checks["…a kick while it works starts no second one"] = _third is None
_gate.set()
_join_drainer(eng5)
checks["…and that one drainer runs every entry once, in order"] = (
    [k for k, _h in _ran5] == ["a", "b", "c"])
checks["…each under its claim, on one thread"] = (
    all(h for _k, h in _ran5) and len(set(_threads5)) == 1
    and not eng5._update_lock.locked())
checks["…and lets go of the slot: the next kick starts a drainer again"] = (
    _kick is not None and getattr(eng5, "_drainer", "x") is None
    and eng5.enqueue_update("d") == 1 and _kick() is not None
    and _join_drainer(eng5) and _ran5[-1][0] == "d")
checks["a kick with nothing queued starts nothing"] = (
    _kick is not None and _kick() is None)

# A drainer that finds the lock held leaves the entry at the head, and
# the release that frees the lock works it off.
eng6 = _qengine()
_ran6 = []
eng6._drain_one = lambda key: _ran6.append(key)
eng6.claim_update()
eng6.enqueue_update("a")
_t6 = _kick6 = srcslice.attr(eng6, "kick_drain", label="UpdateEngine.kick_drain")
if _kick6 is not None:
    _t6 = _kick6()
    if _t6 is not None:
        _t6.join(5)
checks["a drainer that finds the lock held runs nothing and leaves the entry"] = (
    _kick6 is not None and _ran6 == []
    and [k for k, _ in eng6.queued_updates()] == ["a"]
    and eng6._update_lock.locked())
eng6.release_update()
_join_drainer(eng6)
checks["…and the release that frees the lock works it off"] = (
    _ran6 == ["a"] and not eng6.has_queued_updates()
    and not eng6._update_lock.locked())

# Every release kicks, after its own `after()`. A queued self-update is
# what `after` hands on, so when the lock frees with a self-update AND
# containers waiting, the self-update goes first — the order it had
# before (decision 18.11.2026) — and the queue runs once it is done.
eng7 = _qengine()
_order7 = []
eng7._drain_one = lambda key: _order7.append(key)
eng7.claim_update()
eng7.enqueue_update("a")
_su7 = [("2.9.0",)]                       # one self-update, queued


_drainer_during_after = []


def _after7():
    # What `after` does on every release: hand on a queued self-update,
    # nothing when none is waiting.
    _drainer_during_after.append(getattr(eng7, "_drainer", None) is not None)
    if _su7:
        _su7.pop()
        _order7.append("self-update")


eng7.release_update(after=_after7)
_join_drainer(eng7)
checks["a release works off what queued behind it"] = "a" in _order7
checks["…after its own after(): a queued self-update goes first"] = (
    _order7 == ["self-update", "a"]
    and _drainer_during_after[:1] == [False])

# The release that frees the lock can come while the drainer that lost
# its claim still holds the slot; that release's kick then starts
# nothing. The drainer sweeps once on its way out, so the entry is not
# left for the next poll or pass.
eng10 = _qengine()
_ran10 = []
eng10._drain_one = lambda key: _ran10.append(key)
eng10.claim_update()
eng10.enqueue_update("a")
_real_dq10 = eng10.drain_queue
_calls10 = []


def _dq10(*a, **k):
    _calls10.append(1)
    r = _real_dq10(*a, **k)
    if len(_calls10) == 1:
        # The holder lets go now, while this drainer still has the slot.
        eng10.release_update()
    return r


eng10.drain_queue = _dq10
if getattr(eng10, "kick_drain", None) is not None:
    eng10.kick_drain()
_join_drainer(eng10)
checks["a release that lands while the drainer leaves is not lost"] = (
    _ran10 == ["a"] and not eng10.has_queued_updates()
    and not eng10._update_lock.locked())

# A drain that raised is not started again on its own: the same entry
# would raise again, on a new thread, as often as one could be started.
eng11 = _qengine()
eng11.enqueue_update("a")
_calls11 = []


def _dq11(*a, **k):
    _calls11.append(1)
    if len(_calls11) == 1:
        raise RuntimeError("drain broke")
    # A later drain works: the entry goes, so a restarted drainer would
    # be counted here once and then stop, rather than spin.
    eng11.drop_queued_updates()


eng11.drain_queue = _dq11
with _cl.redirect_stdout(_io.StringIO()) as _out11:
    if getattr(eng11, "kick_drain", None) is not None:
        eng11.kick_drain()
    _join_drainer(eng11)
checks["a drain that raised is logged and not restarted by itself"] = (
    _calls11 == [1] and "Queue drain failed: drain broke" in _out11.getvalue()
    and getattr(eng11, "_drainer", "x") is None)

# The same self-update finding a new image: the helper is swapping, the
# lock stays held, the process is about to go. Nothing runs, and what
# will not run is named.
eng8 = _qengine()
_ran8, _said8 = [], []
eng8._drain_one = lambda key: _ran8.append(key)
eng8._say_drain = lambda key, params: _said8.append((key, params))
eng8.claim_update()
for _k in ("a", "b"):
    eng8.enqueue_update(_k)


def _swaps():
    # What selfupdate.start does once the helper is up: holds the lock
    # and says the swap is under way.
    eng8._update_lock.acquire(blocking=False)
    eng8._swap_in_flight = True


eng8.release_update(after=_swaps)
_join_drainer(eng8)
checks["…and one that restarts: nothing queued runs, and it is named"] = (
    _ran8 == [] and _said8 == [("update_queue_dropped_selfupdate",
                                {"keys": ["a", "b"]})]
    and not eng8.has_queued_updates())
eng8._swap_in_flight = False
eng8._update_lock.release()

# ── the real drain, in the callback matrix's world ───────────────────
# A real TelegramBot and engine over the five-host registry, the real
# `_drain_one` → `run_keys` → batch, a recording notifier behind a real
# Broadcast on both, wired as main.py wires them. Every Telegram line and
# every card is marked with whether it went out through the broadcast:
# the drain has one sink, and a line straight through `send_message`
# would be a second.
import test_telegram_callback_matrix as _t3  # noqa: E402
from broadcast import Broadcast as _Broadcast  # noqa: E402

_INSIDE = threading.local()


class _SeenSeam(_Broadcast):
    """The real seam, marking what passes through it."""

    def _through(self, name, *a, **k):
        _INSIDE.on = True
        try:
            return getattr(_Broadcast, name)(self, *a, **k)
        finally:
            _INSIDE.on = False

    def announce(self, *a, **k):
        return self._through("announce", *a, **k)

    def tell(self, *a, **k):
        return self._through("tell", *a, **k)

    def update_result(self, *a, **k):
        return self._through("update_result", *a, **k)

    def update_line(self, *a, **k):
        return self._through("update_line", *a, **k)


class _Plugins:
    """Discord, webhook, e-mail, ntfy."""

    def __init__(self):
        self.results, self.messages = [], []

    def has_channels(self):
        return True

    def send_update_result(self, name, image, success, detail="",
                           source_url=""):
        self.results.append((name, success, getattr(_INSIDE, "on", False)))

    def send_message(self, text, skip=(), urgent=False):
        self.messages.append((text, getattr(_INSIDE, "on", False)))


def _world(telegram=True):
    import hosts as hostslib
    hostslib.forget_unreachable()
    _t3.m.EVENTS.clear()
    del _t3.m.STARTED[:]
    del _t3.m.THREAD_ERRORS[:]
    row = _t3.Row("multi")
    rec = _Plugins()
    row.bot.notifier = rec
    seam = _SeenSeam(telegram=row.bot, notifier=rec, log=lambda *a: None)
    row.bot.broadcast = seam
    row.bot.engine.broadcast = seam
    if not telegram:
        row.bot.config.bot_token = ""
    tg = row.bot.send_message
    lines = []

    def send_message(text, *a, **k):
        lines.append((text, getattr(_INSIDE, "on", False)))
        return tg(text, *a, **k)
    row.bot.send_message = send_message
    ran = []
    cls = type(row.checker)
    orig = cls.update_container

    def update_container(self, name, image, *a, **kw):
        ran.append(f"{self.backend.name}:{name}")
        return orig(self, name, image, *a, **kw)
    cls.update_container = update_container
    row._restore = lambda: setattr(cls, "update_container", orig)
    return row, rec, lines, ran


def _close(row):
    row._restore()
    import shutil
    shutil.rmtree(row.data, ignore_errors=True)


# Queued behind somebody else's claim (the scheduler's, say), worked off
# when that claim is given back.
row, rec, lines, ran = _world()
_eng = row.bot.engine
_eng.enqueue_update("web")
_eng.enqueue_update("nas/web")
_eng.enqueue_update("db")                 # not pending: nothing to run
with _cl.redirect_stdout(_io.StringIO()):
    if _eng.claim_update():
        _eng.release_update()
    _t3._join()
_texts = [t for t, _on in lines]
checks["drained after a release: both pending entries run, each on its host"] = (
    ran == ["local:web", "nas:web"] and not _eng.has_queued_updates())
checks["…every line Telegram hears comes through the broadcast"] = (
    bool(lines) and all(on for _t, on in lines))
checks["…and every card the other channels get"] = (
    [(n, s) for n, s, _on in rec.results] == [("web", True), ("web", True)]
    and all(on for _n, _s, on in rec.results))
checks["…Telegram hears 'Updating' and one result line per container"] = (
    sum(1 for t in _texts if t.startswith("⏳")) == 2
    and sum(1 for t in _texts if "updated (stand-in)" in t) == 2)
checks["…and the result lines are not copied to the other channels"] = (
    not any(t.startswith("⏳") or "stand-in" in t for t, _on in rec.messages))
from i18n import get_translator as _gt  # noqa: E402
_already = _gt("en")("update_already_done", name="db")
checks["…what did not run is told to every channel"] = (
    _already in _texts and (_already, True) in rec.messages)
_close(row)

# A drained head that fails takes its queued group-mate off the queue,
# found through the engine's own group lookup on the host's store, and
# says so to every channel.
row, rec, lines, ran = _world()
_eng = row.bot.engine
row.do(("add_pending", "db", "postgres:16", "local"))
row.do(("group", "local", "g1", ["web", "db"]))
_cls = type(row.checker)
_ok = _cls.update_container


def _web_fails(self, name, image, *a, **kw):
    r = _ok(self, name, image, *a, **kw)
    return (False, "pull failed (stand-in)") if name == "web" else r


_cls.update_container = _web_fails
_eng.enqueue_update("web")
_eng.enqueue_update("db")
with _cl.redirect_stdout(_io.StringIO()):
    if _eng.claim_update():
        _eng.release_update()
    _t3._join()
_cls.update_container = _ok
_skipped = [t for t, _on in lines if "Skipped the rest of group" in t]
checks["drained: a failed head takes its queued group-mate off"] = (
    ran == ["local:web"] and not _eng.has_queued_updates())
checks["…and every channel is told which"] = (
    len(_skipped) == 1 and "`db`" in _skipped[0] and "`g1`" in _skipped[0]
    and any("Skipped the rest of group" in t and on
            for t, on in rec.messages))
_close(row)

# Headless: Telegram switched off, so no poll loop and no Telegram path
# ever lets go of the lock. The scheduler's pass ends in `_after_tick`,
# and that is enough for a queued update to run.
import scheduler as _scheduler  # noqa: E402
import notify_retry as _notify_retry  # noqa: E402

_real_flush = _notify_retry.flush
_notify_retry.flush = lambda: (0, 0)          # the network is not under test
try:
    row, rec, lines, ran = _world(telegram=False)
    _eng = row.bot.engine
    _eng.enqueue_update("web")
    _s = _scheduler.Scheduler.__new__(_scheduler.Scheduler)
    _s.engine = _eng
    with _cl.redirect_stdout(_io.StringIO()):
        _s._after_tick()
        _t3._join()
    checks["headless: a queued update runs without any Telegram poll loop"] = (
        ran == ["local:web"] and not _eng.has_queued_updates())
    checks["…its result reaches the other channels"] = (
        [(n, s) for n, s, _on in rec.results] == [("web", True)])
    checks["…the entry leaves the pending list"] = not any(
        u.get("name") == "web" and (u.get("host") or "local") == "local"
        for u in json.load(open(row.bot.config.pending_file)))
    checks["…and a switched-off Telegram hears nothing"] = (
        not [t for t, _on in lines])
    _close(row)

    # `_after_tick` kicks the drain and goes on: it runs on the
    # scheduler's own thread, and a five-container queue must not hold
    # the next pass up for ten minutes.
    eng9 = _qengine()
    _gate9 = threading.Event()
    _ran9 = []

    def _one9(key):
        _ran9.append(key)
        _gate9.wait(10)

    eng9._drain_one = _one9
    eng9.enqueue_update("a")
    _s = _scheduler.Scheduler.__new__(_scheduler.Scheduler)
    _s.engine = eng9
    _spied = []
    _real_sweep = UpdateEngine.sweep_queue

    def _spy_sweep(self, drain):
        _spied.append(drain)
        return _real_sweep(self, drain)

    UpdateEngine.sweep_queue = _spy_sweep
    try:
        _t0 = _time.monotonic()
        _s._after_tick()
        _took = _time.monotonic() - _t0
    finally:
        UpdateEngine.sweep_queue = _real_sweep
    _started = _wait_until(lambda: _ran9 == ["a"])
    checks["the scheduler reaches sweep_queue through _after_tick"] = (
        len(_spied) == 1 and _spied[0] == getattr(eng9, "kick_drain", None))
    checks["…and returns while the drain it kicked is still running"] = (
        _started and _took < 1.0 and getattr(eng9, "_drainer", None) is not None)
    _gate9.set()
    _join_drainer(eng9)
    # Without an engine the pass still ends cleanly.
    _s0 = _scheduler.Scheduler.__new__(_scheduler.Scheduler)
    _raised0 = None
    try:
        _s0._after_tick()
    except Exception as e:
        _raised0 = e
    checks["…and a scheduler without an engine still ends its pass"] = (
        _raised0 is None)
finally:
    _notify_retry.flush = _real_flush


# ── it is actually wired into the poll loop ──────────────────────────
# The blocks above prove the sweep works. They say nothing about whether
# anything calls it — a correct, unreachable helper is the exact shape
# the original bug had. Read as a tree, because a text search would also
# be satisfied by the name in a comment. Since Q13 the poll loop hands
# the engine's sweep the engine's own drainer, as the scheduler does.

import ast  # noqa: E402

_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "telegram_bot.py")).read()
_tree = ast.parse(_src)
_callers = []
for node in ast.walk(_tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "sweep_queue"
                and len(sub.args) == 1
                and isinstance(sub.args[0], ast.Attribute)
                and sub.args[0].attr == "kick_drain"):
            _callers.append(node.name)

checks["something hands the sweep the engine's drainer"] = bool(_callers)
# It belongs on the loop that runs regardless of what the user does —
# hanging it off a command would reproduce the original bug in a new
# place.
_loop = [n for n in ast.walk(_tree)
         if isinstance(n, ast.FunctionDef)
         and any(isinstance(s, ast.While) for s in ast.walk(n))
         and "getUpdates" in (ast.get_source_segment(_src, n) or "")]
checks["…from the poll loop, not from a command"] = (
    bool(_loop) and _loop[0].name in _callers)
# And Telegram keeps no drain of its own: one drainer, the engine's.
_tg_drains = sorted({n.name for n in ast.walk(_tree)
                     if isinstance(n, ast.FunctionDef)
                     for sub in ast.walk(n)
                     if isinstance(sub, ast.Call)
                     and getattr(sub.func, "attr", "") == "drain_queue"})
checks["…and Telegram runs no drain loop of its own"] = _tg_drains == []


# main.py gives the engine the shared self-update context, so a
# self-update queued behind a drained entry is handed on by the drainer
# the way every front end hands it on after its own release.
_main = ast.parse(open(os.path.join(os.path.dirname(__file__), "..", "app",
                                    "main.py")).read())
checks["main.py hands the engine the shared self-update context"] = any(
    isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Attribute) and t.attr == "selfupdate_ctx"
        and isinstance(t.value, ast.Name) and t.value.id == "engine"
        for t in n.targets)
    and isinstance(n.value, ast.Name) and n.value.id == "su_ctx"
    for n in ast.walk(_main))

# ── what the two messages actually say ───────────────────────────────
# The sweep stops entries stranding. These two stop the *answer* being
# misleading, which is the half the person pressing the button sees.

from i18n import get_translator  # noqa: E402

for _lang in ("en", "de"):
    _t = get_translator(_lang)
    _msg = _t("update_already_queued", name="portainer @srv40", since="27h")
    checks[f"{_lang}: the queued answer names the container"] = (
        "portainer @srv40" in _msg)
    # The whole point: it must say HOW LONG. Without this the message
    # reads as "you already pressed this" to a first-time press.
    checks[f"{_lang}: …and how long it has been waiting"] = "27h" in _msg
    checks[f"{_lang}: …and does not leave a placeholder showing"] = (
        "{" not in _msg and "}" not in _msg)
    checks[f"{_lang}: status has a word for the queue"] = (
        _t("status_queued") != "status_queued")

# …and /status reads the queue from the engine rather than inventing it.
_status_fn = [n for n in ast.walk(_tree)
              if isinstance(n, ast.FunctionDef)
              and "status_queued" in (ast.get_source_segment(_src, n) or "")]
checks["the status page reads the queue"] = bool(_status_fn) and any(
    isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "queued_updates"
    for n in _status_fn for c in ast.walk(n))

update_engine.time.time = _real_time

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
