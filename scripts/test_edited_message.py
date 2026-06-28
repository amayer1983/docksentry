#!/usr/bin/env python3
"""Recalled-command handling (#15, @famewolf).

Pressing ↑ in Telegram (Desktop) edits the last message instead of sending
a new one, so a recalled `/command` arrives as an `edited_message` — which
Docksentry previously didn't subscribe to or process. _message_from_update
now honours a *recent* edit (≤120s) while ignoring stale edits so an old
message edited for unrelated reasons can't silently re-run.

Pure logic. Exits non-zero on any failure.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot

pick = TelegramBot._message_from_update
NOW = 1000.0


def main():
    checks = {
        "normal message passes through":
            pick({"message": {"text": "/x"}}) == {"text": "/x"},
        "recent edit (<=120s) honoured":
            pick({"edited_message": {"text": "/x", "edit_date": 990}}, now=NOW) == {"text": "/x", "edit_date": 990},
        "stale edit (>120s) ignored":
            pick({"edited_message": {"text": "/x", "edit_date": 800}}, now=NOW) == {},
        "edit with no edit_date ignored":
            pick({"edited_message": {"text": "/x"}}, now=NOW) == {},
        "empty update -> {}":
            pick({}, now=NOW) == {},
        "message wins over a co-present edit":
            pick({"message": {"text": "/a"},
                  "edited_message": {"text": "/b", "edit_date": 990}}, now=NOW) == {"text": "/a"},
    }
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
