#!/usr/bin/env python3
"""Notifier bounded retry — Discord + Webhook (audit follow-up to v1.38.1).

The Telegram side got a retry in v1.38.1 after NotRetarded lost a
notification right after a self-update restart. Discord and the generic
webhook had the same structural gap (single urlopen with no retry), so they
now use a shared _post_json_with_retry helper: 3 attempts with 2s/4s
backoff on transient network errors, no retry on HTTP status codes (those
are the server's word, not a transient blip).

urllib + time.sleep mocked — no network, no real waits. Non-zero on failure.
"""
import sys, os, io, socket, time, types, urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import notifier as notifier_mod
from notifier import Notifier


def _cfg():
    return types.SimpleNamespace(
        discord_webhook="https://discord.example/hook",
        webhook_url="https://webhook.example/in",
        bot_label="")


class _Resp:
    def __init__(self, status=204): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def main():
    orig_urlopen = notifier_mod.urllib.request.urlopen
    orig_sleep = time.sleep
    notifier_mod.time.sleep = lambda *a, **k: None
    time.sleep = lambda *a, **k: None
    checks = {}
    try:
        n = Notifier(_cfg())

        # 1. transient timeout twice, success on 3rd attempt → returns status
        state = {"n": 0}
        def f1(req, timeout=None):
            state["n"] += 1
            if state["n"] < 3:
                raise socket.timeout("timed out")
            return _Resp(204)
        notifier_mod.urllib.request.urlopen = f1
        r = n._discord_post({"content": "x"})
        checks["discord: retries then succeeds on 3rd attempt"] = r == 204 and state["n"] == 3

        # 2. always timeouts → None after exactly 3 attempts
        state = {"n": 0}
        def f2(req, timeout=None):
            state["n"] += 1
            raise socket.timeout("timed out")
        notifier_mod.urllib.request.urlopen = f2
        r = n._discord_post({"content": "x"})
        checks["discord: gives up after 3 -> None"] = r is None and state["n"] == 3

        # 3. same for webhook
        state = {"n": 0}
        notifier_mod.urllib.request.urlopen = f2
        r = n._webhook_send("update_result", {"container": "x"})
        checks["webhook: gives up after 3 -> None"] = r is None and state["n"] == 3

        # 4. HTTP 4xx (bad request from server) → NOT retried
        state = {"n": 0}
        def f4(req, timeout=None):
            state["n"] += 1
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b""))
        notifier_mod.urllib.request.urlopen = f4
        r = n._discord_post({"content": "x"})
        checks["discord: HTTP 4xx not retried (server rejection)"] = r is None and state["n"] == 1

        state = {"n": 0}
        notifier_mod.urllib.request.urlopen = f4
        r = n._webhook_send("update_result", {"container": "x"})
        checks["webhook: HTTP 4xx not retried"] = r is None and state["n"] == 1

        # 5. first attempt already succeeds → NO retry, NO sleep
        state = {"n": 0}
        def f5(req, timeout=None):
            state["n"] += 1
            return _Resp(200)
        notifier_mod.urllib.request.urlopen = f5
        r = n._webhook_send("x", {"a": 1})
        checks["webhook: success on first attempt (no retry)"] = r == 200 and state["n"] == 1
    finally:
        notifier_mod.urllib.request.urlopen = orig_urlopen
        notifier_mod.time.sleep = orig_sleep
        time.sleep = orig_sleep

    # ── STOP: the three things about it that were only ever asserted ──
    # Each survived a revert with the whole suite green, which is how
    # they came to be written down here instead.
    import notify_retry as _nr

    # 1. Falsy. `flush` asks `if result:` first, so a truthy STOP would
    #    log "delivered a held message" for a mail that never arrived —
    #    the worst line a log can carry.
    checks["STOP is falsy, so nothing reads it as delivered"] = not _nr.STOP
    _q = _nr.RetryQueue(clock=lambda: 0.0, wall=lambda: 0.0)
    _log = []
    import builtins as _b
    _real_print = _b.print
    _b.print = lambda *a, **k: _log.append(" ".join(str(x) for x in a))
    try:
        _q.remember("ch", "alert", lambda t: _nr.STOP)
        _sent, _dropped = _q.flush()
    finally:
        _b.print = _real_print
    checks["…a STOPped message counts as dropped, never as sent"] = (
        (_sent, _dropped) == (0, 1))
    checks["…and the log does not claim it was delivered"] = (
        not any("delivered" in line for line in _log))

    # 2. It blocks its channel for the rest of the pass. A connection
    #    that takes a message and then dies is still a connection, and
    #    dialling nineteen more held alerts into it costs a timeout each.
    _q = _nr.RetryQueue(clock=lambda: 0.0, wall=lambda: 0.0)
    _tried = []
    _q.remember("ch", "first", lambda t: _tried.append(1) or _nr.STOP)
    for _i in range(5):
        _q.remember("ch", f"later {_i}", lambda t: _tried.append(1) or False)
    _q.flush()
    checks["a STOP blocks its channel for the rest of the pass"] = (
        len(_tried) == 1)
    checks["…while the messages behind it are kept, not lost"] = (
        _q.pending() == 5)

    # 3. …but a different channel still gets its turn in the same pass.
    _q = _nr.RetryQueue(clock=lambda: 0.0, wall=lambda: 0.0)
    _seen = []
    _q.remember("one", "a", lambda t: _seen.append("one") or _nr.STOP)
    _q.remember("two", "b", lambda t: _seen.append("two") or True)
    _q.flush()
    checks["…but another channel is not punished for it"] = _seen == ["one", "two"]

    # ── the cap holds on the way back in, too ─────────────────────
    # `remember` trims, but a flush takes the whole batch out and puts
    # the survivors back, and that path did not. The two overlap exactly
    # during an outage, which is the only time either of them does
    # anything: the sending thread hands in new alerts while the
    # scheduler thread is sitting on timeouts. Measured before this:
    # twenty held plus twenty arriving per pass reached 620 entries over
    # the fifteen-minute window, bounded by nothing but age.
    _q = _nr.RetryQueue(max_items=5, clock=lambda: 0.0, wall=lambda: 0.0)

    def _fails_and_another_arrives(_text):
        # What the other thread is doing while this send sits on a
        # socket. Not something a channel may do — it is modelled here
        # because the race is the point.
        _q.remember("other", "an alert that just happened", lambda t: False)
        return False

    for _i in range(5):
        _q.remember("ch", f"held {_i}", _fails_and_another_arrives)
    _b.print = lambda *a, **k: None
    try:
        _q.flush()
    finally:
        _b.print = _real_print
    checks["a flush cannot leave the queue over its cap"] = (
        _q.pending() <= 5)

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
