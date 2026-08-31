#!/usr/bin/env python3
"""A notification survives a short outage, and a failed one says so (#66,
@NotRetarded).

A power cut took two of his machines down together. For 35 seconds his log
is `Try again` and `Network unreachable`; Discord's gateway reconnected and
delivered the crash alert, Telegram's six seconds of retries had long run
out and its copy simply ceased to exist. Nothing anywhere said so, because
`monitor._notify` throws the send result away.

What is asserted here is the intent, not the plumbing: a message that hit a
dead network is delivered once the network is back, a message too old to
still be true is not, the queue cannot grow without bound, a refused send
writes a line that can be grepped for, and a 429 is waited out rather than
abandoned. The transport is faked at the `urlopen` seam, so the retry, the
queue and the 429 handling under test are the real ones — no network, no
sockets, no real waits.
"""

import io
import json
import os
import socket
import sys
import time
import types
import urllib.error
import urllib.parse
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import monitor as monitor_mod          # noqa: E402
import notifier as notifier_mod        # noqa: E402
import notify_retry                    # noqa: E402
import telegram_bot                    # noqa: E402
from notifier import Notifier          # noqa: E402

checks = {}


# ── the fake wire ────────────────────────────────────────────────────
class Wire:
    """Telegram / Discord, with a plug that can be pulled.

    `down` raises the error his log actually carried (`[Errno -3] Try
    again`); otherwise it answers ok. Every request body is recorded, so
    both order and count can be asserted.
    """

    def __init__(self):
        self.down = False
        self.seen = []

    def __call__(self, req, timeout=None):
        self.seen.append(req.data.decode() if req.data else "")
        if self.down:
            raise urllib.error.URLError(socket.gaierror(-3, "Try again"))
        return Resp({"ok": True, "result": {"message_id": len(self.seen)}})


class Resp:
    def __init__(self, payload, status=204):
        self.payload = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def bot():
    b = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    b.config = types.SimpleNamespace(
        chat_id="-100123", bot_token="123:abc", bot_label="",
        telegram_topic_id=None, debug=False, telegram_allowed_users=[],
        channel_telegram_enabled=True, quiet_hours_start="",
        quiet_hours_end="")
    return b


def quiet(fn, *a, **kw):
    """Run `fn`, return `(result, captured_stdout)`."""
    out = io.StringIO()
    with redirect_stdout(out):
        r = fn(*a, **kw)
    return r, out.getvalue()


def body(raw):
    """A recorded request body as readable text."""
    return urllib.parse.unquote_plus(raw)


def ok(entry):
    """A resend that always lands."""
    return True


CRASH = "🔁 *Unifi-OS-Server* crashed (exit 137) at 15:34:55 (restart #5)."
SECOND = "🔁 *sonarr* crashed (exit 1) at 15:35:10 (restart #2)."

orig_urlopen = telegram_bot.urllib.request.urlopen
orig_sleep = time.sleep
time.sleep = lambda *a, **k: None       # api_call does `import time as _t`

try:
    # ═══ 1. the alert survives the outage ════════════════════════════
    notify_retry.queue.clear()
    wire = Wire()
    telegram_bot.urllib.request.urlopen = wire
    b = bot()

    wire.down = True
    res, log = quiet(b.send_message, CRASH, auto=True)
    checks["a send into a dead network reports failure, not silence"] = (
        res is False)
    checks["…and writes a line that can be grepped for"] = (
        "Telegram send failed" in log)
    checks["…and the message is held rather than dropped"] = (
        notify_retry.pending() == 1)
    quiet(b.send_message, SECOND, auto=True)
    checks["…a second alert during the same outage is held too"] = (
        notify_retry.pending() == 2)

    # Still down: the channel gets ONE go, then it is left alone for the
    # rest of the pass rather than hammered once per held message.
    before = len(wire.seen)
    (sent, dropped), log = quiet(notify_retry.flush)
    checks["a flush while still down delivers nothing"] = (
        (sent, dropped) == (0, 0))
    checks["…and tries the channel once, not once per held message"] = (
        len(wire.seen) - before) == 3
    checks["…and both messages are still held"] = notify_retry.pending() == 2

    wire.down = False
    before = len(wire.seen)
    (sent, dropped), log = quiet(notify_retry.flush)
    checks["once the network is back the alerts go out"] = (
        (sent, dropped) == (2, 0))
    checks["…and the queue is empty again"] = notify_retry.pending() == 0
    checks["…the log says they were held messages"] = (
        log.count("Notify retry: delivered a held telegram message") == 2)

    first, last = body(wire.seen[before]), body(wire.seen[-1])
    checks["…in the order they were raised, not reversed"] = (
        "Unifi-OS-Server" in first and "sonarr" in last)
    checks["…carrying the original alert, intact"] = "exit 137" in first
    checks["…marked as late, so it does not read as fresh"] = (
        "text=⏳ Delayed " in first)      # the notice opens the message
    checks["…and naming the time it actually happened"] = (
        "It happened at " in first)

    # ═══ 2. an alert too old to still be true is not delivered ═══════
    now = [0.0]
    landed = []
    q = notify_retry.RetryQueue(max_age=900, clock=lambda: now[0],
                                wall=lambda: 0.0)
    q.remember("telegram", CRASH, lambda t: landed.append(t) or True)
    now[0] = 901.0
    (sent, dropped), log = quiet(q.flush)
    checks["an alert past the age limit is dropped, not delivered late"] = (
        (sent, dropped) == (0, 1) and landed == [])
    checks["…saying why it was dropped"] = "too late to still be true" in log
    checks["…and it does not stay in the queue"] = q.pending() == 0

    now[0] = 0.0
    q.remember("telegram", CRASH, lambda t: landed.append(t) or True)
    now[0] = 899.0
    quiet(q.flush)
    checks["…while one just inside the window still goes"] = len(landed) == 1
    checks["…telling the reader how late it is"] = (
        landed[0].startswith("⏳ Delayed 14m"))
    checks["…without touching the alert itself"] = landed[0].endswith(CRASH)

    # ═══ 3. a long outage cannot eat the machine ═════════════════════
    q = notify_retry.RetryQueue(max_age=900, max_items=20)
    quiet(lambda: [q.remember("telegram", f"alert {i}", ok)
                   for i in range(200)])
    checks["a long outage cannot grow the queue past its cap"] = (
        q.pending() == 20)

    got = []
    q = notify_retry.RetryQueue(max_age=900, max_items=3)
    quiet(lambda: [q.remember("telegram", f"alert {i}",
                              lambda t: got.append(t) or True)
                   for i in range(5)])
    quiet(q.flush)
    checks["…the oldest are the ones dropped, the newest survive"] = (
        [g.split("\n\n")[-1] for g in got] == ["alert 2", "alert 3", "alert 4"])

    got = []
    q = notify_retry.RetryQueue(max_age=900)
    for i in range(3):
        q.remember("telegram", f"alert {i}", lambda t: got.append(t) or True)
    quiet(q.flush)
    checks["held messages keep their order"] = (
        [g.split("\n\n")[-1] for g in got] == ["alert 0", "alert 1", "alert 2"])

    # ═══ 4. nothing survives a restart ═══════════════════════════════
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "notify_retry.py"), encoding="utf-8").read()
    code = src.split('"""', 2)[2]
    checks["the queue is memory only — nothing is written to disk"] = not any(
        w in code for w in ("open(", "json.dump", "os.path", "settings.json"))

    # ═══ 5. a rejection is not repeated, and it is logged ════════════
    notify_retry.queue.clear()

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {},
            io.BytesIO(json.dumps(
                {"ok": False,
                 "description": "Bad Request: can't parse entities"}).encode()))

    telegram_bot.urllib.request.urlopen = refuse
    res, log = quiet(bot().send_message, CRASH, auto=True)
    checks["a message Telegram refuses is reported as failed"] = res is False
    checks["…with the reason in the log"] = (
        "Telegram send failed" in log and "parse entities" in log)
    checks["…and is NOT held: repeating a 400 only repeats it"] = (
        notify_retry.pending() == 0)

    # ═══ 6. the monitor says WHICH alert did not get out ═════════════
    mon = monitor_mod.ContainerMonitor.__new__(monitor_mod.ContainerMonitor)
    mon.host_name = ""
    mon._latest = {}
    mon.checker = types.SimpleNamespace(_tail_logs=lambda n, lines=10: "")
    mon._resources_for = lambda kind, name: {}
    mon.bot = types.SimpleNamespace(
        t=lambda key, **kw: f"{key} {kw.get('name', '')}",
        send_message=lambda msg, auto=False: False,
        notifier=None)
    detail = {"code": "137", "count": 5, "when": "15:34:55"}
    _, log = quiet(mon._notify, "crash_restart", "Unifi-OS-Server", detail)
    checks["a monitor alert that did not get out is named in the log"] = (
        "Monitor notify failed" in log and "Unifi-OS-Server" in log)

    mon.bot.send_message = lambda msg, auto=False: None
    _, log = quiet(mon._notify, "crash_restart", "Unifi-OS-Server", detail)
    checks["…while quiet hours are not reported as a failure"] = (
        "Monitor notify failed" not in log)

    # A headless install — Discord only, no BOT_TOKEN — must not report a
    # failed Telegram send on every alert it raises.
    headless = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    headless.config = types.SimpleNamespace(
        bot_token="", chat_id="", channel_telegram_enabled=True)
    res, log = quiet(headless.send_message, CRASH, auto=True)
    checks["a bot with no token is a silence, not a failure"] = (
        res is None and "send failed" not in log)

    # ═══ 7. a 429 is waited out, not abandoned ═══════════════════════
    waited = []
    time.sleep = lambda s: waited.append(s)
    state = {"n": 0}

    def limited(req, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {},
                io.BytesIO(json.dumps(
                    {"ok": False, "error_code": 429,
                     "description": "Too Many Requests: retry after 3",
                     "parameters": {"retry_after": 3}}).encode()))
        return Resp({"ok": True, "result": {"message_id": 1}})

    telegram_bot.urllib.request.urlopen = limited
    res, log = quiet(bot().send_message, CRASH, auto=True)
    checks["a rate-limited message is not abandoned"] = bool(
        res and res.get("ok"))
    checks["…it waits exactly as long as Telegram asked"] = waited == [3.0]
    checks["…and says so in the log"] = "rate limited, waiting 3.0s" in log

    waited.clear()

    def forever(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {},
            io.BytesIO(json.dumps(
                {"ok": False, "parameters": {"retry_after": 3600}}).encode()))

    telegram_bot.urllib.request.urlopen = forever
    quiet(bot().send_message, CRASH, auto=True)
    checks["…but an hour-long limit is not sat out inside the request"] = (
        waited == [])
    time.sleep = lambda *a, **k: None

    # ═══ 8. the same for Discord and the generic webhook ═════════════
    notify_retry.queue.clear()
    cfg = types.SimpleNamespace(
        discord_webhook="https://discord.example/hook",
        webhook_url="https://webhook.example/in",
        bot_label="", quiet_hours_start="", quiet_hours_end="",
        smtp_host="", smtp_from="", smtp_to="")
    n = Notifier(cfg)
    dwire = Wire()
    dwire.down = True
    notifier_mod.urllib.request.urlopen = dwire
    _, log = quiet(n.send_message, CRASH)
    checks["Discord and the webhook hold a message too"] = (
        notify_retry.pending() == 2)
    checks["…and each says so in the log"] = (
        "discord send failed" in log and "webhook send failed" in log)

    dwire.down = False
    (sent, dropped), log = quiet(notify_retry.flush)
    checks["…and both go out once the network is back"] = (
        (sent, dropped) == (2, 0) and notify_retry.pending() == 0)
    checks["…late-marked, like the Telegram one"] = all(
        "Delayed" in b for b in dwire.seen[-2:])

    notify_retry.queue.clear()

    def refuse_json(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b"{}"))

    notifier_mod.urllib.request.urlopen = refuse_json
    quiet(n.send_message, CRASH)
    checks["a 400 from Discord is not held for retry"] = (
        notify_retry.pending() == 0)

finally:
    telegram_bot.urllib.request.urlopen = orig_urlopen
    notifier_mod.urllib.request.urlopen = orig_urlopen
    time.sleep = orig_sleep
    notify_retry.queue.clear()

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
