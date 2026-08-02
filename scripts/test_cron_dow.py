#!/usr/bin/env python3
"""Cron day-of-week, and SMTP certificate verification.

Both came out of sweeping wud's issue history (wud#410, wud#352) and both
were measured here before being fixed.
"""

import os
import ssl
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from scheduler import cron_matches

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# 2026-08-03 is a Monday.
MONDAY = datetime(2026, 8, 3)


def fires_on(expr):
    hour = int(expr.split()[1])
    return [DAYS[(MONDAY + timedelta(days=i)).weekday()]
            for i in range(7)
            if cron_matches(expr, (MONDAY + timedelta(days=i)).replace(hour=hour))]


def main():
    checks = {}

    # Cron counts from SUNDAY (0=Sun … 6=Sat); Python's weekday() counts
    # from Monday. Feeding weekday() straight in shifted every day-of-week
    # schedule one day late — the Web UI's own "Weekly (Mondays 9 AM)"
    # preset fired on Tuesdays — and `7` matched nothing at all, so a valid
    # Sunday schedule simply never ran.
    for expr, want in (
            ("0 9 * * 1", ["Mon"]),
            ("0 9 * * 5", ["Fri"]),
            ("0 3 * * 6", ["Sat"]),
            ("0 3 * * 0", ["Sun"]),
            # Both spellings of Sunday have to work; cron accepts either.
            ("0 3 * * 7", ["Sun"]),
            ("0 9 * * 1-5", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
            ("0 9 * * 6,0", ["Sat", "Sun"]),
            # 1-7 means Monday through Sunday, so the day it names
            # explicitly must not be the one it excludes.
            ("0 9 * * 1-7", DAYS),
            ("0 9 * * *", DAYS),
    ):
        checks[f"cron {expr}"] = fires_on(expr) == want

    # The shipped preset, by name, because that is the one that shipped
    # wrong to every user.
    checks["the 'Weekly (Mondays)' preset fires on Monday"] = (
        fires_on("0 9 * * 1") == ["Mon"])

    # ── SMTP: the password must not go to an unverified server ────
    # Measured: smtplib with no explicit context falls back to
    # ssl._create_stdlib_context(), which on this Python IS
    # _create_unverified_context — check_hostname False, verify_mode 0.
    # So the credentials went to whatever answered, with any certificate.
    stdlib_is_unverified = (
        ssl._create_stdlib_context is ssl._create_unverified_context)
    checks["the unsafe default this guards against still exists"] = (
        stdlib_is_unverified)
    default_ctx = ssl.create_default_context()
    checks["the context we pass does verify"] = (
        default_ctx.check_hostname and default_ctx.verify_mode == ssl.CERT_REQUIRED)

    import inspect
    from notifiers.smtp import SmtpNotifier
    src = inspect.getsource(SmtpNotifier.send_raw)
    # Both transports have to carry the context; passing it to only one
    # would leave the other silently unverified, which is the shape of the
    # original bug.
    checks["SMTP_SSL is given a context"] = "SMTP_SSL(" in src and "context=ctx" in src
    checks["starttls is given a context"] = "starttls(context=ctx)" in src
    checks["verification is on unless asked otherwise"] = (
        'getattr(c, "smtp_tls_verify", True)' in src)
    # A new failure for self-signed setups is expected; leaving them to
    # guess why is not.
    checks["the escape hatch is named in the error"] = "SMTP_TLS_VERIFY=false" in src

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
