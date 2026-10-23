#!/usr/bin/env python3
"""Queued work must not depend on who happens to hold the lock.

`_drain_update_queue` runs in exactly one place: at the end of a
Telegram single update, after it has released the lock. The lock is
taken in eleven places — the scheduled run, the Web UI, a self-update
and six other Telegram paths among them. Queue something while one of
THOSE holds it and the entry waits for the next Telegram single update
to finish, however long that is.

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

The clock is stood in for, because a procedure that waits 27 hours
measures patience. The queue itself is the real one.
"""

import os
import sys
import types

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

# ── the sweep: work nobody else would drain ──────────────────────────
# A real bot object with only the collaborators the sweep touches. The
# decision under test — "is there queued work and is the lock free" —
# is the real one; `_drain_update_queue` is observed, not replaced,
# because what is being measured is WHETHER it is reached.

import threading  # noqa: E402

lock = threading.Lock()


def _bot(engine):
    b = TelegramBot.__new__(TelegramBot)
    b.engine = engine
    engine._update_lock = lock
    b._drained = []
    b._drain_update_queue = lambda checker, last_group=None: (
        b._drained.append(checker))
    return b


eng2 = _engine(clock)
eng2._update_lock = lock
bot = _bot(eng2)

# Nothing queued: the sweep must not call the drain at all. A sweep
# that always drains would pass every check below.
bot._sweep_update_queue("checker-A")
checks["an empty queue does not reach the drain"] = bot._drained == []

# Queued, lock free: this is the case that was being dropped.
eng2.enqueue_update("srv40/portainer")
bot._sweep_update_queue("checker-A")
checks["queued work with a free lock is drained"] = bot._drained == ["checker-A"]
checks["…and the poll loop's checker is handed through"] = (
    bot._drained == ["checker-A"])

# Queued, lock held by somebody else: leave it. The holder will either
# drain it or the next sweep will.
bot._drained.clear()
lock.acquire()
try:
    bot._sweep_update_queue("checker-B")
    checks["a held lock is left alone"] = bot._drained == []
finally:
    lock.release()

# …and the very next sweep picks it up, which is the whole point: the
# wait is now one poll cycle rather than "until a Telegram update ends".
bot._sweep_update_queue("checker-C")
checks["…and the next sweep takes it"] = bot._drained == ["checker-C"]

# The sweep runs inside the listener loop. A failure there must not take
# the bot down — losing the listener to retry a queued update would be a
# far worse trade than skipping one sweep.
_broken = _bot(_engine(clock))
_broken.engine._update_lock = threading.Lock()
_broken.engine.enqueue_update("x/y")
_broken._drain_update_queue = lambda *a, **k: 1 / 0
_raised = None
try:
    _broken._sweep_update_queue("checker")
except Exception as e:
    _raised = e
checks["a failing drain does not escape the sweep"] = _raised is None

# ── it is actually wired into the poll loop ──────────────────────────
# The two blocks above prove the sweep works. They say nothing about
# whether anything calls it — a correct, unreachable helper is the exact
# shape the original bug had. Read as a tree, because a text search
# would also be satisfied by the name in a comment.

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
                and sub.func.attr == "_sweep_update_queue"):
            _callers.append(node.name)

checks["something calls the sweep"] = bool(_callers)
# It belongs on the loop that runs regardless of what the user does —
# hanging it off a command would reproduce the original bug in a new
# place.
_loop = [n for n in ast.walk(_tree)
         if isinstance(n, ast.FunctionDef)
         and any(isinstance(s, ast.While) for s in ast.walk(n))
         and "getUpdates" in (ast.get_source_segment(_src, n) or "")]
checks["…from the poll loop, not from a command"] = (
    bool(_loop) and _loop[0].name in _callers)

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
