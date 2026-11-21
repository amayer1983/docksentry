#!/usr/bin/env python3
"""A host that just failed is not asked again on the next page load.

Measured on a real install with one dead endpoint: the status page took
13.6 seconds, of which ten were the probe timing out — against 0.08s for a
page that does not build the host list. Reloading to see whether the host
came back is exactly what a reader does, and every reload paid the wait
again.

The memory is short on purpose: a host that comes back is noticed within a
minute. It is not a cache of the answer, only of "do not ask right now".
"""
import os
import sys
import threading
import time

import srcslice

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import hosts  # noqa: E402

checks = {}

hosts.forget_unreachable()
checks["an unknown host is not being skipped"] = (
    hosts.unreachable_for("nas") == (0.0, ""))

hosts.mark_unreachable("nas", "connection refused")
left, why = hosts.unreachable_for("nas")
checks["a failed host is skipped for a while"] = (left > 0)
checks["…and the wait is bounded, not forever"] = (left <= hosts.UNREACHABLE_COOLDOWN)
checks["…and it quotes the reason it was given"] = (why == "connection refused")
checks["a different host is unaffected"] = (
    hosts.unreachable_for("nas2") == (0.0, ""))

hosts.mark_reachable("nas")
checks["a host that answers is forgotten immediately"] = (
    hosts.unreachable_for("nas") == (0.0, ""))

# A host that keeps failing is asked less and less often: one minute is
# right for a reboot, wrong for an endpoint that was typo'd months ago and
# costs its full timeout on every load a minute apart.
hosts.forget_unreachable()
_waits = []
for _ in range(6):
    hosts.mark_unreachable("gone", "no route")
    _waits.append(hosts.unreachable_for("gone")[0])
    hosts._unreachable.pop("gone", None)
checks["repeated failures are asked about less often"] = (
    _waits[1] > _waits[0] and _waits[2] > _waits[1])
checks["…but the wait stops growing"] = (
    abs(_waits[-1] - _waits[-2]) < 1
    and abs(_waits[-1] - hosts.MAX_UNREACHABLE_COOLDOWN) < 1)
hosts.mark_reachable("gone")
hosts.mark_unreachable("gone", "no route")
checks["one success puts it back to the short wait"] = (
    hosts.unreachable_for("gone")[0] <= hosts.UNREACHABLE_COOLDOWN)

hosts.forget_unreachable()
hosts.mark_unreachable("nas", "boom", cooldown=0)
checks["once the wait is over it is asked again"] = (
    hosts.unreachable_for("nas")[0] == 0.0)

hosts.mark_unreachable("", "boom")
checks["a nameless host is not remembered"] = (
    hosts.unreachable_for("") == (0.0, ""))

hosts.forget_unreachable()
hosts.mark_unreachable("nas", "boom")
hosts.forget_unreachable()
checks["a config reload can drop the whole memory"] = (
    hosts.unreachable_for("nas") == (0.0, ""))

# ── the walk: the memory is read and written in one place ────────────
# A memory nothing reads is not a fix. `hosts.walk` is where it is read
# before a host is asked and written after, so these drive the walk
# itself with stand-in hosts — no daemon, a fetch that counts.


class _Host:
    def __init__(self, name):
        self.name = name


class _Fetch:
    """A probe that counts who it was asked about, and how many at once.

    With `gate`, each call waits (at most a second) until `gate` calls
    are inside at the same time, or every host has been asked, and then
    stays a moment longer. That makes the peak a count rather than a
    race against thread start-up: a pool of eight reaches eight however
    loaded the machine is, and a serial walk stays at one.
    """

    def __init__(self, dead=(), gate=0, total=0):
        self.dead, self.gate, self.total = set(dead), gate, total
        self.asked = []
        self.active = self.peak = 0
        self._cond = threading.Condition()

    def __call__(self, host):
        with self._cond:
            self.asked.append(host.name)
            self.active += 1
            self.peak = max(self.peak, self.active)
            self._cond.notify_all()
            if self.gate:
                self._cond.wait_for(
                    lambda: self.active >= self.gate
                    or len(self.asked) >= self.total, timeout=1.0)
        try:
            if self.gate:
                time.sleep(0.05)
            if host.name in self.dead:
                raise OSError(f"ssh: connect to host {host.name} port 22")
            return f"{host.name}-ok"
        finally:
            with self._cond:
                self.active -= 1


_walk = getattr(hosts, "walk", None)
checks["the core has the host walk"] = callable(_walk)
if not callable(_walk):
    def _walk(*a, **k):
        return []


def _by_name(out):
    return {getattr(e, "host", None) and e.host.name: e for e in out}


# A dead endpoint is marked, with the words the caller chose for it.
hosts.forget_unreachable()
_f = _Fetch(dead={"dead"})
_out = _walk([_Host("a"), _Host("dead"), _Host("c")], _f,
             describe=lambda e: "said: " + str(e))
_e = _by_name(_out)
checks["the walk asks every host once"] = sorted(_f.asked) == ["a", "c", "dead"]
checks["…and hands back each answer"] = (
    "a" in _e and _e["a"].result == "a-ok"
    and "c" in _e and _e["c"].result == "c-ok")
checks["a dead endpoint comes back as its error, not as a crash"] = (
    "dead" in _e and isinstance(_e["dead"].error, OSError)
    and _e["dead"].result is None and _e["dead"].skipped is False)
checks["…with the words the caller made of it"] = (
    "dead" in _e
    and _e["dead"].reason == "said: ssh: connect to host dead port 22")
_left, _why = hosts.unreachable_for("dead")
checks["…and the walk remembers it as dead"] = (
    _left > 0 and _why == "said: ssh: connect to host dead port 22")
checks["…while the hosts that answered are not marked"] = (
    hosts.unreachable_for("a") == (0.0, "")
    and hosts.unreachable_for("c") == (0.0, ""))

# A host still in its cooldown is not asked, and says how long is left.
hosts.forget_unreachable()
hosts.mark_unreachable("b", "ssh: no route to host", cooldown=30)
_f = _Fetch()
_out = _walk([_Host("a"), _Host("b")], _f)
_e = _by_name(_out)
checks["a host found dead is not asked again within its cooldown"] = (
    _f.asked == ["a"])
checks["…its entry says it was skipped, and why"] = (
    "b" in _e and _e["b"].skipped is True
    and _e["b"].reason == "ssh: no route to host"
    and _e["b"].result is None and _e["b"].error is None)
checks["…and how many whole seconds until the retry"] = (
    "b" in _e and isinstance(_e["b"].retry_in, int)
    and 29 <= _e["b"].retry_in <= 30)
checks["…and an answering host carries no retry_in"] = (
    "a" in _e and _e["a"].retry_in is None and _e["a"].skipped is False)

# …unless the caller says it wants a fresh answer whatever the memory says.
_f = _Fetch()
_walk([_Host("b")], _f, use_cache=False)
checks["use_cache=False asks a host even inside its cooldown"] = (
    _f.asked == ["b"])
# It skips reading the memory, not writing it: what it found is kept.
checks["…and a host that answers then is forgotten"] = (
    hosts.unreachable_for("b") == (0.0, "") and "b" not in hosts._failures)
hosts.forget_unreachable()
_walk([_Host("gone")], _Fetch(dead={"gone"}), use_cache=False,
      describe=lambda e: "fresh")
checks["…and one that fails then is remembered"] = (
    hosts.unreachable_for("gone")[1] == "fresh")

# Whole seconds, rounded down, as the page has always shown them: with
# 30.5 seconds left the entry says 30, not 31.
hosts.forget_unreachable()
hosts.mark_unreachable("half", "boom", cooldown=30.5)
_e = _by_name(_walk([_Host("half")], _Fetch()))
checks["…rounded down, the way the page always showed it"] = (
    "half" in _e and _e["half"].retry_in == 30)

# A host that comes back is marked reachable: the failure counter that
# drives the growing cooldown is what only `mark_reachable` clears (an
# expired entry alone reads as (0.0, "") anyway).
hosts.forget_unreachable()
hosts.mark_unreachable("back", "boom")
with hosts._unreachable_lock:
    hosts._unreachable.pop("back", None)        # due, count kept
checks["(a failure is counted before it comes back)"] = (
    hosts._failures.get("back") == 1)
_f = _Fetch()
_out = _walk([_Host("back")], _f)
checks["a host that comes back is asked again once it is due"] = (
    _f.asked == ["back"])
checks["…and marked reachable: its failure count is gone"] = (
    "back" not in hosts._failures)

# Order is the order given, not who finished first.
hosts.forget_unreachable()


class _Slow(_Fetch):
    def __call__(self, host):
        time.sleep({"x": 0.2, "y": 0.0, "z": 0.1}[host.name])
        return super().__call__(host)


_out = _walk([_Host("x"), _Host("y"), _Host("z")], _Slow())
checks["the walk keeps the order it was given"] = (
    [e.host.name for e in _out] == ["x", "y", "z"])

# …whatever became of each host: a skipped one keeps its place between
# one that answered and one that failed, it is not put first or last.
hosts.forget_unreachable()
hosts.mark_unreachable("s", "boom", cooldown=30)
_out = _walk([_Host("x"), _Host("s"), _Host("dead"), _Host("z")],
             _Fetch(dead={"dead"}))
checks["…for skipped, failed and answering hosts alike"] = (
    [(e.host.name, e.skipped, e.error is not None) for e in _out]
    == [("x", False, False), ("s", True, False),
        ("dead", False, True), ("z", False, False)])

# At most eight at once by default, and the caller's cap holds below that.
# The gate asks for all twelve at once: a pool of eight waits out its
# second and stays at eight, a pool without the cap reaches twelve.
_f = _Fetch(gate=12, total=12)
_walk([_Host(f"h{i}") for i in range(12)], _f)
checks["no more than eight hosts are asked at once"] = (
    _f.peak == 8 and len(_f.asked) == 12)
_f = _Fetch(gate=2, total=5)
_walk([_Host(f"h{i}") for i in range(5)], _f, max_workers=2)
checks["…and a smaller max_workers is honoured"] = (
    _f.peak == 2 and len(_f.asked) == 5)
_f = _Fetch(gate=12, total=12)
_walk([_Host(f"h{i}") for i in range(12)], _f, max_workers=12)
checks["…but a larger max_workers cannot lift the cap of eight"] = (
    _f.peak == 8 and len(_f.asked) == 12)
_f = _Fetch(gate=4, total=4)
_walk([_Host(f"h{i}") for i in range(4)], _f)
checks["…and the hosts really are asked side by side"] = _f.peak == 4

checks["an empty host list is not an error"] = (
    callable(getattr(hosts, "walk", None)) and hosts.walk([], _Fetch()) == [])
hosts.forget_unreachable()

# The page has no copy of its own: it hands the walk a probe.
_src = open(os.path.join(os.path.dirname(__file__), "..",
                         "app", "web_ui.py")).read()
_view = srcslice.section(_src, "def _host_views", "\n        def ",
                         label="web_ui.py: _host_views")
checks["the page asks the core for the walk"] = (
    _view is not None and "_hosts_mod.walk(" in _view)
# The words that go into the memory are the page's own: `_why` cuts
# the CLI's error block down to the one line the row quotes.
checks["…and hands it its own words for a dead host"] = (
    _view is not None and "describe=self._why" in _view)
checks["…and keeps no memory handling of its own"] = (
    _view is not None
    and not any(w in _view for w in ("unreachable_for", "mark_reachable",
                                     "mark_unreachable", "ThreadPoolExecutor")))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
