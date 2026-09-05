#!/usr/bin/env python3
"""Apprise survives a short outage, and never repeats a refusal (#66).

Apprise already told a dead network apart from a rejection — it just kept
that knowledge to itself and dropped the notification either way. It now
hands the network case to the same retry queue as Discord and the generic
webhook.

The rejection branch has a second job that must survive this: the request
that produced the error carried APPRISE_URLS, and Apprise echoes the URLs
it could not parse straight back — those embed tokens. The status is
logged, the body is not, and the log says why. That is what the branch
below is checked for, with a real-looking token in the environment.

What is asserted is the intent, not the plumbing, and the transport is
faked at the `urlopen` seam: no network, no sockets.
"""

import io
import json
import os
import socket
import sys
import types
import urllib.error
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import notifiers.apprise as apprise_mod        # noqa: E402
import notify_retry                            # noqa: E402
from notifiers.apprise import AppriseNotifier  # noqa: E402

checks = {}
CRASH = "🔁 *sonarr* crashed (exit 137) at 15:34:55 (restart #2)."
SECRET = "pover://user@sup3rs3cr3ttoken"


class Resp:
    """A urlopen() result. A real class: Python looks up __enter__ on the
    type, not on the instance."""

    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Wire:
    """The Apprise API, with a plug that can be pulled."""

    def __init__(self):
        self.down = None
        self.seen = []

    def __call__(self, req, timeout=None):
        self.seen.append(json.loads(req.data.decode()) if req.data else {})
        if self.down is not None:
            raise self.down
        return Resp()


def quiet(fn, *a, **kw):
    """Run `fn`, return `(result, captured_stdout)`."""
    out = io.StringIO()
    with redirect_stdout(out):
        r = fn(*a, **kw)
    return r, out.getvalue()


def refuses(code, body=b"{}"):
    """A server that answers, and says no."""
    def f(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "no", {},
                                     io.BytesIO(body))
    return f


orig_urlopen = apprise_mod.urllib.request.urlopen
os.environ["APPRISE_URL"] = "http://apprise:8000/notify"
CFG = types.SimpleNamespace(bot_label="")

try:
    a = AppriseNotifier(CFG)
    wire = Wire()
    apprise_mod.urllib.request.urlopen = wire

    # ═══ 1. the notification survives the outage ═════════════════════
    # The Apprise container coming back up after the host rebooted is
    # exactly this: connection refused for a while, then fine.
    notify_retry.queue.clear()
    wire.down = ConnectionRefusedError("connection refused")
    _, log = quiet(a.send_message, CRASH)
    checks["a send into a dead network is held, not dropped"] = (
        notify_retry.pending() == 1)
    checks["…and says so in a line that can be grepped for"] = (
        "apprise send failed" in log)
    checks["…keeping the error Apprise already logged"] = (
        "Apprise notification failed: connection refused" in log)

    wire.down = None
    before = len(wire.seen)
    (sent, dropped), log = quiet(notify_retry.flush)
    checks["once the API answers again the notification goes out"] = (
        (sent, dropped) == (1, 0) and notify_retry.pending() == 0)
    checks["…exactly once"] = len(wire.seen) - before == 1
    checks["…marked as late, so it does not read as fresh"] = (
        wire.seen[-1]["body"].startswith("⏳ Delayed "))
    checks["…carrying the original alert, intact"] = (
        wire.seen[-1]["body"].endswith(CRASH))

    # ═══ 2. a refusal is not repeated, and it leaks nothing ══════════
    # Apprise answers 400 to a URL it cannot parse and quotes the URL
    # back. Retrying that for fifteen minutes re-sends the credential and
    # gets the same 400; the branch that withholds the body must stay the
    # one that runs.
    os.environ["APPRISE_URLS"] = SECRET
    for code in (400, 401, 404, 500):
        notify_retry.queue.clear()
        apprise_mod.urllib.request.urlopen = refuses(
            code, json.dumps({"error": f"invalid urls: {SECRET}"}).encode())
        _, log = quiet(a.send_message, CRASH)
        checks[f"a {code} is NOT held for retry"] = notify_retry.pending() == 0
        checks[f"…and the token never reaches the log ({code})"] = (
            "sup3rs3cr3ttoken" not in log and "invalid urls" not in log)
        checks[f"…while the status is still named ({code})"] = (
            f"Apprise notification failed: HTTP {code}" in log
            and "response body withheld: it can echo APPRISE_URLS" in log)
    apprise_mod.urllib.request.urlopen = refuses(400, b"{}")
    _, log = quiet(a.send_message, CRASH)
    checks["a 4xx points at the two settings worth checking"] = (
        "check APPRISE_URL and APPRISE_URLS" in log)

    # A held message must not carry the credential either: what the queue
    # keeps is the alert text, and the URLs are read fresh at send time.
    notify_retry.queue.clear()
    wire.down = urllib.error.URLError(socket.gaierror(-3, "Try again"))
    apprise_mod.urllib.request.urlopen = wire
    quiet(a.send_message, CRASH)
    checks["a held message holds the alert, not the credential"] = (
        SECRET not in json.dumps(
            [i["text"] for i in notify_retry.queue._items]))
    del os.environ["APPRISE_URLS"]
    wire.down = None
    quiet(notify_retry.flush)

    # ═══ 3. every shape of network failure counts as one ═════════════
    for label, err in (("a DNS failure", urllib.error.URLError("Try again")),
                       ("a timeout", socket.timeout("timed out")),
                       ("a TimeoutError", TimeoutError("timed out")),
                       ("a reset connection",
                        ConnectionResetError("reset by peer"))):
        notify_retry.queue.clear()
        wire.down = err
        apprise_mod.urllib.request.urlopen = wire
        quiet(a.send_message, CRASH)
        checks[f"{label} holds the message"] = notify_retry.pending() == 1
    wire.down = None

    # ═══ 4. a channel nobody configured is silence, not a queue ══════
    notify_retry.queue.clear()
    del os.environ["APPRISE_URL"]
    _, log = quiet(a.send_message, CRASH)
    checks["an unconfigured Apprise holds nothing"] = (
        notify_retry.pending() == 0 and "send failed" not in log)

finally:
    apprise_mod.urllib.request.urlopen = orig_urlopen
    os.environ.pop("APPRISE_URL", None)
    os.environ.pop("APPRISE_URLS", None)
    notify_retry.queue.clear()

# ═══ 5. the clause order, asserted on the source ═════════════════════
# HTTPError is a subclass of URLError. If the network clause moves above
# it, the branch that withholds the response body is never reached — so
# the wrong order both repeats every 4xx for fifteen minutes and prints
# the credential it was written to keep out of the log.
_src = open(apprise_mod.__file__, encoding="utf-8").read()
checks["the HTTPError clause comes before the network one"] = (
    _src.index("except urllib.error.HTTPError")
    < _src.index("except (urllib.error.URLError"))
checks["…and only the network clause signals the queue"] = (
    _src.count("on_network_failure()") == 1
    and _src.index("except (urllib.error.URLError")
    < _src.index("on_network_failure()"))

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
