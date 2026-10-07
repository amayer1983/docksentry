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
    # A check stood here asserting `ssl._create_stdlib_context is
    # ssl._create_unverified_context` — a CPython internal. It stayed
    # green with every line of our SMTP code deleted, and would turn
    # red if Python ever changed that default. What we guard is our
    # own call, which the checks around this one do.
    default_ctx = ssl.create_default_context()
    checks["the context we pass does verify"] = (
        default_ctx.check_hostname and default_ctx.verify_mode == ssl.CERT_REQUIRED)

    # Driven, not grepped. These four used to read the source of
    # `send_raw`, which stopped containing the transport when the body
    # moved to `_attempt` — so they went red for a rename rather than
    # for a defect, and would have gone GREEN for a rename in the other
    # direction. What matters is the context the transport is actually
    # handed, so the transport is replaced and asked.
    import smtplib
    import types as _t
    from notifiers.smtp import SmtpNotifier

    def _ctx_given(tls, verify=True):
        """The ssl context `_attempt` hands this transport, or None."""
        seen = {}

        class _SSL:
            def __init__(self, host, port, timeout=None, context=None):
                seen["ctx"] = context
                seen["how"] = "SMTP_SSL"
                raise smtplib.SMTPServerDisconnected("stop here")

        class _Plain:
            def __init__(self, host, port, timeout=None):
                seen["how"] = "SMTP"

            def starttls(self, context=None):
                seen["ctx"] = context
                raise smtplib.SMTPServerDisconnected("stop here")

            def quit(self):
                pass

        real_ssl, real_plain = smtplib.SMTP_SSL, smtplib.SMTP
        smtplib.SMTP_SSL, smtplib.SMTP = _SSL, _Plain
        try:
            n = SmtpNotifier(_t.SimpleNamespace(
                smtp_host="h", smtp_port=465 if tls == "ssl" else 587,
                smtp_user="", smtp_password="", smtp_from="a@ex.test",
                smtp_to="b@ex.test", smtp_tls=tls, smtp_tls_verify=verify,
                language="en", channel_smtp_enabled=True))
            n.send_raw("s", "b")
        finally:
            smtplib.SMTP_SSL, smtplib.SMTP = real_ssl, real_plain
        return seen

    _implicit = _ctx_given("ssl")
    checks["SMTP_SSL is given a verifying context"] = (
        _implicit.get("how") == "SMTP_SSL"
        and _implicit.get("ctx") is not None
        and _implicit["ctx"].check_hostname
        and _implicit["ctx"].verify_mode == ssl.CERT_REQUIRED)
    _starttls = _ctx_given("starttls")
    checks["starttls is given a verifying context"] = (
        _starttls.get("ctx") is not None
        and _starttls["ctx"].check_hostname
        and _starttls["ctx"].verify_mode == ssl.CERT_REQUIRED)
    # …and the escape hatch really does turn it off, so somebody with a
    # self-signed internal server is not stuck — the point of the
    # setting is that it has to be asked for.
    _off = _ctx_given("starttls", verify=False)
    checks["SMTP_TLS_VERIFY=false is the only way to stop verifying"] = (
        _off.get("ctx") is not None
        and not _off["ctx"].check_hostname
        and _off["ctx"].verify_mode == ssl.CERT_NONE)

    # A new failure for self-signed setups is expected; leaving them to
    # guess why is not. The sentence has to reach stdout, not merely
    # exist in the file.
    import io
    import contextlib
    _buf = io.StringIO()

    class _BadCert:
        def __init__(self, *a, **kw):
            raise ssl.SSLCertVerificationError("self signed certificate")

    _real = smtplib.SMTP_SSL
    smtplib.SMTP_SSL = _BadCert
    try:
        with contextlib.redirect_stdout(_buf):
            SmtpNotifier(_t.SimpleNamespace(
                smtp_host="h", smtp_port=465, smtp_user="", smtp_password="",
                smtp_from="a@ex.test", smtp_to="b@ex.test", smtp_tls="ssl",
                smtp_tls_verify=True, language="en",
                channel_smtp_enabled=True)).send_raw("s", "b")
    finally:
        smtplib.SMTP_SSL = _real
    checks["the escape hatch is named in the error the operator reads"] = (
        "SMTP_TLS_VERIFY=false" in _buf.getvalue())

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
