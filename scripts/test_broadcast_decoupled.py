#!/usr/bin/env python3
"""The all-channel seam lives in the core, not on a front end (#63).

Third step of the core extraction: `broadcast.Broadcast.announce` sends
one text to every switched-on channel, and both bots hold the same
instance. It used to be `TelegramBot.announce`, and Discord reached into
the Telegram bot instance to borrow it (`bot.announce`) — so the one seam
built to stop "unattended message reaches Telegram alone" (#57, #61) was
itself only reachable through Telegram. That coupling is what this pins
gone.

Behaviour is the seam's, not the bots': a Telegram that is switched off
gets nothing, the other channels still do, and a keyboard is Telegram's
alone.
"""
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}

from broadcast import Broadcast  # noqa: E402

checks["broadcast.Broadcast.announce exists"] = callable(
    getattr(Broadcast, "announce", None))

# ── the Telegram bot no longer owns the seam ─────────────────────────
tsrc = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
i = tsrc.index("    def announce(self")
tg_ann = tsrc[i:tsrc.index("\n    def ", i + 10)]
checks["telegram_bot's announce is an adapter, not the seam"] = (
    "notifier.send_message(" not in tg_ann
    and "has_channels()" not in tg_ann
    and "seam.announce(" in tg_ann)

# ── Discord speaks through the seam, not through the Telegram bot ────
dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
i = dsrc.index("def _cmd_testchannel")
body = dsrc[i:dsrc.index("\n    def ", i + 10)]
checks["Discord's /testchannel goes through the seam"] = (
    "seam.announce(" in body)
checks["…and stops borrowing announce from the Telegram bot"] = (
    "bot.announce" not in body and "self.telegram" not in body)

# ── main.py builds ONE seam and hands it to both ─────────────────────
msrc = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
checks["main.py builds the seam once"] = (
    msrc.count("Broadcast(telegram=bot, notifier=notifier)") == 1)
checks["…gives it to the Telegram bot"] = "bot.broadcast = broadcast" in msrc
checks["…and to every DiscordBot it constructs"] = (
    msrc.count("DiscordBot(") == msrc.count("broadcast=broadcast"))

# ── behavioural: who actually receives one announcement ──────────────
class FakeTelegram:
    def __init__(self, enabled):
        self.enabled = enabled
        self.sent = []
    def send_message(self, text, reply_markup=None, auto=False):
        self.sent.append((text, reply_markup, auto))

class FakeNotifier:
    def __init__(self, has=True, boom=False):
        self._has, self._boom, self.sent = has, boom, []
    def has_channels(self):
        return self._has
    def send_message(self, text):
        if self._boom:
            raise RuntimeError("channel is down")
        self.sent.append(text)

tg, n = FakeTelegram(True), FakeNotifier()
Broadcast(telegram=tg, notifier=n).announce("hello", reply_markup={"k": 1})
checks["both sides get the text"] = (
    len(tg.sent) == 1 and n.sent == ["hello"])
checks["…Telegram as an unattended message"] = tg.sent[0][2] is True
checks["…and the keyboard goes to Telegram alone"] = (
    tg.sent[0][1] == {"k": 1})

off, n2 = FakeTelegram(False), FakeNotifier()
Broadcast(telegram=off, notifier=n2).announce("hello")
checks["a switched-off Telegram is skipped"] = not off.sent
checks["…and the other channels still hear it"] = n2.sent == ["hello"]

tg3 = FakeTelegram(True)
logged = []
Broadcast(telegram=tg3, notifier=FakeNotifier(boom=True),
          log=logged.append).announce("hello")
checks["a throwing channel does not swallow the message"] = (
    len(tg3.sent) == 1 and len(logged) == 1)

lone = FakeNotifier()
Broadcast(telegram=None, notifier=lone).announce("hello")
checks["the seam works with no Telegram bot at all"] = lone.sent == ["hello"]

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
