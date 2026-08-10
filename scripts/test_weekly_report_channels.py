#!/usr/bin/env python3
"""Who gets the weekly report (#59, @NotRetarded).

He got it twice, and the interesting part is not the duplicate. It is that
the "both Discord paths are on, so everything arrives twice" warning was
*not* showing on his page — which sounds like a second bug and is in fact
the explanation for the first.

The report wrote its own list of recipients: Telegram, the Discord webhook,
the generic webhook, plus the Discord bot channel from 2.7.1. Two things
were wrong with a list like that.

**It asked whether a URL was set, not whether the channel was on.** So a
Discord webhook that had been switched off still received the report, while
every ordinary notification correctly skipped it — those go through the
facade, which checks `active()`. And the warning card is about the
*switches*, so with the webhook off it stays hidden. One config, both
symptoms: no warning, and a duplicate anyway.

**And the list was never extended.** E-mail, ntfy, Gotify, Matrix and
Apprise have existed for releases and had never once received a weekly
report. Nobody reported that, because a report you have never seen looks
like a report that was not due.

So the recipients come from the facade now, and each channel renders the
report in the shape it already used elsewhere. This file is mostly about
the switch: a channel that is off must get nothing, whatever it has saved.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from notifier import Notifier  # noqa: E402
import weekly_report  # noqa: E402

checks = {}

ALL = ("discord", "discordbot", "webhook", "smtp", "ntfy", "gotify",
       "matrix", "apprise")


def cfg(**kw):
    base = dict(
        bot_label="", quiet_hours_start="", quiet_hours_end="",
        discord_webhook="https://discord.com/api/webhooks/1/x",
        discord_bot_token="tok", discord_bot_channel="789",
        webhook_url="https://example.com/hook",
        smtp_host="smtp.example.com", smtp_from="a@b.c", smtp_to="d@e.f",
        smtp_user="", smtp_password="", smtp_port=587, smtp_tls="starttls",
        smtp_tls_verify=True,
        ntfy_url="https://ntfy.sh/alerts", ntfy_server="", ntfy_topic="",
        ntfy_token="", ntfy_user="", ntfy_password="",
        gotify_url="https://gotify.example.com", gotify_token="tok",
        matrix_homeserver="https://matrix.example.com", matrix_room="!r:x",
        matrix_token="tok",
        apprise_url="https://apprise.example.com/notify",
        apprise_urls="mailto://x", apprise_tag="")
    for c in ALL:
        base[f"channel_{c}_enabled"] = True
    base.update(kw)
    return types.SimpleNamespace(**base)


def recipients(config):
    """Which channels the report actually reaches, by name."""
    n = Notifier(config)
    got = []
    for p in n._plugins:
        p.send_weekly_report = (
            lambda name: lambda s, t, e=None: got.append(name))(p.name)
    n.send_weekly_report({"successes": 1}, "text", {"title": "Weekly"})
    return sorted(got)


# ── every channel that is on, and only those ─────────────────────────
checks["every switched-on channel receives the weekly report"] = (
    recipients(cfg()) == sorted(ALL))
# The five that had never received one. Named individually rather than as
# a count, because "some channels get it" was the old state too.
for chan in ("smtp", "ntfy", "gotify", "matrix", "apprise"):
    checks[f"…including {chan}, which never got one before"] = (
        chan in recipients(cfg()))

# ── the switch is what decides, not whether a URL is filled in ───────
# The exact configuration behind #59: webhook still saved, channel off.
off = cfg(channel_discord_enabled=False)
checks["a switched-off Discord webhook gets nothing"] = (
    "discord" not in recipients(off))
checks["…while its URL is still saved"] = bool(off.discord_webhook)
checks["…and the bot channel still gets it"] = (
    "discordbot" in recipients(off))
# Which is the whole point: in that configuration the report must arrive
# exactly once, and it used to arrive twice.
checks["…so Discord receives it exactly once"] = (
    len([c for c in recipients(off) if c.startswith("discord")]) == 1)
for chan in ALL:
    if recipients(cfg(**{f"channel_{chan}_enabled": False})).count(chan):
        checks[f"switching {chan} off silences it"] = False
checks["switching any single channel off silences that one"] = all(
    chan not in recipients(cfg(**{f"channel_{chan}_enabled": False}))
    for chan in ALL)
# An incomplete channel is not a recipient whatever its switch says.
checks["an incomplete channel is not a recipient"] = (
    "gotify" not in recipients(cfg(gotify_token="")))
checks["nothing configured means nobody is sent to"] = recipients(
    types.SimpleNamespace(**{**vars(cfg()), **{
        k: "" for k in vars(cfg()) if not k.startswith("channel_")
        and not k.startswith("quiet") and k not in ("smtp_port", "smtp_tls",
                                                    "smtp_tls_verify")}})) == []

# ── each channel keeps the shape it already used ─────────────────────
n = Notifier(cfg())
shapes = []
n._by_name["discord"].post = lambda p: shapes.append(("discord", tuple(p)))
n._by_name["discordbot"].post = lambda p: shapes.append(("discordbot", tuple(p)))
n._by_name["webhook"].send_raw = lambda e, d: shapes.append(("webhook", e))
n._by_name["smtp"].send_message = lambda t: shapes.append(("smtp", t))
n._by_name["ntfy"].send_message = lambda t: shapes.append(("ntfy", t))
n._by_name["gotify"].send_message = lambda t: shapes.append(("gotify", t))
n._by_name["matrix"].send_message = lambda t: shapes.append(("matrix", t))
n._by_name["apprise"].send_message = lambda t: shapes.append(("apprise", t))
n.send_weekly_report({"successes": 1}, "the report text", {"title": "Weekly"})
by = dict((s[0], s[1:]) for s in shapes)
checks["Discord gets the embed it always got"] = by["discord"] == (("embeds",),)
checks["…and the bot channel gets the identical one"] = (
    by["discordbot"] == by["discord"])
checks["the generic webhook keeps its weekly_report event"] = (
    by["webhook"] == ("weekly_report",))
checks["everything else gets the plain text"] = all(
    by[c] == ("the report text",)
    for c in ("smtp", "ntfy", "gotify", "matrix", "apprise"))

# ── and the report no longer keeps its own list ──────────────────────
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "weekly_report.py"), encoding="utf-8").read()
checks["the report asks the facade instead of naming channels"] = (
    "notifier.send_weekly_report(" in src)
checks["…and names no channel of its own"] = not any(
    s in src for s in ("_discord_post", "_discordbot_post", "_webhook_send",
                       "config.discord_webhook", "config.webhook_url"))
# Telegram stays outside the facade: it is the bot, not a notifier channel,
# and it has its own switch and its own `auto=True` quiet-hours handling.
checks["Telegram is still sent to separately"] = "bot.send_message(text" in src
checks["a report that reached nobody is not marked as sent"] = (
    "if sent_any:" in src and "mark_sent" in src)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
