#!/usr/bin/env python3
"""Quiet-hours helper.

When the user configures `QUIET_HOURS_START` / `QUIET_HOURS_END` (HH:MM),
auto-notifications (update available, update result, cleanup result, disk
warning) are silently dropped during that window. Manual replies to bot
commands always go through — the user is actively asking, so they get an
answer regardless of clock time.

Drop instead of queue: keeps the implementation simple and avoids stale
notifications piling up. The user explicitly opted into "leave me alone
during these hours", so dropping is the honest interpretation.
"""

from datetime import datetime, time


def _parse_hhmm(value):
    """Parse 'HH:MM' or 'H:MM' into a datetime.time. Returns None on failure
    or when value is empty (= feature disabled)."""
    if not value:
        return None
    try:
        parts = value.strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
        return time(h, m)
    except (ValueError, AttributeError):
        return None


def is_quiet_now(config, now=None):
    """Return True if current local time is inside the configured quiet
    window. Empty start or end → feature off → always returns False.

    Window can wrap midnight (e.g. start=22:00, end=07:00).
    """
    start = _parse_hhmm(getattr(config, "quiet_hours_start", ""))
    end = _parse_hhmm(getattr(config, "quiet_hours_end", ""))
    if start is None or end is None or start == end:
        return False
    current = (now or datetime.now()).time()
    if start < end:
        return start <= current < end
    # Wrap around midnight
    return current >= start or current < end
