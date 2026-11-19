#!/usr/bin/env python3
"""One door to the update lock: `UpdateEngine.claim_update()` and
`release_update(after=...)`.

Every flow that changes containers takes the same lock, and until now
each of them wrote `acquire(blocking=False)` / `release()` out by hand,
followed by the call that hands on a self-update that queued behind
it. Six of those sat in the Telegram bot alone. They go through the
engine now, and this pins what the door promises before anybody leans
on it:

  * a free lock is claimed, a held one is not, and a claim never waits;
  * the door is the one lock, not a second one next to it — a claim is
    what `update_running` and `bot._update_lock` see;
  * a release while the self-update's helper is swapping the container
    leaves the lock held (nothing may start in the last seconds of this
    process). That used to live in `check_selfupdate_auto`'s `finally`;
  * the swap flag is read defensively: an engine whose `_swap_in_flight`
    raises still gets its lock back (test_engine_init has the story);
  * a release of a lock nobody holds does not raise, it says so in the
    log — a `finally` that raises there would hide whatever the flow
    was about to report;
  * `after` runs once the lock is back, and when the release happens in
    an exception unwind, `after` still sees `sys.exc_info()`.
    `selfupdate.run_queued` relies on that: after a crashed batch it
    cancels the queued self-update instead of restarting on top of it;
  * `update_claim()` is the same pair as a `with` block.

Exits non-zero on any failure.
"""

import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import types

import srcslice

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from config import Config  # noqa: E402
from container_store import ContainerStore  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402
import selfupdate  # noqa: E402

checks = {}


def fresh():
    """A real engine, built the way main.py builds one."""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        try:
            return UpdateEngine(Config.from_env(),
                                ContainerStore(Config.from_env()))
        finally:
            os.environ.pop("DATA_DIR", None)


def door(engine):
    """The three entry points, or None each (with a FAIL line)."""
    return (srcslice.attr(engine, "claim_update",
                          label="UpdateEngine.claim_update"),
            srcslice.attr(engine, "release_update",
                          label="UpdateEngine.release_update"),
            srcslice.attr(engine, "update_claim",
                          label="UpdateEngine.update_claim"))


def quietly(fn, *a, **k):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        ret = fn(*a, **k)
    return ret, out.getvalue()


# ── claim ────────────────────────────────────────────────────────────
eng = fresh()
claim, release, claim_cm = door(eng)
have = claim is not None and release is not None and claim_cm is not None
attrs_before = set(vars(eng))

if claim is not None:
    checks["a free lock is claimed"] = claim() is True
    checks["…and it is THE update lock, not a second one"] = (
        eng._update_lock.locked() and eng.update_running is True)
    # A held lock: answered at once, and the holder keeps it. Asked from
    # another thread, like the scheduler asking while a tap holds it; a
    # daemon, so a claim that blocks shows up red instead of hanging us.
    done = []

    def other():
        t0 = time.monotonic()
        done.append((claim(), time.monotonic() - t0))
    t = threading.Thread(target=other, daemon=True)
    t.start()
    t.join(3)
    checks["a held lock is not claimed, and the claim does not wait"] = (
        not t.is_alive() and len(done) == 1 and done[0][0] is False
        and done[0][1] < 0.5)
    checks["…and the holder still holds it"] = eng._update_lock.locked()
else:
    checks["a free lock is claimed"] = False

if release is not None and eng._update_lock.locked():
    _r, log = quietly(release)
    checks["release_update gives it back"] = not eng._update_lock.locked()
    checks["…without a word in the log"] = log == ""
else:
    checks["release_update gives it back"] = False

# Gated on the door like its neighbours: without one there is nothing
# that could have added state, and a PASS here would only pad the count.
checks["the door adds no state of its own to the engine"] = (
    have and set(vars(eng)) == attrs_before)

# ── after ────────────────────────────────────────────────────────────
eng = fresh()
claim, release, _cm = door(eng)
seen = []
if have:
    claim()
    release(after=lambda: seen.append(eng._update_lock.locked()))
checks["after() runs once"] = len(seen) == 1
checks["…with the lock already back"] = seen == [False]
checks["…and a release without after is fine"] = False
if have:
    claim()
    try:
        release()
        checks["…and a release without after is fine"] = (
            not eng._update_lock.locked())
    except Exception:                                   # noqa: BLE001
        pass

# ── the self-update's swap keeps the lock ────────────────────────────
eng = fresh()
claim, release, _cm = door(eng)
seen = []
if have:
    claim()
    eng._swap_in_flight = True
    release(after=lambda: seen.append("after"))
checks["a release while the helper swaps the container keeps it held"] = (
    have and eng._update_lock.locked())
checks["…and after() still runs (it is what comes after this flow)"] = (
    seen == ["after"])
checks["…so nothing else gets in"] = have and claim() is False


class Boom(UpdateEngine):
    """An engine whose `_swap_in_flight` raises, like the broken one did."""

    def __init__(self):
        self._update_lock = threading.Lock()
        self._queued_selfupdate = None

    @property
    def _swap_in_flight(self):
        raise AttributeError("'UpdateEngine' object has no attribute "
                             "'_swap_in_flight'")


boom = Boom()
claim, release, _cm = door(boom)
raised = None
if have:
    claim()
    try:
        quietly(release)
    except Exception as e:                              # noqa: BLE001
        raised = e
checks["a swap flag that raises does not keep the lock held"] = (
    have and raised is None and not boom._update_lock.locked())

# ── a release of a lock nobody holds ─────────────────────────────────
eng = fresh()
claim, release, _cm = door(eng)
raised, log, seen = None, "", []
if have:
    try:
        _r, log = quietly(release, after=lambda: seen.append(1))
    except Exception as e:                              # noqa: BLE001
        raised = e
checks["releasing a free lock does not raise"] = have and raised is None
checks["…it says so in the log"] = (
    "release_update" in log and "not held" in log)
# Nothing in this block took the lock, so "still free" alone says little;
# the claim afterwards shows the free release left a lock that works.
checks["…leaves the lock free, and the next claim wins it"] = (
    have and not eng._update_lock.locked() and claim() is True)
if have and eng._update_lock.locked():
    eng._update_lock.release()
checks["…and still runs after()"] = seen == [1]

# ── after() in an exception unwind ───────────────────────────────────
eng = fresh()
claim, release, claim_cm = door(eng)
seen = []


def batch(crash):
    claim()
    try:
        if crash:
            raise RuntimeError("batch crashed")
    finally:
        release(after=lambda: seen.append(sys.exc_info()[0]))


if have:
    batch(False)
    try:
        batch(True)
    except RuntimeError:
        pass
checks["after() on a clean finish sees no exception"] = (
    seen[:1] == [None])
checks["after() in an unwind still sees it"] = (
    seen[1:] == [RuntimeError])
checks["…and the lock is back either way"] = (
    have and not eng._update_lock.locked())

# The reader that depends on it: a self-update queued behind a batch that
# crashed is cancelled with a message, not started on top of the crash.
ctx = types.SimpleNamespace(_queued_selfupdate=(None,), sent=[],
                            t=lambda k, **kw: k)
ctx.send_message = lambda text, **kw: ctx.sent.append(text)
started = []
_start = selfupdate.start
selfupdate.start = lambda *a, **k: started.append(a)
try:
    if have:
        claim()
        try:
            try:
                raise RuntimeError("batch crashed")
            finally:
                release(after=lambda: selfupdate.run_queued(ctx))
        except RuntimeError:
            pass
finally:
    selfupdate.start = _start
checks["a crashed batch cancels the queued self-update through the door"] = (
    ctx.sent == ["selfupdate_queue_cancelled"] and started == []
    and ctx._queued_selfupdate is None)

# ── update_claim() ───────────────────────────────────────────────────
eng = fresh()
claim, release, claim_cm = door(eng)
seen, inside = [], []
if have:
    with claim_cm(after=lambda: seen.append(sys.exc_info()[0])) as got:
        inside.append((got, eng._update_lock.locked()))
checks["update_claim on a free lock yields True and holds it"] = (
    inside == [(True, True)])
checks["…gives it back on the way out and runs after()"] = (
    have and not eng._update_lock.locked() and seen == [None])

seen, inside = [], []
if have:
    claim()
    with claim_cm(after=lambda: seen.append("after")) as got:
        inside.append(got)
checks["update_claim on a held lock yields False"] = inside == [False]
checks["…leaves the holder's lock alone and runs no after()"] = (
    have and eng._update_lock.locked() and seen == [])
if have and eng._update_lock.locked():
    release()

seen = []
if have:
    try:
        with claim_cm(after=lambda: seen.append(sys.exc_info()[0])):
            raise ValueError("body crashed")
    except ValueError:
        seen.append("propagated")
checks["update_claim: a crash inside gives the lock back, after() sees it, "
       "and the crash goes on"] = (
    have and not eng._update_lock.locked()
    and seen == [ValueError, "propagated"])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
