#!/usr/bin/env python3
"""A finished update is reported once per channel, not twice.

@NotRetarded's Discord showed, for one auto-update of two containers:
an "Auto-updating 2 container(s)" line, then an `Update OK` card for
each container, then "Auto-update complete: 2 updated" repeating both
results. Telegram showed it once and looked right.

The two halves each had a reason. Every non-Telegram channel is a
notifier plugin, so a batch calls `send_update_result` for each
container and they get a card apiece. Telegram is not a plugin and gets
none of those, so in v2.6.0 the summary grew the full result list —
@LeeNX (#56) had "how did it go" spread across several messages. That
list then went out over `announce`, which reaches every channel.

So the fix is not to drop one of them: it is that the list belongs only
where the cards do not. Each channel keeps its richest form and says it
once.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# On this line `announce` still lives on the bot; the core refactor
# moves the seam into its own module. What it has to do is the same.
from telegram_bot import TelegramBot                        # noqa: E402

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")


class _Notifier:
    def __init__(self):
        self.sent = []

    def has_channels(self):
        return True

    def send_message(self, text):
        self.sent.append(text)


class _Bot:
    """Just enough of the bot for `announce` to run against."""
    enabled = True

    def __init__(self):
        self.sent, self.notifier = [], _Notifier()

    def send_message(self, text, **k):
        self.sent.append(text)

    announce = TelegramBot.announce


tg = _Bot()
nf = tg.notifier
tg.announce("⚡ Auto-update complete: 2 updated",
            detail="\n\n✅ beszel-agent: OK\n✅ Beszel: OK")

checks["Telegram gets the head and the results"] = (
    len(tg.sent) == 1 and "complete: 2 updated" in tg.sent[0]
    and "beszel-agent" in tg.sent[0] and "Beszel: OK" in tg.sent[0])
checks["the other channels get the head"] = (
    len(nf.sent) == 1 and "complete: 2 updated" in nf.sent[0])
checks["…and not the results they already had one by one"] = (
    "beszel-agent" not in nf.sent[0])

# Without a detail nothing changes for anyone — every other announcement
# in the program still reaches both sides identically.
tg2 = _Bot()
nf2 = tg2.notifier
tg2.announce("⚡ Auto-updating 2: a, b")
checks["a plain announcement still reaches both alike"] = (
    tg2.sent == nf2.sent == ["⚡ Auto-updating 2: a, b"])

# ── only the one caller that announces to everyone splits ────────────
# Enumerated rather than grepped: five places run a batch, and four of
# them report through a Telegram-only send or a command reply, so their
# per-container cards were never a duplicate.
src = open(os.path.join(ROOT, "app", "telegram_bot.py"),
           encoding="utf-8").read()
_auto = src.split("def handle_autoupdates")[1].split("\n    def ")[0]
checks["the auto-update summary splits head from results"] = (
    'self.announce(_head, detail=' in _auto)
checks["…and no longer glues them together for everyone"] = (
    'self.announce(_head + "\\n\\n"' not in src)

for name, where in (("run_updates", src),
                    ("_run_web_update_batch",
                     open(os.path.join(ROOT, "app", "web_ui.py"),
                          encoding="utf-8").read())):
    body = where.split(f"def {name}")[1].split("\n        def ")[0][:4000]
    checks[f"{name} still reports to Telegram alone"] = (
        "self.announce(" not in body and "bot.announce(" not in body)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
