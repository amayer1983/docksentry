#!/usr/bin/env python3
"""Maintenance-Mode helper.

When maintenance is active, the scheduler skips its cron-update tick and
auto-notifications are suppressed. Manual command replies (Telegram /status,
Web UI button clicks) keep working.

State is a tiny JSON file:
    {"until": "2026-05-06T22:30:00"}     # ends at given local datetime
    {"until": "forever"}                 # ends only when explicitly off
    {}                                   # not in maintenance

The state file is the source of truth. We never trust an in-memory cache —
the user can flip maintenance via Telegram, Web UI, or by editing the file
directly, and all three should converge to the same view.
"""

import json
import os
from datetime import datetime, timedelta


def _read(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except OSError as e:
        print(f"Failed to write maintenance state: {e}")


def is_active(config, now=None):
    """True if maintenance is currently active (and hasn't expired)."""
    state = _read(config.maintenance_file)
    until = state.get("until")
    if not until:
        return False
    if until == "forever":
        return True
    try:
        end = datetime.fromisoformat(until)
    except (ValueError, TypeError):
        return False
    if (now or datetime.now()) >= end:
        # Expired — clean up the state file silently
        _write(config.maintenance_file, {})
        return False
    return True


def get_state(config):
    """Return the raw state dict — used by the UI to show the banner.

    Returns:
        {"active": True,  "until": datetime|None, "until_iso": str}  or
        {"active": False}
    """
    state = _read(config.maintenance_file)
    until_str = state.get("until")
    if not until_str:
        return {"active": False}
    if until_str == "forever":
        return {"active": True, "until": None, "until_iso": "forever"}
    try:
        end = datetime.fromisoformat(until_str)
    except (ValueError, TypeError):
        return {"active": False}
    if datetime.now() >= end:
        _write(config.maintenance_file, {})
        return {"active": False}
    return {"active": True, "until": end, "until_iso": until_str}


def enable(config, hours=None):
    """Activate maintenance for `hours` (None = forever)."""
    if hours is None:
        _write(config.maintenance_file, {"until": "forever"})
        return None
    until = datetime.now() + timedelta(hours=float(hours))
    _write(config.maintenance_file, {"until": until.isoformat(timespec="seconds")})
    return until


def disable(config):
    _write(config.maintenance_file, {})


def parse_duration(text):
    """Parse strings like '2h', '30m', '1d', 'forever', 'off' into hours.

    Returns:
        float (hours) for finite durations
        None for 'forever'
        False for 'off' / 'disable'
        Raises ValueError on unparseable input.
    """
    s = (text or "").strip().lower()
    if s in ("off", "disable", "stop", "end", "0"):
        return False
    if s in ("forever", "indefinite", "until-stopped"):
        return None
    # Suffix-based: 30m, 2h, 1d
    if s.endswith("m"):
        return float(s[:-1]) / 60.0
    if s.endswith("h"):
        return float(s[:-1])
    if s.endswith("d"):
        return float(s[:-1]) * 24.0
    # Bare number = hours
    return float(s)


def format_remaining(state):
    """Human-readable 'X min remaining' / 'X hours remaining'. State must
    be the dict from get_state() with active=True."""
    if state.get("until_iso") == "forever":
        return "until disabled"
    end = state.get("until")
    if end is None:
        return ""
    delta = end - datetime.now()
    secs = int(delta.total_seconds())
    if secs <= 0:
        return ""
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"
