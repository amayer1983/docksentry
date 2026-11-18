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


class _Wire:
    """An urlopen whose answer can be switched between calls.

    `mode` is an HTTP status, "ok", "net" (nothing answers) or "reset"
    (the connection is reset). `html` sends a gateway's HTML page in
    place of Telegram's JSON. Every call and every sent text is kept.
    """

    def __init__(self):
        self.mode, self.html = "ok", False
        self.calls, self.texts = [], []

    def __call__(self, req, timeout=None):
        import json as _j
        import urllib.parse as _p
        self.calls.append(self.mode)
        if req.data:
            self.texts.extend(_p.parse_qs(req.data.decode()).get("text", []))
        if self.mode == "ok":
            return _Resp(_j.dumps({"ok": True, "result": {}}).encode())
        if self.mode == "net":
            raise urllib.error.URLError("connection refused")
        if self.mode == "reset":
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        code = self.mode
        if self.html:
            raw = (f"<html><body><h1>{code}</h1>nginx</body></html>").encode()
        else:
            body = {"ok": False, "error_code": code, "description": "x"}
            if code == 429:
                body["parameters"] = {"retry_after": 30}
            raw = _j.dumps(body).encode()
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(raw))


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
    for code in (502, 503, 408):
        calls = []
        telegram_bot.urllib.request.urlopen = _http(code, "nope", calls)
        with contextlib.redirect_stdout(io.StringIO()):
            r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
        checks[f"{code} is retried to the end of the budget"] = len(calls) == 3
        checks[f"{code} comes back as 'try again later', not as a body"] = r is None

    # 504 is the exception: the gateway gave up, Telegram may not have.
    # Another attempt inside the call is a possible second copy.
    calls = []
    telegram_bot.urllib.request.urlopen = _http(504, "Gateway Timeout", calls)
    with contextlib.redirect_stdout(io.StringIO()):
        r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
    checks["504 is not retried inside the call"] = len(calls) == 1
    checks["504 comes back as 'try again later', not as a body"] = r is None

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
        buf.getvalue().count("the poll has been failing") == 1)
    # Telegram did answer — with 502. "No answer" sends the reader to
    # their own network.
    checks["…and does not call a 502 'no answer'"] = (
        "no answer" not in buf.getvalue()
        and "Telegram answered 502" in buf.getvalue())

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

    # ── 6. a held redelivery is one attempt and no sleeping ─────────────
    # The queue resends on the scheduler thread, which also drives the
    # monitor ticks. Measured before this: a sustained 429 with
    # `retry_after: 30` slept 60 s there on every pass, a 502 six.
    wire = _Wire()
    telegram_bot.urllib.request.urlopen = wire
    for code in TEMPORARY_STATUS:
        wire.mode = code
        wire.calls.clear()
        waits.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            r = TelegramBot._redeliver(_bot(), "held")
        checks[f"a held redelivery answered {code} is one attempt"] = (
            len(wire.calls) == 1)
        checks[f"…and sleeps nothing ({code})"] = waits == []
        if code == 504:
            checks["…and a 504 on it ends the holding"] = (
                r is notify_retry.STOP)
        else:
            checks[f"…and a {code} on it stays held"] = (
                r is False and r is not notify_retry.STOP)
    # Counter-directions: a dead network on a redelivery is also one
    # attempt and stays held; a refusal on one stops the holding.
    # A 400 is two: `send_message` tries once more without Markdown,
    # which is its answer to a parse error and not a retry of the same
    # request.
    for mode, want, tries in (("net", False, 1), (400, True, 2)):
        wire.mode = mode
        wire.calls.clear()
        waits.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            r = TelegramBot._redeliver(_bot(), "held")
        checks[f"a held redelivery with {mode} is {tries} attempt(s), no sleep"] = (
            len(wire.calls) == tries and waits == [])
        checks[f"…and {'stays held' if want is False else 'is let go'} ({mode})"] = (
            r is want)

    # ── 7. fifteen minutes on the real queue, the clock handed in ───────
    notify_retry.remember = orig_remember
    orig_queue = notify_retry.queue
    clock = [0.0]
    notify_retry.queue = notify_retry.RetryQueue(
        clock=lambda: clock[0], wall=lambda: 1.7e9 + clock[0])

    def fifteen(code, ok_from=None, html=False):
        """One alert at t=0, then a scheduler pass every 30 s for 15 min
        (31 passes: the one at 930 s finds it past the age limit)."""
        notify_retry.queue.clear()
        wire.mode, wire.html = code, html
        wire.calls.clear()
        clock[0] = 0.0
        b = _bot()
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            TelegramBot.send_message(b, "ONE ALERT")
            first = len(wire.calls)
            delivered = 0
            for i in range(1, 32):
                clock[0] = 30.0 * i
                if ok_from is not None and clock[0] >= ok_from:
                    wire.mode = "ok"
                sent, _d = notify_retry.flush()
                delivered += sent
        return first, len(wire.calls), delivered, log.getvalue()

    first, total, delivered, log = fifteen(504)
    checks["a 504 on the first send is held, not retried in the call"] = (
        first == 1)
    # Worst case two copies: the first 504 and the one redelivery. Before
    # this it was 93 attempts — three per call, every pass.
    checks["15 minutes of 504: at most 2 sendMessage attempts in total"] = (
        total <= 2)
    checks["…exactly one of them a redelivery"] = total == 2
    checks["…and the drop says why"] = (
        "answered 504 too" in log and notify_retry.pending() == 0)
    # Counter-direction: a 504 then a clean answer is delivered.
    first, total, delivered, log = fifteen(504, ok_from=30)
    checks["a 504 then an ok redelivery is delivered once"] = (
        delivered == 1 and total == 2)

    # 502 is "not now": held to the age limit. Bound at one pass every
    # 30 s: 3 attempts on the first send + 1 per pass for the 30 passes
    # inside 900 s = 33. Before this: 93.
    first, total, delivered, log = fifteen(502)
    checks["15 minutes of 502: held the whole time, never delivered"] = (
        delivered == 0 and first == 3)
    checks["…in at most 33 sendMessage attempts"] = total <= 33
    first, total, delivered, log = fifteen(502, ok_from=840)
    checks["502 for 14 minutes, then ok: delivered once"] = delivered == 1
    checks["…in at most 33 sendMessage attempts too"] = total <= 33

    # A dead network over the same window still holds (counter-direction
    # to "504 is dropped").
    first, total, delivered, log = fifteen("net", ok_from=600)
    checks["a dead network is held and delivered when it comes back"] = (
        delivered == 1)

    # ── 8. a 502 page without JSON is a 502 ─────────────────────────────
    # A proxy or edge answers with its own HTML. Measured before this:
    # one attempt instead of three, and a log line per poll cycle.
    wire.mode, wire.html = 502, True
    wire.calls.clear()
    waits.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        r = TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
    checks["an HTML 502 is retried like a JSON 502"] = (
        len(wire.calls) == 3 and r is None)
    first, total, delivered, log = fifteen(502, ok_from=840, html=True)
    checks["…held, and delivered when Telegram answers"] = (
        delivered == 1 and total <= 33)
    b = _bot()
    wire.mode, wire.html = 502, True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for _ in range(5):
            TelegramBot.api_call(b, "getUpdates", {}, quiet_timeout=True)
    checks["…and a burst of HTML 502 polls says nothing"] = (
        buf.getvalue() == "")
    # Counter-direction: an HTML page with a status that is not
    # temporary is still not dressed up as one.
    wire.mode, wire.html = 404, True
    wire.calls.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        TelegramBot.api_call(_bot(), "sendMessage", {"x": "y"})
    checks["…while an HTML 404 is not retried"] = len(wire.calls) == 1
    wire.html = False

    # ── 9. what the reader is told ──────────────────────────────────────
    notify_retry.queue.clear()
    wire.mode = 502
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        TelegramBot.send_message(_bot(), "the alert")
    checks["a held 502 is not logged as 'no answer from Telegram'"] = (
        "no answer" not in buf.getvalue()
        and "Telegram answered 502" in buf.getvalue())
    notify_retry.queue.clear()
    wire.mode = "net"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        TelegramBot.send_message(_bot(), "the alert")
    checks["…while a dead network still is"] = (
        "no answer from Telegram" in buf.getvalue())
    # The redelivered text, through the real flush.
    notify_retry.queue.clear()
    clock[0] = 0.0
    wire.mode = 502
    with contextlib.redirect_stdout(io.StringIO()):
        TelegramBot.send_message(_bot(), "the alert")
    wire.mode = "ok"
    wire.texts.clear()
    clock[0] = 120.0
    with contextlib.redirect_stdout(io.StringIO()):
        notify_retry.flush()
    landed = wire.texts[-1] if wire.texts else ""
    checks["a redelivered message still says it is late"] = (
        landed.startswith("⏳ Delayed 2m"))
    checks["…without blaming the network for a 502"] = (
        "no network" not in landed and "the alert" in landed)
    # The long poll, counter-direction: a reset IS no answer.
    b = _bot()
    b._poll_drop_since = time.time() - 4 * 60
    wire.mode = "reset"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        TelegramBot.api_call(b, "getUpdates", {}, quiet_timeout=True)
    checks["a poll that gets no answer for minutes still says 'no answer'"] = (
        "no answer to the poll" in buf.getvalue())
finally:
    telegram_bot.urllib.request.urlopen = orig_urlopen
    time.sleep = orig_sleep
    notify_retry.remember = orig_remember
    if "orig_queue" in globals():
        notify_retry.queue = orig_queue


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
