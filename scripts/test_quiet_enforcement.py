#!/usr/bin/env python3
"""Quiet hours and maintenance mode, driven through the gate they use.

README:38 says Docksentry can be told to stay silent between two times,
and `docs/notifications.md` repeats it. On 06.10.2026 a mutation sweep
found the promise was kept by nothing the suite could see: setting
`is_quiet_now()` to return `False`, deleting the after-midnight half of
a wrapping window, and neutralising `Notifier._suppressed()` — which is
the single enforcement point for all three automatic paths — each left
all 181 procedures green. Ten procedures import `quiet_hours`; none of
them asked it anything.

Two halves, and the second matters more than it looks.

The window arithmetic is easy to get wrong in one specific way: a quiet
period that crosses midnight (23:00–07:00) is not one interval but two,
and an implementation that compares `start <= now <= end` is silent from
23:00 to 07:00 only on paper — in practice it is never quiet at all.

And "quiet" must not mean "silent". An urgent message — a rollback, a
host that went away, a disk filling up — goes out anyway, because a
window you set to sleep through is not a reason to lose the one alert
worth waking up for. That half was shipped after it bit (#61): between
23:00 and 07:00 a rollback was Telegram-only.
"""
import datetime
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import quiet_hours  # noqa: E402
from notifier import Notifier  # noqa: E402

checks = {}


def _cfg(**kw):
    base = dict(quiet_hours_enabled=True, quiet_hours_start="23:00",
                quiet_hours_end="07:00", maintenance_until=None,
                maintenance_mode=False, language="en")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _at(h, m=0):
    return datetime.datetime(2026, 10, 6, h, m)


# ── the window, including the half that is after midnight ────────────
# `is_quiet_now` takes `now`, so nothing here depends on when the suite
# runs. That is not a nicety: the first version of this file patched
# `quiet_hours.is_quiet_now` while `notifier.py` had already bound the
# name at import, so the gate ran against the real clock — and because
# it happened to be 01:00, the "held back" checks passed for the wrong
# reason and the "goes out" ones failed. A test whose answer depends on
# the hour is the thing this file was written to replace.
wrap = _cfg()
for hour, want in ((23, True), (0, True), (3, True), (6, True),
                   (7, False), (12, False), (22, False)):
    word = "quiet" if want else "awake"
    checks[f"23:00–07:00 is {word} at {hour:02d}:00"] = (
        quiet_hours.is_quiet_now(wrap, now=_at(hour)) is want)
# The boundaries, stated: start is inclusive, end is not, or an alert at
# exactly 07:00 falls in a crack depending on which way it rounds.
checks["…quiet from the first minute of the window"] = (
    quiet_hours.is_quiet_now(wrap, now=_at(23, 0)) is True)
checks["…and awake again at the end of it"] = (
    quiet_hours.is_quiet_now(wrap, now=_at(7, 0)) is False)

# A window inside one day must not accidentally work by wrapping.
same_day = _cfg(quiet_hours_start="13:00", quiet_hours_end="14:00")
checks["a window inside one day is quiet only inside it"] = (
    quiet_hours.is_quiet_now(same_day, now=_at(13, 30)) is True
    and quiet_hours.is_quiet_now(same_day, now=_at(12, 59)) is False
    and quiet_hours.is_quiet_now(same_day, now=_at(14, 1)) is False)

# Off is "no window set" — there is no separate switch, which is worth
# pinning because a reader of the settings page would expect one.
checks["with no window set, it is never quiet"] = (
    quiet_hours.is_quiet_now(_cfg(quiet_hours_start=""), now=_at(2)) is False
    and quiet_hours.is_quiet_now(_cfg(quiet_hours_end=""), now=_at(2)) is False)
checks["…and a window of zero length is not 'all day'"] = (
    quiet_hours.is_quiet_now(_cfg(quiet_hours_start="09:00",
                                  quiet_hours_end="09:00"),
                             now=_at(9, 0)) is False)
# An unparseable setting must fail towards noisy, not towards silent:
# losing alerts is worse than sending one during a window somebody
# typed wrong.
for bad_value in ("", "nonsense", "25:00", None):
    checks[f"a start of {bad_value!r} does not silence everything"] = (
        quiet_hours.is_quiet_now(_cfg(quiet_hours_start=bad_value),
                                 now=_at(2)) is False)


# ── the gate: what actually reaches the channels ─────────────────────
class _Plugin:
    name = "stub"
    order = 10

    def __init__(self):
        self.sent = []

    def active(self):
        return True

    def send_message(self, text, skip=()):
        self.sent.append(("message", text))

    def send_updates_available(self, updates):
        self.sent.append(("updates", updates))

    def send_update_result(self, name, image, success, detail="",
                           source_url=""):
        self.sent.append(("result", name))

    def send_weekly_report(self, stats, text, embed=None):
        self.sent.append(("weekly", text))


import notifier as notifier_mod                               # noqa: E402


def _through_gate(quiet, fn, maint=False):
    """Run `fn(notifier)` with the gate told what time it is.

    `notifier.is_quiet_now` is the name that matters — the module binds
    it at import, so patching `quiet_hours.is_quiet_now` leaves the gate
    talking to the real clock. `_suppressed` itself is the real method.
    """
    import maintenance
    real_q = notifier_mod.is_quiet_now
    real_m = maintenance.is_active
    notifier_mod.is_quiet_now = lambda cfg, now=None: quiet
    maintenance.is_active = lambda cfg: maint
    try:
        n = Notifier.__new__(Notifier)
        n.config = _cfg()
        p = _Plugin()
        n._plugins = [p]
        n._by_name = {p.name: p}
        fn(n)
        return p.sent
    finally:
        notifier_mod.is_quiet_now = real_q
        maintenance.is_active = real_m


_UPDATES = [{"name": "web", "image": "nginx:1.25"}]

checks["during quiet hours, an update list is held back"] = (
    _through_gate(True, lambda n: n.send_updates_available(_UPDATES)) == [])
checks["…and an update result is too"] = (
    _through_gate(True, lambda n: n.send_update_result("web", "nginx", True)) == [])
checks["…and a routine message is"] = (
    _through_gate(True, lambda n: n.send_message("checked, nothing new")) == [])

# The other direction, so the three above cannot pass on a stand-in that
# never sends anything at all — which is how the first version of this
# file was green while measuring nothing.
checks["outside them, the same update list goes out"] = (
    [k for k, _ in _through_gate(False, lambda n: n.send_updates_available(_UPDATES))]
    == ["updates"])
checks["…and so does the same result"] = (
    [k for k, _ in _through_gate(False,
     lambda n: n.send_update_result("web", "nginx", True))] == ["result"])
checks["…and the same message"] = (
    [k for k, _ in _through_gate(False, lambda n: n.send_message("hello"))]
    == ["message"])

# …and the exception that was shipped after it bit (#61): between 23:00
# and 07:00 a rollback was Telegram-only.
checks["an urgent message goes out during quiet hours anyway"] = (
    [k for k, _ in _through_gate(True,
     lambda n: n.send_message("rolled back", urgent=True))] == ["message"])

# Maintenance mode is the other half of the same gate, and urgent beats
# it too: a window you opened to work in is not a reason to lose a
# storage alert.
checks["maintenance mode holds a routine notification back"] = (
    _through_gate(False, lambda n: n.send_updates_available(_UPDATES),
                  maint=True) == [])
checks["…and an urgent message still gets through it"] = (
    [k for k, _ in _through_gate(False,
     lambda n: n.send_message("disk nearly full", urgent=True),
     maint=True)] == ["message"])

# The weekly report deliberately ignores quiet hours — it is sent at an
# hour the operator picked, and holding it would skip the week rather
# than delay it. Stated here because "quiet means quiet" would look
# like the obvious rule otherwise.
checks["the weekly report is not held by quiet hours"] = (
    _through_gate(True, lambda n: n.send_weekly_report({}, "the week")) != [])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
