#!/usr/bin/env python3
"""A reply goes to whoever asked; an event goes to everyone (#63).

Two failures, opposite directions, both real:

* Before the extraction, self-update reported through the Telegram bot.
  Twelve of its thirteen messages had no second recipient, so a
  /selfupdate started from Discord answered "started" and then went
  silent — @NotRetarded reported exactly that.
* The extraction overcorrected: every report went through the
  all-channel seam. Then ONE /selfupdate answered in Telegram AND
  Discord, fourteen messages over — which the owner hit on his own
  instance the same day.

So the routing is split by what a message IS, not by who is listening.
A reply belongs to the front end that started the run; an event ("an
update was found, restarting") belongs to every channel, whoever asked.
The scheduled path has no asker, so its replies fall back to the seam —
which is what it always did.
"""
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import selfupdate  # noqa: E402

checks = {}

seam, tg, dc = [], [], []
base = selfupdate.Context(engine=types.SimpleNamespace(),
                          config=types.SimpleNamespace(language="en"),
                          say=seam.append)

# ── a run started from Telegram ──────────────────────────────────────
c = base.with_reply(tg.append)
c.send_message("reply")
c.tell("event")
checks["a reply reaches the front end that asked"] = tg == ["reply"]
checks["…and nobody else"] = dc == []
checks["an event reaches the all-channel seam"] = seam == ["event"]

# ── a run started from Discord ───────────────────────────────────────
seam.clear(); tg.clear()
c = base.with_reply(dc.append)
c.send_message("reply")
c.tell("event")
checks["the same holds when Discord is the asker"] = (
    dc == ["reply"] and tg == [])
checks["…and its event still reaches every channel"] = seam == ["event"]

# ── the scheduled path has no asker ──────────────────────────────────
seam.clear(); dc.clear()
base.send_message("scheduled")
checks["with no asker, a reply falls back to the seam"] = seam == ["scheduled"]

# ── the shared context is not mutated by a run ───────────────────────
checks["with_reply does not rewire the shared context"] = (
    base._reply is base._say)

# ── source: the two front ends hand in their own reply ───────────────
tsrc = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
checks["Telegram passes its own sender as the reply"] = (
    "reply=self.send_message" in tsrc)
checks["Discord passes its own channel as the reply"] = (
    '"reply": self.announce' in dsrc)
# The restart event must NOT be a reply — it concerns everyone.
ssrc = open(os.path.join(APP, "selfupdate.py"), encoding="utf-8").read()
checks["the 'restarting' event goes through tell(), not the reply"] = (
    "ctx.tell(msg)" in ssrc)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
