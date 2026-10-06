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
# Everything, not only unprompted notices. For a week this had an
# exception for command replies, justified with the same sentence
# Telegram uses for the opposite rule. @NotRetarded settled it on his
# own install: "I expected silence everywhere." The answer to a command
# is not lost by it — it goes back to the interaction instead.
# Driven, not read. The check that stood here asked whether `"reply="`
# was among the parameter NAMES — it never is, the name would be
# `reply` — so it was true no matter what the method looked like, and
# its companion was satisfied by the docstring quoting the rule. An
# auditor restored the exception in full and both stayed green.
import inspect as _inspect                                   # noqa: E402
_sw = DiscordBot.__new__(DiscordBot)
_sw.config = types.SimpleNamespace(discord_bot_channel="9",
                                   channel_discordbot_enabled=False,
                                   language="en")
_sw.rest, _sw.log = _Rest(), lambda *_a: None
for _kw in ({}, {"reply": True}):
    try:
        _sw.announce("anything", **_kw)
    except TypeError:
        pass            # the parameter is gone, which is the point
# …and it may not come back under another name either: the signature is
# `(self, text)`, nothing more. Swallowing the TypeError alone would let
# `is_reply=False` reintroduce the whole exception unnoticed.
checks["…with no exception for replies"] = (
    _sw.rest.posts == []
    and list(_inspect.signature(DiscordBot.announce).parameters) == ["self",
                                                                     "text"])
# (Six source greps stood here. They are the harness above now.)


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
_ndir = os.path.join(os.path.dirname(__file__), "..", "app", "notifiers")
_plugins = sorted(f[:-3] for f in os.listdir(_ndir)
                  if f.endswith(".py") and f != "__init__.py" and f != "base.py")
_missing = []
for _p in _plugins:
    _src = open(os.path.join(_ndir, f"{_p}.py"), encoding="utf-8").read()
    if "notify_retry.remember" in _src and "still_wanted=" not in _src:
        _missing.append(_p)
checks["every plugin that queues a message asks again before resending"] = (
    not _missing and len(_plugins) >= 8)
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


# The REAL gate, not a stand-in that reimplements it. Both existing
# checks used a fake whose own `if not urgent and quiet: return` was the
# thing under test — so deleting `not urgent and` from app/notifier.py
# changed nothing anywhere in the suite.
import notifier as _nmod                                     # noqa: E402

class _Live(_nmod.Notifier):
    """A real Notifier with one plugin that records, and a quiet clock."""

    def __init__(self, quiet):
        self.seen = []
        self._quiet = quiet
        self.config = types.SimpleNamespace(language="en")

    def _suppressed(self):
        return self._quiet

    def has_channels(self):
        return True

    def _dispatch(self, method, *a, **kw):
        self.seen.append(a[0] if a else "")


_night = _Live(quiet=True)
_nmod.Notifier.send_message(_night, "routine: an update is available")
checks["the real gate silences routine traffic at night"] = _night.seen == []
_nmod.Notifier.send_message(_night, "urgent: the host stopped answering",
                            urgent=True)
checks["…and lets the urgent class through"] = (
    _night.seen == ["urgent: the host stopped answering"])

_day = _Live(quiet=False)
_nmod.Notifier.send_message(_day, "routine, in daylight")
checks["…while by day the routine class goes out as before"] = (
    _day.seen == ["routine, in daylight"])


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
# Three of the four go through `_say_once`, which is `announce` with a
# "have I already said this" in front of it — they are recomputed on
# every check run, so without it a closed maintenance window repeated
# the same sentence to every channel on every tick.
for _key, _door in (("window_skipped", "_say_once("),
                    ("policy_held", "_say_once("),
                    ("age_held", "_say_once("),
                    ("major_confirm_prompt", "self.announce(")):
    _i = _tgsrc.index(f'self.t("{_key}"')
    _before = _tgsrc[max(0, _i - 260):_i]
    checks[f"'{_key}' goes out through the seam"] = _door in _before
checks["…and _say_once is the seam with a memory in front"] = (
    "_held_seen[kind] = signature" in _tgsrc
    and "self.announce(text, **kw)" in _tgsrc)

# ── /selfupdate, driven ──────────────────────────────────────────────
# Six checks used to grep `_cmd_selfupdate` for `self.rest.followup(` and
# for an index ordering. An auditor made `followup` unreachable dead
# code and put `self.announce(text)` first in the private closure — the
# #63 leak, restored — and every one of them stayed green. So the
# command is run here instead, in the four combinations that matter.
import selfupdate as _su                                     # noqa: E402


class _RecRest:
    def __init__(self):
        self.followups, self.posts = [], []

    def followup(self, app_id, token, content, *, ephemeral=True):
        self.followups.append((content, ephemeral))
        return {"id": "1"}

    def create_message(self, channel, text, **kw):
        self.posts.append((channel, text))
        return {"id": "2"}


def _run_selfupdate(private, channel_on, dm_ok=True):
    """`(followups, channel_posts, dms)` for one /selfupdate."""
    dms = []
    bot = DiscordBot.__new__(DiscordBot)
    bot.config = types.SimpleNamespace(
        discord_bot_channel="chan", channel_discordbot_enabled=channel_on,
        discord_public_replies=not private, language="en",
        discord_bot_token="t")
    bot.rest, bot.application_id = _RecRest(), "app"
    bot.log = lambda *_a: None
    bot.selfupdate_ctx = object()
    # `t` is a property on the class, so the language has to come from
    # the config the real translator reads.
    bot._replies_private = lambda: private
    bot._invoker = lambda _d: "u1"
    bot._clip = lambda t: t

    import discord_bot as _db
    _start, _send = _su.start, _db.send_private
    _su.start = lambda ctx, target=None, reply=None, reply_to=None: reply(
        "checking localhost:5000/docksentry:test")

    def _fake_dm(cfg, uid, text, log=print):
        dms.append(text)
        return dm_ok

    _db.send_private = _fake_dm
    try:
        bot._cmd_selfupdate({}, data={"token": "tok", "channel_id": "typed"})
    finally:
        _su.start, _db.send_private = _start, _send
    return bot.rest.followups, bot.rest.posts, dms


# Indexing into these lists would turn a broken guard into a crash
# instead of a named failure, so every check reads them defensively.
def _one(seq):
    """The single entry, or a placeholder that fails every comparison."""
    return seq[0] if len(seq) == 1 else (None, None)


_f, _p, _d = _run_selfupdate(private=False, channel_on=True)
checks["a public /selfupdate answers on the interaction"] = (
    len(_f) == 1 and "checking localhost" in (_one(_f)[0] or ""))
checks["…publicly, because the operator asked for public replies"] = (
    _one(_f)[1] is False)
checks["…and not in the configured channel"] = _p == []

_f, _p, _d = _run_selfupdate(private=True, channel_on=True)
checks["a private one takes the direct message"] = len(_d) == 1
checks["…and says nothing on the interaction or in the channel"] = (
    _f == [] and _p == [])

_f, _p, _d = _run_selfupdate(private=True, channel_on=True, dm_ok=False)
checks["a DM that will not open falls back to the interaction"] = (
    len(_d) == 1 and len(_f) == 1)
checks["…ephemerally, because the request was private"] = (
    _one(_f)[1] is True)

_f, _p, _d = _run_selfupdate(private=False, channel_on=False)
checks["with the channel off a public request still answers the asker"] = (
    len(_f) == 1)

# ── the hand-rolled fan-outs, and the ones that stay ─────────────────
# Each pair was `bot.send_message(...)` followed by
# `notifier.send_message(...)`, kept in step by hand — the shape behind
# #57 and #61. The claim that every same-text pair is gone was wrong
# when it was first written here: an auditor found three still standing.
# Rather than restate a count that rots, the check below names the three
# that stay ON PURPOSE and asserts each still says why, and counts the
# remaining hand-written pairs so a new one cannot appear unnoticed.
_sch = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "scheduler.py"), encoding="utf-8").read()
_tellblk = _sch[_sch.index("    def _tell(self"):]
_tellblk = _tellblk[:_tellblk.index("\n    def ", 10)]
# The seam when one is wired in. The fallback below it is for bots
# built outside main.py, and it has to do BOTH legs — a fallback that
# only reaches Telegram is the very reporting this release removed.
checks["the scheduler's host notice is the seam, not a pair"] = (
    "seam.tell(text)" in _tellblk)
checks["…and its fallback still reaches the plugins"] = (
    "notifier.send_message(text, urgent=True)" in _tellblk
    and _tellblk.index("seam.tell(text)")
    < _tellblk.index("notifier.send_message"))
# Driven, not grepped. Both halves of the old check were satisfiable
# by a comment, and an auditor emptied the method to `return` with the
# whole suite staying green — so the auto-self-update notice could have
# reached nobody and nothing would have said so.
from telegram_bot import TelegramBot as _TB                   # noqa: E402
_ulegs = {"tg": [], "plug": []}
_ubot = _TB.__new__(_TB)
_ubot.broadcast = Broadcast(
    telegram=types.SimpleNamespace(
        enabled=True,
        send_message=lambda t, **kw: _ulegs["tg"].append((t, kw.get("auto")))),
    notifier=types.SimpleNamespace(
        has_channels=lambda: True,
        send_message=lambda t, skip=(), urgent=False: _ulegs["plug"].append(
            (t, urgent, tuple(skip)))))
_ubot.announce_urgent("rolled back", skip=("discord",))

# …and the same again with no seam wired in, where the adapter has to
# build one rather than fall back to Telegram alone.
_ulegs2 = {"tg": [], "plug": []}
_ubare = _TB.__new__(_TB)
_ubare.broadcast = None
# `enabled` is a property reading the config, so give it one that is on.
_ubare.config = types.SimpleNamespace(bot_token="t", chat_id="1",
                                      language="en",
                                      channel_telegram_enabled=True)
_ubare.send_message = lambda t, **kw: _ulegs2["tg"].append(t)
# `notifier` is a property onto the engine, so the stand-in needs one.
_ubare.engine = types.SimpleNamespace(notifier=None)
_ubare.engine.notifier = types.SimpleNamespace(
    has_channels=lambda: True,
    send_message=lambda t, skip=(), urgent=False: _ulegs2["plug"].append(
        (t, urgent)))
_ubare.announce_urgent("storage misconfigured")
checks["…and with no seam wired in it still reaches both legs"] = (
    _ulegs2["tg"] == ["storage misconfigured"]
    and _ulegs2["plug"] == [("storage misconfigured", True)])
checks["the bot has an adapter for the urgent door too"] = (
    _ulegs["tg"] == [("rolled back", None)]
    and _ulegs["plug"] == [("rolled back", True, ("discord",))])

# The three that stay, each for a reason the seam cannot express, and
# each saying so where the next reader will look.
import re as _re                                             # noqa: E402
# Four, not three: an auditor found the auto-cleanup prefix pair in
# scheduler.py, which had no reason written down at all.
_keep = {"app/monitor.py": 2, "app/web_ui.py": 1, "app/scheduler.py": 1}
_said = {}
for _f in _keep:
    _src = open(os.path.join(os.path.dirname(__file__), "..", _f),
                encoding="utf-8").read()
    _said[_f] = _src.count("Not the seam, on purpose")
checks["all four kept pairs say why they are kept"] = _said == _keep

# …and no new hand-written pair has appeared. A `bot.send_message`
# followed within a few lines by a `notifier.send_message` is the shape;
# the number may only go down.
# Every module under app/, both orders, no cleverness. The first
# version of this scanned six files forward-only and skipped any line
# mentioning the word "notifier" — an auditor walked past it three ways:
# a pair in a seventh file, a pair written notifier-first, and a pair
# whose first line carried the word in a comment.
# scheduler: four fallback branches for a bot built without a seam —
# those have to send both legs, so they look like pairs and are not —
# plus the auto-cleanup prefix pair, which is deliberate.
_PAIRS_PER_FILE = {"scheduler.py": 5, "monitor.py": 2, "web_ui.py": 1}
_pairs = []
_appdir = os.path.join(os.path.dirname(__file__), "..", "app")
for _f in sorted(os.listdir(_appdir)):
    if not _f.endswith(".py"):
        continue
    _lines = open(os.path.join(_appdir, _f), encoding="utf-8").read().split("\n")
    for _i, _ln in enumerate(_lines):
        _win = "\n".join(_lines[_i:_i + 12])
        _bot = _re.search(r"\b(bot|self)\.send_message\(", _ln)
        _not = _re.search(r"\bnotifier\.send_message\(", _ln)
        if _bot and "notifier.send_message(" in _win[len(_ln):]:
            _pairs.append(f"{_f}:{_i + 1}")
        elif _not and _re.search(r"\b(bot|self)\.send_message\(",
                                 _win[len(_ln):]):
            _pairs.append(f"{_f}:{_i + 1}")
_by_file = {}
for _p in _pairs:
    _by_file[_p.split(":")[0]] = _by_file.get(_p.split(":")[0], 0) + 1
checks["no hand-written fan-out pair has appeared"] = _by_file == _PAIRS_PER_FILE
if _by_file != _PAIRS_PER_FILE:
    print(f"      → gefunden {_by_file}, erwartet {_PAIRS_PER_FILE}")
    print("      → " + ", ".join(_pairs))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
