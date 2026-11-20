#!/usr/bin/env python3
"""The scheduler's end-of-pass work has one place: Scheduler._after_tick.

The scheduler loop is the one thing that runs on its own whatever else
is configured — no Telegram poll, no Discord, no Web UI needed. So far
it ends every pass by giving held notifications their next go
(notify_retry.flush, #66) inline, right before its 30 s sleep. The queue
sweep (Q13) has to hang off the same spot, and on installs without a
Telegram poll loop it is the only spot there is. Moving the whole loop
for that would be a far bigger change than it needs, so the end of a
pass gets a method of its own and the scheduler gets the engine.

Driven here:

- the engine: passed in, or taken from the bot when not (main.py passes
  it; the older three-argument call keeps working);
- `_after_tick` itself: flushes once, and a flush that raises stays in
  the scheduler's log rather than ending the thread;
- the real `_run`, with the clock and the sleep stood in for: every pass
  goes through `_after_tick`, and the flush happens before that pass's
  sleep, not after it;
- read as a tree: the loop's last two statements are `self._after_tick()`
  and `time.sleep(30)`, and the flush sits in `_after_tick` alone.

Exits non-zero on any failure.
"""
import ast
import contextlib
import io
import os
import sys
import tempfile
import types

import srcslice

HERE = os.path.dirname(__file__)
APP = os.path.join(HERE, "..", "app")
sys.path.insert(0, APP)

import notify_retry                                          # noqa: E402
import scheduler                                             # noqa: E402
import weekly_report                                         # noqa: E402

checks = {}


def check(label, ok, why=""):
    checks[label] = bool(ok)
    if not ok and why:
        print(f"      → {label}: {why}")


def _config(tmp):
    return types.SimpleNamespace(
        cron_schedule="0 0 31 2 *",          # never: no cron tick interferes
        monitor_enabled=False,
        disk_check_interval_seconds=300,
        monitor_interval_seconds=60,
        maintenance_file=os.path.join(tmp, "maintenance.json"),
        language="en")


_tmp = tempfile.mkdtemp()
_MISSING = object()

# ── which engine the scheduler holds ─────────────────────────────────
engine_a = object()
engine_b = object()

s1 = scheduler.Scheduler(_config(_tmp), object(),
                         types.SimpleNamespace(engine=engine_a))
got = srcslice.attr(s1, "engine", label="Scheduler.engine")
check("a bot that carries an engine hands it to the scheduler",
      got is engine_a, f"got {got!r}")

try:
    s2 = scheduler.Scheduler(_config(_tmp), object(),
                             types.SimpleNamespace(engine=engine_a),
                             engine=engine_b)
except TypeError as e:
    s2 = None
    print(f"      → Scheduler(engine=…): {e}")
got = None if s2 is None else getattr(s2, "engine", _MISSING)
check("an engine passed in wins over the bot's", got is engine_b,
      f"got {got!r}")

s3 = scheduler.Scheduler(_config(_tmp), object(), object())
got = getattr(s3, "engine", _MISSING)
check("a bot without an engine leaves the scheduler without one",
      got is None, f"got {got!r}")

# ── _after_tick on its own ───────────────────────────────────────────
events = []
_real_flush = notify_retry.flush


def _flush_rec(*a, **k):
    events.append("flush")
    return (0, 0)


def _flush_boom(*a, **k):
    events.append("flush")
    raise RuntimeError("network still down")


tick = srcslice.attr(s3, "_after_tick", label="Scheduler._after_tick")
notify_retry.flush = _flush_rec
try:
    if tick is not None:
        tick()
finally:
    notify_retry.flush = _real_flush
check("_after_tick gives held notifications their next go, once",
      tick is not None and events == ["flush"], f"events {events}")

events.clear()
raised = None
log = io.StringIO()
notify_retry.flush = _flush_boom
try:
    with contextlib.redirect_stdout(log):
        if tick is not None:
            tick()
except Exception as e:
    raised = e
finally:
    notify_retry.flush = _real_flush
check("a flush that raises does not escape _after_tick",
      tick is not None and raised is None and events == ["flush"],
      f"raised {raised!r}, events {events}")
check("…and it says so in the log",
      tick is not None and "Notify retry error: network still down"
      in log.getvalue(), f"log {log.getvalue()!r}")


# ── through the real loop ────────────────────────────────────────────
def run_loop(passes, spy_tick=False, flush=_flush_rec):
    """Run `_run` for `passes` passes of its 30 s sleep and return what
    happened, in order: "tick" (when spied), "flush", "sleep"."""
    clk = {"t": 1000.0, "n": 0}
    s = scheduler.Scheduler(_config(_tmp), object(), object())
    s._check_disk_space = lambda: None
    if spy_tick:
        real = srcslice.attr(s, "_after_tick", label="Scheduler._after_tick")

        def _spy():
            events.append("tick")
            if real is not None:
                real()
        s._after_tick = _spy

    def fake_sleep(sec):
        events.append(f"sleep {sec}")
        clk["t"] += sec
        clk["n"] += 1
        if clk["n"] >= passes:
            s.running = False

    saved = (scheduler.time, weekly_report.maybe_send_weekly_report,
             notify_retry.flush)
    scheduler.time = types.SimpleNamespace(
        monotonic=lambda: clk["t"], sleep=fake_sleep, time=lambda: clk["t"])
    weekly_report.maybe_send_weekly_report = lambda *a, **k: False
    notify_retry.flush = flush
    s.running = True
    crashed = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            s._run()
    except Exception as e:
        crashed = e
    finally:
        (scheduler.time, weekly_report.maybe_send_weekly_report,
         notify_retry.flush) = saved
    return crashed


events.clear()
crashed = run_loop(3)
check("every pass of the loop flushes, before its sleep",
      crashed is None and events == ["flush", "sleep 30"] * 3,
      f"crashed {crashed!r}, events {events}")

events.clear()
crashed = run_loop(3, spy_tick=True)
check("…and it gets there through _after_tick",
      crashed is None and events == ["tick", "flush", "sleep 30"] * 3,
      f"crashed {crashed!r}, events {events}")

events.clear()
crashed = run_loop(3, flush=_flush_boom)
check("a flush that keeps raising does not stop the loop",
      crashed is None and events == ["flush", "sleep 30"] * 3,
      f"crashed {crashed!r}, events {events}")

# ── read as a tree ───────────────────────────────────────────────────
# Driving proves the order on the passes this procedure runs. The tree
# says it holds for the loop as written, and that the flush did not stay
# behind as a second copy next to the call.
_src = open(os.path.join(APP, "scheduler.py"), encoding="utf-8").read()
_tree = ast.parse(_src)
_cls = next((n for n in _tree.body
             if isinstance(n, ast.ClassDef) and n.name == "Scheduler"), None)
_methods = {} if _cls is None else {
    n.name: n for n in _cls.body if isinstance(n, ast.FunctionDef)}


def _is_call(stmt, owner, attr):
    return (isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == attr
            and isinstance(stmt.value.func.value, ast.Name)
            and stmt.value.func.value.id == owner)


_run_fn = _methods.get("_run")
_loop = None if _run_fn is None else next(
    (n for n in ast.walk(_run_fn) if isinstance(n, ast.While)
     and ast.unparse(n.test) == "self.running"), None)
_tail = [] if _loop is None else _loop.body[-2:]
_tail_ok = (len(_tail) == 2
            and _is_call(_tail[0], "self", "_after_tick")
            and _is_call(_tail[1], "time", "sleep")
            and [ast.unparse(a) for a in _tail[1].value.args] == ["30"])
check("the loop ends a pass with self._after_tick(), then time.sleep(30)",
      _tail_ok, f"tail {[ast.unparse(t) for t in _tail]}")

_flushers = []
for name, fn in _methods.items():
    for sub in ast.walk(fn):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "flush"
                and ast.unparse(sub.func.value) == "notify_retry"):
            _flushers.append(name)
check("the scheduler flushes in _after_tick and nowhere else",
      _flushers == ["_after_tick"], f"flush called from {_flushers}")

# ── main.py hands the scheduler its engine ───────────────────────────
_msrc = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
_calls = [n for n in ast.walk(ast.parse(_msrc))
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
          and n.func.id == "Scheduler"]
check("main.py builds the scheduler once",
      len(_calls) == 1, f"{len(_calls)} Scheduler(...) calls")
check("…and passes it the engine it built",
      len(_calls) == 1 and any(
          k.arg == "engine" and isinstance(k.value, ast.Name)
          and k.value.id == "engine" for k in _calls[0].keywords),
      "" if len(_calls) != 1 else f"keywords "
      f"{[k.arg for k in _calls[0].keywords]}")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
