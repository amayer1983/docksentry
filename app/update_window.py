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
    weekdays = window.get("weekdays") or []
    weekdays = [int(d) for d in weekdays if isinstance(d, (int, str)) and str(d).isdigit()]

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
