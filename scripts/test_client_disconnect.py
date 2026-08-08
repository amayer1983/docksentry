#!/usr/bin/env python3
"""A browser hanging up is not an error report.

@NotRetarded opened #58 with thirteen lines of Python internals out of his
container log and the honest summary "not sure what's going on here":

    Exception occurred during processing of request from ('192.168.0.117', 59966)
    Traceback (most recent call last):
      ...
      File "/usr/local/lib/python3.12/http/server.py", line 408, in handle_one_request
        self.raw_requestline = self.rfile.readline(65537)
      File "/usr/local/lib/python3.12/socket.py", line 720, in readinto
        return self._sock.recv_into(b)
    ConnectionResetError: [Errno 104] Connection reset by peer

Nothing in that says "a client on your LAN closed a socket". It says
"Exception", it names files inside the Python standard library, and it
lands in `docker logs` between the things that genuinely are wrong. So it
gets read as a fault in Docksentry, and filed as one.

The reset itself is ordinary and unavoidable: a browser abandons a request
when you navigate away mid-load, closes a tab while a response is being
written, or opens a speculative connection it then decides not to use. The
Settings page makes background requests of its own — the cron preview fires
as you type — and any of those can be cancelled. There is nothing to fix
about the reset. The traceback was the defect.

`socketserver.BaseServer.handle_error` prints that block for ANY exception
escaping a request thread. Suppressing all of them would be worse than the
noise, because anything that is not a client hanging up is a real bug and
has to stay loud. So this asserts the split: the client-gone family goes
quiet (one line, and only under DEBUG), everything else keeps its full
traceback.
"""

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402

#: The address out of #58, so the check is against the case as reported.
PEER = ("192.168.0.117", 59966)


def handle(exc, debug):
    """Run handle_error for `exc` and return everything it printed."""
    srv = web_ui._QuietHTTPServer.__new__(web_ui._QuietHTTPServer)
    srv.debug = debug
    buf = io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf
    try:
        try:
            raise exc
        except Exception:
            srv.handle_error(None, PEER)
    finally:
        sys.stdout, sys.stderr = saved
    return buf.getvalue()


def main():
    checks = {}

    # ── the reported case ────────────────────────────────────────
    reset = ConnectionResetError(104, "Connection reset by peer")
    out = handle(reset, debug=False)
    checks["the reset from #58 prints nothing at all"] = out == ""

    out = handle(reset, debug=True)
    checks["under DEBUG it prints one line, not a traceback"] = (
        out.count("\n") == 1 and "Traceback" not in out)
    checks["…that names the peer"] = "192.168.0.117" in out
    checks["…and says it is harmless"] = "harmless" in out

    # ── the rest of the family ───────────────────────────────────
    # Same cause, different point in the exchange: aborted before the
    # request was read, broken pipe while the response was being written,
    # timeout on a client that stopped talking mid-request.
    for exc in (ConnectionAbortedError(), BrokenPipeError(), TimeoutError()):
        name = type(exc).__name__
        checks[f"{name} is quiet too"] = handle(exc, debug=False) == ""

    # ── and everything else stays loud ───────────────────────────
    # The whole point of not suppressing blindly. A bug in a request
    # handler must still arrive with its traceback.
    out = handle(ValueError("something actually wrong"), debug=False)
    checks["a real exception still prints its traceback"] = "Traceback" in out
    checks["…and names itself"] = "ValueError" in out
    checks["…even though DEBUG is off"] = "something actually wrong" in out

    # An OSError that is NOT a disconnect — a full disk while writing a
    # response, say — is a real fault and must not be swallowed by a
    # too-wide isinstance check.
    out = handle(OSError(28, "No space left on device"), debug=False)
    checks["an unrelated OSError is not mistaken for a disconnect"] = (
        "Traceback" in out)

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
