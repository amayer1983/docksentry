#!/usr/bin/env python3
"""A real UpdateEngine, built the way main.py builds one.

The owner's own instance stopped updating anything and nobody noticed for
43 minutes. He tapped `gitlab` in the update notification and got

    🕐 gitlab eingereiht — Platz 1. Startet, sobald das laufende Update
    fertig ist.

except no update was running. His containers sat untouched, and the queue
entry died with the next restart.

The cause was one line in the wrong place. v2.9.0 added the update queue
by inserting three methods into the middle of `UpdateEngine.__init__`,
which left the tail of the constructor — `_swap_in_flight` and
`notifier` — sitting *after a return statement inside the last method*.
Dead code. The attributes were never set on any real engine.

Then:

1. the scheduled auto-selfupdate ran and took the update lock;
2. the pull failed, which is fine and handled;
3. the `finally` that releases the lock reads `_swap_in_flight` to decide
   whether the helper container is about to stop the process —
   **AttributeError, raised inside the finally, before release()**;
4. the lock stayed held for the life of the process. Every update after
   that answered "an update is already running" and queued behind a batch
   that had finished, forever.

Two things were wrong and both are fixed: the constructor is whole again,
and neither `finally` can raise before releasing — a lock release must
not depend on an attribute lookup succeeding.

**Why no test caught it:** every existing test builds its engine with
`UpdateEngine.__new__(UpdateEngine)` and sets the two or three attributes
it needs by hand, precisely to avoid the constructor's dependencies. That
is a reasonable fixture and it is also a blind spot the size of the
constructor. This one builds the real thing.
"""

import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from config import Config  # noqa: E402
from container_store import ContainerStore  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402

checks = {}

with tempfile.TemporaryDirectory() as tmp:
    os.environ["DATA_DIR"] = tmp
    config = Config.from_env()
    engine = UpdateEngine(config, ContainerStore(config))
    os.environ.pop("DATA_DIR", None)

    # Everything the constructor promises, on a real instance.
    for attr in ("_update_lock", "_update_queue", "_update_queue_lock",
                 "_queued_selfupdate", "_swap_in_flight", "notifier"):
        checks[f"__init__ sets {attr}"] = hasattr(engine, attr)

    checks["…and the lock starts free"] = engine.update_running is False
    checks["…the queue starts empty"] = engine._update_queue == []
    checks["…nothing is mid-swap on a fresh engine"] = (
        engine._swap_in_flight is False)

    # The methods that were inserted into the constructor still work.
    checks["the queue still queues"] = engine.enqueue_update("a") == 1
    checks["…and hands entries back"] = engine.take_queued_update() == "a"
    engine.enqueue_update("x")
    checks["…and drops them by predicate"] = (
        engine.drop_queued_updates() == ["x"])
    # The orphaned lines must not have landed inside one of them.
    import inspect  # noqa: E402
    body = inspect.getsource(UpdateEngine.drop_queued_updates)
    checks["no constructor code is stranded after a return"] = (
        "_swap_in_flight" not in body and "self.notifier" not in body)


# ── the lock release cannot be stopped by anything ───────────────────
class Boom:
    """An engine whose `_swap_in_flight` raises, like the broken one did."""

    def __init__(self):
        self._update_lock = threading.Lock()

    @property
    def _swap_in_flight(self):
        raise AttributeError("'UpdateEngine' object has no attribute "
                             "'_swap_in_flight'")


import telegram_bot  # noqa: E402


class Bot(telegram_bot.TelegramBot):
    def __init__(self, engine):
        self.engine = engine
        self.sent = []

    def send_message(self, text, **kw):
        self.sent.append(text)

    def t(self, key, **kw):
        return key

    def _selfupdate_locked(self, target, reply_to=None):
        raise RuntimeError("pull failed")


eng = Boom()
bot = Bot(eng)
try:
    bot.check_selfupdate(None)
except RuntimeError:
    pass  # the failure itself is expected and reported by the caller
except AttributeError:
    pass
checks["a self-update that fails still frees the lock"] = (
    eng._update_lock.acquire(blocking=False))
if eng._update_lock.locked():
    eng._update_lock.release()

# And the same for the auto path, which is the one that runs unattended
# on every scheduled check and therefore the one that wedged him.
eng2 = Boom()
bot2 = Bot(eng2)
try:
    bot2.check_selfupdate_auto()
except Exception:
    pass
checks["…and so does the scheduled one"] = (
    eng2._update_lock.acquire(blocking=False))

src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "telegram_bot.py"), encoding="utf-8").read()
checks["no release is guarded by a bare attribute read"] = (
    "if not self._swap_in_flight:\n                self._update_lock.release()"
    not in src)
checks["…they read it defensively instead"] = (
    src.count('swap = bool(getattr(self, "_swap_in_flight", False))') == 2)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
