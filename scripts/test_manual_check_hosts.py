#!/usr/bin/env python3
"""A manual check covers every managed host, not just the local one.

The scheduled check has walked every host since multi-host landed. The two
*manual* checks did not: the Web UI's "Check Updates" button and Telegram's
`/check --dry-run` each called `checker.check_all()` on the one local
checker, with no loop anywhere near them.

Measured on a two-host demo — four containers on moving tags, two of them
on `nas` — before the fix:

    Checking 2 containers for updates...
      Checking: nginx-proxy (registry-1.docker.io/library/nginx:latest)
      Checking: redis-cache (registry-1.docker.io/library/redis:alpine)

and after:

    Checking 2 containers for updates...
      Checking: nginx-proxy (…/library/nginx:latest)
      Checking: redis-cache (…/library/redis:alpine)
    Checking 2 containers for updates...
      Checking: backup (…/restic/restic:latest)
      Checking: syncthing (…/syncthing/syncthing:latest)

This is the bad kind of wrong. It does not fail, it answers — and the
answer is "checked", so you believe your estate was checked when half of it
was never looked at. A dry run in particular exists to be trusted before an
update.

The cause was that `Scheduler._checkers` was the only thing in the process
that knew how to walk the hosts. It is `hosts.host_checkers` now, and the
first check below is against that shared helper, because the next front end
to be added will reach for it rather than reinventing the loop.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from hosts import HostRegistry, ManagedHost, host_checkers  # noqa: E402
import web_ui  # noqa: E402


class FakeChecker:
    """Records that it was asked, and what it would have found."""

    def __init__(self, name, updates=None, fail=False):
        self.name = name
        self.updates = updates or []
        self.fail = fail
        self.calls = 0

    def check_all(self, bot=None):
        self.calls += 1
        if self.fail:
            raise OSError(f"{self.name} unreachable")
        return list(self.updates)


def registry(*checkers):
    """A HostRegistry over `checkers`, first one local."""
    hosts = [ManagedHost(c.name, None, c, None, is_local=(i == 0))
             for i, c in enumerate(checkers)]
    return HostRegistry(hosts)


def web_check(hosts, checker):
    """Drive the Web UI's check button and return what it notified."""
    notified = []
    bot = types.SimpleNamespace(update_running=False,
                                notify_updates=notified.extend)
    handler_cls = web_ui.create_handler(
        types.SimpleNamespace(), checker=checker, bot=bot, store=None,
        backend=object(), hosts=hosts)
    h = handler_cls.__new__(handler_cls)
    h._api_check()
    return notified


def main():
    checks = {}

    # ── the shared helper ────────────────────────────────────────
    one = FakeChecker("local")
    r = registry(one)
    got = host_checkers(r, one)
    checks["a single host yields one checker"] = len(got) == 1
    # The blank name is what keeps single-host output free of an "on
    # local" that says nothing — asserted so it stays deliberate.
    checks["…with a blank host name, so nothing gains a label"] = got[0][1] == ""

    a, b = FakeChecker("local"), FakeChecker("nas")
    got = host_checkers(registry(a, b), a)
    checks["two hosts yield two checkers"] = len(got) == 2
    checks["…each carrying its own name"] = [g[1] for g in got] == ["local", "nas"]
    checks["…local first"] = got[0][0] is a

    # No registry at all (older call sites, handlers built in tests).
    got = host_checkers(None, one)
    checks["no registry falls back to the one checker"] = got == [(one, "")]

    # ── the Web UI button ────────────────────────────────────────
    a = FakeChecker("local", updates=[{"name": "nginx"}])
    b = FakeChecker("nas", updates=[{"name": "backup"}])
    found = web_check(registry(a, b), a)
    checks["the check button asks the local host"] = a.calls == 1
    checks["the check button asks the second host too"] = b.calls == 1
    checks["and reports what both of them found"] = (
        [u["name"] for u in found] == ["nginx", "backup"])

    # A single-host install must behave exactly as before: one call, and
    # the registry's own local checker rather than the passed-in one is
    # neither here nor there as long as it is asked exactly once.
    solo = FakeChecker("local", updates=[{"name": "nginx"}])
    found = web_check(registry(solo), solo)
    checks["a single-host install still checks once"] = solo.calls == 1
    checks["…and reports its findings"] = len(found) == 1

    # One unreachable host must not cost the others their check. This is
    # the rule the scheduler already follows, and the reason it reports
    # per host instead of aborting the sweep.
    dead = FakeChecker("local", fail=True)
    alive = FakeChecker("nas", updates=[{"name": "backup"}])
    found = web_check(registry(dead, alive), dead)
    checks["an unreachable host does not stop the others"] = alive.calls == 1
    checks["…and the reachable host's findings still arrive"] = (
        [u["name"] for u in found] == ["backup"])

    # ── Telegram's dry run ───────────────────────────────────────
    # Source-level, not behavioural: driving that branch needs a whole
    # bot. What matters is that it no longer calls check_all on a single
    # checker, and that it resolves targets the way the arg-less /check
    # does — those two together are the defect and the fix.
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "telegram_bot.py"), encoding="utf-8").read()
    i = src.index('elif text in ("/check --dry-run"')
    branch = src[i:src.index("\n        elif ", i + 10)]
    checks["the dry run resolves host targets"] = "_resolve_targets" in branch
    checks["…loops over them"] = "for host in (targets or [None])" in branch
    checks["…and asks each host's own checker"] = "_checker_for" in branch
    checks["…tolerating one unreachable host"] = "host_check_failed" in branch

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
