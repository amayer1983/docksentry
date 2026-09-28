#!/usr/bin/env python3
"""An unattended message goes through the seam, or it goes wrong.

A survey on 25.09.2026 counted every site in `app/` that says something
nobody asked for: 25 of them, of which three went through `Broadcast`.
The rest each picked a door by hand, and the picking is where the bugs
were. This file pins the ones that were fixed.

The worst was not a missing message but a switch that did not switch:
`DiscordBot.announce` never read `channel_discordbot_enabled`, and the
retry queue delivered messages up to fifteen minutes after the channel
had been turned off — because what it holds is the raw transport, which
has to walk past the switch to work at all.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import notify_retry                                          # noqa: E402
from broadcast import Broadcast                              # noqa: E402

checks = {}


# ── off means off, on every channel that has a switch ────────────────
class _Rest:
    def __init__(self):
        self.posts = []

    def create_message(self, channel, text, **kw):
        self.posts.append((channel, text))
        return {"id": "1"}


from discord_bot import DiscordBot                           # noqa: E402

_cfg = types.SimpleNamespace(discord_bot_channel="123",
                             channel_discordbot_enabled=True,
                             language="en")
_bot = DiscordBot.__new__(DiscordBot)
_bot.config, _bot.rest, _bot.log = _cfg, _Rest(), lambda *_a: None
_bot.announce("bot started")
checks["a switched-on Discord channel is announced to"] = (
    len(_bot.rest.posts) == 1)

_cfg.channel_discordbot_enabled = False
_bot.announce("bot started again")
checks["…and a switched-off one is not"] = len(_bot.rest.posts) == 1


# ── the retry queue asks again before it delivers ────────────────────
_delivered = []
_on = {"v": True}
notify_retry.remember("probe", "held while the network was down",
                      lambda t: (_delivered.append(t), True)[1],
                      still_wanted=lambda: _on["v"])
_on["v"] = False
notify_retry.flush()
checks["a message held for a channel that was switched off is dropped"] = (
    _delivered == [])

_on["v"] = True
notify_retry.remember("probe", "second", lambda t: (_delivered.append(t), True)[1],
                      still_wanted=lambda: _on["v"])
notify_retry.flush()
checks["…while one for a channel still on is delivered"] = (
    len(_delivered) == 1 and _delivered[0].endswith("second"))

# Every plugin that queues its own transport passes the question along —
# counted, because one that forgets is invisible until somebody turns a
# channel off and keeps receiving.
_plugins = ("discord", "webhook", "gotify", "apprise", "ntfy", "matrix", "smtp")
_missing = []
for _p in _plugins:
    _src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                             "notifiers", f"{_p}.py"), encoding="utf-8").read()
    if "notify_retry.remember" in _src and "still_wanted=self.active" not in _src:
        _missing.append(_p)
checks["every plugin that queues a message asks again before resending"] = (
    not _missing)
if _missing:
    print("      → ohne Nachfrage: " + ", ".join(_missing))


# ── quiet hours silence the routine, not the urgent ──────────────────
class _TG:
    enabled = True

    def __init__(self):
        self.sent = []

    def send_message(self, text, reply_markup=None, auto=False, **kw):
        self.sent.append((text, auto))


class _Plugins:
    def __init__(self, quiet):
        self.sent, self._quiet = [], quiet

    def has_channels(self):
        return True

    def send_message(self, text, skip=(), urgent=False):
        if not urgent and self._quiet:
            return
        self.sent.append(text)


for _quiet in (False, True):
    _tg, _pl = _TG(), _Plugins(_quiet)
    _seam = Broadcast(telegram=_tg, notifier=_pl)
    _seam.tell("the host stopped answering")
    _seam.announce("an update is available")
    _label = "during quiet hours" if _quiet else "outside quiet hours"
    checks[f"{_label}: an urgent report reaches the plugins"] = (
        "the host stopped answering" in _pl.sent)
    checks[f"{_label}: …and Telegram"] = any(
        "stopped answering" in t for t, _a in _tg.sent)
    checks[f"{_label}: a routine one is marked auto for Telegram"] = any(
        a for t, a in _tg.sent if "update is available" in t)

# Was `"…" not in _Plugins(True).sent` — a fresh stand-in that had
# never been sent anything, so the check read an empty list it had just
# built. It has to be the instance that went through the quiet round.
checks["routine traffic is still silenced on the plugins at night"] = (
    _pl.sent == ["the host stopped answering"])


# ── the engine reports through the seam, not around it ───────────────
import update_engine                                          # noqa: E402

class _Seam:
    def __init__(self):
        self.results = []

    def update_result(self, name, image, success, detail="", source_url=""):
        self.results.append((name, success))


_eng = types.SimpleNamespace(broadcast=_Seam(), notifier=None)
update_engine._report_result(_eng, "web", "nginx:1", True, "done")
checks["a finished container is reported through the seam"] = (
    _eng.broadcast.results == [("web", True)])

# …and an engine nobody wired a seam into still reports.
class _Note:
    def __init__(self):
        self.results = []

    def send_update_result(self, name, image, success, detail="", source_url=""):
        self.results.append((name, success))


_bare = types.SimpleNamespace(broadcast=None, notifier=_Note())
update_engine._report_result(_bare, "db", "postgres:17", False, "boom")
checks["…and one built without a seam falls back rather than raising"] = (
    _bare.notifier.results == [("db", False)])


# ── a message may not promise what the channel will not get ──────────
import json                                                   # noqa: E402

_en = json.load(open(os.path.join(os.path.dirname(__file__), "..", "app",
                                  "lang", "en.json"), encoding="utf-8"))
checks["the mass-stop footer for Telegram mentions the attachment"] = (
    "attached" in _en["monitor_mass_footer"])
checks["…and the one for every other channel does not"] = (
    "attach" not in _en["monitor_mass_footer_nofile"].lower())
_langs = [f for f in os.listdir(os.path.join(os.path.dirname(__file__), "..",
                                             "app", "lang"))
          if f.endswith(".json")]
checks["…in all 16 languages"] = len(_langs) == 16 and all(
    "monitor_mass_footer_nofile" in json.load(
        open(os.path.join(os.path.dirname(__file__), "..", "app", "lang", f),
             encoding="utf-8"))
    for f in _langs)

_mon = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "monitor.py"), encoding="utf-8").read()
checks["…and the monitor sends the right one to each"] = (
    "self.bot.send_message(msg, auto=True)" in _mon
    and "notifier.send_message(msg_nofile)" in _mon)


# ── the four notices that used to be Telegram's alone ────────────────
_tgsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                           "telegram_bot.py"), encoding="utf-8").read()
for _key in ("window_skipped", "policy_held", "age_held",
             "major_confirm_prompt"):
    _i = _tgsrc.index(f'self.t("{_key}"')
    _before = _tgsrc[max(0, _i - 200):_i]
    checks[f"'{_key}' goes out through the seam"] = (
        "self.announce(" in _before and "send_message(" not in
        _before.split("self.announce(")[-1])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
