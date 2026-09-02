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

# The wiring: the page must consult the memory BEFORE probing, and must
# clear it when a host answers — a memory nothing reads is not a fix.
_src = open(os.path.join(os.path.dirname(__file__), "..",
                         "app", "web_ui.py")).read()
_view = _src[_src.index("def _host_views"):]
_view = _view[:_view.index("\n        def ", 10)]
checks["the page asks the memory before it probes"] = (
    _view.index("unreachable_for") < _view.index("backend.ps"))
checks["…and clears it when the host answers"] = ("mark_reachable" in _view)
checks["…and records the failure it just had"] = ("mark_unreachable" in _view)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
