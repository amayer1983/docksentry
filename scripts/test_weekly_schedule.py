#!/usr/bin/env python3
"""When the weekly report fires — the part its own test replaced.

`test_weekly_report_channels.py` covers where the report goes, and it
does that by writing `weekly_report.should_send_now = lambda *a, **k:
True`, which is sensible for a file about channels and left the decision
itself with nothing. On 06.10.2026 a mutation sweep found that making it
return `True` always, OR `False` always, left all 181 procedures green.
I reproduced the `True` half myself: zero red out of 185.

Both directions are bad in different ways. Always-True sends a weekly
report on every scheduler tick — once a minute, to every channel.
Always-False silently stops a feature somebody set up and will not
notice for a week, and then not be sure whether it ever worked.

The rules, and each is a thing to get wrong on its own: switched off
means off; the weekday and hour have to match; "at or after the hour"
rather than "exactly at it", because the scheduler may have been down at
the minute; and once per day, which is what stops the hour-onwards rule
from firing every tick for the rest of the day.

The state file is deliberately NOT written here — the caller marks it
after a successful send, so a failed delivery leaves the day open to
retry. That split is easy to lose in a refactor, so it is pinned too.
"""
import datetime
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import weekly_report  # noqa: E402

checks = {}

MON, TUE, SUN = 0, 1, 6
_tmp = tempfile.mkdtemp(prefix="weekly-")


def _cfg(last_sent=None, **kw):
    """A config whose state file says when the last report went out."""
    path = os.path.join(_tmp, f"state-{len(os.listdir(_tmp))}.json")
    if last_sent is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"last_sent": last_sent}, f)
    base = dict(weekly_report_enabled=True, weekly_report_weekday=MON,
                weekly_report_hour=9, weekly_report_state_file=path)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _at(weekday, hour, minute=0):
    """2026-10-05 was a Monday."""
    return datetime.datetime(2026, 10, 5 + weekday, hour, minute)


# ── switched off is off ──────────────────────────────────────────────
checks["switched off, it never fires"] = (
    weekly_report.should_send_now(_cfg(weekly_report_enabled=False),
                                  now=_at(MON, 9)) is False)

# ── the right day and hour ───────────────────────────────────────────
checks["on the configured day and hour, it fires"] = (
    weekly_report.should_send_now(_cfg(), now=_at(MON, 9)) is True)
checks["…and not on another day"] = (
    weekly_report.should_send_now(_cfg(), now=_at(TUE, 9)) is False
    and weekly_report.should_send_now(_cfg(), now=_at(SUN, 9)) is False)
checks["…and not before the hour"] = (
    weekly_report.should_send_now(_cfg(), now=_at(MON, 8, 59)) is False)

# ── at or after the hour, not exactly at it ──────────────────────────
# The scheduler ticks on its own cadence and may have been down at 09:00
# — a report that only fires in that one minute is a report that skips a
# week whenever the container restarted at the wrong moment.
checks["later the same day it still fires"] = (
    weekly_report.should_send_now(_cfg(), now=_at(MON, 14)) is True)
checks["…right up to the end of the day"] = (
    weekly_report.should_send_now(_cfg(), now=_at(MON, 23, 59)) is True)

# ── …but only once ───────────────────────────────────────────────────
# Which is what stops the rule above from firing on every tick for the
# rest of the day. Measured on the real state file, not on a flag.
already = _cfg(last_sent="2026-10-05")
checks["once it has gone out today, it does not go again"] = (
    weekly_report.should_send_now(already, now=_at(MON, 14)) is False)
checks["…and the week after, it does"] = (
    weekly_report.should_send_now(already, now=_at(MON + 7, 9)) is True)

# ── a weekday or hour that is not a number ───────────────────────────
# Fail closed here, unlike the quiet-hours window: a report is a
# convenience and sending it at an unintended time is worse than not
# sending it, so an unreadable setting stops rather than guesses.
# Written out rather than looped: the first version built the check
# name from the setting with `split("_")[-1]`, which gives "hour" for
# two different cases — so one silently overwrote the other and only
# one of the two ever ran. That is the dict-key collision
# `test_check_quality.py` hunts, written by hand in the file that hunts
# it.
checks["an unreadable weekday does not fire"] = (
    weekly_report.should_send_now(_cfg(weekly_report_weekday="nonsense"),
                                  now=_at(MON, 9)) is False)
checks["an unreadable hour does not fire"] = (
    weekly_report.should_send_now(_cfg(weekly_report_hour="nonsense"),
                                  now=_at(MON, 9)) is False)
# …but an empty one is not unreadable, it is unset, and falls back to
# the documented default of 09:00. Stated because the two look alike in
# a settings form and behave differently on purpose.
checks["an empty hour falls back to the default, it does not stop"] = (
    weekly_report.should_send_now(_cfg(weekly_report_hour=None),
                                  now=_at(MON, 9)) is True
    and weekly_report.should_send_now(_cfg(weekly_report_hour=None),
                                      now=_at(MON, 8)) is False)

# ── the decision does not write the state ────────────────────────────
# The caller marks the day after a SUCCESSFUL send, so a failed delivery
# leaves it open to retry. If the decision wrote it, one network blip
# would cost the whole week.
_c = _cfg()
weekly_report.should_send_now(_c, now=_at(MON, 9))
checks["deciding to send does not record it as sent"] = (
    not os.path.exists(_c.weekly_report_state_file)
    or "last_sent" not in json.load(
        open(_c.weekly_report_state_file, encoding="utf-8")))
checks["…and asking twice still says yes"] = (
    weekly_report.should_send_now(_c, now=_at(MON, 9)) is True)
# …and `mark_sent` is what closes the day.
weekly_report.mark_sent(_c, now=_at(MON, 9))
checks["…until the caller says it went out"] = (
    weekly_report.should_send_now(_c, now=_at(MON, 10)) is False)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
