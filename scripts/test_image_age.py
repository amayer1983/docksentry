#!/usr/bin/env python3
"""Hold an update back until the image has been public for N days.

Two independent reasons people ask for this, and the second is what makes
it more than a preference. Risk deferral — let someone else find the broken
release first — and supply chain: a compromised image is usually noticed
within days, so not being first to pull it is a real defence. Four of the
five compared projects had the request; dockcheck shipped it as `-d`.
"""

import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_engine import UpdateEngine


def _eng(days):
    e = UpdateEngine.__new__(UpdateEngine)
    e.config = types.SimpleNamespace(min_image_age_days=days)
    return e


def _checker(label=None):
    return types.SimpleNamespace(
        get_container_labels=lambda n: ({"docksentry.min-age": label}
                                        if label is not None else {}))


def _built(days_ago):
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.strftime("%Y-%m-%d")


def main():
    checks = {}
    dec = UpdateEngine._age_decision

    u_fresh = {"name": "app", "new_created": _built(1)}
    u_old = {"name": "app", "new_created": _built(30)}

    # Off by default — nobody who has not asked for a delay should get one.
    checks["off by default"] = dec(_eng(0), u_fresh, _checker()) is None
    checks["a fresh image is held"] = dec(_eng(7), u_fresh, _checker()) is not None
    checks["an aged image passes"] = dec(_eng(7), u_old, _checker()) is None
    # Exactly at the threshold counts as old enough; otherwise "wait 7 days"
    # would mean eight.
    checks["the boundary is inclusive"] = dec(
        _eng(7), {"name": "a", "new_created": _built(7)}, _checker()) is None

    held = dec(_eng(7), u_fresh, _checker())
    checks["the decision carries age and requirement"] = held == (1, 7)

    # Per container, like every other docksentry.* setting.
    checks["a label raises the bar"] = dec(
        _eng(0), u_fresh, _checker("14")) is not None
    checks["a label lowers it too"] = dec(
        _eng(30), u_old, _checker("7")) is None
    checks["a label of 0 disables it"] = dec(
        _eng(7), u_fresh, _checker("0")) is None
    checks["a nonsense label falls back to the global"] = dec(
        _eng(7), u_fresh, _checker("soon")) is not None

    # Fails OPEN when the date is unknown. The gate cannot judge what it
    # cannot see, and failing closed would silently stop updates for every
    # image whose registry exposes no build date — the trap UPDATE_POLICY
    # fell into, where a safety setting quietly did nothing.
    checks["no date, no hold"] = dec(
        _eng(7), {"name": "a", "new_created": ""}, _checker()) is None
    checks["a malformed date does not hold"] = dec(
        _eng(7), {"name": "a", "new_created": "not-a-date"}, _checker()) is None
    checks["a missing key does not hold"] = dec(_eng(7), {"name": "a"}, _checker()) is None

    # A checker that throws must not take the update decision with it.
    bad = types.SimpleNamespace(
        get_container_labels=lambda n: (_ for _ in ()).throw(RuntimeError("x")))
    checks["a failing label read falls back to the global"] = dec(
        _eng(7), u_fresh, bad) is not None

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
