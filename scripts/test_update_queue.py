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

**The lock is claimed per entry, not held across the queue.** Holding it
for five containers would lock out the scheduler, "update all" and a
queued self-update for ten minutes. Claiming per entry keeps the
concurrency guard while letting other work in between two entries —
and since Q10 only there: the drain holds each entry's claim through
that entry and is the one that gives it back.

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

import srcslice

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_engine import UpdateEngine  # noqa: E402

checks = {}


def engine():
    e = UpdateEngine.__new__(UpdateEngine)
    import threading
    e._update_queue = []
    # Queued-at timestamps, same shape `__init__` builds. These
    # procedures assemble the engine by hand rather than through
    # `__init__`, so a new field has to be added here too.
    e._queued_at = {}
    e._update_queue_lock = threading.Lock()
    e._update_lock = threading.Lock()      # the real shared update mutex
    e._queued_selfupdate = None
    # Since Q11c a single update runs through the shared batch, which
    # reads the host's groups and cooldowns and the disk setting.
    e.store = types.SimpleNamespace(get_groups=lambda: {},
                                    get_cooldown=lambda name: 0)
    e.config = types.SimpleNamespace(disk_warn_auto_cleanup=False)
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

    def _run_single_update(self, checker, key, from_queue=False,
                           claimed=False):
        """Stand in for the real one: record it, honour `fails`."""
        self.updated.append(key)
        name = self._short_key(key)
        self._last_update_group = (self._group_of(key) if name in self.fails
                                   else None)


eng = engine()
b = Bot(eng)
for k in ("paperless-ngx-db-1", "notes-app-db-1", "paperless-ngx-webserver-1"):
    eng.enqueue_update(k)
b._drain_update_queue(checker=None)
checks["every queued tap actually runs"] = b.updated == [
    "paperless-ngx-db-1", "notes-app-db-1", "paperless-ngx-webserver-1"]
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

# A run that raises is said, raw and cut to 200 characters, and the
# drain goes on with the next entry; the error does not carry a group.
eng = engine()
b = Bot(eng, groups={"bad": "vpn", "mate": "vpn"})
b.raises = {"bad"}
_orig_run = b._run_single_update


def _raising(checker, key, from_queue=False, claimed=False):
    _orig_run(checker, key, from_queue)
    if b._short_key(key) in b.raises:
        raise RuntimeError("x" * 300)


b._run_single_update = _raising
for k in ("bad", "mate"):
    eng.enqueue_update(k)
b._drain_update_queue(checker=None)
checks["a run that raises is said and the drain goes on"] = (
    b.updated == ["bad", "mate"]
    and "❌ `bad`: " + "x" * 200 in b.sent)

# ── the loop is the engine's (Q8) ────────────────────────────────────
# `UpdateEngine.drain_queue` holds the drain; a connection hands it
# `run_one` (update this key, return the group a failure takes down),
# `group_of` and `say`. `run_one` here is a recorder in place of the
# container update, not of the drain decision: the decisions are what
# these checks are about. `say` hands over an i18n key and raw keys,
# never a sentence.


def _drain(e, fails=(), groups=None, last_group=None, raises=(),
           during=None):
    """Drive `e.drain_queue` with a recorder; None if there is none."""
    fn = getattr(e, "drain_queue", None)
    if fn is None:
        return None
    groups = groups or {}
    rec = {"ran": [], "said": [], "held_while_running": []}

    def run_one(key):
        rec["ran"].append(key)
        rec["held_while_running"].append(e._update_lock.locked())
        if during:
            during(key)
        if key in raises:
            raise RuntimeError("boom " + key)
        return groups.get(key) if key in fails else None

    def say(key, params):
        rec["said"].append((key, params))

    fn(run_one, group_of=groups.get, say=say, last_group=last_group)
    return rec


e = engine()
for k in ("a", "b", "c"):
    e.enqueue_update(k)
r = _drain(e)
checks["engine: every queued key runs, in the order it was queued"] = (
    r is not None and r["ran"] == ["a", "b", "c"] and e._update_queue == []
    and r["said"] == [])
# Flipped in Q10: run_one used to run with the lock free and claim it
# itself, and in the moment between the drain's look and that claim
# anybody could take it — the entry then went to the back of the queue
# with a second "queued at position N". The drain holds the claim now.
checks["engine: run_one runs under the drain's claim, not with the lock free"] = (
    r is not None and r["held_while_running"] == [True, True, True]
    and not e._update_lock.locked())

e = engine()
for k in ("broken", "x", "y"):
    e.enqueue_update(k)
r = _drain(e, fails={"broken"})
checks["engine: a failure with no group does not stop the rest"] = (
    r is not None and r["ran"] == ["broken", "x", "y"] and r["said"] == [])

e = engine()
g = {"gluetun": "vpn", "sonarr": "vpn", "radarr": "vpn"}
for k in ("gluetun", "searxng", "sonarr", "radarr"):
    e.enqueue_update(k)
r = _drain(e, fails={"gluetun"}, groups=g)
checks["engine: a failure takes its group-mates off the queue"] = (
    r is not None and r["ran"] == ["gluetun", "searxng"]
    and e._update_queue == [])
checks["engine: …and says which, with the group, as keys"] = (
    r is not None and r["said"] == [
        ("update_queue_skipped_group",
         {"group": "vpn", "keys": ["sonarr", "radarr"]})])

# The group of the run that came before the drain is handed in.
e = engine()
for k in ("sonarr", "searxng"):
    e.enqueue_update(k)
r = _drain(e, groups={"sonarr": "vpn"}, last_group="vpn")
checks["engine: the group handed in is dropped before the first run"] = (
    r is not None and r["ran"] == ["searxng"]
    and r["said"] == [("update_queue_skipped_group",
                       {"group": "vpn", "keys": ["sonarr"]})])

# Only the run right before counts: a success after the failure clears
# it. `c` of the failed group is queued while `b` runs, after the drop.
e = engine()
g = {"a": "g1", "c": "g1"}
for k in ("a", "b"):
    e.enqueue_update(k)
r = _drain(e, fails={"a"}, groups=g,
           during=lambda k: e.enqueue_update("c") if k == "b" else None)
checks["engine: a success clears the failed group for what comes next"] = (
    r is not None and r["ran"] == ["a", "b", "c"] and r["said"] == [])

e = engine()
e._queued_selfupdate = ("2.9.0",)
for k in ("one", "two"):
    e.enqueue_update(k)
r = _drain(e)
checks["engine: a pending self-update stops the drain before any run"] = (
    r is not None and r["ran"] == [] and e._update_queue == [])
checks["engine: …and names every key it dropped"] = (
    r is not None and r["said"] == [
        ("update_queue_dropped_selfupdate", {"keys": ["one", "two"]})])

e = engine()
e._queued_selfupdate = ("2.9.0",)
r = _drain(e)
checks["engine: …and says nothing when there was nothing to drop"] = (
    r is not None and r["said"] == [])

# Queued while the drain runs: the next entry is the one that stops.
e = engine()
for k in ("first", "second", "third"):
    e.enqueue_update(k)
r = _drain(e, during=lambda k: setattr(e, "_queued_selfupdate", ("2.9.0",)))
checks["engine: a self-update queued mid-drain stops it after that run"] = (
    r is not None and r["ran"] == ["first"]
    and r["said"] == [("update_queue_dropped_selfupdate",
                       {"keys": ["second", "third"]})])

# Lost claim: somebody holds the lock when the drain wants the next
# entry. The claim comes before the take since Q10, so nothing was taken
# and the entry keeps its place at the head; it used to go to the back.
e = engine()
for k in ("a", "b"):
    e.enqueue_update(k)
e._update_lock.acquire()
r = _drain(e)
checks["engine: a lost claim runs nothing and leaves the entry at the head"] = (
    r is not None and r["ran"] == [] and e._update_queue == ["a", "b"]
    and r["said"] == [])
checks["engine: …and leaves the holder's lock alone"] = (
    r is not None and e._update_lock.locked())
e._update_lock.release()

e = engine()
for k in ("a", "b"):
    e.enqueue_update(k)
r = _drain(e, raises={"a"}, groups={"a": "g", "b": "g"}, fails={"a"})
checks["engine: a run that raises is said, and the drain goes on"] = (
    r is not None and r["ran"] == ["a", "b"]
    and r["said"] == [("entry_failed", {"key": "a", "error": "boom a"})])

# Telegram's drain is a wrapper: it hands its own last group and lets
# the engine run the loop.
eng = engine()
b = Bot(eng)
seen = []
_real = getattr(eng, "drain_queue", None)


def _spy(run_one, **kw):
    seen.append(kw.get("last_group"))
    return _real(run_one, **kw)


eng.drain_queue = _spy
eng.enqueue_update("k")
b._drain_update_queue(checker=None, last_group="g")
checks["Telegram's drain hands the loop to engine.drain_queue"] = (
    _real is not None and seen == ["g"] and b.updated == ["k"])

# The real `_run_single_update` under Telegram's drain: an entry off the
# queue is run as one (`from_queue=True`). Two things hang on that. A key
# that is no longer pending is work that already happened, not a typo;
# and a queued run does not start a drain of its own from inside the
# drain, so the queue is worked off by one loop, not one level per entry.
import json  # noqa: E402
import tempfile  # noqa: E402


def _queued_bot(eng, pending_entries):
    said = []
    # Bounded: with a mutation that never lets a run claim the free lock,
    # each run puts its key back and the drain would spin forever. Fifty
    # takes is far more than these three keys need, and a hang is not red.
    _take, _taken = eng.take_queued_update, []

    def _bounded_take():
        _taken.append(1)
        return _take() if len(_taken) <= 50 else None

    eng.take_queued_update = _bounded_take
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(pending_entries, fh)
    bot = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    bot.engine = eng
    bot.t = _T_drain
    bot.send_message = lambda m_, **kw: said.append(m_)
    bot.config = types.SimpleNamespace(pending_file=path)
    bot.notifier = None
    bot._host_by_name = lambda n: None
    bot._checker_for = lambda host, checker: checker
    bot._run_queued_selfupdate = lambda: None
    bot._group_of = lambda k: None
    bot._enrich_with_source_url = lambda us: None
    bot._display_name = lambda u: u["name"]
    return bot, said, path


class _OkChecker:
    def netns_target_name(self, name):
        return None

    def update_container(self, name, image, **kw):
        return True, "ok"


from i18n import get_translator as _gt  # noqa: E402
_T_drain = _gt("en")

eng = engine()
eng._updating = {}
eng.enqueue_update("gone")
qb, said, path = _queued_bot(eng, [])
qb._drain_update_queue(_OkChecker())
os.unlink(path)
checks["a queued key no longer pending is called done, not unknown"] = (
    said == [_T_drain("update_already_done", name="gone")])

eng = engine()
eng._updating = {}
for k in ("a", "b", "c"):
    eng.enqueue_update(k)
qb, said, path = _queued_bot(eng, [{"name": n, "image": f"{n}:1"}
                                   for n in ("a", "b", "c")])
drains = []
# getattr, so an engine without `drain_queue` goes red here, not crashes.
_real_dq = getattr(eng, "drain_queue", None)


def _count(run_one, **kw):
    drains.append(1)
    return _real_dq(run_one, **kw)


if _real_dq is not None:
    eng.drain_queue = _count
qb._drain_update_queue(_OkChecker())
os.unlink(path)
checks["…and a queued run does not drain the queue again from inside"] = (
    _real_dq is not None and len(drains) == 1 and eng._update_queue == []
    and [m for m in said if m.endswith(": ok")] == [
        "✅ a: ok", "✅ b: ok", "✅ c: ok"])

# ── Q10: the drain holds each entry's claim through that entry ───────
# Before Q10 the drain took the lock, gave it straight back and let the
# entry's own run claim it again. In between, anybody could take it —
# the scheduler, a confirmed major — and the entry then went to the
# back of the queue with a second "queued at position N", for a
# container that had been waiting at the front. Now the drain claims
# before it takes an entry, holds the claim through `run_one` and is the
# one that gives it back, with `after`, once per entry. The lock is
# still free between two entries, so other work can get in there; it
# just cannot get in halfway through one.
import inspect  # noqa: E402
import threading  # noqa: E402
import time as _time  # noqa: E402


def _drain_q10(e, run_one, after=None):
    """`e.drain_queue` with `after` where it takes one; the said list,
    or None without a drain. On an engine whose drain has no `after`
    the call goes through without it, so what is checked is what the
    drain does, not whether it accepts the argument."""
    fn = getattr(e, "drain_queue", None)
    if fn is None:
        return None
    said = []
    kw = {"group_of": lambda k: None,
          "say": lambda k, p: said.append((k, p))}
    if "after" in inspect.signature(fn).parameters:
        kw["after"] = after
    fn(run_one, **kw)
    return said


def _door_spy(e, events):
    """Record the engine's claims and releases, and let them through."""
    real_claim, real_release = e.claim_update, e.release_update

    def claim():
        won = real_claim()
        events.append(("claim", won))
        return won

    def release(**kw):
        events.append(("release",))
        return real_release(**kw)

    e.claim_update, e.release_update = claim, release


# One claim per entry, held through it, given back after it — and the
# lock is free whenever `after` runs, so no claim reaches from one entry
# into the next. (Re-pointed from the source check that counted one
# `acquire` in the drain.)
e = engine()
ev = []
_door_spy(e, ev)
for k in ("a", "b", "c"):
    e.enqueue_update(k)
said = _drain_q10(
    e, lambda k: ev.append(("run", k, e._update_lock.locked())),
    after=lambda: ev.append(("after", e._update_lock.locked())))
_want = []
for k in ("a", "b", "c"):
    _want += [("claim", True), ("run", k, True), ("release",),
              ("after", False)]
checks["engine: one claim per entry, held through it, one release after it"] = (
    said == [] and ev == _want)
checks["…and the lock is free and the queue empty when the drain is done"] = (
    said == [] and not e._update_lock.locked() and e._update_queue == [])

# A competitor waiting on `claim_update` while an entry runs gets the
# lock only once the drain has given it back. It then wins the gap
# before the next entry, and that entry stays where it was: at the head,
# not put back at the tail, and nobody is told anything.
e = engine()
ev = []
_door_spy(e, ev)
for k in ("a", "b", "c"):
    e.enqueue_update(k)
_got, _let_go = threading.Event(), threading.Event()


def _competitor():
    deadline = _time.time() + 5
    while not UpdateEngine.claim_update(e):
        if _time.time() > deadline:
            return
        _time.sleep(0.001)
    ev.append(("competitor got it",))
    _got.set()
    _let_go.wait(5)
    try:
        e._update_lock.release()
    except RuntimeError:
        # Somebody gave back a claim that was not theirs — the double
        # release a lock without an owner cannot refuse.
        ev.append(("competitor's lock was released under it",))


_th = threading.Thread(target=_competitor, daemon=True)


def _run_waited_on(key):
    ev.append(("run", key))
    if key == "a":
        _th.start()
    _time.sleep(0.1)
    ev.append(("run done", key, _got.is_set()))


said = _drain_q10(e, _run_waited_on, after=lambda: _got.wait(2))
_events = list(ev)
_queue = list(e._update_queue)
_let_go.set()
_th.join(5)
checks["engine: a competitor waiting on claim_update does not get in while an entry runs"] = (
    ("run done", "a", False) in _events)
checks["…it gets the lock only after the drain gave it back, once"] = (
    ("competitor got it",) in _events
    and _events.count(("release",)) == 1
    and _events.index(("release",)) < _events.index(("competitor got it",)))
checks["…and the entry after it keeps its place, nothing re-queued or said"] = (
    said == [] and _queue == ["b", "c"]
    and [x for x in _events if x[0] == "run"] == [("run", "a")])
checks["…and the competitor's lock is its own to give back"] = (
    not _th.is_alive() and not e._update_lock.locked()
    and ("competitor's lock was released under it",) not in ev)

# A self-update queued while an entry runs goes next — after that
# entry's release, through `after`. Whether it takes the rest with it is
# only known once it has run: up to date, or a pull that failed, ends
# without a restart, and then the rest of the queue runs as before. The
# first cut of Q10 named the rest as dropped before handing on, which
# told the person "about to restart" when nothing restarted, and lost
# b and c for nothing (review, measured on 34c1040).
e = engine()
ev = []
for k in ("first", "second", "third"):
    e.enqueue_update(k)


def _queues_selfupdate(key):
    ev.append(("run", key))
    if key == "first":
        e._queued_selfupdate = ("2.9.0",)


def _runs_queued_selfupdate():
    # What `selfupdate.run_queued` does with one that finds nothing to
    # do: take it, start it, and come back without a restart.
    if e._queued_selfupdate:
        ev.append(("selfupdate", list(e._update_queue),
                   e._update_lock.locked()))
        e._queued_selfupdate = None


said = _drain_q10(e, _queues_selfupdate, after=_runs_queued_selfupdate)
checks["engine: a self-update queued between two entries goes next, lock free"] = (
    ev[:2] == [("run", "first"), ("selfupdate", ["second", "third"], False)])
checks["…one that ends without a restart leaves the rest to run"] = (
    ev[2:] == [("run", "second"), ("run", "third")]
    and said == [] and e._update_queue == []
    and not e._update_lock.locked())

# One that does restart: the helper is launched and the process is about
# to be stopped. Only now is "about to restart" true, so only now is the
# rest named and taken off. `start` claimed the lock for the swap and
# keeps it; the drain does not touch it.
e = engine()
ev = []
for k in ("first", "second", "third"):
    e.enqueue_update(k)


def _swaps():
    if e._queued_selfupdate:
        e._queued_selfupdate = None
        e.claim_update()
        ev.append(("swap", list(said_sw)))
        e._swap_in_flight = True


said_sw = []
_fn = getattr(e, "drain_queue", None)
if _fn is not None:
    _fn(_queues_selfupdate, group_of=lambda k: None,
        say=lambda k, p: said_sw.append((k, p)),
        **({"after": _swaps}
           if "after" in inspect.signature(_fn).parameters else {}))
checks["engine: a self-update that restarts takes the rest, named after it began"] = (
    ev == [("run", "first"), ("swap", [])]
    and said_sw == [("update_queue_dropped_selfupdate",
                     {"keys": ["second", "third"]})]
    and e._update_queue == [])
checks["…and the swap keeps the lock it claimed"] = e._update_lock.locked()

# `say` never runs while the drain holds an entry's claim. A say that
# raises there left the update lock held for the life of the process —
# the incident class `release_update`'s docstring describes — and the
# queued self-update behind it never ran. The sweep swallows the error,
# so nobody would have seen it (review, measured on 34c1040).
e = engine()
for k in ("first", "second", "third"):
    e.enqueue_update(k)


def _say_breaks(k, p):
    raise RuntimeError("send broke")


_raised = []
_fn = getattr(e, "drain_queue", None)
try:
    if _fn is not None:
        _fn(_queues_selfupdate, group_of=lambda k: None, say=_say_breaks)
except RuntimeError as _e:
    _raised.append(str(_e))
checks["engine: a say that raises after an entry leaves the lock free"] = (
    _fn is not None and _raised == ["send broke"]
    and not e._update_lock.locked()
    and e._queued_selfupdate == ("2.9.0",))

# An entry that raises is given back inside the handler, so `after`
# still sees the crash: `selfupdate.run_queued` cancels a queued
# self-update after one instead of restarting on top of it.
e = engine()
seen = []
for k in ("bad", "good"):
    e.enqueue_update(k)


def _bad(key):
    if key == "bad":
        raise RuntimeError("boom")


said = _drain_q10(e, _bad, after=lambda: seen.append(sys.exc_info()[0]))
checks["engine: after() of an entry that raised sees the crash, the next one does not"] = (
    seen == [RuntimeError, None] and not e._update_lock.locked()
    and said == [("entry_failed", {"key": "bad", "error": "boom"})])

# Something that is not an Exception (an exit) is not swallowed, and the
# claim is given back on its way out.
e = engine()
seen = []
for k in ("x", "y"):
    e.enqueue_update(k)


def _exits(key):
    raise SystemExit(3)


try:
    _drain_q10(e, _exits, after=lambda: seen.append(1))
    _escaped = None
except BaseException as _ex:            # anything else is red, not a crash
    _escaped = getattr(_ex, "code", repr(_ex))
checks["engine: a run that exits goes through, after giving the claim back"] = (
    _escaped == 3 and seen == [1] and not e._update_lock.locked()
    and e._update_queue == ["y"])

# Emptied between the drain's look and its claim (another drain got
# there first): the claim goes straight back. `has_queued_updates` is
# the queue's state here, not the decision under test.
e = engine()
ev = []
_door_spy(e, ev)
_looks = iter([True])
e.has_queued_updates = lambda: next(_looks, False)
_ran = []
said = _drain_q10(e, _ran.append, after=lambda: ev.append(("after",)))
checks["engine: a queue emptied between the look and the claim gives it back"] = (
    _ran == [] and said == [] and not e._update_lock.locked()
    and ev == [("claim", True), ("release",), ("after",)])

# The same through Telegram's real `_run_single_update`.
# A competing claim inside an entry — where the old drain had let go —
# is refused, and the entry runs instead of going to the back with a
# second "queued at position N".
eng = engine()
eng._updating = {}
for k in ("a", "b"):
    eng.enqueue_update(k)
qb, said, path = _queued_bot(eng, [{"name": n, "image": f"{n}:1"}
                                   for n in ("a", "b")])
_tries = []


def _checker_for_competing(host, checker):
    # The scheduler's tick, say, landing while the drain works on `a`.
    if not _tries:
        _tries.append(eng.claim_update())
    return checker


qb._checker_for = _checker_for_competing
qb._drain_update_queue(_OkChecker())
if _tries and _tries[0] and eng._update_lock.locked():
    eng._update_lock.release()          # the competitor gives back what it won
os.unlink(path)
_queued_lines = [m_ for m_ in said for n in ("a", "b") for p in (1, 2)
                 if m_ == _T_drain("update_queued", name=n, pos=p)]
checks["Telegram drain: a claim landing inside an entry is refused"] = (
    _tries == [False])
checks["…the entry is not re-queued and nobody hears 'queued' a second time"] = (
    _queued_lines == [] and eng._update_queue == []
    and [m_ for m_ in said if m_.endswith(": ok")] == ["✅ a: ok", "✅ b: ok"])

# A self-update queued while a drained entry updates goes next. Here the
# real `selfupdate.start` queues it and the real `run_queued` starts it,
# through Telegram's own hand-over; only `selfupdate.run` — the pull and
# the swap — is a stand-in. Up to date: no restart, so b and c still run
# and nobody hears "about to restart".
import selfupdate  # noqa: E402


def _tg_selfupdate_drain(run_stand_in):
    eng = engine()
    eng._updating = {}
    eng._swap_in_flight = False
    for k in ("a", "b", "c"):
        eng.enqueue_update(k)
    qb, said, path = _queued_bot(eng, [{"name": n, "image": f"{n}:1"}
                                       for n in ("a", "b", "c")])
    # The real hand-over, not the stub `_queued_bot` puts there.
    del qb._run_queued_selfupdate
    qb.selfupdate_ctx = selfupdate.Context(
        eng, types.SimpleNamespace(language="en"),
        say=lambda m_, skip=(): said.append(m_),
        reply=said.append)

    class _QueuesSelfupdate(_OkChecker):
        def update_container(self, name, image, **kw):
            if name == "a":
                selfupdate.start(qb.selfupdate_ctx)
            return True, "ok"

    real_run = selfupdate.run
    selfupdate.run = lambda ctx, target=None, reply_to=None: run_stand_in(
        ctx, said)
    try:
        qb._drain_update_queue(_QueuesSelfupdate())
    finally:
        selfupdate.run = real_run
        os.unlink(path)
    return eng, said


def _up_to_date(ctx, said):
    ctx.send_message(ctx.t("selfupdate_up_to_date"))


def _swap_launched(ctx, said):
    # The helper is up and this process is about to be stopped. In real
    # life the stop lands inside `swap`'s wait; here `run` returns, which
    # is what a stop that never came looks like.
    said.append("<swap>")
    ctx._swap_in_flight = True


_dropped = _T_drain("update_queue_dropped_selfupdate", names="`b`, `c`")
eng, said = _tg_selfupdate_drain(_up_to_date)
_oks = [m_ for m_ in said if m_.endswith(": ok")]
_order = [m_ for m_ in said if m_ in (
    _T_drain("selfupdate_queued"), "✅ a: ok",
    _T_drain("selfupdate_up_to_date"), "✅ b: ok")]
checks["Telegram drain: a self-update queued during an entry runs after it"] = (
    _order == [_T_drain("selfupdate_queued"), "✅ a: ok",
               _T_drain("selfupdate_up_to_date"), "✅ b: ok"])
checks["…one without a restart drops nothing: b and c still run"] = (
    _oks == ["✅ a: ok", "✅ b: ok", "✅ c: ok"] and _dropped not in said
    and eng._update_queue == [] and eng._queued_selfupdate is None
    and not eng._update_lock.locked())

eng, said = _tg_selfupdate_drain(_swap_launched)
checks["Telegram drain: a self-update that swaps names b and c, after it began"] = (
    [m_ for m_ in said if m_.endswith(": ok")] == ["✅ a: ok"]
    and "<swap>" in said and _dropped in said
    and said.index("<swap>") < said.index(_dropped)
    and eng._update_queue == [] and eng._update_lock.locked())

# The drain is the one releaser: a drained entry gives back nothing
# itself, so each entry is released once and the self-update hand-over
# runs once per entry, from the drain. And the entry's update runs
# under that claim.
eng = engine()
eng._updating = {}
for k in ("a", "b", "c"):
    eng.enqueue_update(k)
qb, said, path = _queued_bot(eng, [{"name": n, "image": f"{n}:1"}
                                   for n in ("a", "b", "c")])
_rel, _hand, _held = [], [], []
_real_release = eng.release_update


def _count_release(**kw):
    _rel.append(1)
    return _real_release(**kw)


class _HeldChecker(_OkChecker):
    def update_container(self, name, image, **kw):
        _held.append((name, eng._update_lock.locked()))
        return True, "ok"


eng.release_update = _count_release
qb._run_queued_selfupdate = lambda: _hand.append(1)
import contextlib as _cl  # noqa: E402
import io as _io  # noqa: E402
_out = _io.StringIO()
with _cl.redirect_stdout(_out):
    qb._drain_update_queue(_HeldChecker())
os.unlink(path)
checks["Telegram drain: each entry is released exactly once, by the drain"] = (
    len(_rel) == 3 and len(_hand) == 3
    and "was not held" not in _out.getvalue())
checks["…and each entry's update runs under that claim"] = (
    _held == [("a", True), ("b", True), ("c", True)]
    and not eng._update_lock.locked() and eng._update_queue == [])

# A drained entry that raises: the drain's release is the one, inside the
# crash, so Telegram's self-update hand-over still sees it. The crash is
# the "Updating …" line failing to go out (the `starting` hook raises
# past run_keys). Until the Q11c follow-up a raising link lookup was the
# vehicle here; that one is logged and the update goes on now.
eng = engine()
eng._updating = {}
for k in ("a", "b"):
    eng.enqueue_update(k)
qb, said, path = _queued_bot(eng, [{"name": n, "image": f"{n}:1"}
                                   for n in ("a", "b")])
_exc = []


def _send_raises(m_, **kw):
    if m_.startswith("⏳") and "`a`" in m_:
        raise RuntimeError("send broke")
    said.append(m_)


qb.send_message = _send_raises
qb._run_queued_selfupdate = lambda: _exc.append(sys.exc_info()[0])
qb._drain_update_queue(_OkChecker())
os.unlink(path)
checks["Telegram drain: the hand-over after a crashed entry sees the crash"] = (
    _exc == [RuntimeError, None] and "❌ `a`: send broke" in said
    and "✅ b: ok" in said and not eng._update_lock.locked())

# ── claim_or_queue: the engine decides, a connection only translates ─
# Q7 moved the decision out of Telegram's tap handler. The answer is a
# key and its parameters, never a sentence, so the Web UI and Discord
# can say the same thing in their own words once they queue too. Raw
# seconds for "since": rendering "2m" is the connection's business.
import time  # noqa: E402

MAX = UpdateEngine.UPDATE_QUEUE_MAX


def _coq(e, key):
    """`e.claim_or_queue(key)`, or a marker when the engine has none."""
    f = getattr(e, "claim_or_queue", None)
    return f(key) if f else ("no claim_or_queue",)


def _coqm(e, keys):
    f = getattr(e, "claim_or_queue_many", None)
    return f(keys) if f else ("no claim_or_queue_many",)


e = engine()
e._updating = {}
got = _coq(e, "web")
checks["claim_or_queue on a free lock claims it: (True, None, {})"] = (
    got == (True, None, {}) and e._update_lock.locked())
checks["…and queues nothing"] = e._update_queue == []
if e._update_lock.locked():
    e._update_lock.release()

e = engine()
e._updating = {"nas/web": "1.26.0"}
e._update_lock.acquire()
got = _coq(e, "nas/web")
checks["held, the same key being updated: update_same_running"] = (
    got == (False, "update_same_running", {}))
checks["…not queued behind itself"] = e._update_queue == []
checks["…and the holder keeps its lock"] = e._update_lock.locked()

e = engine()
e._updating = {}
for i in range(MAX):
    e.enqueue_update(f"c{i}")
e._update_lock.acquire()
got = _coq(e, "one-too-many")
checks["held and full: update_queue_full with max"] = (
    got == (False, "update_queue_full", {"max": MAX}))
checks["…and the queue is left as it was"] = (
    e._update_queue == [f"c{i}" for i in range(MAX)])

e = engine()
e._updating = {}
e.enqueue_update("db")
e._queued_at["db"] = time.time() - 120
e._update_lock.acquire()
got = _coq(e, "db")
checks["held and already queued: update_already_queued, raw seconds"] = (
    isinstance(got, tuple) and len(got) == 3
    and got[:2] == (False, "update_already_queued")
    and set(got[2]) == {"since_seconds"}
    and isinstance(got[2]["since_seconds"], (int, float))
    and 119 <= got[2]["since_seconds"] < 125)
checks["…queued once, not twice"] = e._update_queue == ["db"]

e = engine()
e._updating = {"busy": ""}
e.enqueue_update("first")
e._update_lock.acquire()
got = _coq(e, "second")
checks["held otherwise: update_queued with its position"] = (
    got == (False, "update_queued", {"pos": 2}))
checks["…and it is on the queue, behind the first"] = (
    e._update_queue == ["first", "second"])
checks["…and the holder still has the lock"] = e._update_lock.locked()

# ── claim_or_queue_many: one claim for the whole request ─────────────
e = engine()
e._updating = {}
got = _coqm(e, ["a", "b", "c"])
checks["three keys on a free lock: one claim, no outcomes"] = (
    got == (True, []) and e._update_lock.locked())
checks["…and a batch never queues behind itself"] = e._update_queue == []
if e._update_lock.locked():
    e._update_lock.release()

e = engine()
e._updating = {}
e._update_lock.acquire()
got = _coqm(e, ["a", "b", "c"])
checks["three keys on a held lock: three outcomes, in order"] = (
    got == (False, [("a", "update_queued", {"pos": 1}),
                    ("b", "update_queued", {"pos": 2}),
                    ("c", "update_queued", {"pos": 3})]))
checks["…and the queue keeps the request's order"] = (
    e._update_queue == ["a", "b", "c"])
checks["…and the holder keeps its lock"] = e._update_lock.locked()

e = engine()
e._updating = {"run": ""}
e.enqueue_update("old")
e._update_lock.acquire()
got = _coqm(e, ["run", "old", "new", "new"])
_out = got[1] if isinstance(got, tuple) and len(got) == 2 else None
checks["held, mixed: each key answered for itself"] = (
    _out is not None and [(k, key) for k, key, _p in _out]
    == [("run", "update_same_running"), ("old", "update_already_queued"),
        ("new", "update_queued")]
    and _out[2][2] == {"pos": 2})
checks["…a key asked for twice is one entry"] = (
    e._update_queue == ["old", "new"])

# Full mid-batch. My decision for batches (18.11.2026): all of it or
# none of it. Half a request on the queue is a request nobody asked for.
e = engine()
e._updating = {}
for i in range(MAX - 2):
    e.enqueue_update(f"c{i}")
e._update_lock.acquire()
got = _coqm(e, ["c0", "x", "y", "z"])
_out = got[1] if isinstance(got, tuple) and len(got) == 2 else None
checks["full mid-batch: nothing of it goes on the queue"] = (
    e._update_queue == [f"c{i}" for i in range(MAX - 2)])
# And it is answered as a whole: "queue full" for every key of it, the
# one that was already waiting too (same decision). That one keeps its
# place; the answer is about the request, and the request did not fit.
checks["…the whole batch is told the queue is full, in order"] = (
    _out is not None
    and [(k, key, p) for k, key, p in _out]
    == [(k, "update_queue_full", {"max": MAX})
        for k in ("c0", "x", "y", "z")])
checks["…and the one already waiting keeps its place"] = (
    e._update_queue[0] == "c0" and "c0" in e._queued_at)

e = engine()
e._updating = {"run": ""}
for i in range(MAX - 1):
    e.enqueue_update(f"c{i}")
e._update_lock.acquire()
got = _coqm(e, ["run", "x", "y"])
_out = got[1] if isinstance(got, tuple) and len(got) == 2 else None
checks["full, with the running one in it: the batch as a whole is full"] = (
    _out is not None
    and [(k, key) for k, key, _p in _out]
    == [(k, "update_queue_full") for k in ("run", "x", "y")]
    and len(e._update_queue) == MAX - 1)

e = engine()
e._updating = {"run": ""}
for i in range(MAX):
    e.enqueue_update(f"c{i}")
e._update_lock.acquire()
got = _coqm(e, ["run", "c3"])
_out = got[1] if isinstance(got, tuple) and len(got) == 2 else None
checks["a full queue, but nothing new asked for: each key answered itself"] = (
    _out is not None
    and [(k, key) for k, key, _p in _out]
    == [("run", "update_same_running"), ("c3", "update_already_queued")])

e = engine()
e._updating = {}
for i in range(MAX - 3):
    e.enqueue_update(f"c{i}")
e._update_lock.acquire()
got = _coqm(e, ["x", "y", "z"])
checks["a batch that fits exactly goes in whole"] = (
    isinstance(got, tuple) and got[0] is False
    and [p for _k, _key, p in got[1]]
    == [{"pos": MAX - 2}, {"pos": MAX - 1}, {"pos": MAX}]
    and len(e._update_queue) == MAX)

# A key that came in with a batch knows since when, like a single one.
# The clock is the engine's own `time`, moved by hand: writing the
# stamp from here would test my write, not the engine's.
import update_engine as _ue  # noqa: E402


class _Clock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def __getattr__(self, name):
        return getattr(time, name)


_real_time, _ue.time = _ue.time, _Clock(1_000_000.0)
try:
    e = engine()
    e._updating = {}
    e._update_lock.acquire()
    _coqm(e, ["p", "q"])
    _ue.time.t += 300
    got = _coq(e, "q")
    snap = e.queued_updates()
finally:
    _ue.time = _real_time
checks["batch-queued, asked again 300s later: since_seconds is 300"] = (
    got == (False, "update_already_queued", {"since_seconds": 300.0}))
checks["…and the queue snapshot says 300s for both"] = (
    snap == [("p", 300.0), ("q", 300.0)])

# ── Telegram's tap only translates the engine's answer ───────────────
# The real `_run_single_update` on a real engine, through every busy
# answer, against the sentence the old handler built by hand.
from i18n import get_translator  # noqa: E402
from notify_retry import _ago  # noqa: E402

_T = get_translator("en")


def _tap(eng, key):
    said = []
    bot = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    bot.engine = eng
    bot.t = _T
    bot.send_message = lambda m_, **kw: said.append(m_)
    bot._host_by_name = lambda n: None
    bot._checker_for = lambda host, checker: checker
    bot._run_queued_selfupdate = lambda: None
    bot._run_single_update(object(), key)
    return said


e = engine()
e._updating = {"nas/web": ""}
e._update_lock.acquire()
checks["Telegram: same container → its sentence, with the host"] = (
    _tap(e, "nas/web") == [_T("update_same_running", name="web @nas")])

e = engine()
e._updating = {}
for i in range(MAX):
    e.enqueue_update(f"c{i}")
e._update_lock.acquire()
checks["Telegram: queue full → its sentence, with the cap"] = (
    _tap(e, "web") == [_T("update_queue_full", max=MAX)])

e = engine()
e._updating = {}
e.enqueue_update("nas/db")
e._queued_at["nas/db"] = time.time() - 125
e._update_lock.acquire()
checks["Telegram: already queued → since, rendered by Telegram"] = (
    _tap(e, "nas/db") == [_T("update_already_queued", name="db @nas",
                             since=_ago(125))])

e = engine()
e._updating = {}
e.enqueue_update("first")
e._update_lock.acquire()
checks["Telegram: queued → its position"] = (
    _tap(e, "db") == [_T("update_queued", name="db", pos=2)]
    and e._update_queue == ["first", "db"])
checks["…and the tap leaves the holder's lock alone"] = (
    e._update_lock.locked())

# ── the source says what the docstring says ─────────────────────────
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "telegram_bot.py"), encoding="utf-8").read()
esrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "update_engine.py"), encoding="utf-8").read()
# The loop lives in the engine since Q8; Telegram's drain is a wrapper.
drain = srcslice.section(esrc, "    def drain_queue(self", "\n    def ",
                         label="update_engine.py: drain_queue")
# Re-pointed in Q10: the drain claims through the door, once per entry
# (driven above: "one claim per entry…"), and no longer touches the raw
# lock or puts an entry back on the queue.
checks["the drain claims through the door, in one place"] = (
    drain is not None and drain.count("self.claim_update()") == 1
    and "_update_lock" not in drain)
checks["…and never puts an entry back on the queue"] = (
    drain is not None and "enqueue_update" not in drain)
# The rejected tap is queued, not answered with the old brush-off.
single = srcslice.section(src, "def _run_single_update",
                          "\n    def _drain_update_queue",
                          label="telegram_bot.py: _run_single_update")
checks["a busy lock queues the tap instead of discarding it"] = (
    single is not None
    and "self.engine.claim_or_queue(container_key)" in single
    and "update_already_running" not in single)
# …and the queueing itself is the engine's (Q7). The behaviour is driven
# above; this only says where it lives, so a second copy in a connection
# cannot come back unnoticed.
coq =srcslice.section(esrc, "    def claim_or_queue(self",
                       "\n    def claim_or_queue_many(",
                       label="update_engine.py: claim_or_queue")
checks["…through the engine, which puts it on the queue"] = (
    coq is not None and "self.enqueue_update(container_key)" in coq)
checks["…and the tap no longer queues by hand"] = (
    single is not None and "enqueue_update(" not in single)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
