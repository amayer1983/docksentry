#!/usr/bin/env python3
"""Every Telegram-bot method Discord borrows actually exists (#63).

Discord's front end reaches into the Telegram bot instance for the bits
of shared machinery that have not been pulled into a neutral core yet
(self-update, restart, announce). Nothing checked that the borrowed names
were real — so `/selfupdate` called `bot.check_selfupdate`, which never
existed, and the command just AttributeError'd. Exactly like `/changelog`
did on a different call. This scans every `bot.NAME(...)` /
`self.telegram.NAME(...)` call in the Discord front end and fails if the
name is not a real method on TelegramBot. The real fix is the extraction
that removes the borrowing entirely; until then, this keeps the borrows
honest.
"""
import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
from telegram_bot import TelegramBot  # noqa: E402

dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()

# Names called on the borrowed Telegram bot: `bot.NAME(` and
# `self.telegram.NAME(`. (Attribute reads like `bot.store` are set in
# __init__, not class methods, and are not what broke here — only CALLS.)
called = set(re.findall(r"\bbot\.([A-Za-z_]\w*)\(", dsrc))
called |= set(re.findall(r"self\.telegram\.([A-Za-z_]\w*)\(", dsrc))

checks = {}
checks["something is actually borrowed (the scan found calls)"] = bool(called)
missing = sorted(n for n in called
                 if not callable(getattr(TelegramBot, n, None)))
checks["every borrowed method exists on TelegramBot"] = not missing
if missing:
    print("  → missing on TelegramBot: " + ", ".join(missing))

# And the specific one that was broken is wired to the real method.
i = dsrc.index("def _cmd_selfupdate")
body = dsrc[i:dsrc.index("\n    def ", i + 10)]
checks["/selfupdate calls the real _handle_selfupdate"] = (
    "_handle_selfupdate" in body)
checks["…and no longer calls the phantom check_selfupdate"] = (
    "check_selfupdate(" not in body)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
