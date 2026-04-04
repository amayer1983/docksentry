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
                try:
                    updates = self.checker.check_all()
                    if updates:
                        self.bot.handle_autoupdates(updates, self.checker)
                    # If no updates, stay quiet (--quiet behavior)
                except Exception as e:
                    print(f"Scheduled check error: {e}")

                # Auto selfupdate after regular check
                if self.config.auto_selfupdate:
                    try:
                        self.bot.check_selfupdate_auto()
                    except Exception as e:
                        print(f"Auto selfupdate error: {e}")

            time.sleep(30)
