#!/usr/bin/env python3
"""A typo in an environment variable must not be able to kill the boot.

`Config.from_env()` read every numeric variable with a bare `int()`.
`main()` calls it on its first line with nothing around it, so
`PULL_TIMEOUT=30m` raised `ValueError` out of the constructor, the
container exited with a traceback, and the restart read the same
variable and did it again. Measured on 07.11.2026 across four of the
eleven; all eleven had the same shape.

The second half is quieter and was the same cause. The Settings form
has always clamped these — `max(50, min(v, 100))` and friends — but the
environment went around the form, so `DISK_WARN_PERCENT=200` was
accepted and the warning could never fire. A bound that only one of two
doors enforces is not a bound.

So: the same range on both doors, and `_clamp_int` instead of `int` on
this one. The table below is the contract, driven through the real
`from_env()` — not read back out of the registry that declares it,
which is what the first version of this check did and why deleting the
line that reads `PULL_TIMEOUT` at all left it green.

Each variable is driven four ways, and the fourth is the one that makes
the other three mean something: a legitimate value has to survive
unchanged. Without it, a `from_env` that ignored the environment and
returned defaults would pass everything above.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import config as config_mod  # noqa: E402

checks = {}

# name, attribute, lo, hi, default, a legitimate in-range value
BOUNDS = [
    ("MONITOR_INTERVAL", "monitor_interval_seconds", 15, 86400, 60, 300),
    ("CLEANUP_GRACE_HOURS", "cleanup_grace_hours", 0, 8760, 24, 48),
    ("CLEANUP_BACKUP_DAYS", "cleanup_backup_days", 1, 365, 7, 30),
    ("DISK_WARN_PERCENT", "disk_warn_percent", 1, 100, 85, 90),
    ("WEEKLY_REPORT_WEEKDAY", "weekly_report_weekday", 0, 6, 0, 3),
    ("WEEKLY_REPORT_HOUR", "weekly_report_hour", 0, 23, 9, 18),
    ("WEB_PORT", "web_port", 1, 65535, 8080, 9000),
    ("HEALTHCHECK_MAX_STARTING", "healthcheck_max_starting", 30, 86400, 600, 900),
    ("DOCKER_STOP_TIMEOUT", "docker_stop_timeout", 1, 86400, 60, 120),
    ("PULL_TIMEOUT", "pull_timeout", 60, 86400, 1800, 7200),
    ("SMTP_PORT", "smtp_port", 1, 65535, 587, 465),
]

# Every name under test is cleared first, so a value in the developer's
# own shell cannot decide the outcome either way.
for name, *_ in BOUNDS:
    os.environ.pop(name, None)
os.environ.setdefault("DATA_DIR", "/tmp/ds-env-bounds")


def _value(name, raw, attr):
    """Build a real Config with `name=raw` and read `attr` back."""
    if raw is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = raw
    try:
        got = getattr(config_mod.Config.from_env(), attr)
    except Exception as e:
        got = e
    finally:
        os.environ.pop(name, None)
    return got


for name, attr, lo, hi, default, good in BOUNDS:
    # 1. Rubbish is not a crash. `int("30m")` is where the boot died.
    got = _value(name, "30m", attr)
    checks[f"{name}: a value that is not a number does not raise"] = (
        not isinstance(got, Exception))
    checks[f"{name}: …and falls back to the documented default"] = (
        got == default)

    # 2. Empty is the same thing — an unset-looking variable that is set.
    got = _value(name, "", attr)
    checks[f"{name}: an empty value falls back too"] = (got == default)

    # 3. Out of range in both directions, which the form already refused.
    checks[f"{name}: below the floor is lifted to it"] = (
        _value(name, str(lo - 1000), attr) == lo)
    checks[f"{name}: above the ceiling is cut to it"] = (
        _value(name, str(hi + 1000), attr) == hi)

    # 4. The counter-direction. Everything above is satisfied by a
    #    `from_env` that ignores the environment entirely.
    checks[f"{name}: a value inside the range is used as given"] = (
        _value(name, str(good), attr) == good)
    # …and the default really is what arrives with nothing set, so
    # check 1 and 2 cannot pass by accident on a wrong default.
    checks[f"{name}: unset means the default"] = (
        _value(name, None, attr) == default)

# ── a clamp that moves a value has to SAY so ────────────────────────
# The ranges arrived to stop a crash: `MONITOR_INTERVAL=30m` raised a
# ValueError during __init__, which is a boot loop. Enforcing policy
# came along for the ride, and it was silent — somebody who had set
# `DOCKER_STOP_TIMEOUT=5400`, which the documentation tells them to do
# for a slow database, got 3600 with nothing anywhere saying why
# (audit, 16.11.2026). Two of those ceilings contradicted our own
# advice and have been raised; the rest now announce themselves.
import contextlib  # noqa: E402
import io as _io  # noqa: E402


def _load_saying(name, value):
    """(config, what it printed) for one environment variable."""
    for _n, *_ in BOUNDS:
        os.environ.pop(_n, None)
    os.environ[name] = str(value)
    buf = _io.StringIO()
    # A raise is an ANSWER here, not an accident: the whole reason the
    # ranges exist is that `int("30m")` threw during __init__ and turned
    # a typo into a boot loop. Letting it propagate would kill this
    # procedure instead, and a procedure that dies prints no verdict —
    # which `test_guard_mutations.py` reads as "the mutation was
    # caught". Both entries that mutate `_clamp_int` back to `int()`
    # scored [unrunnable] until this try was here.
    try:
        with contextlib.redirect_stdout(buf):
            cfg = config_mod.Config.from_env()
    except Exception as exc:                                  # noqa: BLE001
        cfg = None
        buf.write(f"RAISED {type(exc).__name__}")
    finally:
        os.environ.pop(name, None)
    return cfg, buf.getvalue()


for name, attr, lo, hi, default, good in BOUNDS:
    # Out of range at the top: clamped AND named.
    _cfg, _said = _load_saying(name, hi + 1)
    checks[f"{name}: a value above the range is reported, not swallowed"] = (
        _cfg is not None and name in _said and str(hi) in _said)
    # Not a number at all: the original crash, still no crash, still said.
    _cfg, _said = _load_saying(name, "30m")
    checks[f"{name}: …and so is a value that is not a number"] = (
        _cfg is not None and name in _said
        and getattr(_cfg, attr) == default)
    if _cfg is None:
        print(f"      → {name}=30m still takes the start down: {_said}")
    # The counter-direction, and the one that keeps the two above
    # honest: a legitimate value must go through in silence. Without
    # it, printing a warning unconditionally would pass everything.
    _cfg, _said = _load_saying(name, good)
    checks[f"{name}: …while a value inside it passes without a word"] = (
        _cfg is not None and _said.strip() == ""
        and getattr(_cfg, attr) == good)

# ── and the documented range has to be the enforced one ─────────────
# Three ranges changed in this commit. The table in docs/configuration.md
# is where people read them, and nothing tied the two together.
_doc = _io.open(os.path.join(os.path.dirname(__file__), "..", "docs",
                             "configuration.md"), encoding="utf-8").read()
_missing = []
for name, attr, lo, hi, default, good in BOUNDS:
    _row = next((ln for ln in _doc.splitlines()
                 if ln.startswith(f"| `{name}`")), "")
    # Written variously as "(1–100)", "0-23", "1–65535" or "0–8760h",
    # so this asks for both numbers rather than for one spelling.
    if not _row or str(lo) not in _row or str(hi) not in _row:
        _missing.append(name)
checks["every range the code enforces is in the documentation"] = not _missing
if _missing:
    print(f"      → not documented with their bounds: {_missing}")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
