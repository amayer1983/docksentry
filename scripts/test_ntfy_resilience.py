#!/usr/bin/env python3
"""ntfy survives a short outage, and never repeats a refusal (#66).

ntfy was the one channel that did not tell the two apart at all: a single
`except Exception` logged everything the same way, so a dead network and a
401 on a reserved topic were the same event to it, and neither was ever
tried again. It now hangs on the same retry queue as Discord and the
generic webhook.

What is asserted is the intent, not the plumbing: a push that hit a dead
network is held and delivered once the network is back, a push the server
*refused* is not held (it would be refused again), the existing log lines
still say what they said, and the HTTPError clause stays ahead of the
network one — HTTPError is a subclass of URLError, so the wrong order
turns every 4xx into fifteen minutes of retries.

The transport is faked at the `urlopen` seam: no network, no sockets.
"""

import io
import os
import socket
import sys
import types
import urllib.error
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import notifiers.ntfy as ntfy_mod              # noqa: E402
import notify_retry                            # noqa: E402
from notifiers.ntfy import NtfyNotifier        # noqa: E402

checks = {}
CRASH = "🔁 *sonarr* crashed (exit 137) at 15:34:55 (restart #2)."


class Resp:
    """A urlopen() result. A real class: Python looks up __enter__ on the
    type, not on the instance."""

    status = 200

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Wire:
    """The ntfy topic, with a plug that can be pulled.

    `down` is the exception the next call raises, or None for a normal
    answer. Every request body is recorded, so both order and count can
    be asserted.
    """

    def __init__(self):
        self.down = None
        self.seen = []

    def __call__(self, req, timeout=None):
        self.seen.append(req.data.decode() if req.data else "")
        if self.down is not None:
            raise self.down
        return Resp()


def quiet(fn, *a, **kw):
    """Run `fn`, return `(result, captured_stdout)`."""
    out = io.StringIO()
    with redirect_stdout(out):
        r = fn(*a, **kw)
    return r, out.getvalue()


def refuses(code, body=b"nope"):
    """A server that answers, and says no."""
    def f(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "no", {},
                                     io.BytesIO(body))
    return f


orig_urlopen = ntfy_mod.urllib.request.urlopen
os.environ["NTFY_URL"] = "https://ntfy.example/docksentry"
CFG = types.SimpleNamespace(bot_label="")

try:
    n = NtfyNotifier(CFG)
    wire = Wire()
    ntfy_mod.urllib.request.urlopen = wire

    # ═══ 1. the push survives the outage ═════════════════════════════
    notify_retry.queue.clear()
    wire.down = urllib.error.URLError(socket.gaierror(-3, "Try again"))
    _, log = quiet(n.send_message, CRASH)
    checks["a push into a dead network is held, not dropped"] = (
        notify_retry.pending() == 1)
    checks["…and says so in a line that can be grepped for"] = (
        "ntfy send failed" in log)
    checks["…keeping the error ntfy already logged"] = (
        "ntfy error: <urlopen error [Errno -3] Try again>" in log)

    wire.down = None
    before = len(wire.seen)
    (sent, dropped), log = quiet(notify_retry.flush)
    checks["once the topic answers again the push goes out"] = (
        (sent, dropped) == (1, 0) and notify_retry.pending() == 0)
    checks["…exactly once"] = len(wire.seen) - before == 1
    checks["…marked as late, so it does not read as fresh"] = (
        wire.seen[-1].startswith("⏳ Delayed "))
    checks["…carrying the original alert, intact"] = (
        wire.seen[-1].endswith("crashed (exit 137) at 15:34:55 (restart #2)."))

    # ═══ 2. a refusal is not repeated ════════════════════════════════
    # A reserved topic with the wrong token answers 401 — and will answer
    # 401 again in fifteen minutes. Holding it just repeats it.
    for code in (400, 401, 403, 404, 413):
        notify_retry.queue.clear()
        ntfy_mod.urllib.request.urlopen = refuses(code)
        _, log = quiet(n.send_message, CRASH)
        checks[f"a {code} from ntfy is NOT held for retry"] = (
            notify_retry.pending() == 0)
        checks[f"…and still logs `ntfy error: HTTP {code}`"] = (
            f"ntfy error: HTTP {code}" in log)

    # ═══ 3. every shape of network failure counts as one ═════════════
    for label, err in (("a DNS failure", urllib.error.URLError("Try again")),
                       ("a timeout", socket.timeout("timed out")),
                       ("a TimeoutError", TimeoutError("timed out")),
                       ("a reset connection",
                        ConnectionResetError("reset by peer"))):
        notify_retry.queue.clear()
        wire.down = err
        ntfy_mod.urllib.request.urlopen = wire
        quiet(n.send_message, CRASH)
        checks[f"{label} holds the message"] = notify_retry.pending() == 1
    wire.down = None

    # ═══ 4. what is ours is not the network's ════════════════════════
    # A title that will not encode is a bug here, not an outage; it comes
    # back identical on every retry.
    notify_retry.queue.clear()

    def broken(req, timeout=None):
        raise UnicodeEncodeError("latin-1", "x", 0, 1, "not latin-1")

    ntfy_mod.urllib.request.urlopen = broken
    _, log = quiet(n.send_message, CRASH)
    checks["a failure of our own making is not held"] = (
        notify_retry.pending() == 0)
    checks["…but it is still logged"] = "ntfy error:" in log

    # ═══ 5. a channel nobody configured is silence, not a queue ══════
    notify_retry.queue.clear()
    del os.environ["NTFY_URL"]
    _, log = quiet(n.send_message, CRASH)
    checks["an unconfigured ntfy holds nothing"] = (
        notify_retry.pending() == 0 and "send failed" not in log)
    os.environ["NTFY_URL"] = "https://ntfy.example/docksentry"

finally:
    ntfy_mod.urllib.request.urlopen = orig_urlopen
    os.environ.pop("NTFY_URL", None)
    notify_retry.queue.clear()

# ═══ 6. the clause order, asserted on the source ═════════════════════
# HTTPError is a subclass of URLError. If the network clause moves above
# it, every 4xx is read as an outage and the queue repeats it for fifteen
# minutes — which is exactly the bug this file exists to prevent, and it
# is invisible in a passing test that only ever raises one of the two.
_src = open(ntfy_mod.__file__, encoding="utf-8").read()
checks["the HTTPError clause comes before the network one"] = (
    _src.index("except urllib.error.HTTPError")
    < _src.index("except (urllib.error.URLError"))
checks["…and the network clause is the only one that signals"] = (
    _src.count("on_network_failure()") == 1
    and _src.index("except (urllib.error.URLError")
    < _src.index("on_network_failure()")
    < _src.index("except Exception"))

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
