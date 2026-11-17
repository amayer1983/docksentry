#!/usr/bin/env python3
"""A gateway error from Telegram is "not now", not "no" (#63).

@NotRetarded's log, 17.11.2026: dozens of `Telegram API 502: Bad
Gateway` in a row, with a `429: Too Many Requests: retry after 5`
among them. Two different things were wrong behind that one screen.

On the long poll it was only noise — the loop sleeps five seconds and
asks again with the same offset, so nothing is lost — but the quiet
treatment written in September only knew about resets and timeouts, so
a burst printed one line per cycle.

On a *send* it lost the message. Only 429 was ever treated as
transient; 502/503/504 fell through to "Telegram refused this", and a
refusal is not held for redelivery. Measured before the fix: a dead
cable kept the alert and sent it later, a bad minute at Telegram threw
it away. Exactly backwards.

The table is not a new one: `notifiers.base.TEMPORARY_STATUS` has been
the answer for every other channel since #66, and `test_channel_backoff.py`
guards its contents. This file is about Telegram using it.

Stand-ins are the network (urlopen), the clock (sleep) and the retry
queue — never the classification, which is the thing under test.
"""
import contextlib
import io
import os
import sys
import time
import types
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import telegram_bot                      # noqa: E402
import notify_retry                      # noqa: E402
from telegram_bot import TelegramBot     # noqa: E402
from notifiers.base import TEMPORARY_STATUS   # noqa: E402

checks = {}


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def _http(code, description, calls=None):
    """An urlopen that always answers with one HTTP status."""
    import json as _j

    def f(req, timeout=None):
        if calls is not None:
            calls.append(code)
        body = _j.dumps({"ok": False, "error_code": code,
                         "description": description}).encode()
        raise urllib.error.HTTPError(req.full_url, code, description, {},
                                     io.BytesIO(body))
    return f


def _bot():
    """A real instance: `enabled` is a property and the methods are the
    ones under test. Only the config is made up."""
    b = TelegramBot.__new__(TelegramBot)
    b.config = types.SimpleNamespace(
        bot_token="t", chat_id="-1", telegram_topic_id=None, bot_label="",
        channel_telegram_enabled=True)
    b._migrated_chat_id = None
    b._migration_notice_pending = False
    b.t = lambda k, **kw: k
    return b


orig_urlopen = telegram_bot.urllib.request.urlopen
orig_sleep = time.sleep
orig_remember = notify_retry.remember
waits = []
time.sleep = lambda s=0, *a, **k: waits.append(s)   # api_call uses `import time as _t`
held = []
notify_retry.remember = lambda channel, text, send, still_wanted=None: held.append(
    (channel, text))

try:
    # ── 1. api_call: a gateway error is retried and reported as silence ──
    for code in (502, 503, 504, 408):
        calls = []
        telegram_bot.urllib.request.urlopen = _http(code, "nope", calls)
        with contextlib.redirect_stdout(io.StringIO()):
            r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
        checks[f"{code} is retried to the end of the budget"] = len(calls) == 3
        checks[f"{code} comes back as 'nothing answered', not as a body"] = r is None

    # …and it is not silent about it in the log.
    telegram_bot.urllib.request.urlopen = _http(502, "Bad Gateway")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
    checks["a send still says 502 in the log"] = "Telegram API 502" in buf.getvalue()

    # ── 2. the counter-direction: a refusal stays a refusal ─────────────
    # Without this the fix would be "hold everything forever", and a
    # message Telegram will refuse every time would ride the queue for
    # fifteen minutes before being dropped anyway.
    calls = []
    telegram_bot.urllib.request.urlopen = _http(400, "cant parse entities", calls)
    with contextlib.redirect_stdout(io.StringIO()):
        r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
    checks["a 400 is still answered, not retried"] = (
        isinstance(r, dict) and r.get("ok") is False and len(calls) == 1)

    # 500 is deliberately NOT in the table — it is as likely to be a
    # permanent bug on the far side as a passing one, and retrying it
    # every thirty seconds is a loop against something that keeps
    # failing. The reasoning lives in notifiers/base.py; this pins that
    # Telegram follows the same table rather than a second opinion.
    calls = []
    telegram_bot.urllib.request.urlopen = _http(500, "Internal Server Error", calls)
    with contextlib.redirect_stdout(io.StringIO()):
        r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
    checks["500 is not treated as temporary"] = (
        isinstance(r, dict) and len(calls) == 1)
    checks["…which is what the shared table says"] = (
        502 in TEMPORARY_STATUS and 500 not in TEMPORARY_STATUS)

    # ── 3. the consequence: the message is held, not thrown away ────────
    held.clear()
    telegram_bot.urllib.request.urlopen = _http(502, "Bad Gateway")
    b = _bot()
    with contextlib.redirect_stdout(io.StringIO()):
        out = TelegramBot.send_message(b, "the alert", auto=True)
    checks["a 502 send is held for redelivery"] = (
        len(held) == 1 and held[0][0] == "telegram")
    checks["…and is reported as the network, not as a refusal"] = (
        getattr(b, "_last_send_problem", "") == "network")
    checks["…and the caller is told it did not go out"] = out is False

    # The same send against a real refusal must NOT be held.
    held.clear()
    telegram_bot.urllib.request.urlopen = _http(400, "cant parse entities")
    b = _bot()
    with contextlib.redirect_stdout(io.StringIO()):
        TelegramBot.send_message(b, "the alert", auto=True)
    checks["a refused send is not held"] = held == []
    checks["…and says so"] = getattr(b, "_last_send_problem", "") == "rejected"

    # …and the promise that was already there: a dead network holds.
    held.clear()
    telegram_bot.urllib.request.urlopen = (
        lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.URLError("connection refused")))
    b = _bot()
    with contextlib.redirect_stdout(io.StringIO()):
        TelegramBot.send_message(b, "the alert", auto=True)
    checks["a dead network is still held"] = len(held) == 1

    # ── 4. the long poll stays quiet about it ───────────────────────────
    b = _bot()
    telegram_bot.urllib.request.urlopen = _http(502, "Bad Gateway")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(5):
            TelegramBot.api_call(b, "getUpdates", {}, quiet_timeout=True)
    checks["a short burst of 502 polls says nothing"] = buf.getvalue() == ""

    # …but a poll that has been answering 502 for minutes is a deaf bot,
    # and that has to be said — once, not per cycle.
    # Four minutes of 502s, past POLL_QUIET_SECONDS. The clock is handed
    # in rather than waited out; only `sleep` is stood in for above.
    b._poll_drop_since = time.time() - 4 * 60
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(5):
            TelegramBot.api_call(b, "getUpdates", {}, quiet_timeout=True)
    checks["…a long one says so once"] = (
        buf.getvalue().count("no answer to the poll") == 1)

    # The counter-direction on the poll too: a 400 is not transient and
    # is not quietened, or a token that stopped working would look like
    # an idle evening.
    b = _bot()
    telegram_bot.urllib.request.urlopen = _http(401, "Unauthorized")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        TelegramBot.api_call(b, "getUpdates", {}, quiet_timeout=True)
    checks["a poll that is refused still speaks up"] = (
        "Telegram API 401" in buf.getvalue())

    # ── 5. 429 keeps the behaviour it already had ───────────────────────
    # It is in the same table now, so this is the check that the fix did
    # not flatten it into a plain backoff: Retry-After is honoured.
    waits.clear()
    import json as _json

    def _429(req, timeout=None):
        body = _json.dumps({"ok": False, "error_code": 429,
                            "description": "Too Many Requests: retry after 5",
                            "parameters": {"retry_after": 5}}).encode()
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {}, io.BytesIO(body))
    telegram_bot.urllib.request.urlopen = _429
    with contextlib.redirect_stdout(io.StringIO()):
        TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
    checks["429 still waits the time it was told"] = 5 in waits
    checks["…and not the plain 2s backoff"] = 2 not in waits
finally:
    telegram_bot.urllib.request.urlopen = orig_urlopen
    time.sleep = orig_sleep
    notify_retry.remember = orig_remember


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
