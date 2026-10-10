#!/usr/bin/env python3
""""Send test" answers about now, and leaves nothing behind.

@NotRetarded paused Mailrise, pressed the button, and was told
`Failed: SMTP error: Connection unexpectedly closed: timed out`. Then
he started Mailrise again and the test message arrived (#66).

Both halves of that are wrong in their own way. The message was not
lost, so "Failed" on its own is not what happened — and a test is a
question about *now*, so it must not leave work in the retry queue that
turns up minutes later as a surprise. The button runs the real send
path on purpose, which is the whole value of it; what it must not do is
have side effects that outlive the answer.

So: whatever the test queued is taken back out, and the toast says what
a real alert would have done instead of calling it a flat failure.

Driven through the real endpoint with a real `Notifier` and a real
retry queue. The transport is the only thing replaced.
"""
import io
import json
import os
import smtplib
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app",
                                "notifiers"))

import notify_retry  # noqa: E402
import web_ui  # noqa: E402

checks = {}


class _Cfg:
    """Enough for the SMTP channel to be complete, and nothing else."""

    _CALLS = ("env_override", "save", "load", "apply", "write", "set_")

    def __init__(self):
        self.smtp_host = "mail.example.test"
        self.smtp_port = 25
        self.smtp_user = ""
        self.smtp_password = ""
        self.smtp_from = "ds@example.test"
        self.smtp_to = "ops@example.test"
        self.smtp_tls = "none"
        self.smtp_tls_verify = True
        self.language = "en"
        self.channel_smtp_enabled = True

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name.startswith(self._CALLS):
            return lambda *a, **kw: None
        return ""


def _press(dead):
    """Press "send test" for e-mail. Returns (response, queue length)."""
    q = notify_retry.RetryQueue(clock=lambda: 0.0, wall=lambda: 0.0)
    real_queue, notify_retry.queue = notify_retry.queue, q
    real_smtp = smtplib.SMTP

    class _T:
        def __init__(self, *a, **kw):
            if dead:
                raise smtplib.SMTPServerDisconnected(
                    "Connection unexpectedly closed: timed out")

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

    smtplib.SMTP = _T
    cls = web_ui.create_handler(_Cfg(), checker=None,
                                bot=types.SimpleNamespace(t=None), store=None)
    h = cls.__new__(cls)
    body = "name=smtp"
    h.path = "/api/test_channel"
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x"}
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode())
    h.client_address = ("127.0.0.1", 0)
    h.wfile = io.BytesIO()
    out = {}
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    h.send_response = lambda code: out.setdefault("code", code)
    h.send_error = lambda code, msg="": out.setdefault("code", code)
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    try:
        h.do_POST()
    except Exception as e:                                  # noqa: BLE001
        out["raised"] = f"{type(e).__name__}: {e}"
    finally:
        smtplib.SMTP = real_smtp
        left = q.pending()
        notify_retry.queue = real_queue
    raw = h.wfile.getvalue().decode("utf-8", "replace")
    try:
        out["body"] = json.loads(raw) if raw.strip() else {}
    except ValueError:
        out["body"] = {"raw": raw[:200]}
    return out, left


# ── the server is up: the test says so, and queues nothing ───────────
# Stated first, because every check below is "…and in the other case
# it does not". Without this they would all pass on a button that
# never sends anything at all.
_ok, _left = _press(dead=False)
checks["with the server up, the test reports success"] = (
    _ok.get("body", {}).get("ok") is True)
checks["…and leaves nothing in the retry queue"] = _left == 0

# ── the server is down ───────────────────────────────────────────────
_bad, _left = _press(dead=True)
checks["with the server down, the test does not report success"] = (
    _bad.get("body", {}).get("ok") is False)
# The half that was missing: it did not vanish, and saying only
# "Failed" sends someone looking for a broken channel.
checks["…and says a real alert would be held and retried"] = (
    "held" in str(_bad.get("body", {}).get("error", "")).lower())
checks["…while still quoting what the server said"] = (
    "timed out" in str(_bad.get("body", {}).get("error", "")).lower())
# And the part that bit @NotRetarded: the test message itself must not
# turn up minutes later.
checks["…and the test message is not left in the queue"] = _left == 0

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
