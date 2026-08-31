#!/usr/bin/env python3
"""A self-update asked for in private stays private across the restart.

#63, @NotRetarded. His Discord replies are ephemeral — only he sees them,
because a container listing names services the rest of the server has no
business reading. Then `/selfupdate` restarted Docksentry and the new
process greeted the whole channel with "🚀 Docksentry started (v2.17.6)".
The one event that could not be answered privately was announcing to
everyone both that somebody administers this bot and which version it
runs.

There is no answering the original interaction afterwards: that process
is gone and the token died with it. So the route is a direct message to
whoever asked, and the carrier for "who asked, and they asked privately"
is the self-update marker — the one piece of state that already survives
the recreate.

What is checked here is the intent, not the plumbing:

  * a private self-update puts nothing in a Discord channel — not the
    "restarting" notice before, not the banner after;
  * the result still reaches the person, and is not lost even when the
    direct message bounces;
  * a self-update triggered the ordinary way behaves exactly as before;
  * an ordinary boot — no self-update at all — is untouched.

`boot_announce` and `deliver_private` live inside `main()`, so they are
lifted out of the real source and executed here rather than mirrored:
a mirror would go on passing after main.py changed underneath it.
"""
import ast
import json
import os
import sys
import tempfile
import textwrap
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import discord_bot                                              # noqa: E402
from container_store import atomic_write_json                   # noqa: E402
from i18n import get_translator                                 # noqa: E402
from notifier import DISCORD_CHANNELS, Notifier                 # noqa: E402
from telegram_bot import TelegramBot                            # noqa: E402

APP = os.path.join(os.path.dirname(__file__), "..", "app")
MAIN_SRC = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
DISCORD_SRC = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
TELEGRAM_SRC = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()

checks = {}


# ── fakes ────────────────────────────────────────────────────────────
class FakeChannel:
    """A notifier plugin that records instead of sending."""

    def __init__(self, name):
        self.name = name
        self.enabled = True
        self.sent = []

    def active(self):
        return True

    def configured(self):
        return True

    def missing(self):
        return []

    def send_message(self, text):
        self.sent.append(text)


def fresh_notifier():
    """A REAL Notifier with fake channels, so the skip logic under test is
    the shipped one and only the transport is stubbed."""
    cfg = types.SimpleNamespace(quiet_hours_start="", quiet_hours_end="",
                                maintenance_file="/nonexistent")
    n = Notifier.__new__(Notifier)
    n.config = cfg
    n._plugins = [FakeChannel("discord"), FakeChannel("discordbot"),
                  FakeChannel("ntfy"), FakeChannel("smtp")]
    n._by_name = {p.name: p for p in n._plugins}
    return n


def load_boot_announce():
    """`boot_announce` / `deliver_private` out of main.py, ready to call."""
    tree = ast.parse(MAIN_SRC)
    wanted = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "boot_announce", "deliver_private"):
            wanted[node.name] = textwrap.dedent(
                ast.get_source_segment(MAIN_SRC, node))
    if set(wanted) != {"boot_announce", "deliver_private"}:
        return None, None
    ns = {"t": get_translator("en")}
    for name in ("deliver_private", "boot_announce"):
        exec(compile(wanted[name], "<main.py>", "exec"), ns)
    return ns, wanted


def run_boot(ns, reply_to, dm_result=True):
    """One boot's worth of notices. Returns (notifier, telegram_sent, dms)."""
    notifier = fresh_notifier()
    telegram_sent = []
    dms = []

    def fake_send_private(config, user_id, text, log=print):
        dms.append((user_id, text))
        return dm_result

    ns["config"] = types.SimpleNamespace(discord_bot_token="t")
    ns["notifier"] = notifier
    ns["bot"] = types.SimpleNamespace(
        enabled=True, send_message=lambda text: telegram_sent.append(text))
    ns["selfupdate_reply_to"] = reply_to
    original = discord_bot.send_private
    discord_bot.send_private = fake_send_private
    try:
        ns["boot_announce"]("🚀 Docksentry started (v9.9.9)")
    finally:
        discord_bot.send_private = original
    return notifier, telegram_sent, dms


def got(notifier, name):
    return notifier._by_name[name].sent


# ── 1. the marker carries the reply route, and only when there is one ──
tmp = tempfile.mkdtemp()

pub_path = os.path.join(tmp, "public.json")
stub = types.SimpleNamespace(
    config=types.SimpleNamespace(selfupdate_marker_file=pub_path))
TelegramBot._write_selfupdate_marker(stub, "repo:latest")
public_mark = json.load(open(pub_path))
checks["a self-update nobody asked for privately writes the marker "
       "it always wrote"] = set(public_mark) == {"image", "ts"}

priv_path = os.path.join(tmp, "private.json")
stub.config.selfupdate_marker_file = priv_path
TelegramBot._write_selfupdate_marker(stub, "repo:latest",
                                     reply_to={"discord_user": "4711"})
private_mark = json.load(open(priv_path))
checks["a private one records who to answer once we are back"] = (
    private_mark.get("reply_to") == {"discord_user": "4711"}
    and float(private_mark.get("ts", 0)) > 0)


# ── 2. …and the next boot only trusts it while the marker is fresh ────
# Same one-hour rule the restart cause already lives by: an abandoned
# self-update from last week must not silence a channel today.
main_tree = ast.parse(MAIN_SRC)
route_guarded = False
for node in ast.walk(main_tree):
    if not isinstance(node, ast.If):
        continue
    body_src = "\n".join(ast.get_source_segment(MAIN_SRC, s) or ""
                         for s in node.body)
    if "selfupdate_restart = True" not in body_src:
        continue
    if "selfupdate_reply_to = " in body_src:
        route_guarded = True
checks["the reply route is only believed from a fresh marker"] = route_guarded

route_assigns = [n for n in ast.walk(main_tree)
                 if isinstance(n, ast.Assign)
                 for tgt in n.targets
                 if isinstance(tgt, ast.Name) and tgt.id == "selfupdate_reply_to"]
checks["…and it starts out as 'nobody', so a boot with no marker is "
       "an ordinary boot"] = any(
    isinstance(a.value, ast.Constant) and a.value.value is None
    for a in route_assigns)


# ── 3. the boot notices themselves ────────────────────────────────────
ns, sources = load_boot_announce()
checks["main.py routes its boot notices through one function"] = ns is not None

if ns is not None:
    # (a) nothing was triggered privately — every channel, exactly as before
    n0, tg0, dm0 = run_boot(ns, None)
    checks["an ordinary boot still tells every channel, Discord included"] = (
        len(got(n0, "discordbot")) == 1 and len(got(n0, "discord")) == 1
        and len(got(n0, "ntfy")) == 1 and len(got(n0, "smtp")) == 1
        and len(tg0) == 1 and dm0 == [])

    # (b) triggered privately — the channel hears nothing
    n1, tg1, dm1 = run_boot(ns, {"discord_user": "4711"})
    checks["after a private self-update the Discord channels hear "
           "nothing"] = got(n1, "discordbot") == [] and got(n1, "discord") == []
    checks["…the operator's own channels are untouched"] = (
        len(got(n1, "ntfy")) == 1 and len(got(n1, "smtp")) == 1
        and len(tg1) == 1)
    checks["…and the person who asked is told, privately"] = (
        len(dm1) == 1 and dm1[0][0] == "4711"
        and "v9.9.9" in dm1[0][1])

    # (c) the direct message bounces — DMs from server members turned off.
    # Losing the result is worse than the leak: he asked for a self-update
    # and would otherwise never learn whether it worked.
    n2, tg2, dm2 = run_boot(ns, {"discord_user": "4711"}, dm_result=False)
    fallback = got(n2, "discordbot")
    checks["a direct message that bounces is not lost"] = (
        len(fallback) == 1 and "v9.9.9" in fallback[0])
    checks["…and it says why it ended up in the channel"] = (
        bool(fallback) and get_translator("en")(
            "selfupdate_private_undeliverable") in fallback[0])
    checks["…without sending it twice to the channels that already "
           "had it"] = len(got(n2, "ntfy")) == 1 and len(got(n2, "smtp")) == 1


# ── 4. the notice sent BEFORE the restart is held back too ────────────
# "v2.17.5 → v2.17.6, restarting" gives away everything the ephemeral
# answer was hiding; suppressing only the boot side would fix nothing.
n3 = fresh_notifier()
n3.send_message("secret", skip=DISCORD_CHANNELS)
checks["the notifier can withhold one message from named channels"] = (
    got(n3, "discordbot") == [] and got(n3, "discord") == []
    and got(n3, "ntfy") == ["secret"])

locked = TELEGRAM_SRC.split("def _selfupdate_locked(")[1].split("\n    def ")[0]
checks["the pre-restart 'restarting' notice skips them as well"] = (
    "skip=DISCORD_CHANNELS if reply_to else ()" in locked)


# ── 5. the route survives the queue ───────────────────────────────────
# A /selfupdate that waits behind a container batch is still the one that
# was asked for privately when it finally runs.
class QueueBot(TelegramBot):
    def __init__(self):
        self.engine = types.SimpleNamespace(_queued_selfupdate=None)
        self.calls = []

    def send_message(self, text, **kw):
        pass

    def t(self, key, **kw):
        return key

    def _handle_selfupdate(self, target=None, reply_to=None):
        self.calls.append((target, reply_to))


qb = QueueBot()
qb._queued_selfupdate = ("1.2.3", {"discord_user": "4711"})
qb._run_queued_selfupdate()
checks["a queued self-update is still private when it finally runs"] = (
    qb.calls == [("1.2.3", {"discord_user": "4711"})])

qb2 = QueueBot()
qb2._queued_selfupdate = (None,)
qb2._run_queued_selfupdate()
checks["…and an old one-item queue entry still runs, publicly"] = (
    qb2.calls == [(None, None)])


# ── 6. Discord's /selfupdate calls something that exists ──────────────
# It called `bot.check_selfupdate(...)`, which TelegramBot has never had:
# every `/selfupdate` from Discord raised AttributeError and answered
# "Something went wrong" after telling the user it had started.
cmd = DISCORD_SRC.split("def _cmd_selfupdate(")[1].split("\n    def ")[0]
called = sorted(set(part.split("(")[0] for part in cmd.split("bot.")[1:]))
checks["/selfupdate on Discord calls methods TelegramBot actually "
       "has"] = bool(called) and all(hasattr(TelegramBot, m) for m in called)
checks["…and only records a reply route when replies are private"] = (
    "self._replies_private()" in cmd)


for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
