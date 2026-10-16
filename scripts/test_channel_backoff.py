#!/usr/bin/env python3
""""Not now" is not "no" — the same answer from every channel.

#66 was about SMTP: a relay saying `450 Greylisted — try again later` was
filed as a refusal and the crash alert thrown away while the server was
asking us to come back. Fixing that raised the obvious question about
the other channels, and the answer was measured rather than assumed:

    429 Too Many Requests   → dropped, in all four
    503 Service Unavailable → dropped, in all four

ntfy.sh rate-limits free accounts. A burst of container failures is
exactly when a rate limit is hit and exactly when the message matters,
so the alert most likely to go missing was the one worth having. The
HTTP family was right about refusals — a 401 must not be retried for
fifteen minutes — and wrong about the two statuses whose whole meaning
is "ask again".

One procedure for all six channels rather than six copies in six files.
The five `*_resilience.py` share six distinct assertion expressions
between them, 24 occurrences in all and none of them in all five —
counted on 16.10.2026, after an audit could not reproduce the
"twenty-five" that stood here on any reading. The identical check
copied four times is still how a rule drifts in one place and nobody
notices. Each channel keeps
its own file for what is genuinely its own; this is the part that has
to be the same everywhere, so it is written once and the table says who
it applies to.

Driven end to end: a real notifier, a real retry queue, an HTTP error
raised where `urlopen` would raise it. What is asserted is whether the
message is waiting afterwards — not which clause was taken.
"""
import io
import os
import sys
import types
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app",
                                "notifiers"))

import notify_retry  # noqa: E402
from notifiers.base import TEMPORARY_STATUS  # noqa: E402

checks = {}

#: status → is it worth asking again?
#:
#: `500` is on the "no" side on purpose: it is as likely to be a
#: permanent bug on the server as a passing one, and a queue that
#: repeats it every thirty seconds for a quarter of an hour is a loop.
#: If that turns out to be the wrong call it is one line, and this table
#: is where the argument lives.
STATUS = [(408, True), (429, True), (502, True), (503, True), (504, True),
          (400, False), (401, False), (403, False), (404, False),
          (422, False), (500, False)]


def _cfg(**kw):
    base = dict(language="en", bot_label="")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _ntfy():
    from notifiers.ntfy import NtfyNotifier
    return NtfyNotifier(_cfg(
        ntfy_url="https://ntfy.sh", ntfy_topic="docksentry", ntfy_token="",
        ntfy_user="", ntfy_password="", ntfy_priority="default",
        channel_ntfy_enabled=True))


def _gotify():
    from notifiers.gotify import GotifyNotifier
    return GotifyNotifier(_cfg(gotify_url="https://gotify.example",
                               gotify_token="tok",
                               channel_gotify_enabled=True))


def _apprise():
    from notifiers.apprise import AppriseNotifier
    return AppriseNotifier(_cfg(apprise_url="https://apprise.example",
                                apprise_tag="",
                                channel_apprise_enabled=True))


def _matrix():
    from notifiers.matrix import MatrixNotifier
    # `matrix_room`, not `matrix_room_id` — a fixture with the wrong name
    # makes `configured()` False, and then every status looks like "not
    # queued" because nothing was ever sent. That is how this file first
    # reported Matrix as broken when it was not.
    return MatrixNotifier(_cfg(matrix_homeserver="https://matrix.example",
                               matrix_room="!room:example",
                               matrix_token="tok",
                               channel_matrix_enabled=True))


def _discord():
    from notifiers.discord import DiscordNotifier
    return DiscordNotifier(_cfg(discord_webhook="https://discord.example/w",
                                channel_discord_enabled=True))


def _webhook():
    from notifiers.webhook import WebhookNotifier
    return WebhookNotifier(_cfg(webhook_url="https://hook.example",
                                webhook_secret="",
                                channel_webhook_enabled=True))


CHANNELS = [("ntfy", _ntfy), ("gotify", _gotify), ("apprise", _apprise),
            ("matrix", _matrix), ("discord", _discord), ("webhook", _webhook)]


def _queued_after(build, status):
    """Send one message into an HTTP `status`; is it waiting afterwards?"""
    q = notify_retry.RetryQueue(clock=lambda: 0.0, wall=lambda: 0.0)
    real_queue, notify_retry.queue = notify_retry.queue, q
    real_open = urllib.request.urlopen
    real_sleep = None

    def _raise(*_a, **_kw):
        raise urllib.error.HTTPError("https://example/x", status, "nope",
                                     {}, io.BytesIO(b"{}"))

    urllib.request.urlopen = _raise
    # `post_json_with_retry` sleeps between attempts; not here.
    import time as _time
    real_sleep, _time.sleep = _time.sleep, lambda *_a: None
    try:
        build().send_message("three containers failed to update")
        return q.pending()
    finally:
        urllib.request.urlopen = real_open
        _time.sleep = real_sleep
        notify_retry.queue = real_queue


for name, build in CHANNELS:
    held, dropped = [], []
    for status, retryable in STATUS:
        waiting = _queued_after(build, status) > 0
        (held if waiting else dropped).append(status)
        if waiting != retryable:
            print(f"      → {name}: HTTP {status} "
                  f"{'was held' if waiting else 'was dropped'}, "
                  f"expected the opposite")
    checks[f"{name}: a 'not now' answer is held for another go"] = (
        held == [s for s, r in STATUS if r])
    # The other half, and it is the one that stops this being satisfied
    # by a channel that queues everything: a refusal must NOT come back
    # every thirty seconds for a quarter of an hour.
    checks[f"{name}: …and a refusal is not asked again"] = (
        dropped == [s for s, r in STATUS if not r])

# The table itself has to match what the shared helper believes, or two
# ideas of "temporary" drift apart in two files.
checks["the table and TEMPORARY_STATUS agree"] = (
    sorted(s for s, r in STATUS if r) == sorted(TEMPORARY_STATUS))

# …and a held message really is the message, not an empty placeholder.
q = notify_retry.RetryQueue(clock=lambda: 0.0, wall=lambda: 0.0)
_real = notify_retry.queue
notify_retry.queue = q
_open = urllib.request.urlopen
urllib.request.urlopen = lambda *_a, **_kw: (_ for _ in ()).throw(
    urllib.error.HTTPError("https://x", 429, "slow down", {}, io.BytesIO(b"{}")))
try:
    _ntfy().send_message("three containers failed to update")
    checks["the held message is the alert, not an empty entry"] = (
        q.pending() == 1
        and "three containers failed" in q._items[0]["text"])
finally:
    urllib.request.urlopen = _open
    notify_retry.queue = _real

# ── a `Retry-After` the server names is honoured, within reason ──────
# Waiting out a two-second limit in place is right: a trip through the
# queue would cost the message thirty seconds for nothing. Sleeping as
# long as the server asks is not — `Retry-After: 3600` is a legal
# answer, and a notifier that obeys it has stopped being a notifier for
# an hour, on the thread that also runs the monitor ticks.
#
# This path is shared by Discord webhooks and the generic webhook. Not
# four: ntfy, Gotify, Apprise and Matrix each have their own
# `_post_text` and honour no `Retry-After` at all, which the first
# version of this comment got wrong — it claimed four and nothing here
# says anything about those four.
import time as _time                                          # noqa: E402

import retry_after as _ra                                     # noqa: E402
from notifiers import base as _base                           # noqa: E402


def _slept_for(retry_after):
    """How long the shared path sleeps when the server says to wait.

    `_base.time` *is* the `time` module, so patching both names put the
    recorder back in place in the `finally` and left `time.sleep`
    monkey-patched for the rest of the process. One name, restored.
    """
    slept = []
    attempts = []
    real_sleep = _time.sleep
    _time.sleep = lambda s: slept.append(s)
    real_open = urllib.request.urlopen

    def _limited(*_a, **_kw):
        attempts.append(1)
        raise urllib.error.HTTPError(
            "https://example/x", 429, "slow down",
            {"Retry-After": str(retry_after)}, io.BytesIO(b"{}"))

    urllib.request.urlopen = _limited
    try:
        _base.post_json_with_retry("https://example/x", {"a": 1}, {},
                                   "test", on_network_failure=lambda: None)
    finally:
        urllib.request.urlopen = real_open
        _time.sleep = real_sleep
    return slept, len(attempts)


_short, _tries = _slept_for(2)
checks["a short rate limit is waited out in place"] = 2 in _short
checks["…and the request is tried again after it"] = _tries > 1

_long, _tries = _slept_for(3600)
checks["an hour-long one is not slept through"] = not any(
    s >= 3600 for s in _long)
checks["…it goes to the queue instead"] = _tries == 1

# The cap is for the whole send, not for each attempt. Two answers of
# `Retry-After: 60` used to mean `slept=[60.0, 60.0]` — two minutes on
# the scheduler's thread, where the commit that added the cap had
# claimed "a rate limit that cannot put the scheduler to sleep".
_at_cap, _tries = _slept_for(_ra.MAX_TOTAL_WAIT)
checks["the whole send waits at most the budget, not that per attempt"] = (
    sum(_at_cap) <= _ra.MAX_TOTAL_WAIT)
checks["…and the budget is spent, so the rest goes to the queue"] = (
    len(_at_cap) == 1)

# A server may answer anything at all, including something hostile.
# `float("-1") <= 60` is true and `time.sleep(-1)` raises — measured
# propagating out of `post_json_with_retry`, past a channel's promise
# never to raise into the facade, and the alert was lost *without*
# reaching the queue because `on_network_failure` was never called.
_held = []
for _hostile in ("-1", "-3600", "nan", "inf", "tomorrow", ""):
    _s, _t = _slept_for(_hostile)
    _held.append((_hostile, _s, _t))
checks["a hostile Retry-After is not slept on"] = all(
    s == [] for _v, s, _t in _held)
checks["…and does not raise out of the notifier"] = all(
    t == 1 for _v, _s, t in _held)
for _v, _s, _t in _held:
    if _s or _t != 1:
        print(f"      → Retry-After {_v!r}: slept={_s}, attempts={_t}")

# …and the one message still ends up held, which is the whole point of
# refusing to sleep: the slow path is the correct one.
_q = notify_retry.RetryQueue(clock=lambda: 0.0, wall=lambda: 0.0)
_real_q, notify_retry.queue = notify_retry.queue, _q
_open2 = urllib.request.urlopen
urllib.request.urlopen = lambda *_a, **_kw: (_ for _ in ()).throw(
    urllib.error.HTTPError("https://x", 429, "slow down",
                           {"Retry-After": "-1"}, io.BytesIO(b"{}")))
try:
    _ntfy().send_message("three containers failed to update")
    checks["…the alert is held rather than lost"] = _q.pending() == 1
finally:
    urllib.request.urlopen = _open2
    notify_retry.queue = _real_q

# The three call sites share one number rather than three literals that
# happen to agree. Nothing held them to each other before.
from discord_rest import MAX_RETRY_AFTER as _dc_cap            # noqa: E402
from telegram_bot import TelegramBot as _TB                    # noqa: E402

checks["all three in-line waits share one budget"] = (
    _dc_cap == _ra.MAX_TOTAL_WAIT
    and _TB.MAX_RETRY_AFTER == _ra.MAX_TOTAL_WAIT)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
