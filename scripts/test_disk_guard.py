#!/usr/bin/env python3
"""Unit test for the disk-warning in-memory re-send guard (v1.23.6).

When the disk is full, check_disk_usage() can't persist its file-based
throttle, so it returns "warn" on every pass. The scheduler's in-memory
guard must keep the notification to ~once/day even then — and re-arm once
the disk drops back below the threshold.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from scheduler import Scheduler


class Bot:
    enabled = True
    notifier = None

    def __init__(self):
        self.sent = []

    def send_message(self, msg, auto=False):
        self.sent.append(msg)


class Checker:
    """Stub: returns whatever (action, percent, free_gb) we set."""
    def __init__(self, action):
        self.action = action

    def check_disk_usage(self):
        return self.action

    def cleanup_images(self):
        return True, "noop"


class Config:
    disk_warn_auto_cleanup = False


def main():
    bot = Bot()
    chk = Checker(("warn", 95.0, 1.0))          # full disk; throttle can't persist
    s = Scheduler(Config(), chk, bot)

    # 1) Sustained full disk: many checks → exactly ONE warning
    for _ in range(6):
        s._check_disk_space()
    assert len(bot.sent) == 1, f"sustained full disk: expected 1 warning, got {len(bot.sent)}"

    # 2) After >23h, it warns again
    s._disk_warned_at -= 24 * 3600
    s._check_disk_space()
    assert len(bot.sent) == 2, f"after 24h: expected 2, got {len(bot.sent)}"

    # 3) Disk recovers → guard re-arms, no message on "ok"
    chk.action = ("ok", 40.0, 100.0)
    s._check_disk_space()
    assert s._disk_warned_at is None, "guard should re-arm on recovery"
    assert len(bot.sent) == 2, "no message expected when ok"

    # 4) Fills again → warns immediately (re-armed)
    chk.action = ("warn", 96.0, 0.5)
    s._check_disk_space()
    assert len(bot.sent) == 3, f"refill: expected 3, got {len(bot.sent)}"

    # 5) "silent" (file throttle active, write succeeded) → never sends
    chk.action = ("silent", 96.0, 0.5)
    before = len(bot.sent)
    s._check_disk_space()
    assert len(bot.sent) == before, "silent must not send"

    print("PASS: 1 warn under sustained full disk, re-warn after 23h, "
          "re-arm on recovery, silent suppressed")


if __name__ == "__main__":
    main()
