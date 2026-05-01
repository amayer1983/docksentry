#!/usr/bin/env python3
"""Docksentry - Main entry point."""

import os
import signal
import socket
import threading
import time
import sys

# Force IPv4-only by default — many containers lack IPv6 routing,
# causing "[Errno 101] Network unreachable" when Python prefers IPv6.
# Set DOCKSENTRY_IPV6=true to enable IPv6 (only if your network supports it).
if os.environ.get("DOCKSENTRY_IPV6", "false").lower() not in ("true", "1", "yes"):
    _orig_getaddrinfo = socket.getaddrinfo
    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _ipv4_only_getaddrinfo

from config import Config
from container_store import ContainerStore
from telegram_bot import TelegramBot
from update_checker import UpdateChecker
from scheduler import Scheduler
from notifier import Notifier


def main():
    config = Config.from_env()

    # Telegram is now optional. At least one notification/control channel
    # must be configured, otherwise Docksentry has no way to talk to the
    # operator (and no way to be talked to).
    telegram_partial = bool(config.bot_token) ^ bool(config.chat_id)
    if telegram_partial:
        print("ERROR: BOT_TOKEN and CHAT_ID must be set together.")
        sys.exit(1)
    telegram_on = bool(config.bot_token and config.chat_id)
    has_any_channel = (
        telegram_on
        or config.web_ui
        or config.discord_webhook
        or config.webhook_url
    )
    if not has_any_channel:
        print("ERROR: configure at least one of: BOT_TOKEN+CHAT_ID, WEB_UI=true,")
        print("       DISCORD_WEBHOOK, WEBHOOK_URL — otherwise Docksentry has")
        print("       no way to notify or be controlled.")
        sys.exit(1)

    store = ContainerStore(config)
    bot = TelegramBot(config, store)
    notifier = Notifier(config)
    bot.notifier = notifier
    checker = UpdateChecker(config)
    scheduler = Scheduler(config, checker, bot)
    web = None

    # Graceful shutdown
    def shutdown(sig, frame):
        print("Shutting down...")
        scheduler.stop()
        bot.stop()
        if web:
            web.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Start scheduler in background
    scheduler.start()

    # Start Web UI if enabled
    if config.web_ui:
        from web_ui import WebUI
        web = WebUI(config, checker, bot, store, config.web_port, config.web_password)
        web.start()

    print(f"Docksentry started.")
    print(f"Schedule: {config.cron_schedule}")
    print(f"Excluded: {config.exclude_containers or 'none'}")
    print(f"Auto selfupdate: {'ON' if config.auto_selfupdate else 'OFF'}")
    print(f"Language: {config.language}")
    print(f"Telegram: {'ON' if telegram_on else 'OFF'}")
    if config.web_ui:
        print(f"Web UI: http://0.0.0.0:{config.web_port}")
    if config.discord_webhook:
        print(f"Discord: webhook configured")
    if config.webhook_url:
        # Don't log the full URL — it can contain auth tokens (Ntfy, Gotify,
        # Home Assistant) that would otherwise leak via `docker logs` or log
        # aggregators.
        print(f"Webhook: configured")

    # Send startup notification to all channels
    from version import VERSION
    from i18n import get_translator
    t = get_translator(config.language)
    startup_msg = t("startup_message", version=VERSION)
    if bot.enabled:
        bot.send_message(startup_msg)
    if notifier.has_channels():
        notifier.send_message(startup_msg)

    # Start bot listener (blocking).
    # When Telegram is off this just blocks until shutdown — scheduler/Web UI
    # run in their own threads.
    bot.listen(checker, scheduler)


if __name__ == "__main__":
    main()
