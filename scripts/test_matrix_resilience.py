#!/usr/bin/env python3
"""Matrix survives a short outage — once, not twice (#66).

Two things here, and the second is the older bug.

Matrix already told a dead network apart from a rejection; it just kept
that to itself and dropped the message either way. It now hands the
network case to the same retry queue as Discord and the generic webhook.

And the transaction ID. Matrix deduplicates a send on it, which is what
makes repeating a send safe — the comment in `matrix.py` said exactly
that. But the ID was minted inside the send, once per *attempt*, so it
never deduplicated anything: a PUT that timed out after the homeserver
had accepted it went out again under a fresh ID and posted the message a
second time. The retry queue turns that from a rare race into a routine
one, so the ID now belongs to the message and rides along in the closure
handed to the queue. Asserted below both ways round: two separate alerts
still get two IDs, and one alert retried keeps the one it had.

`ValueError` came out of the network clause at the same time. A homeserver
that answers with something that is not JSON has answered — repeating the
request gets the same rubbish back, so that is a rejection, not an outage.

The transport is faked at the `urlopen` seam: no network, no sockets.
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

import notifiers.matrix as matrix_mod          # noqa: E402
import notify_retry                            # noqa: E402
from notifiers.matrix import MatrixNotifier    # noqa: E402

checks = {}
CRASH = "🔁 *sonarr* crashed (exit 137) at 15:34:55 (restart #2)."


class Resp:
    """A urlopen() result. A real class: Python looks up __enter__ on the
    type, not on the instance."""

    def __init__(self, payload=b"{}"):
        self.payload = payload
        self.status = 200

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Wire:
    """The homeserver, with a plug that can be pulled.

    Records the full URL of every PUT, because that is where the
    transaction ID lives.
    """

    def __init__(self):
        self.down = None
        self.payload = b"{}"
        self.seen = []

    def __call__(self, req, timeout=None):
        self.seen.append({
            "url": req.full_url,
            "body": json.loads(req.data.decode()) if req.data else {}})
        if self.down is not None:
            raise self.down
        return Resp(self.payload)

    def txns(self):
        """The transaction ID of every send, in order."""
        return [u["url"].rsplit("/", 1)[-1] for u in self.seen]


def quiet(fn, *a, **kw):
    """Run `fn`, return `(result, captured_stdout)`."""
    out = io.StringIO()
    with redirect_stdout(out):
        r = fn(*a, **kw)
    return r, out.getvalue()


def refuses(code, body=b"{}"):
    """A homeserver that answers, and says no."""
    def f(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "no", {},
                                     io.BytesIO(body))
    return f


orig_urlopen = matrix_mod.urllib.request.urlopen
os.environ["MATRIX_HOMESERVER"] = "https://matrix.example.com"
os.environ["MATRIX_TOKEN"] = "syt_abc"
os.environ["MATRIX_ROOM"] = "!abc:example.com"
CFG = types.SimpleNamespace(bot_label="")

try:
    m = MatrixNotifier(CFG)
    wire = Wire()
    matrix_mod.urllib.request.urlopen = wire

    # ═══ 1. the message survives the outage ══════════════════════════
    notify_retry.queue.clear()
    wire.down = urllib.error.URLError(socket.gaierror(-3, "Try again"))
    _, log = quiet(m.send_message, CRASH)
    checks["a send into a dead network is held, not dropped"] = (
        notify_retry.pending() == 1)
    checks["…and says so in a line that can be grepped for"] = (
        "matrix send failed" in log)
    checks["…keeping the error Matrix already logged"] = (
        "Matrix notification failed: <urlopen error [Errno -3] Try again>"
        in log)

    first_txn = wire.txns()[-1]
    wire.down = None
    before = len(wire.seen)
    (sent, dropped), log = quiet(notify_retry.flush)
    checks["once the homeserver answers again the message goes out"] = (
        (sent, dropped) == (1, 0) and notify_retry.pending() == 0)
    checks["…exactly once"] = len(wire.seen) - before == 1
    checks["…marked as late, so it does not read as fresh"] = (
        wire.seen[-1]["body"]["body"].startswith("⏳ Delayed "))
    checks["…carrying the original alert, intact"] = (
        wire.seen[-1]["body"]["body"].endswith(CRASH))

    # ═══ 2. the retry is the SAME message, and says so ═══════════════
    # This is the point of the exercise. A PUT that times out after the
    # homeserver accepted it is indistinguishable from one that never
    # arrived — so the resend must carry the ID the first attempt used,
    # and let the homeserver throw the duplicate away. Under the old
    # per-attempt ID the room got the alert twice, the second copy
    # wearing a "Delayed" notice.
    checks["a retry re-uses the first attempt's transaction ID"] = (
        wire.txns()[-1] == first_txn)
    checks["…which is the only thing that can stop a double post"] = (
        first_txn.startswith("docksentry-"))

    # …while two genuinely different alerts must NOT collide, or the
    # homeserver would swallow the second one.
    notify_retry.queue.clear()
    quiet(m.send_message, "first alert")
    quiet(m.send_message, "second alert")
    checks["two separate sends still get two transaction IDs"] = (
        wire.txns()[-1] != wire.txns()[-2])

    # A message held THROUGH several failed flushes keeps one ID the
    # whole way — every attempt is the same message.
    notify_retry.queue.clear()
    wire.down = urllib.error.URLError("down")
    quiet(m.send_message, CRASH)
    held_txn = wire.txns()[-1]
    quiet(notify_retry.flush)
    quiet(notify_retry.flush)
    wire.down = None
    quiet(notify_retry.flush)
    checks["…and one held message keeps its ID across every attempt"] = (
        wire.txns()[-4:] == [held_txn] * 4)
    checks["…so the homeserver sees one message, not four"] = (
        len(set(wire.txns()[-4:])) == 1)

    # ═══ 3. a refusal is not repeated ════════════════════════════════
    notify_retry.queue.clear()
    for code in (401, 403):
        matrix_mod.urllib.request.urlopen = refuses(code)
        _, log = quiet(m.send_message, CRASH)
        checks[f"a {code} is NOT held for retry"] = notify_retry.pending() == 0
        checks[f"…and still names the token and the room ({code})"] = (
            "Matrix rejected the token — check MATRIX_TOKEN and that "
            "the account has joined MATRIX_ROOM" in log)

    for code in (400, 404, 429, 500):
        matrix_mod.urllib.request.urlopen = refuses(code, b"M_FORBIDDEN")
        _, log = quiet(m.send_message, CRASH)
        checks[f"a {code} is NOT held for retry"] = notify_retry.pending() == 0
        checks[f"…and still logs the status and detail ({code})"] = (
            f"Matrix notification failed: HTTP {code} M_FORBIDDEN" in log)

    # ═══ 4. a broken answer is a rejection, not an outage ════════════
    # `ValueError` used to sit in the network clause, so a homeserver (or
    # a reverse proxy in front of it) answering with an HTML error page
    # had the message held and re-sent for fifteen minutes — against a
    # server that was plainly answering.
    notify_retry.queue.clear()
    matrix_mod.urllib.request.urlopen = wire
    wire.payload = b"<html>502 Bad Gateway</html>"
    _, log = quiet(m.send_message, CRASH)
    checks["an answer that is not JSON is NOT held for retry"] = (
        notify_retry.pending() == 0)
    checks["…and it is still logged"] = "Matrix notification failed:" in log
    wire.payload = b"{}"

    # ═══ 5. every shape of network failure counts as one ═════════════
    for label, err in (("a DNS failure", urllib.error.URLError("Try again")),
                       ("a timeout", socket.timeout("timed out")),
                       ("a TimeoutError", TimeoutError("timed out")),
                       ("a refused connection",
                        ConnectionRefusedError("connection refused"))):
        notify_retry.queue.clear()
        wire.down = err
        quiet(m.send_message, CRASH)
        checks[f"{label} holds the message"] = notify_retry.pending() == 1
    wire.down = None

    # ═══ 6. a channel nobody configured is silence, not a queue ══════
    notify_retry.queue.clear()
    del os.environ["MATRIX_ROOM"]
    _, log = quiet(m.send_message, CRASH)
    checks["a Matrix without a room holds nothing"] = (
        notify_retry.pending() == 0 and "send failed" not in log)

finally:
    matrix_mod.urllib.request.urlopen = orig_urlopen
    for _k in ("MATRIX_HOMESERVER", "MATRIX_TOKEN", "MATRIX_ROOM"):
        os.environ.pop(_k, None)
    notify_retry.queue.clear()

# ═══ 7. the clause order, asserted on the source ═════════════════════
# HTTPError is a subclass of URLError. If the network clause moves above
# it, the token hint is never reached and every 403 is repeated for
# fifteen minutes — invisible in a test that raises only one of the two.
_src = open(matrix_mod.__file__, encoding="utf-8").read()
checks["the HTTPError clause comes before the network one"] = (
    _src.index("except urllib.error.HTTPError")
    < _src.index("except (urllib.error.URLError"))
checks["ValueError is out of the network clause"] = (
    "OSError, ValueError" not in _src
    and _src.index("except ValueError")
    < _src.index("except (urllib.error.URLError"))
checks["…and only the network clause signals the queue"] = (
    _src.count("on_network_failure()") == 1
    and _src.index("except (urllib.error.URLError")
    < _src.index("on_network_failure()"))
checks["the transaction ID is minted once per message, not per attempt"] = (
    _src.count("time.time_ns()") == 1
    and _src.index("def _new_txn") < _src.index("def _send")
    and "txn = txn or _new_txn()" in _src)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
