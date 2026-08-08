#!/usr/bin/env python3
"""Saving a settings form: clearing a field, and the Discord bot.

Two things, one of which had been broken since the settings page existed.

Both forms are covered — Settings and the Connections page the notification
channels moved to. They share the shape (validate, write once, then act) and
they shared the bug.

**No text field on the settings page could be emptied.** `parse_qs` drops
`name=` with an empty value unless you ask it not to, and every branch in
the save handler is guarded by `if "x" in params` — so the one submission
that means "clear this" was the one submission that looked like the field
had not been sent at all. The page redirected to `?saved=1`, the field came
back filled, and nothing said why. Measured against a running instance:

    POST discord_webhook=https://discord.com/api/webhooks/123/abc  -> saved
    POST discord_webhook=                                          -> saved
    settings.json: "discord_webhook": "https://discord.com/api/webhooks/123/abc"

The same for the generic webhook URL, the Telegram topic id, both
allowed-user lists, the bot label and the quiet hours. Found while adding
the Discord bot fields, because a server id that cannot be cleared is a bot
that cannot be pointed at a different server.

**The Discord bot moved into the form (#57, @NotRetarded).** He wrote a
screenshot-by-screenshot guide for setting it up through environment
variables, then asked whether it could just be in the interface. Three
properties matter and are asserted here: the token behaves like the
password (empty means unchanged, never "clear"), clearing it is a separate
deliberate act, and the bot is only restarted when one of its own fields
actually changed — a save on the Cleanup tab must not bounce a working bot.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402


class FakeConfig(types.SimpleNamespace):
    """Enough config for the save path, plus a record of what was saved."""

    def __init__(self, **kw):
        base = dict(
            language="en", cron_schedule="0 18 * * *", exclude_containers=[],
            debug=False, auto_selfupdate=False, auto_cleanup=False,
            cleanup_backup_local_only=False, cleanup_grace_hours=24,
            cleanup_backup_days=7, disk_warn_percent=85,
            disk_warn_auto_cleanup=False, quiet_hours_start="",
            quiet_hours_end="", weekly_report_enabled=False,
            weekly_report_weekday=0, weekly_report_hour=9,
            monitor_enabled=True, monitor_interval_seconds=60,
            healthcheck_max_starting=600, docker_stop_timeout=60,
            telegram_topic_id="", telegram_allowed_users=[], bot_label="",
            discord_webhook="", webhook_url="", web_password="",
            discord_bot_token="", discord_app_id="", discord_guild_id="",
            discord_allowed_users=[], ui_mode="advanced",
            smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
            smtp_from="", smtp_to="", smtp_tls="starttls",
            smtp_tls_verify=True,
            ntfy_url="", ntfy_server="", ntfy_topic="", ntfy_token="",
            ntfy_user="", ntfy_password="",
            gotify_url="", gotify_token="",
            matrix_homeserver="", matrix_room="", matrix_token="",
            apprise_url="", apprise_urls="", apprise_tag="",
            saves=0,
        )
        base.update(kw)
        super().__init__(**base)

    def save_persistent(self):
        self.saves += 1


def post(cfg, body, restart=None, path="/connections"):
    """Drive do_POST for `path` and return the redirect target.

    Defaults to /connections: the channels moved off the Settings page
    when the Discord bot arrived, and the clearing bug this file exists
    for is theirs. Pass path="/settings" for the fields that stayed.
    """
    handler_cls = web_ui.create_handler(
        cfg, checker=None, bot=types.SimpleNamespace(t=None),
        store=None, restart_discord=restart)
    h = handler_cls.__new__(handler_cls)
    h.path = path
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x"}
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode())
    h.client_address = ("127.0.0.1", 0)
    out = {}
    h._send_redirect = lambda loc: out.update(loc=loc)
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    h._page_settings = lambda: None
    h._page_connections = lambda: None
    h.do_POST()
    return out.get("loc", "")


def main():
    checks = {}

    # ── an empty field clears the value ──────────────────────────
    cfg = FakeConfig(discord_webhook="https://discord.com/api/webhooks/1/a",
                     webhook_url="https://example.invalid/hook",
                     telegram_topic_id="42", bot_label="prod",
                     telegram_allowed_users=["1", "2"])
    post(cfg, "discord_webhook=&webhook_url=&telegram_topic_id="
              "&bot_label=&telegram_allowed_users=")
    checks["an emptied Discord webhook is cleared"] = cfg.discord_webhook == ""
    checks["an emptied webhook URL is cleared"] = cfg.webhook_url == ""
    checks["an emptied topic id is cleared"] = cfg.telegram_topic_id == ""
    checks["an emptied bot label is cleared"] = cfg.bot_label == ""
    checks["an emptied allowed-user list is cleared"] = (
        cfg.telegram_allowed_users == [])
    checks["and the save was actually written"] = cfg.saves == 1

    # A field that is not in the submission at all is still untouched —
    # keep_blank_values must not turn "absent" into "empty".
    cfg = FakeConfig(bot_label="prod")
    post(cfg, "discord_webhook=")
    checks["a field that was not submitted keeps its value"] = (
        cfg.bot_label == "prod")

    # ── the token behaves like the password ──────────────────────
    cfg = FakeConfig(discord_bot_token="KEEP.ME", discord_app_id="1")
    post(cfg, "discord_bot_token=&discord_app_id=1")
    checks["an empty token field means unchanged, not cleared"] = (
        cfg.discord_bot_token == "KEEP.ME")
    post(cfg, "discord_bot_token=NEW.TOKEN&discord_app_id=1")
    checks["a submitted token replaces the old one"] = (
        cfg.discord_bot_token == "NEW.TOKEN")
    post(cfg, "discord_bot_token=&discord_bot_token_clear=on&discord_app_id=1")
    checks["the clear checkbox is what removes it"] = (
        cfg.discord_bot_token == "")

    # ── e-mail ──────────────────────────────────────────────────
    cfg = FakeConfig(smtp_password="KEEP.ME", smtp_host="old.example.com")
    post(cfg, "conn_page=1&smtp_host=smtp.example.com&smtp_port=465"
              "&smtp_tls=ssl&smtp_from=a@b.c&smtp_to=d@e.f"
              "&smtp_user=u&smtp_password=&smtp_tls_verify=on")
    checks["the SMTP server is saved"] = cfg.smtp_host == "smtp.example.com"
    checks["the port is saved as a number"] = cfg.smtp_port == 465
    checks["the encryption choice is saved"] = cfg.smtp_tls == "ssl"
    checks["an empty SMTP password means unchanged"] = (
        cfg.smtp_password == "KEEP.ME")
    post(cfg, "conn_page=1&smtp_password=&smtp_password_clear=on")
    checks["its clear checkbox is what removes it"] = cfg.smtp_password == ""

    # A nonsense transport is ignored rather than stored — smtp.py
    # branches on exactly three values and anything else would silently
    # fall through to no encryption at all.
    post(cfg, "conn_page=1&smtp_tls=plaintext-please")
    checks["an unknown encryption value is refused"] = cfg.smtp_tls == "ssl"
    post(cfg, "conn_page=1&smtp_port=not-a-number")
    checks["a non-numeric port is ignored, not zeroed"] = cfg.smtp_port == 465

    # Certificate verification decides whether the password is handed to
    # whatever answers. An unchecked box submits nothing, so it may only
    # be read as "off" for a submission that really came from this form.
    cfg = FakeConfig(smtp_tls_verify=True)
    post(cfg, "conn_page=1&smtp_host=x")
    checks["unticking verification on this form turns it off"] = (
        cfg.smtp_tls_verify is False)
    cfg = FakeConfig(smtp_tls_verify=True)
    post(cfg, "smtp_host=x")
    checks["a POST without the form marker cannot turn it off"] = (
        cfg.smtp_tls_verify is True)

    # ── ntfy / Gotify / Matrix / Apprise ────────────────────────
    # Nine plain values and five credentials, all handled by two loops.
    # The credentials follow the same rule as every other secret here.
    cfg = FakeConfig(ntfy_token="KEEP", matrix_token="KEEP",
                     apprise_urls="discord://keep")
    post(cfg, "conn_page=1&ntfy_server=https://ntfy.sh&ntfy_topic=alerts"
              "&gotify_url=https://gotify.example.com"
              "&matrix_homeserver=https://matrix.org&matrix_room=%23a:b.c"
              "&apprise_url=http://apprise:8000/notify"
              "&ntfy_token=&matrix_token=&apprise_urls=")
    checks["ntfy server and topic are saved"] = (
        cfg.ntfy_server == "https://ntfy.sh" and cfg.ntfy_topic == "alerts")
    checks["the Gotify URL is saved"] = (
        cfg.gotify_url == "https://gotify.example.com")
    checks["the Matrix room is saved"] = cfg.matrix_room == "#a:b.c"
    checks["empty credentials mean unchanged, for all of them"] = (
        cfg.ntfy_token == "KEEP" and cfg.matrix_token == "KEEP"
        and cfg.apprise_urls == "discord://keep")

    post(cfg, "conn_page=1&ntfy_token=new-token")
    checks["a submitted credential replaces the old one"] = (
        cfg.ntfy_token == "new-token")
    post(cfg, "conn_page=1&ntfy_token=&ntfy_token_clear=on"
              "&apprise_urls=&apprise_urls_clear=on")
    checks["and each has its own clear checkbox"] = (
        cfg.ntfy_token == "" and cfg.apprise_urls == ""
        and cfg.matrix_token == "KEEP")

    # An emptied plain field still clears, exactly like the others.
    post(cfg, "conn_page=1&ntfy_topic=&matrix_room=")
    checks["an emptied plain plugin field is cleared"] = (
        cfg.ntfy_topic == "" and cfg.matrix_room == "")

    # ── the bot is restarted only when its own fields change ─────
    calls = []

    def restart():
        calls.append(1)
        return True, "", ""

    cfg = FakeConfig(discord_bot_token="T", discord_app_id="1",
                     discord_guild_id="2")
    loc = post(cfg, "cleanup_grace_hours=48", restart=restart,
               path="/settings")
    checks["saving another tab does not restart the bot"] = not calls
    checks["…and says nothing about Discord"] = "discord=" not in loc

    loc = post(cfg, "discord_guild_id=3&discord_app_id=1", restart=restart)
    checks["changing the server id restarts the bot"] = len(calls) == 1
    checks["a clean start is reported"] = "discord=ok" in loc

    # Re-submitting the identical values is not a change.
    loc = post(cfg, "discord_guild_id=3&discord_app_id=1", restart=restart)
    checks["re-saving the same values does not restart it"] = len(calls) == 1

    # ── a failure reaches the page, with the reason ──────────────
    def reject():
        return False, "token", 'HTTP 401: {"message": "401: Unauthorized"}'

    cfg = FakeConfig(discord_app_id="1", discord_guild_id="2")
    loc = post(cfg, "discord_bot_token=bad&discord_app_id=1"
                    "&discord_guild_id=2", restart=reject)
    checks["a rejected token is reported as such"] = "discord=token" in loc
    checks["…with what Discord actually said"] = "401" in loc
    checks["…and the value is still saved, so it can be corrected"] = (
        cfg.discord_bot_token == "bad")
    checks["the reason is URL-encoded, not raw"] = (
        " " not in loc and '"' not in loc)

    # No callback at all (a handler built outside main): say so rather
    # than silently doing nothing.
    cfg = FakeConfig()
    loc = post(cfg, "discord_bot_token=x&discord_app_id=1")
    checks["with no restart hook the page asks for a restart"] = (
        "discord=restart_needed" in loc)

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
