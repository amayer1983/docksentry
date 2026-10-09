#!/usr/bin/env python3
"""A maintenance window that actually holds an update back.

README:54 and `docs/updates.md`: a container can be set to auto-update
only inside a time range on chosen weekdays, so a database is not
recreated in the middle of the working day. On 06.10.2026 a mutation
sweep found nothing enforced it — `is_window_open` could be made to
return `True` always, the weekday list could be ignored, and the filter
in `handle_autoupdates` could be replaced with an empty list, and all
181 procedures stayed green.

Two halves, same shape as quiet hours, and the second is the one that
was missing entirely: the arithmetic was reachable and nobody drove the
gate that uses it.

The wrapping case carries a decision worth pinning. A window of
"Saturday 23:00 → 03:00" is one slot in a person's head and two
intervals on a clock, and the after-midnight half belongs to the
weekday the window OPENED on — so 02:00 on Sunday is inside a Saturday
window. Getting that backwards means an update at two in the morning on
a day the user did not pick.

Fail-open is deliberate throughout: no window, a malformed one, or a
zero-length one all mean "no restriction". The failure direction
matters — an update that is skipped stays pending and happens later,
but a window nobody can configure out of is a tool that stops working.
"""
import datetime
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import update_window  # noqa: E402

checks = {}

MON, SAT, SUN = 0, 5, 6


def _at(weekday, h, m=0):
    """A datetime on a chosen weekday — 2026-10-05 was a Monday."""
    return datetime.datetime(2026, 10, 5 + weekday, h, m)


def _open(window, when):
    return update_window.is_window_open(window, now=when)


# ── no window at all means no restriction ────────────────────────────
for empty in (None, {}, {"start": "", "end": ""},
              {"start": "nonsense", "end": "03:00"},
              {"start": "09:00", "end": "09:00"}):
    checks[f"{empty!r} is no restriction at all"] = (
        _open(empty, _at(MON, 14)) is True)

# ── a plain same-day window ──────────────────────────────────────────
night = {"start": "02:00", "end": "04:00"}
checks["inside a same-day window, it is open"] = _open(night, _at(MON, 3)) is True
checks["…and closed before it"] = _open(night, _at(MON, 1, 59)) is False
checks["…and closed after it"] = _open(night, _at(MON, 4, 1)) is False
checks["…open from its first minute"] = _open(night, _at(MON, 2, 0)) is True
checks["…and shut at its last"] = _open(night, _at(MON, 4, 0)) is False

# ── weekdays ─────────────────────────────────────────────────────────
monday_only = {"start": "02:00", "end": "04:00", "weekdays": [MON]}
checks["a weekday list keeps the window shut on other days"] = (
    _open(monday_only, _at(MON, 3)) is True
    and _open(monday_only, _at(SAT, 3)) is False)
checks["…and an empty weekday list means every day"] = (
    _open({"start": "02:00", "end": "04:00", "weekdays": []}, _at(SAT, 3))
    is True)
# A list the user gave must not evaporate. The filter dropped whatever
# it could not read, and an empty list means "every day" — so `["Sat"]`,
# `[5.0]` and `["-1"]` all opened the window on all seven days, which is
# the restriction disappearing in the one direction that matters: it
# updates on a day the user said not to. The old check here passed
# `["5"]` and read True, which is also what a list of pure rubbish gave.
checks["weekdays given as digit strings still count"] = (
    _open({"start": "02:00", "end": "04:00", "weekdays": ["5"]}, _at(SAT, 3))
    is True
    and _open({"start": "02:00", "end": "04:00", "weekdays": ["5"]},
              _at(MON, 3)) is False)
checks["…and a whole number written as a float does too"] = (
    _open({"start": "02:00", "end": "04:00", "weekdays": [5.0]}, _at(SAT, 3))
    is True)
for _rubbish in (["Sat"], ["-1"], [5.5], [None], [True]):
    checks[f"a weekday list of {_rubbish!r} does not mean every day"] = (
        not any(_open({"start": "02:00", "end": "04:00",
                       "weekdays": _rubbish}, _at(_d, 3))
                for _d in range(7)))
# …but one readable entry among rubbish is honoured, not thrown away.
checks["one readable weekday among rubbish still counts"] = (
    _open({"start": "02:00", "end": "04:00", "weekdays": ["5", "oops"]},
          _at(SAT, 3)) is True)

# ── the wrapping window, and whose day the small hours belong to ─────
weekend = {"start": "23:00", "end": "03:00", "weekdays": [SAT]}
checks["a window across midnight is open late on its own day"] = (
    _open(weekend, _at(SAT, 23, 30)) is True)
checks["…and still open after midnight, on the following day"] = (
    _open(weekend, _at(SUN, 2)) is True)
checks["…but not in the small hours of the day it opens on"] = (
    _open(weekend, _at(SAT, 2)) is False)
checks["…and not in the evening of the day it closes on"] = (
    _open(weekend, _at(SUN, 23, 30)) is False)
checks["…and shut in the middle of either day"] = (
    _open(weekend, _at(SAT, 14)) is False
    and _open(weekend, _at(SUN, 14)) is False)


# ── the gate: does an update actually get held back? ─────────────────
# The arithmetic above was reachable from a test and the thing that uses
# it was not, which is the gap that mattered. This drives the real
# filter in `handle_autoupdates`.
from telegram_bot import TelegramBot  # noqa: E402


class _Store:
    def __init__(self, windows):
        self._w = windows

    def get_autoupdate(self):
        return ["web", "db"]

    def get_update_windows(self):
        return self._w


_TMP = tempfile.mkdtemp(prefix="window-gate-")


def _auto_applied(windows, when_open):
    """Which containers the auto path would update right now."""
    bot = TelegramBot.__new__(TelegramBot)
    applied = []
    # A config that answers anything asked of it: the method reads a
    # handful of settings on its way past the gate, and none of them is
    # what is under test.
    class _Cfg:
        language = "en"
        auto_update_all = False
        bot_token = ""
        chat_id = "1"
        # In a temp directory, not the repo: `handle_autoupdates`
        # writes this file on its way past the gate, and the first
        # version pointed it at the working tree — which committed an
        # empty `nonexistent-pending.json` into the repository. A test
        # that leaves something behind is a test that touched something
        # it was not asked to.
        pending_file = os.path.join(_TMP, "pending.json")

        def __getattr__(self, name):
            return ""

    bot.config = _Cfg()
    from i18n import get_translator
    bot.t = get_translator("en")
    bot.send_message = lambda *a, **kw: None
    bot.announce = lambda *a, **kw: None
    bot.log = lambda *a, **kw: None
    bot._policy_decision = lambda u, c: None
    bot._age_decision = lambda u, c: None
    bot._say_once = lambda *a, **kw: None
    bot._display_name = lambda u: u["name"]
    # The batch runner is where the decision has landed: whatever
    # reaches it is what would actually be recreated.
    bot._process_update_batch = lambda ups, checker, auto=False: (
        applied.extend(u["name"] for u in ups) or ([], 0, []))
    import threading
    bot.engine = types.SimpleNamespace(_update_lock=threading.Lock(),
                                       _queued_selfupdate=None,
                                       _swap_in_flight=False)

    import update_engine
    real_store = update_engine.host_store
    update_engine.host_store = lambda scope, host: _Store(windows)
    real_open = update_window.is_window_open
    # The clock is the only thing faked: `is_window_open` is the real
    # function, so a change to the arithmetic shows up here too.
    try:
        bot.handle_autoupdates(
            [{"name": "web", "image": "nginx:1.25"},
             {"name": "db", "image": "postgres:16"}], None)
    finally:
        update_engine.host_store = real_store
        update_window.is_window_open = real_open
    return applied


# `db` is restricted to a window that is shut right now; `web` has none.
_SHUT = {"db": {"start": "02:00", "end": "04:00", "weekdays": [MON]}}
_real_now = update_window.is_window_open
update_window.is_window_open = lambda w, now=None: _real_now(
    w, now=datetime.datetime(2026, 10, 10, 14, 0))      # a Saturday afternoon
try:
    _applied = _auto_applied(_SHUT, when_open=False)
finally:
    update_window.is_window_open = _real_now
checks["a container whose window is shut is not auto-updated"] = (
    "db" not in _applied)
checks["…while one without a window still is"] = "web" in _applied

update_window.is_window_open = lambda w, now=None: _real_now(
    w, now=datetime.datetime(2026, 10, 5, 3, 0))        # Monday, 03:00
try:
    _applied = _auto_applied(_SHUT, when_open=True)
finally:
    update_window.is_window_open = _real_now
checks["…and inside its window it is updated after all"] = (
    "db" in _applied and "web" in _applied)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
