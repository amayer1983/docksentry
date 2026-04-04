#!/usr/bin/env python3
"""Docksentry - Main entry point."""

import os
import signal
import threading
import time
import sys

from config import Config
from telegram_bot import TelegramBot
from update_checker import UpdateChecker
from scheduler import Scheduler
from notifier import Notifier


def main():
    config = Config.from_env()

    if not config.bot_token or not config.chat_id:
        print("ERROR: BOT_TOKEN and CHAT_ID environment variables are required.")
        sys.exit(1)

    bot = TelegramBot(config)
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
        web = WebUI(config, checker, bot, config.web_port, config.web_password)
        web.start()

    print(f"Docksentry started.")
    print(f"Schedule: {config.cron_schedule}")
    print(f"Excluded: {config.exclude_containers or 'none'}")
    print(f"Auto selfupdate: {'ON' if config.auto_selfupdate else 'OFF'}")
    print(f"Language: {config.language}")
    if config.web_ui:
        print(f"Web UI: http://0.0.0.0:{config.web_port}")
    if config.discord_webhook:
        print(f"Discord: webhook configured")
    if config.webhook_url:
        print(f"Webhook: {config.webhook_url}")

    # Send startup notification to all channels
    from version import VERSION
    from i18n import get_translator
    t = get_translator(config.language)
    startup_msg = t("startup_message", version=VERSION)
    bot.send_message(startup_msg)
    if notifier.has_channels():
        notifier.send_message(startup_msg)

    # Start bot listener (blocking)
    bot.listen(checker, scheduler)


if __name__ == "__main__":
    main()
