#!/usr/bin/env python3
"""Per-container maintenance-window helper.

A container can be configured to only auto-update during a specific
HH:MM time range on selected weekdays. Outside the window, the
scheduler skips that container's auto-update for this tick — the
update stays queued in pending_updates.json and triggers the next
time the cron + window line up.

A container without a window entry → unrestricted (current behaviour).
"""

from datetime import datetime, time


def _parse_hhmm(value):
    if not value:
        return None
    try:
        h, m = value.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
        return time(h, m)
    except (ValueError, AttributeError):
        return None


def is_window_open(window, now=None):
    """window: dict with keys 'start', 'end', 'weekdays' — or None / empty.

    None / empty → no restriction → always True.
    weekdays uses Python's convention (Monday=0, Sunday=6). Empty list of
    weekdays = "all weekdays".

    Time window can wrap midnight (e.g. start=22:00 end=06:00). When wrapping,
    the relevant weekday for the post-midnight half is the day on which the
    window opened — practical effect: a "Saturday 23:00 → Sunday 03:00" slot
    counts as in-window throughout, including 02:00 Sunday.
    """
    if not window:
        return True
    start = _parse_hhmm(window.get("start", ""))
    end = _parse_hhmm(window.get("end", ""))
    if start is None or end is None or start == end:
        return True  # malformed → treat as "no restriction"
    # A weekday list that cannot be read must not quietly become "every
    # day". The filter used to drop whatever it did not understand, and
    # an empty list means no restriction — so `["Sat"]`, `[5.0]` and
    # `["-1"]` all opened the window on all seven days. Measured. That
    # is a restriction the user set on purpose evaporating, in the one
    # direction that matters: it updates a container on a day they said
    # not to.
    #
    # Note this fails the opposite way from the quiet-hours window, and
    # on purpose. There, an unreadable setting must not silence alerts,
    # so it falls back to noisy. Here it gates an ACTION, so it falls
    # back to not acting — the update stays pending and happens on a day
    # that does parse.
    _raw = window.get("weekdays") or []
    weekdays = []
    for d in _raw:
        if isinstance(d, bool):
            continue                      # True is not Tuesday
        if isinstance(d, int):
            weekdays.append(d)
        elif isinstance(d, float) and d.is_integer():
            weekdays.append(int(d))
        elif isinstance(d, str) and d.strip().isdigit():
            weekdays.append(int(d.strip()))
    if _raw and not weekdays:
        return False                      # asked for, unreadable: stay shut

    now = now or datetime.now()
    current_time = now.time()
    current_wd = now.weekday()
    yesterday_wd = (current_wd - 1) % 7

    if start <= end:
        # Same-day window
        if weekdays and current_wd not in weekdays:
            return False
        return start <= current_time < end
    else:
        # Wraps midnight
        if start <= current_time:
            # Late-evening half — uses today's weekday
            return not weekdays or current_wd in weekdays
        if current_time < end:
            # After-midnight half — counts as "yesterday's" weekday entry
            return not weekdays or yesterday_wd in weekdays
        return False
