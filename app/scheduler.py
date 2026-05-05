#!/usr/bin/env python3
"""Cron-like scheduler for periodic update checks."""

import threading
import time
from datetime import datetime


class Scheduler:
    def __init__(self, config, checker, bot):
        self.config = config
        self.checker = checker
        self.bot = bot
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _matches_cron(self, now):
        """Simple cron matching: minute hour day month weekday."""
        parts = self.config.cron_schedule.split()
        if len(parts) != 5:
            return False

        fields = [
            (parts[0], now.minute),
            (parts[1], now.hour),
            (parts[2], now.day),
            (parts[3], now.month),
            (parts[4], now.weekday()),  # 0=Monday in Python, but cron uses 0=Sunday
        ]

        for pattern, value in fields:
            if pattern == "*":
                continue
            # Handle range with step: "start-end/step" (e.g. "0-20/3")
            if "/" in pattern and "-" in pattern.split("/")[0]:
                range_part, step_part = pattern.split("/", 1)
                start, end = range_part.split("-")
                step = int(step_part)
                if not (int(start) <= value <= int(end)):
                    return False
                if (value - int(start)) % step != 0:
                    return False
                continue
            # Handle */n step values
            if pattern.startswith("*/"):
                step = int(pattern[2:])
                if value % step != 0:
                    return False
                continue
            # Handle comma-separated values
            if "," in pattern:
                if str(value) not in pattern.split(","):
                    return False
                continue
            # Handle ranges
            if "-" in pattern:
                start, end = pattern.split("-")
                if not (int(start) <= value <= int(end)):
                    return False
                continue
            # Exact match
            if int(pattern) != value:
                return False

        return True

    def _run(self):
        last_check = None
        print(f"Scheduler started with schedule: {self.config.cron_schedule}")

        while self.running:
            now = datetime.now()
            current_minute = now.strftime("%Y-%m-%d %H:%M")

            if current_minute != last_check and self._matches_cron(now):
                last_check = current_minute
                print(f"Scheduled check triggered at {current_minute}")
                auto_updated = 0
                try:
                    updates = self.checker.check_all()
                    if updates:
                        auto_updated = self.bot.handle_autoupdates(updates, self.checker) or 0
                    # If no updates, stay quiet (--quiet behavior)
                except Exception as e:
                    print(f"Scheduled check error: {e}")

                # Auto selfupdate after regular check
                if self.config.auto_selfupdate:
                    try:
                        self.bot.check_selfupdate_auto()
                    except Exception as e:
                        print(f"Auto selfupdate error: {e}")

                # Auto cleanup after successful auto-updates. The 24h filter
                # in cleanup_images() prevents removing images we just
                # pulled (the rollback safety-net stays intact).
                if self.config.auto_cleanup and auto_updated > 0:
                    try:
                        ok, msg = self.checker.cleanup_images()
                        print(f"Auto cleanup: {msg}")
                        notifier = getattr(self.bot, "notifier", None)
                        if ok and notifier and notifier.has_channels():
                            notifier.send_message(f"🧹 Auto cleanup: {msg}")
                        if ok and self.bot.enabled:
                            self.bot.send_message(f"🧹 {msg}")
                    except Exception as e:
                        print(f"Auto cleanup error: {e}")

            time.sleep(30)
