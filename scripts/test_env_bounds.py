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
    ("DISK_WARN_PERCENT", "disk_warn_percent", 50, 100, 85, 90),
    ("WEEKLY_REPORT_WEEKDAY", "weekly_report_weekday", 0, 6, 0, 3),
    ("WEEKLY_REPORT_HOUR", "weekly_report_hour", 0, 23, 9, 18),
    ("WEB_PORT", "web_port", 1, 65535, 8080, 9000),
    ("HEALTHCHECK_MAX_STARTING", "healthcheck_max_starting", 30, 3600, 600, 900),
    ("DOCKER_STOP_TIMEOUT", "docker_stop_timeout", 1, 3600, 60, 120),
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

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
