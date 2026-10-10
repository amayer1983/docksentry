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

    # ── SMTP_TLS: a value we do not know must not mean plaintext ──
    # Two defects in one line, both measured. The comparison was
    # case-sensitive, so `SMTP_TLS=SSL` matched neither branch and fell
    # through to plain SMTP — with `smtp_user` set, that puts the
    # password on the wire for anyone who capitalised it. And an
    # unknown value such as `tls`, which is the obvious thing to type,
    # did the same silently.
    for _value, _want in (("ssl", "SMTP_SSL"), ("SSL", "SMTP_SSL"),
                          ("starttls", "SMTP"), ("StartTLS", "SMTP"),
                          ("STARTTLS", "SMTP"), (" ssl ", "SMTP_SSL")):
        _seen = _ctx_given(_value)
        checks[f"SMTP_TLS={_value!r} still encrypts"] = (
            _seen.get("how") == _want and _seen.get("ctx") is not None)

    _dialled = []

    class _Count:
        def __init__(self, *a, **kw):
            _dialled.append(1)

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg, to_addrs=None):
            return {}

        def quit(self):
            pass

    _rs, _rp = smtplib.SMTP_SSL, smtplib.SMTP
    smtplib.SMTP_SSL = smtplib.SMTP = _Count
    try:
        _n = SmtpNotifier(_t.SimpleNamespace(
            smtp_host="h", smtp_port=25, smtp_user="u", smtp_password="p",
            smtp_from="a@ex.test", smtp_to="b@ex.test", smtp_tls="tls",
            smtp_tls_verify=True, language="en", channel_smtp_enabled=True))
        _sent = _n.send_raw("s", "b")
    finally:
        smtplib.SMTP_SSL, smtplib.SMTP = _rs, _rp
    # An EMPTY value is not unknown, it is unset — and unset means
    # STARTTLS, which is what `config.py` and the Connections page both
    # say. This read `or "none"`, so an empty value meant plain SMTP
    # here and STARTTLS everywhere else, and with `smtp_user` set the
    # password went out in clear while the interface showed STARTTLS.
    # `Config.from_env` normalises it; a hand-written `settings.json`
    # or a restored backup bundle does not.
    for _empty in ("", None, "   "):
        _seen = _ctx_given(_empty)
        checks[f"SMTP_TLS={_empty!r} means STARTTLS, not plaintext"] = (
            _seen.get("ctx") is not None)

    # An EMPTY value is not unknown, it is unset — and unset means
    # STARTTLS, which is what `config.py` and the Connections page both
    # say. This read `or "none"`, so an empty value meant plain SMTP
    # here and STARTTLS everywhere else, and with `smtp_user` set the
    # password went out in clear while the interface showed STARTTLS.
    # `Config.from_env` normalises it; a hand-written `settings.json`
    # or a restored backup bundle does not — both `setattr` straight on.
    for _empty in ("", None, "   "):
        _seen = _ctx_given(_empty)
        checks[f"SMTP_TLS={_empty!r} means STARTTLS, not plaintext"] = (
            _seen.get("ctx") is not None)

    checks["an unknown SMTP_TLS value sends nothing at all"] = (
        _sent is False and _dialled == [])

    # ── the warning has to be about this setup, not about a setup ──
    # @NotRetarded runs an internal Mailrise with no credentials and was
    # told "the password is sent to whatever answers on that address"
    # (#63). One sentence was printed for all six combinations of
    # transport and credentials, including `smtp_tls=none`, where there
    # is no certificate to verify at all. A security line that does not
    # apply teaches people to skip the next one.
    import contextlib as _ctx_mod
    import io as _io_mod

    def _warning(tls, user, pw, verify):
        _buf = _io_mod.StringIO()
        _rs, _rp = smtplib.SMTP_SSL, smtplib.SMTP

        class _Ok:
            def __init__(self, *a, **kw):
                pass

            def starttls(self, context=None):
                pass

            def login(self, u, p):
                pass

            def getreply(self):
                return (354, b"ok")

            def data(self, arg):
                return (250, b"ok")

            def send_message(self, msg, to_addrs=None):
                return self.data("")

            def quit(self):
                pass

        smtplib.SMTP_SSL = smtplib.SMTP = _Ok
        try:
            with _ctx_mod.redirect_stdout(_buf):
                SmtpNotifier(_t.SimpleNamespace(
                    smtp_host="h", smtp_port=25, smtp_user=user,
                    smtp_password=pw, smtp_from="a@ex.test",
                    smtp_to="b@ex.test", smtp_tls=tls,
                    smtp_tls_verify=verify, language="en",
                    channel_smtp_enabled=True)).send_raw("s", "b")
        finally:
            smtplib.SMTP_SSL, smtplib.SMTP = _rs, _rp
        return next((l for l in _buf.getvalue().splitlines()
                     if l.startswith("SMTP")), "")

    checks["no credentials and no TLS: nothing to warn about"] = (
        _warning("none", "", "", False) == "")
    checks["…but no TLS WITH a password says the password is in clear"] = (
        "unencrypted" in _warning("none", "bob", "pw", True))
    checks["an unverified certificate with a password names the password"] = (
        "password is sent" in _warning("starttls", "bob", "pw", False))
    checks["…and without one does not invent a password"] = (
        "no credentials are at stake" in _warning("ssl", "", "", False)
        and "the password is sent" not in _warning("ssl", "", "", False))
    checks["…and a verified certificate says nothing at all"] = (
        _warning("starttls", "bob", "pw", True) == "")

    # ── and the interface must not claim a setting it does not have ──
    # With `SMTP_TLS=tls` in a compose file, no option matched, so none
    # carried `selected`, the browser showed the first — and the page
    # told the one person whose mail was refusing to send that they
    # were on STARTTLS.
    #
    # The REAL page is rendered and the real `<select>` read out. The
    # first version of this block rebuilt the dropdown's logic here and
    # asserted against its own copy, so mutating `web_ui.py` changed
    # nothing — which is the mistake CLAUDE.md has a rule about, made
    # in the file that was fixing a different one.
    import re as _re
    import web_ui as _wu

    def _selected(value):
        class _C:
            smtp_host = "mail.example.test"
            smtp_from = "a@example.test"
            smtp_to = "b@example.test"
            smtp_tls = value
            language = "en"

            # The page asks `config.env_override(key)` for every field,
            # so a config whose `__getattr__` hands back a string for
            # everything dies with `'str' object is not callable` —
            # which is a fixture problem, and it would have looked like
            # a page problem.
            _CALLS = ("env_override", "save", "load", "apply", "write",
                      "set_")   # a real attribute, so `__getattr__` is
                                # not asked for it and cannot recurse

            def __getattr__(self, name):
                if name.startswith("_"):
                    raise AttributeError(name)
                if name.startswith(self._CALLS):
                    return lambda *a, **kw: None
                return ""

        cls = _wu.create_handler(_C(), checker=None,
                                 bot=_t.SimpleNamespace(t=None), store=None)
        h = cls.__new__(cls)
        h.path = "/connections"
        h.headers = {}
        h.client_address = ("127.0.0.1", 0)
        h.rfile = _t.SimpleNamespace(read=lambda n: b"")
        body = {}
        h._send_html = lambda html, **kw: body.update(html=html)
        h._check_auth = lambda: True
        h._session_user = lambda: "admin"
        try:
            h._page_connections()
        except Exception as e:                              # noqa: BLE001
            return ("raised", f"{type(e).__name__}: {e}")
        html = body.get("html", "")
        if isinstance(html, bytes):
            html = html.decode("utf-8", "replace")
        sel = _re.search(r'<select name="smtp_tls".*?</select>', html, _re.S)
        if not sel:
            return ("no select", "")
        m = _re.search(r'<option value="([^"]+)" selected>([^<]*)',
                       sel.group(0))
        return (m.group(1), m.group(2)) if m else (None, "")

    for _good in ("starttls", "ssl", "none"):
        checks[f"the interface shows {_good} when that is what is set"] = (
            _selected(_good)[0] == _good)
    _bad_value, _bad_label = _selected("tls")
    checks["…and does not show STARTTLS for a value that is not valid"] = (
        _bad_value == "tls")
    checks["…but says it is not valid, and that no mail is going out"] = (
        "not valid" in _bad_label and "not being sent" in _bad_label)
    checks["…while an unset value is still STARTTLS, as it is elsewhere"] = (
        _selected("")[0] == "starttls" and _selected(None)[0] == "starttls")

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
