#!/usr/bin/env python3
"""Gotify survives a short outage, and never repeats a refusal (#66).

Gotify already told a dead network apart from a rejection — it just kept
that knowledge to itself and dropped the notification either way. It now
hands the network case to the same retry queue as Discord and the generic
webhook.

The line that matters most here is the 401/403 one: an *application*
token and a *client* token look alike, only the application one may post,
and the server's own message says nothing useful about it. That hint must
survive the change, and it must NOT be held for retry — a client token is
still a client token in fifteen minutes.

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

import notifiers.gotify as gotify_mod          # noqa: E402
import notify_retry                            # noqa: E402
from notifiers.gotify import GotifyNotifier    # noqa: E402

checks = {}
CRASH = "🔁 *sonarr* crashed (exit 137) at 15:34:55 (restart #2)."


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
    """The Gotify server, with a plug that can be pulled."""

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


orig_urlopen = gotify_mod.urllib.request.urlopen
os.environ["GOTIFY_URL"] = "https://gotify.example.com"
os.environ["GOTIFY_TOKEN"] = "AtokenXYZ"
CFG = types.SimpleNamespace(bot_label="")

try:
    g = GotifyNotifier(CFG)
    wire = Wire()
    gotify_mod.urllib.request.urlopen = wire

    # ═══ 1. the push survives the outage ═════════════════════════════
    notify_retry.queue.clear()
    wire.down = urllib.error.URLError(socket.gaierror(-3, "Try again"))
    _, log = quiet(g.send_message, CRASH)
    checks["a push into a dead network is held, not dropped"] = (
        notify_retry.pending() == 1)
    checks["…and says so in a line that can be grepped for"] = (
        "gotify send failed" in log)
    checks["…keeping the error Gotify already logged"] = (
        "Gotify notification failed: <urlopen error [Errno -3] Try again>"
        in log)

    wire.down = None
    before = len(wire.seen)
    (sent, dropped), log = quiet(notify_retry.flush)
    checks["once the server answers again the push goes out"] = (
        (sent, dropped) == (1, 0) and notify_retry.pending() == 0)
    checks["…exactly once"] = len(wire.seen) - before == 1
    checks["…marked as late, so it does not read as fresh"] = (
        wire.seen[-1]["message"].startswith("⏳ Delayed "))
    checks["…carrying the original alert, intact"] = (
        wire.seen[-1]["message"].endswith(CRASH))

    # ═══ 2. the token hint survives, and is not repeated ═════════════
    # An application token and a client token look alike and only the
    # first may post. Gotify's own 401 says nothing about that, so this
    # line is the whole explanation the user gets — and repeating the
    # request for fifteen minutes would not turn one token into the other.
    for code in (401, 403):
        notify_retry.queue.clear()
        gotify_mod.urllib.request.urlopen = refuses(code)
        _, log = quiet(g.send_message, CRASH)
        checks[f"a {code} is NOT held for retry"] = notify_retry.pending() == 0
        checks[f"…and still names the application token ({code})"] = (
            "Gotify rejected the token — GOTIFY_TOKEN must be an "
            "APPLICATION token (Apps tab), not a client token" in log)

    for code in (400, 404, 500):
        notify_retry.queue.clear()
        gotify_mod.urllib.request.urlopen = refuses(code, b"body text")
        _, log = quiet(g.send_message, CRASH)
        checks[f"a {code} is NOT held for retry"] = notify_retry.pending() == 0
        checks[f"…and still logs the status and body ({code})"] = (
            f"Gotify notification failed: HTTP {code} body text" in log)

    # ═══ 3. every shape of network failure counts as one ═════════════
    for label, err in (("a DNS failure", urllib.error.URLError("Try again")),
                       ("a timeout", socket.timeout("timed out")),
                       ("a TimeoutError", TimeoutError("timed out")),
                       ("a refused connection",
                        ConnectionRefusedError("connection refused"))):
        notify_retry.queue.clear()
        wire.down = err
        gotify_mod.urllib.request.urlopen = wire
        quiet(g.send_message, CRASH)
        checks[f"{label} holds the message"] = notify_retry.pending() == 1
    wire.down = None

    # ═══ 4. a channel nobody configured is silence, not a queue ══════
    notify_retry.queue.clear()
    del os.environ["GOTIFY_TOKEN"]
    _, log = quiet(g.send_message, CRASH)
    checks["a Gotify without a token holds nothing"] = (
        notify_retry.pending() == 0 and "send failed" not in log)
    os.environ["GOTIFY_TOKEN"] = "AtokenXYZ"

finally:
    gotify_mod.urllib.request.urlopen = orig_urlopen
    os.environ.pop("GOTIFY_URL", None)
    os.environ.pop("GOTIFY_TOKEN", None)
    notify_retry.queue.clear()

# ═══ 5. the clause order, asserted on the source ═════════════════════
# HTTPError is a subclass of URLError. If the network clause moves above
# it, the 401 hint above is never reached and every rejection is repeated
# for fifteen minutes — invisible in a test that only raises one of the two.
_src = open(gotify_mod.__file__, encoding="utf-8").read()
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
