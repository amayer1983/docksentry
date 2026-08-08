#!/usr/bin/env python3
"""Three states per channel, and a switch that cannot lie.

Until now a channel was on exactly when it was filled in. That is a fine
default and a poor answer to two ordinary situations: turning a working
channel off for a week, and finding out why nothing is arriving.

Turning it off meant clearing the fields — and for the five channels whose
credentials are write-only in the interface, that means fetching a token
again to turn it back on. So there is a switch.

The trap a switch brings with it is the one this whole file guards: a
switch you can turn on for a channel that then does nothing produces "I
enabled it and nothing happened", which is the failure mode of every other
bug fixed around it. Two things prevent it.

**`active()` is not `configured()`.** Sending needs both — switched on and
complete. They are kept apart because the page has to say "off" and
"incomplete" differently: they need different things done about them, and
a single boolean answers neither question.

**A switch is only rendered once the channel is complete.** Which means an
incomplete channel's switch is absent from the form, and absence of a
checkbox normally reads as "off" — so a plain `"x" in params` would switch
a channel off at the very moment its last field was filled in. The form
sends a `_shown` marker alongside each switch it renders, and only a switch
that was shown may be read as off.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from notifier import Notifier  # noqa: E402


def cfg(**kw):
    base = dict(
        bot_label="", quiet_hours_start="", quiet_hours_end="",
        discord_webhook="", webhook_url="",
        smtp_host="", smtp_from="", smtp_to="", smtp_user="",
        smtp_password="", smtp_port=587, smtp_tls="starttls",
        smtp_tls_verify=True,
        ntfy_url="", ntfy_server="", ntfy_topic="", ntfy_token="",
        ntfy_user="", ntfy_password="",
        gotify_url="", gotify_token="",
        matrix_homeserver="", matrix_room="", matrix_token="",
        apprise_url="", apprise_urls="", apprise_tag="",
    )
    for chan in ("discord", "webhook", "smtp", "ntfy", "gotify",
                 "matrix", "apprise"):
        base[f"channel_{chan}_enabled"] = True
    base.update(kw)
    return types.SimpleNamespace(**base)


def state(config, name):
    for n, enabled, complete, missing in Notifier(config).channel_states():
        if n == name:
            return enabled, complete, missing
    raise AssertionError(f"no channel {name}")


def main():
    checks = {}

    # ── incomplete: says so, and says what is missing ────────────
    enabled, complete, missing = state(cfg(), "smtp")
    checks["an untouched channel is not complete"] = not complete
    checks["…and names every field it needs"] = missing == [
        "web_smtp_host", "web_smtp_from", "web_smtp_to"]

    enabled, complete, missing = state(
        cfg(smtp_host="smtp.example.com", smtp_from="a@b.c"), "smtp")
    checks["a half-filled channel names only what is still missing"] = (
        missing == ["web_smtp_to"])

    # ntfy takes a topic URL OR a server plus a topic, so a flat list of
    # requirements cannot express it and it answers for itself.
    _, _, missing = state(cfg(), "ntfy")
    checks["ntfy asks for the topic URL when nothing is set"] = (
        missing == ["web_ntfy_url"])
    _, _, missing = state(cfg(ntfy_server="https://ntfy.sh"), "ntfy")
    checks["…and for the topic once a server is set"] = (
        missing == ["web_ntfy_topic"])
    _, complete, missing = state(
        cfg(ntfy_server="https://ntfy.sh", ntfy_topic="alerts"), "ntfy")
    checks["…and is complete with both"] = complete and missing == []
    _, complete, _ = state(cfg(ntfy_url="https://ntfy.sh/alerts"), "ntfy")
    checks["…or with the full URL alone"] = complete

    # ── complete + on = sending; complete + off = not ────────────
    full = dict(smtp_host="smtp.example.com", smtp_from="a@b.c",
                smtp_to="d@e.f")
    n = Notifier(cfg(**full))
    checks["a complete, switched-on channel is dispatched to"] = (
        "smtp" in [p.name for p in n._configured_plugins()])
    checks["…and counts as a channel"] = n.has_channels()

    off = Notifier(cfg(channel_smtp_enabled=False, **full))
    checks["switching it off stops dispatch"] = (
        "smtp" not in [p.name for p in off._configured_plugins()])
    checks["…and it stops counting as a channel"] = not off.has_channels()
    # The distinction the page depends on: still complete, just off.
    enabled, complete, missing = state(
        cfg(channel_smtp_enabled=False, **full), "smtp")
    checks["…but it is still reported as complete, only off"] = (
        complete and not enabled and missing == [])
    # And callers that ask "will an e-mail go out?" must agree.
    checks["_smtp_configured follows the switch"] = (
        not off._smtp_configured() and n._smtp_configured())

    # An incomplete channel is not sent to whatever the switch says —
    # the switch is not a way to force a half-configured channel out.
    half = Notifier(cfg(smtp_host="smtp.example.com"))
    checks["a switch cannot activate an incomplete channel"] = (
        "smtp" not in [p.name for p in half._configured_plugins()])

    # ── isolation, for the test button ───────────────────────────
    both = cfg(gotify_url="https://gotify.example.com",
               gotify_token="tok", **full)
    iso = Notifier(both).isolated_config("gotify")
    probe = Notifier(iso)
    names = [p.name for p in probe._configured_plugins()]
    checks["isolating a channel silences the others"] = names == ["gotify"]
    checks["…every field of theirs is blanked"] = iso.smtp_host == ""
    checks["…the original config is untouched"] = (
        both.smtp_host == "smtp.example.com")
    # Testing a channel you have just switched off is a question about
    # whether it works, not about whether it is on.
    iso = Notifier(cfg(channel_gotify_enabled=False,
                       gotify_url="https://g.example.com",
                       gotify_token="tok")).isolated_config("gotify")
    checks["…and a switched-off channel can still be tested"] = (
        Notifier(iso).has_channels())
    # Quiet hours must not swallow the one message someone is waiting for.
    iso = Notifier(cfg(quiet_hours_start="00:00", quiet_hours_end="23:59",
                       gotify_url="https://g.example.com",
                       gotify_token="tok")).isolated_config("gotify")
    checks["…nor are quiet hours allowed to swallow the test"] = (
        iso.quiet_hours_start == "" and iso.quiet_hours_end == "")

    # ── every channel has declared what it owns ──────────────────
    # OWNS is what isolation blanks. A channel that forgets it would
    # keep talking during another channel's test, and the test would be
    # reporting about the wrong thing.
    for plugin in Notifier(cfg())._plugins:
        checks[f"{plugin.name} declares the fields it owns"] = bool(plugin.OWNS)

    # ── Telegram counts, even though it is not a plugin ──────────
    # It is on the same page and is a notification channel to anyone
    # reading it, but it is the bot — BOT_TOKEN and CHAT_ID, not a
    # notifier. Leaving it out produced "0 active" on an instance whose
    # Telegram notifications were working, which is the kind of
    # confidently wrong answer the state lines exist to stop. The count
    # lives in the page, so this asserts the condition it uses.
    import web_ui  # noqa: E402
    src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "web_ui.py"), encoding="utf-8").read()
    i = src.index("_tg_on = bool(")
    seg = src[i:i + 200]
    checks["the summary counts Telegram from its own two variables"] = (
        "bot_token" in seg and "chat_id" in seg)

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
