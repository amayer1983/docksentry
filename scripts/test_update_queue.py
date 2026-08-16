#!/usr/bin/env python3
"""Tapping four containers runs four, not one.

From the owner's own instance, screenshotted: he gets the update
notification, taps several containers in a row, and only the first one
runs. The rest answer

    ⚠️ Update läuft bereits...

and are then **thrown away**. Nothing remembers them, so he has to come
back after each update finishes and tap the next one again — and nothing
tells him which ones never ran. His words: "dann laufen nicht alle durch".

The lock itself is right. Two updates recreating containers at the same
time is the bug v1.23.1 was added to prevent, and it stays prevented. What
was wrong is what happened to the rejected taps: nothing at all.

So they queue. The design decisions, all deliberate:

**The lock is re-taken per entry, not held across the queue.** Holding it
for five containers would lock out the scheduler, "update all" and a
queued self-update for ten minutes. Re-acquiring per entry keeps the
concurrency guard while letting other work in between.

**A failure takes its group-mates out of the queue.** Group order exists
because those containers depend on each other; updating the next one
against a head that just failed is how an app ends up talking to a
database that rolled back. The automatic batch already works this way.
Containers with no group are unaffected — the owner asked for exactly
this: "wenn einer fehlschlägt mit dem nächsten weiter außer es gibt
Abhängigkeiten".

**A pending self-update stops the drain and names what is left.** The
queue lives in memory and a self-update restarts the process, so carrying
on would drop the rest silently — the exact failure mode that cost
@famewolf ten days in #2 and that this whole area has spent a week
fixing. Announcing it is the difference between a limitation and a bug.

**And it is bounded.** A queue anyone can fill by tapping is a queue
anyone can fill by tapping.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_engine import UpdateEngine  # noqa: E402

checks = {}


def engine():
    e = UpdateEngine.__new__(UpdateEngine)
    import threading
    e._update_queue = []
    e._update_queue_lock = threading.Lock()
    e._update_lock = threading.Lock()      # the real shared update mutex
    e._queued_selfupdate = None
    return e


# ── the queue itself ─────────────────────────────────────────────────
e = engine()
checks["a queued container reports its position"] = (
    e.enqueue_update("a") == 1 and e.enqueue_update("b") == 2)
checks["a repeat tap is recognised, not queued twice"] = (
    e.enqueue_update("a") == 0 and len(e._update_queue) == 2)
checks["entries come back out in the order they were tapped"] = (
    e.take_queued_update() == "a" and e.take_queued_update() == "b")
checks["an empty queue hands back nothing"] = e.take_queued_update() is None

e = engine()
for i in range(UpdateEngine.UPDATE_QUEUE_MAX):
    e.enqueue_update(f"c{i}")
checks["the queue is bounded"] = e.enqueue_update("one-too-many") == -1
checks["…and the cap is a refusal, not a silent drop"] = (
    len(e._update_queue) == UpdateEngine.UPDATE_QUEUE_MAX)

# ── dropping by predicate, for the group rule ────────────────────────
e = engine()
for k in ("db", "app", "unrelated"):
    e.enqueue_update(k)
taken = e.drop_queued_updates(lambda k: k in ("db", "app"))
checks["group-mates can be taken out together"] = sorted(taken) == ["app", "db"]
checks["…and everything else stays queued"] = e._update_queue == ["unrelated"]
checks["dropping everything returns everything"] = (
    e.drop_queued_updates() == ["unrelated"] and e._update_queue == [])

# ── the bot side: what the owner's four taps now do ──────────────────
import telegram_bot  # noqa: E402


class Bot(telegram_bot.TelegramBot):
    """Just enough bot to drive the queue paths."""

    def __init__(self, eng, fails=(), groups=None):
        self.engine = eng
        self.sent = []
        self.updated = []
        self.fails = set(fails)
        self.groups = groups or {}
        self.config = types.SimpleNamespace(pending_file="/nonexistent")
        self._queued_selfupdate = None
        self._last_update_group = None

    def send_message(self, text, **kw):
        self.sent.append(text)

    def t(self, key, **kw):
        return key + (" " + " ".join(f"{k}={v}" for k, v in kw.items()) if kw else "")

    def _group_of(self, key):
        return self.groups.get(self._short_key(key))

    def _run_single_update(self, checker, key, from_queue=False):
        """Stand in for the real one: record it, honour `fails`."""
        self.updated.append(key)
        name = self._short_key(key)
        self._last_update_group = (self._group_of(key) if name in self.fails
                                   else None)


eng = engine()
b = Bot(eng)
for k in ("paperless-ngx-db-1", "aktien-tool-db-1", "paperless-ngx-webserver-1"):
    eng.enqueue_update(k)
b._drain_update_queue(checker=None)
checks["every queued tap actually runs"] = b.updated == [
    "paperless-ngx-db-1", "aktien-tool-db-1", "paperless-ngx-webserver-1"]
checks["…and the queue ends up empty"] = eng._update_queue == []

# ── a failure carries on, unless there are dependencies ─────────────
eng = engine()
b = Bot(eng, fails={"broken"})
for k in ("broken", "unrelated-1", "unrelated-2"):
    eng.enqueue_update(k)
b._drain_update_queue(checker=None)
checks["a failure with no group does not stop the rest"] = (
    b.updated == ["broken", "unrelated-1", "unrelated-2"])

eng = engine()
b = Bot(eng, fails={"gluetun"},
        groups={"gluetun": "vpn", "gluetun-sonarr": "vpn",
                "gluetun-radarr": "vpn", "searxng": None})
for k in ("gluetun", "gluetun-sonarr", "gluetun-radarr", "searxng"):
    eng.enqueue_update(k)
b._drain_update_queue(checker=None)
checks["a failure in a group skips its group-mates"] = (
    "gluetun-sonarr" not in b.updated and "gluetun-radarr" not in b.updated)
checks["…but not containers outside that group"] = "searxng" in b.updated
checks["…and it says which ones it skipped, and why"] = any(
    "update_queue_skipped_group" in m and "gluetun-sonarr" in m
    for m in b.sent)

# ── a pending self-update stops the drain and names the casualties ──
eng = engine()
b = Bot(eng)
b._queued_selfupdate = ("2.9.0",)
for k in ("one", "two"):
    eng.enqueue_update(k)
b._drain_update_queue(checker=None)
checks["a pending self-update stops the drain"] = b.updated == []
checks["…and names what will not run"] = any(
    "update_queue_dropped_selfupdate" in m and "one" in m and "two" in m
    for m in b.sent)
checks["…leaving nothing queued to be lost by the restart"] = (
    eng._update_queue == [])

# ── the source says what the docstring says ─────────────────────────
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "telegram_bot.py"), encoding="utf-8").read()
i = src.index("def _drain_update_queue")
drain = src[i:src.index("\n    @staticmethod", i)]
checks["the drain re-takes the lock per entry"] = (
    "_update_lock.acquire(blocking=False)" in drain)
checks["…and hands the entry back if it loses the race"] = (
    "self.engine.enqueue_update(key)" in drain)
checks["…rather than holding it across the whole queue"] = (
    drain.count("_update_lock.acquire") == 1)
# The rejected tap is queued, not answered with the old brush-off.
i = src.index("def _run_single_update")
single = src[i:src.index("\n    def _drain_update_queue", i)]
checks["a busy lock queues the tap instead of discarding it"] = (
    "self.engine.enqueue_update(container_key)" in single
    and "update_already_running" not in single)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
