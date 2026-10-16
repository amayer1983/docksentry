#!/usr/bin/env python3
"""When the disk warning fires, and when it stays quiet.

`docs/notifications.md:78`: warns above a threshold that defaults to
85, accepts 50 to 100, and says it once per 23 hours rather than on
every scheduler pass (one every five minutes, not every thirty
seconds — the comment here said the latter for a while). On 10.10.2026 a sweep found three reverts here that no
procedure noticed — the threshold comparison, the 23-hour lock, and the
clamp on what the settings page accepts.

`test_disk_guard.py` covers the scheduler's own in-memory throttle, a
second one layered above this; what nothing covered was the decision
this function makes and the state file it keeps.

Both directions matter and they fail in opposite ways. A warning that
does not fire is a disk that fills up silently, which is how a host
stops updating anything at all. One that fires on every tick is a
message every thirty seconds, which people mute — and then the first
one does not help either.
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

checks = {}


def _checker(percent, threshold=None, last_warn=None):
    """A checker looking at a disk `percent` full."""
    d = tempfile.mkdtemp(prefix="diskwarn-")
    state = os.path.join(d, "disk_warn_state.json")
    if last_warn is not None:
        with open(state, "w", encoding="utf-8") as f:
            json.dump({"last_warn": last_warn.strftime("%Y-%m-%d %H:%M:%S"),
                       "bucket": threshold or 85}, f)
    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(
        data_dir=d, disk_warn_state_file=state,
        disk_warn_percent=threshold if threshold is not None else 85)
    c._debug = lambda *a, **kw: None
    # Only the measurement is faked; the decision is the real one.
    c.get_disk_usage = lambda: (percent, 10 * 1024**3, 100 * 1024**3)
    return c


def _action(percent, threshold=None, last_warn=None):
    return _checker(percent, threshold, last_warn).check_disk_usage()[0]


# ── the threshold ────────────────────────────────────────────────────
checks["below the threshold it says nothing"] = _action(80) == "ok"
checks["…right up to the last percent below it"] = _action(84.9) == "ok"
checks["at the threshold it warns"] = _action(85) == "warn"
checks["…and above it"] = _action(97) == "warn"

# A configured threshold is the one used, or the setting is decoration.
checks["a lower threshold warns earlier"] = (
    _action(55, threshold=50) == "warn" and _action(45, threshold=50) == "ok")
checks["…and a higher one later"] = (
    _action(90, threshold=95) == "ok" and _action(96, threshold=95) == "warn")

# ── once a day, not once a tick ──────────────────────────────────────
# The scheduler asks every five minutes (`scheduler.py`, default 300 —
# the outer loop's own 30-second sleep is not the cadence of this
# check, which is what the comment here used to say). Without the
# lock the operator gets a full-disk warning every five minutes, mutes
# the channel, and the next real alert goes with it.
_recent = datetime.now() - timedelta(hours=2)
checks["having warned two hours ago, it stays quiet"] = (
    _action(97, last_warn=_recent) == "silent")
checks["…and still quiet at twenty-two hours"] = (
    _action(97, last_warn=datetime.now() - timedelta(hours=22)) == "silent")
# 23, which is the documented figure (`docs/notifications.md:78`), not
# 24 — the check was named "after twenty-three" and tested 24, so
# changing the code to 24 left it green and the documented window was
# pinned by nothing. A few minutes past the boundary, because exactly
# 23 h is the one moment the comparison is allowed to go either way.
checks["…but warns again after twenty-three"] = (
    _action(97, last_warn=datetime.now()
            - timedelta(hours=23, minutes=5)) == "warn")
checks["…and not five minutes before that"] = (
    _action(97, last_warn=datetime.now()
            - timedelta(hours=22, minutes=55)) == "silent")

# "silent" is not "ok": the caller has to be able to tell "nothing is
# wrong" from "something is wrong and I have already said so".
checks["staying quiet is not the same answer as being fine"] = (
    _action(97, last_warn=_recent) != _action(50))

# A state file that cannot be read must not silence the warning —
# failing towards quiet is how a full disk goes unnoticed.
_c = _checker(97)
with open(_c.config.disk_warn_state_file, "w", encoding="utf-8") as _f:
    _f.write("not json at all")
checks["an unreadable state file does not silence it"] = (
    _c.check_disk_usage()[0] == "warn")

# ── and the warning carries the numbers ──────────────────────────────
_action_, _pct, _free_gb = _checker(93.5).check_disk_usage()
checks["the warning says how full the disk is"] = _pct == 93.5
checks["…and how much is left, in gigabytes"] = round(_free_gb) == 10

# ── what the settings page will accept ───────────────────────────────
# Below 50 would warn on a healthy disk; above 100 can never fire. Both
# turn the feature off in a way that looks like it is on.
import web_ui  # noqa: E402


def _save(value):
    """POST the settings form and read back what was stored.

    Driven, not grepped. The first version of this block had
    `"max(50, min(v, 100))" in src` next to a local copy of the same
    arithmetic — a check on a copy, which is the thing this suite keeps
    finding in itself.
    """
    class _Cfg:
        _CALLS = ("env_override", "save", "load", "apply", "write", "set_")
        disk_warn_percent = 85
        language = "en"

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            if name.startswith(self._CALLS):
                return lambda *a, **kw: None
            return ""

    cfg = _Cfg()
    cls = web_ui.create_handler(cfg, checker=None,
                                bot=types.SimpleNamespace(t=None), store=None,
                                restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    body = f"settings_page=1&disk_warn_percent={value}"
    h.path = "/settings"
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x"}
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode())
    h.client_address = ("127.0.0.1", 0)
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    h._send_redirect = lambda loc: None
    h._page_settings = lambda: None
    h._page_connections = lambda: None
    try:
        h.do_POST()
    except Exception:                                       # noqa: BLE001
        pass
    return cfg.disk_warn_percent


checks["a threshold inside the range is stored as given"] = (
    _save(70) == 70 and _save(50) == 50 and _save(100) == 100)
# Below 50 would warn on a healthy disk; above 100 can never fire. Both
# switch the feature off in a way that looks like it is on.
checks["…one below the range is raised to 50"] = _save(5) == 50
checks["…one above it is lowered to 100"] = _save(900) == 100
checks["…and something that is not a number leaves it alone"] = (
    _save("abc") == 85)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
