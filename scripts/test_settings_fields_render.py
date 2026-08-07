#!/usr/bin/env python3
"""Web UI render test for the five settings that gained a form field.

web_password, healthcheck_max_starting, docker_stop_timeout,
monitor_enabled and monitor_interval_seconds all sit in PERSISTENT_KEYS —
so save_persistent() froze them into settings.json on the first save — but
had no field in the Settings mask, leaving hand-editing the JSON as the
only way to change them. This test renders _page_settings() and asserts
each of the five now shows up in the right tab.

The password gets extra scrutiny: it is a secret, so the field must never
render the stored value back into value= (that would leak it to anyone who
can view-source the page). A known password is put into the fake config and
the test asserts it is absent from the rendered HTML.

Same shape as scripts/test_web_selfupdate_row.py: the handler class is
built via create_handler() and instantiated without a socket (__new__),
with _render_page / the sub-panels stubbed so no store or Docker is needed.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402

MISSING = "/nonexistent/docksentry-test"

# A password value we can hunt for in the output. If it appears anywhere in
# the HTML, the field leaked it.
SECRET_PW = "SUPERSECRETpw-should-never-render-987"

# Same for the Discord bot token, which moved into this form in v2.3.0.
SECRET_DISCORD_TOKEN = "MTIzNDU2.SECRETdiscord-should-never-render-654"


def _config():
    return types.SimpleNamespace(
        language="en",
        ui_mode="advanced",
        bot_token="123456:ABCDEF-token-abcdefghijklmnop",
        chat_id="1234567890",
        data_dir="/data",
        exclude_containers=[],
        cron_schedule="0 18 * * *",
        debug=False,
        auto_selfupdate=False,
        healthcheck_max_starting=600,
        docker_stop_timeout=60,
        auto_cleanup=False,
        cleanup_backup_local_only=False,
        cleanup_grace_hours=24,
        cleanup_backup_days=7,
        cleanup_backup_dir="/data/backups",
        disk_warn_percent=85,
        disk_warn_auto_cleanup=False,
        quiet_hours_start="",
        quiet_hours_end="",
        weekly_report_enabled=False,
        weekly_report_weekday=0,
        weekly_report_hour=9,
        monitor_enabled=True,
        monitor_interval_seconds=60,
        telegram_topic_id="",
        telegram_allowed_users=[],
        bot_label="",
        discord_webhook="",
        webhook_url="",
        # The interactive Discord bot (#57). The token is the second
        # secret on this page and gets the same scrutiny as the
        # password below — it is set to a value the test hunts for.
        discord_bot_token=SECRET_DISCORD_TOKEN,
        discord_app_id="1234567890123456789",
        discord_guild_id="9876543210987654321",
        discord_allowed_users=[],
        web_password=SECRET_PW,
        # env_() calls this for every field — no env overrides in the test.
        env_override=lambda key: None,
    )


def render(page="settings"):
    """Render one page's HTML. `page` is "settings" or "connections".

    The channels moved to a page of their own when the Discord bot
    arrived (#57), so this file now renders both — the token is a secret
    and the check that it never reaches the HTML has to follow it.
    """
    cfg = _config()
    handler_cls = web_ui.create_handler(cfg, checker=None, bot=None, store=None)
    h = handler_cls.__new__(handler_cls)
    out = {}
    h._send_html = lambda html, status=200: out.update(html=html)
    # Bypass the chrome and the sub-panels: they need a real store /
    # maintenance file and are not what this test is about.
    h._render_page = lambda content, active=None: content
    h._windows_html = lambda t: ""
    h._maint_mode_html = lambda t: ""
    getattr(h, f"_page_{page}")()
    return out.get("html", "")


def pane(html, name):
    """Return the tab-pane block for `data-tab-name="<name>"`."""
    marker = f'data-tab-name="{name}"'
    i = html.find(marker)
    if i < 0:
        return ""
    # Cut at the next pane marker (or end) — good enough to keep fields in
    # their own tab for the assertions below.
    nxt = html.find('data-tab-name="', i + len(marker))
    return html[i:nxt if nxt > 0 else len(html)]


def main():
    checks = {}
    html = render()

    # ── The password must never leak, in any form ────────────────────
    checks["password value is NOT rendered anywhere in the HTML"] = (
        SECRET_PW not in html)
    general = pane(html, "general")
    checks["web_password field is in the General tab"] = (
        'name="web_password"' in general)
    checks["web_password field is type=password"] = (
        'type="password" name="web_password"' in general
        or 'name="web_password"' in general and 'type="password"' in general)
    checks["web_password field renders empty (value=\"\")"] = (
        'name="web_password" value=""' in general)
    checks["web_password field has a placeholder"] = (
        'placeholder="leave empty = unchanged"' in general)

    # ── Updates tab: the two timeouts ────────────────────────────────
    updates = pane(html, "updates")
    checks["healthcheck_max_starting field is in the Updates tab"] = (
        'name="healthcheck_max_starting"' in updates)
    checks["healthcheck field shows the current value"] = (
        'name="healthcheck_max_starting" value="600"' in updates)
    checks["docker_stop_timeout field is in the Updates tab"] = (
        'name="docker_stop_timeout"' in updates)
    checks["docker_stop_timeout field shows the current value"] = (
        'name="docker_stop_timeout" value="60"' in updates)

    # ── Notifications tab: the Monitoring group ──────────────────────
    notifs = pane(html, "notifs")
    checks["Monitoring heading is in the Notifications tab"] = (
        ">Monitoring</h3>" in notifs)
    checks["monitor_enabled checkbox is in the Notifications tab"] = (
        'name="monitor_enabled"' in notifs)
    checks["monitor_enabled reflects the config (checked)"] = (
        'name="monitor_enabled" id="cb-monitor" checked' in notifs)
    checks["monitor_interval_seconds field is in the Notifications tab"] = (
        'name="monitor_interval_seconds"' in notifs)
    checks["monitor_interval shows the current value with a 15s floor"] = (
        'name="monitor_interval_seconds" value="60"' in notifs
        and 'min="15"' in notifs)

    # ── Connections page: the interactive Discord bot (#57) ──────────
    # @NotRetarded wrote a screenshot-by-screenshot guide for setting the
    # bot up from environment variables, then asked whether it could just
    # be in the interface. These are the fields that answer that. They
    # sat on the Settings page's Channels tab for one release and then
    # moved, with the rest of the channels, to a page of their own.
    channels = render("connections")
    checks["Discord bot token field is on the Connections page"] = (
        'name="discord_bot_token"' in channels)
    # Both secrets, checked against both pages. A secret that does not
    # leak on the page that owns it can still leak on the other one —
    # and the token was on the Settings page one release ago.
    checks["bot token value is NOT rendered on either page"] = (
        SECRET_DISCORD_TOKEN not in channels
        and SECRET_DISCORD_TOKEN not in html)
    checks["password value is NOT rendered on either page"] = (
        SECRET_PW not in channels and SECRET_PW not in html)
    checks["bot token field is type=password and renders empty"] = (
        'type="password" name="discord_bot_token" value=""' in channels)
    checks["a saved token is signalled by the placeholder, not the value"] = (
        "leave empty = unchanged" in channels)
    checks["a saved token offers an explicit way to remove it"] = (
        'name="discord_bot_token_clear"' in channels)
    checks["application id is on the Connections page, with its value"] = (
        'name="discord_app_id" value="1234567890123456789"' in channels)
    checks["server id is on the Connections page, with its value"] = (
        'name="discord_guild_id" value="9876543210987654321"' in channels)
    checks["the allowed-user list is on the Connections page"] = (
        'name="discord_allowed_users"' in channels)
    # Every one of them has to reach the (empty) settings form by id —
    # the trap test_form_nesting.py exists for.
    _dc_fields = ("discord_bot_token", "discord_app_id",
                  "discord_guild_id", "discord_allowed_users")
    checks["every Discord field associates with the connections form"] = all(
        f'name="{f}"' in channels
        and 'form="conn-form"' in channels[channels.index(f'name="{f}"'):
                                               channels.index(f'name="{f}"') + 400]
        for f in _dc_fields)

    # ── None of the five leaked into a different tab ─────────────────
    checks["password field is not on the Updates tab"] = (
        'name="web_password"' not in updates)
    checks["monitor fields are not on the Updates tab"] = (
        'name="monitor_enabled"' not in updates)

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
