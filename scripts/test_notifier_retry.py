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

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
