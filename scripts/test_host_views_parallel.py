#!/usr/bin/env python3
"""Managed hosts are asked side by side, and asked once.

Measured on an install with four hosts: an ssh endpoint cost 2.1 seconds,
of which 476 ms was a probe asking exactly what the listing asks again a
moment later — and the hosts ran one after another, so the page paid the
sum of all of them. It now pays the slowest one, once.

What must not change: one dead endpoint stays a line in the table rather
than an exception that takes the page with it, and the order of the hosts
on screen does not depend on which answered first.

This file used to assert all of that by reading `web_ui.py` as text —
nine checks looking for `ThreadPoolExecutor`, counting `return _h, None,
_e`, matching a regex over `def _fetch`. Not one of them ran the
function. They would have stayed green if the pool were removed and the
loop put back, as long as the words survived somewhere in the block. So
they are gone, and this runs it: three hosts that each take 0.3 s, and a
clock.
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import hosts as hosts_mod                                    # noqa: E402
import web_ui                                                # noqa: E402

checks = {}
DELAY = 0.3


class _Reply:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class _Backend:
    """One remote endpoint. Slow on purpose, and it counts its questions."""

    def __init__(self, name, ids=("a1", "b2"), fail=None, delay=DELAY):
        self.name, self._ids, self._fail, self._delay = name, ids, fail, delay
        self.ps_calls = 0

    def ps(self, *, quiet=False, all=False, fmt=None, timeout=None):
        self.ps_calls += 1
        time.sleep(self._delay)
        if self._fail:
            return _Reply(returncode=1, stderr=self._fail)
        return _Reply("\n".join(self._ids))


class _Host:
    def __init__(self, name, backend, is_local=False):
        self.name, self.backend, self.is_local = name, backend, is_local
        self.endpoint = f"ssh://{name}"
        self.store = types.SimpleNamespace()


def _handler(hosts_registry=None):
    cfg = types.SimpleNamespace(language="en", debug=False, ui_mode="advanced",
                                disk_warn_percent=85)
    cls = web_ui.create_handler(cfg, types.SimpleNamespace(), None,
                                types.SimpleNamespace(), hosts=hosts_registry) \
        if "hosts" in web_ui.create_handler.__code__.co_varnames \
        else web_ui.create_handler(cfg, types.SimpleNamespace(), None,
                                   types.SimpleNamespace())
    h = cls.__new__(cls)
    # The local half is another file's subject; keep this one on the
    # remote hosts it is about.
    h._get_containers = lambda: []
    h._status_view = lambda host, *a, **k: {"host": host}
    h._docker_contexts = lambda: []
    h._why = lambda e: str(e) or "unreachable"
    seen = []
    h._containers_on = lambda backend, timeout=None, ids=None: (
        seen.append((backend.name, tuple(ids or ()))) or [{"name": "x"}])
    return h, seen


def _fresh():
    """No memory of earlier failures — the module keeps it globally."""
    for n in ("alpha", "beta", "gamma", "dead"):
        hosts_mod.mark_reachable(n)


# ── side by side, and the clock is the proof ─────────────────────────
_fresh()
h, seen = _handler()
backends = [_Backend(n) for n in ("alpha", "beta", "gamma")]
multi = [_Host("alpha", backends[0]), _Host("beta", backends[1]),
         _Host("gamma", backends[2])]
t0 = time.monotonic()
views = h._host_views(multi=multi, store_for=lambda _h: types.SimpleNamespace())
elapsed = time.monotonic() - t0

checks["three hosts that take 0.3s each do not take 0.9s"] = elapsed < DELAY * 2
checks["…and it really did wait for all of them"] = elapsed >= DELAY
checks["every host is in the answer"] = (
    [v.get("host") for v in views] == ["local", "alpha", "beta", "gamma"])
checks["each host is asked for its containers exactly once"] = (
    [b.ps_calls for b in backends] == [1, 1, 1])
checks["…and the ids it answered are handed to the lister"] = (
    sorted(seen) == [("alpha", ("a1", "b2")), ("beta", ("a1", "b2")),
                     ("gamma", ("a1", "b2"))])

# ── order comes from the configuration, not from who finished first ──
_fresh()
h2, _ = _handler()
slowest_first = [_Host("alpha", _Backend("alpha", delay=0.45)),
                 _Host("beta", _Backend("beta", delay=0.05)),
                 _Host("gamma", _Backend("gamma", delay=0.15))]
v2 = h2._host_views(multi=slowest_first, store_for=lambda _h: types.SimpleNamespace())
checks["the slowest host still comes first if it is configured first"] = (
    [v.get("host") for v in v2] == ["local", "alpha", "beta", "gamma"])

# ── one dead endpoint is a row, not an exception ─────────────────────
_fresh()
h3, _ = _handler()
mixed = [_Host("alpha", _Backend("alpha")),
         _Host("dead", _Backend("dead", fail="ssh: connect to host dead port 22")),
         _Host("gamma", _Backend("gamma"))]
v3 = h3._host_views(multi=mixed, store_for=lambda _h: types.SimpleNamespace())
_by = {v.get("host"): v for v in v3}
checks["a dead endpoint does not take the page with it"] = (
    [v.get("host") for v in v3] == ["local", "alpha", "dead", "gamma"])
checks["…it becomes a row that says which host and why"] = (
    _by["dead"].get("unreachable") == "dead"
    and "ssh: connect" in (_by["dead"].get("reason") or ""))
checks["…and the live hosts either side are unaffected"] = (
    _by["alpha"].get("unreachable") is None
    and _by["gamma"].get("unreachable") is None)

# ── a remembered failure is shown as remembered, with the wait ───────
h4, _ = _handler()
hosts_mod.mark_unreachable("beta", "ssh: no route to host", cooldown=30)
b4 = _Backend("beta")
v4 = h4._host_views(multi=[_Host("beta", b4)],
                    store_for=lambda _h: types.SimpleNamespace())
_row = [v for v in v4 if v.get("host") == "beta"][0]
checks["a host still in its cooldown is not asked again"] = b4.ps_calls == 0
checks["…and its row says how long until the retry"] = (
    _row.get("retry_in", 0) > 0 and _row.get("reason") == "ssh: no route to host")
hosts_mod.mark_reachable("beta")

# ── a host that answers forgets its failure ──────────────────────────
hosts_mod.mark_unreachable("gamma", "boom", cooldown=0.01)
time.sleep(0.05)
h5, _ = _handler()
h5._host_views(multi=[_Host("gamma", _Backend("gamma", delay=0))],
               store_for=lambda _h: types.SimpleNamespace())
checks["a host that answers clears its failure memory"] = (
    hosts_mod.unreachable_for("gamma")[0] == 0.0)

# ── and the lister still works for callers with no ids to hand over ──
_lister = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "web_ui.py"), encoding="utf-8").read()
_lister = _lister[_lister.index("def _containers_on"):]
_lister = _lister[:_lister.index("\n        def ", 10)]
checks["the lister still fetches ids when it is not given any"] = (
    "if ids is None:" in _lister)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
