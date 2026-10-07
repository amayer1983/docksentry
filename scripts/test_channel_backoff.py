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
The five `*_resilience.py` already share twenty-five identical assertion
expressions between them, and the identical check copied five times is
how a rule drifts in one place and nobody notices. Each channel keeps
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

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
