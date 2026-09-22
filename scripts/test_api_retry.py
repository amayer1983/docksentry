#!/usr/bin/env python3
"""Telegram api_call bounded retry on transient network failures (#2,
@NotRetarded).

A single timeout right after a self-update restart used to silently drop a
notification. api_call now retries transient network errors (timeout /
connection) up to 3× for normal calls, but NOT for the long-poll
(quiet_timeout=True), and NOT for HTTP 4xx bodies. urllib + time.sleep are
mocked — no network, no real waits. Exits non-zero on any failure.
"""
import sys, os, io, socket, time, types, urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import telegram_bot
from telegram_bot import TelegramBot


def _bot():
    return types.SimpleNamespace(
        enabled=True,
        config=types.SimpleNamespace(bot_token="t", chat_id="-1"),
        _is_timeout=lambda e: True,
        # The real classifier, not a lambda: what the poll does with a
        # reset connection IS the thing under test below.
        _is_poll_drop=TelegramBot._is_poll_drop,
        # A 4xx body is also where a supergroup migration would be
        # announced; "no migration here" keeps this test on retries.
        # That path has its own: test_silent_rejection.py.
        _note_migration=lambda body: None,
    )


class _Resp:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._p


def main():
    orig_urlopen = telegram_bot.urllib.request.urlopen
    orig_sleep = time.sleep
    time.sleep = lambda *a, **k: None  # api_call uses `import time as _t`
    checks = {}
    try:
        # 1. fail twice, succeed on the 3rd → returns payload, 3 calls
        calls = {"n": 0}
        def f1(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise socket.timeout("timed out")
            return _Resp(b'{"ok": true}')
        telegram_bot.urllib.request.urlopen = f1
        r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
        checks["retries then succeeds (3rd attempt)"] = r == {"ok": True} and calls["n"] == 3

        # 2. normal call, always times out → None after exactly 3 attempts
        calls = {"n": 0}
        def f2(req, timeout=None):
            calls["n"] += 1
            raise socket.timeout("timed out")
        telegram_bot.urllib.request.urlopen = f2
        r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
        checks["gives up after 3 attempts -> None"] = r is None and calls["n"] == 3

        # 3. long-poll (quiet_timeout) → NO retry, single attempt
        calls = {"n": 0}
        telegram_bot.urllib.request.urlopen = f2
        r = TelegramBot.api_call(_bot(), "getUpdates", {"x": "y"}, quiet_timeout=True)
        checks["long-poll not retried (1 attempt)"] = r is None and calls["n"] == 1

        # 3b. An idle long poll that Telegram resets is not an error.
        # It used to print `Telegram API error: <urlopen error [Errno 104]
        # Connection reset by peer>` every time, because `quiet_timeout`
        # only ever knew about timeouts (#63, @NotRetarded: "they start
        # building up in the logs").
        import contextlib
        def _raises(exc):
            def f(req, timeout=None):
                raise exc
            return f
        _reset = urllib.error.URLError(
            ConnectionResetError(104, "Connection reset by peer"))
        telegram_bot.urllib.request.urlopen = _raises(_reset)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = TelegramBot.api_call(_bot(), "getUpdates", {"x": "y"},
                                     quiet_timeout=True)
        checks["a reset long poll says nothing"] = (r is None and buf.getvalue() == "")

        # …but only the long poll, and only for that. A name that will
        # not resolve is a real problem and still speaks up.
        _dns = urllib.error.URLError(OSError("Name or service not known"))
        telegram_bot.urllib.request.urlopen = _raises(_dns)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            TelegramBot.api_call(_bot(), "getUpdates", {"x": "y"},
                                 quiet_timeout=True)
        checks["…while a name that will not resolve still does"] = (
            "Telegram API error" in buf.getvalue())

        telegram_bot.urllib.request.urlopen = _raises(_reset)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
        checks["…and a reset on a real send is still reported"] = (
            "Telegram API error" in buf.getvalue())

        # 4. HTTP 4xx → body returned, NOT retried
        calls = {"n": 0}
        def f4(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                "u", 400, "Bad Request", {},
                io.BytesIO(b'{"ok": false, "description": "bad markdown"}'))
        telegram_bot.urllib.request.urlopen = f4
        r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
        checks["HTTP 4xx returns body, not retried"] = (
            isinstance(r, dict) and r.get("ok") is False and calls["n"] == 1)
    finally:
        telegram_bot.urllib.request.urlopen = orig_urlopen
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
