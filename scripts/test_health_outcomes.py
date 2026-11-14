#!/usr/bin/env python3
"""The four answers the health wait can give, produced rather than injected.

`docs/configuration.md:87` promises that a container still in `starting`
when the wait runs out is **left running** — slow is not broken, and
rolling it back would hit every container with a `start_period`, which is
most of the ones that have a healthcheck at all.

`test_health_rollback_wiring.py` already holds the other half: given the
string `"starting"`, the update path does not roll back. What nothing
held was the string's *production*. A mutation sweep on 11.10.2026
changed `return "starting"` to `return "unhealthy"` and no procedure went
red — the decision was guarded and the verdict feeding it was not. Two
sound halves of one chain, with the join untested.

So this drives the real `_wait_healthy` against a daemon stand-in that
answers `inspect` the way a real one would, and reads back which of the
four outcomes comes out. Only the clock and the daemon are stood in for;
the decision is the real one.

The four matter for different reasons:

* `healthy` has to survive the stability window — a container that boots
  and dies four seconds later must not pass as a successful update
  (the GitLab DB-migration failure of 18.06.2026).
* `crashloop` has to beat a healthcheck stuck in `starting`, which is
  exactly how that failure masqueraded as success.
* `unhealthy` is the rollback case.
* `starting` is the one that must NOT roll back.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import update_checker as uc  # noqa: E402
from update_checker import UpdateChecker  # noqa: E402

checks = {}


class _Daemon:
    """`inspect` answers from a script of (state, health) snapshots.

    `restarts` is a list of RestartCount values read in step with the
    snapshots, so a crash loop can be described rather than asserted.
    """

    def __init__(self, snapshots, restarts=None, start_period_ns=""):
        self.snapshots = list(snapshots)
        self.restarts = list(restarts or [])
        #: What `docker inspect` prints for
        #: `.Config.Healthcheck.StartPeriod` — nanoseconds, or "" when
        #: the image declares no healthcheck at all.
        self.start_period_ns = start_period_ns
        self.i = -1

    def _now(self, which):
        idx = min(max(self.i, 0), len(self.snapshots) - 1)
        state, health = self.snapshots[idx]
        return state if which == "state" else health

    def run(self, args, **kw):
        if args[:2] == ["inspect", "--format"]:
            fmt = args[2]
            if "State.Status" in fmt:
                # One poll = one snapshot; state is read first each round.
                self.i += 1
                return types.SimpleNamespace(
                    returncode=0, stdout=self._now("state") + "\n", stderr="")
            if "State.Health.Status" in fmt:
                return types.SimpleNamespace(
                    returncode=0, stdout=self._now("health") + "\n", stderr="")
            if "StartPeriod" in fmt:
                return types.SimpleNamespace(
                    returncode=0, stdout=f"{self.start_period_ns}\n",
                    stderr="")
            if "RestartCount" in fmt:
                idx = min(max(self.i, 0), len(self.restarts) - 1) \
                    if self.restarts else 0
                val = self.restarts[idx] if self.restarts else 0
                return types.SimpleNamespace(
                    returncode=0, stdout=f"{val}\n", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")


def _outcome(snapshots, restarts=None, max_starting=30, trust=False,
             stable=10, interval=10, start_period_ns=""):
    """Drive the real `_wait_healthy`; return (outcome, state, health)."""
    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(healthcheck_max_starting=max_starting,
                                     debug=False)
    c._backend = _Daemon(snapshots, restarts, start_period_ns)
    c._debug = lambda *a, **kw: None
    # `_get_start_period_seconds` is NOT stood in for. It used to be
    # (`lambda name: 0`), which made `max(max_starting, 0 * 1.5)` always
    # the first term — so the whole start_period branch was dead and
    # could be deleted with all 214 procedures staying green, and the
    # nanosecond parser behind it had never run at all (audit,
    # 07.11.2026). The daemon answers the inspect instead.
    c._is_trust_running = lambda name: trust
    c.CRASHLOOP_STABLE_SECONDS = stable
    # The clock is environment: a procedure that really waited 30 seconds
    # per case would cost two minutes and measure nothing extra.
    real_sleep = uc.time.sleep
    uc.time.sleep = lambda s: None
    try:
        return c._wait_healthy("app", interval=interval)
    finally:
        uc.time.sleep = real_sleep


HEALTHY = ("running", "healthy")
STARTING = ("running", "starting")
SICK = ("running", "unhealthy")
STOPPED = ("exited", "")

# ── starting, and still starting ─────────────────────────────────────
# The whole wait spent in `starting`, no restarts: alive but slow.
_out, _state, _health = _outcome([STARTING] * 5)
checks["a container that stays 'starting' is reported as starting"] = (
    _out == "starting")
# `_out != "unhealthy"` was here and is implied by the line above —
# it cannot fail while that one passes. What is worth asking instead
# is the thing the rollback path keys on.
checks["…which the rollback path does not act on"] = (
    _out not in ("unhealthy", "crashloop"))
checks["…and the state it was last seen in comes back with it"] = (
    _state == "running" and _health == "starting")

# ── healthy, and stayed that way ────────────────────────────────────
checks["a healthy container is reported healthy"] = (
    _outcome([HEALTHY] * 5)[0] == "healthy")
# A container with no healthcheck at all is healthy if it is running —
# that is most containers, and treating "" as a failure would roll back
# every one of them.
checks["…and so is one with no healthcheck that keeps running"] = (
    _outcome([("running", "")] * 5)[0] == "healthy")

# But not immediately: it has to stay up for the stability window. A
# container that boots fine and dies five seconds later is a failed
# update, and this is the case that let the GitLab migration through.
checks["a container that dies inside the stability window is not healthy"] = (
    _outcome([HEALTHY, HEALTHY, STOPPED, STOPPED], stable=30)[0]
    == "unhealthy")

# ── unhealthy, and the opt-in that forgives it ──────────────────────
checks["an unhealthy container is reported unhealthy"] = (
    _outcome([SICK] * 5)[0] == "unhealthy")
checks["…and so is one that is not running at all"] = (
    _outcome([STOPPED] * 5)[0] == "unhealthy")
# `#9`: per-container opt-in for brittle healthchecks.
checks["with trust_running set, unhealthy-but-up is accepted"] = (
    _outcome([SICK] * 5, trust=True)[0] == "healthy")
# …and the opt-in must not swallow a container that is genuinely down.
checks["…but a stopped container is still a failure"] = (
    _outcome([STOPPED] * 5, trust=True)[0] == "unhealthy")

# ── the crash loop, which beats everything else ─────────────────────
# A climbing RestartCount while the healthcheck sits in `starting` is
# how a failed update looked like a slow one.
checks["a climbing restart count is a crash loop"] = (
    _outcome([STARTING] * 5, restarts=[0, 1, 2, 3, 4])[0] == "crashloop")
checks["…even while it looks healthy"] = (
    _outcome([HEALTHY] * 5, restarts=[0, 1, 2, 3, 4])[0] == "crashloop")
# …and trust_running does not forgive it either.
checks["…and trust_running does not forgive one"] = (
    _outcome([SICK] * 5, restarts=[0, 1, 2], trust=True)[0] == "crashloop")
# The counter-direction: a container that restarted *before* the update
# has a non-zero count and must not be condemned for it.
checks["a count that was already high is not a crash loop"] = (
    _outcome([HEALTHY] * 5, restarts=[7, 7, 7, 7, 7])[0] == "healthy")

# ── slow but stable is accepted, once it looks healthy ──────────────
# `starting` for a while and then healthy for the window: the timeout is
# a floor on patience, not a verdict.
# Inside the loop, which is the ordinary case…
checks["slow-then-healthy is accepted"] = (
    _outcome([STARTING, STARTING, HEALTHY, HEALTHY, HEALTHY],
             max_starting=50, stable=10)[0] == "healthy")
# …and at the timeout, which is the other half and the one this
# procedure was not reaching: the wait runs out while the container
# has looked healthy but not yet for the full stability window. An
# audit deleted `if healthy_since is not None: return "healthy"` and
# only a live-daemon procedure noticed.
checks["…and so is one that was healthy when the wait ran out"] = (
    _outcome([STARTING, HEALTHY, HEALTHY], max_starting=30, stable=600)[0]
    == "healthy")
checks["…while one that never looked healthy is still 'starting'"] = (
    _outcome([STARTING, STARTING, STARTING], max_starting=30,
             stable=600)[0] == "starting")


# ── the image's own start_period, which nothing used to drive ────────
# `effective = max(max_starting, start_period * 1.5)` is promised in
# README.md, docs/configuration.md and docs/updates.md. The fixture
# above used to stand in for `_get_start_period_seconds` with
# `lambda name: 0`, so the second term was always 0 and the branch was
# dead: it could be deleted outright with every one of 214 procedures
# staying green, and the nanosecond parser had never executed once
# (audit, 07.11.2026). The cost of that is a real rollback — an image
# declaring `start_period: 15m` judged unhealthy after 600s and
# reverted while it was still coming up, exactly as designed to.
#
# `while elapsed < effective` steps by `interval`, so the number of
# polls the daemon is asked for IS the effective timeout, divided.

def _polls(start_period_ns, max_starting=30, interval=10):
    """How many times a forever-starting container gets polled."""
    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(healthcheck_max_starting=max_starting,
                                     debug=False)
    d = _Daemon([("running", "starting")] * 500, None, start_period_ns)
    c._backend = d
    c._debug = lambda *a, **kw: None
    c._is_trust_running = lambda name: False
    c.CRASHLOOP_STABLE_SECONDS = 10
    real_sleep = uc.time.sleep
    uc.time.sleep = lambda s: None
    try:
        c._wait_healthy("app", interval=interval)
    finally:
        uc.time.sleep = real_sleep
    return d.i + 1


# The parser first, on its own — nanoseconds in, seconds out.
_p = UpdateChecker.__new__(UpdateChecker)
_p.config = types.SimpleNamespace(debug=False)
_p._debug = lambda *a, **kw: None
for _label, _ns, _want in (("15 minutes", "900000000000", 900.0),
                           ("30 seconds", "30000000000", 30.0),
                           ("no healthcheck", "", 0.0),
                           ("an explicit zero", "0", 0.0),
                           ("something unparsable", "garbage", 0.0)):
    _p._backend = _Daemon([("running", "healthy")], None, _ns)
    checks[f"start_period: {_label} reads back correctly"] = (
        _p._get_start_period_seconds("app") == _want)

# …and the effect. Without a declared start_period the configured
# maximum stands.
checks["without a start_period the configured maximum applies"] = (
    _polls("") == 3)                       # 30s / 10s
# With one, it is the larger of the two, times 1.5.
checks["a longer start_period extends the wait"] = (
    _polls("60000000000") == 9)            # max(30, 60*1.5) = 90 → 9 polls
# …and a shorter one does not shorten it, which is the other half of
# `max()` and the direction that would cause the rollback.
checks["…while a shorter one never shortens it"] = (
    _polls("10000000000") == 3)            # max(30, 15) = 30 → 3 polls

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
