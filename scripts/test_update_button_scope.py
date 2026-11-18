#!/usr/bin/env python3
"""A host-scoped update notification keeps its scope after a button tap.

`/check @srv30` renders update buttons for srv30 only. Tapping one used
to rebuild the keyboard from the GLOBAL `pending_updates.json`, so every
OTHER host's pending containers (the local `gitlab`, srv40's containers)
reappeared as live buttons in a reply that was about srv30 — and tapping
one of those recreated the wrong host's container from the wrong entry.
"Update all" also lost its per-notification snapshot token and degraded
to the bare global form that updates every host.

The rebuild now works from the message's OWN keyboard, so the scope —
and the `update_all:<token>` binding — survive the tap. The Web UI fixed
the same cross-host button leak by host-filtering; this is its Telegram
twin (#7).
"""
import json
import os
import sys
import tempfile
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
from telegram_bot import TelegramBot                # noqa: E402
import srcslice                                     # noqa: E402


def datas(kb):
    return [b["callback_data"] for r in kb["inline_keyboard"] for b in r]


def texts(kb):
    return [b["text"] for r in kb["inline_keyboard"] for b in r]


# A global pending file holding OTHER hosts' entries. The old rebuild read
# this; the new one must never touch it. If it does, these leak in.
_tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump([
    {"name": "gitlab"},                                   # local
    {"name": "paperless-ngx-db-1"},                       # local
    {"name": "dsentry-web", "host": "srv40"},             # another host
    {"name": "vaultwarden", "host": "srv40"},
    {"name": "dsentry-web", "host": "srv30"},             # the scoped one
], _tmp)
_tmp.close()

checks = {}

bot = TelegramBot.__new__(TelegramBot)
bot.config = types.SimpleNamespace(pending_file=_tmp.name)
rebuild = srcslice.attr(bot, "_rebuild_keyboard_without",
                        label="TelegramBot._rebuild_keyboard_without")
EMPTY = {"inline_keyboard": []}

# ── 1) single srv30 button: tap it → scope kept, no global leak ───────────
scoped = [
    [{"text": "🔄 dsentry-web @srv30 (20 MB)",
      "callback_data": "update_one:srv30/dsentry-web"}],
    [{"text": "🚀 Update all", "callback_data": "update_all:7"},
     {"text": "✋ Manual", "callback_data": "update_skip"}],
]
out = (rebuild("update_one:srv30/dsentry-web", scoped)
       if rebuild is not None else EMPTY)
d, t = datas(out), texts(out)

# no other host's container leaked in from the global pending file
checks["a tap does not pull the local gitlab in"] = (
    not any("gitlab" in x for x in t))
checks["…nor the local paperless"] = not any("paperless" in x for x in t)
checks["…nor another host's buttons"] = not any("@srv40" in x for x in t)
checks["…nor another host's callback"] = "update_one:srv40/dsentry-web" not in d
# the tapped container is marked done and de-activated (keeps its label)
checks["the tapped button is marked done and keeps its label"] = any(
    x.startswith("✅ dsentry-web @srv30") for x in t)
checks["…and does nothing when tapped again"] = "noop" in d
# it was the only container button → the all/manual control row is dropped
checks["the last button gone drops 'Update all'"] = not any(
    x.startswith("update_all") for x in d)
checks["…and 'Manual'"] = "update_skip" not in d

# ── 2) two srv30 buttons: tap one → other stays, token survives ───────────
scoped2 = [
    [{"text": "🔄 dsentry-web @srv30 (20 MB)",
      "callback_data": "update_one:srv30/dsentry-web"}],
    [{"text": "🔄 dsentry-api @srv30 (10 MB)",
      "callback_data": "update_one:srv30/dsentry-api"}],
    [{"text": "🚀 Update all", "callback_data": "update_all:7"},
     {"text": "✋ Manual", "callback_data": "update_skip"}],
]
out2 = (rebuild("update_one:srv30/dsentry-web", scoped2)
        if rebuild is not None else EMPTY)
d2, t2 = datas(out2), texts(out2)

checks["the sibling button is kept"] = "update_one:srv30/dsentry-api" in d2
checks["…and the tapped one marked done"] = any(
    x.startswith("✅ dsentry-web @srv30") for x in t2)
# control row kept, and STILL carries its snapshot token — not the bare
# global "update_all" the old rebuild degraded it to.
checks["'Update all' keeps its snapshot token"] = "update_all:7" in d2
checks["…and is never the bare global form"] = "update_all" not in d2
checks["'Manual' stays"] = "update_skip" in d2
# still nothing from the global file
checks["…and still nothing from the global file"] = not any(
    "gitlab" in x for x in t2)

# ── 3) the callback wiring passes the message's OWN keyboard ──────────────
captured = {}
bot2 = TelegramBot.__new__(TelegramBot)
bot2._check_auth = lambda *a, **k: True
bot2.answer_callback = lambda *a, **k: None
bot2._run_single_update = lambda *a, **k: None
bot2._remove_single_button = (
    lambda chat, mid, data, kbd: captured.update(kbd=kbd, data=data))

msg_kb = [
    [{"text": "🔄 dsentry-web @srv30 (20 MB)",
      "callback_data": "update_one:srv30/dsentry-web"}],
    [{"text": "🚀 Update all", "callback_data": "update_all:7"},
     {"text": "✋ Manual", "callback_data": "update_skip"}],
]
handle = srcslice.attr(TelegramBot, "_handle_callback",
                       label="TelegramBot._handle_callback")
if handle is not None:
    handle(bot2, {
        "data": "update_one:srv30/dsentry-web", "id": "c", "from": {"id": 1},
        "message": {"message_id": 9, "chat": {"id": 1},
                    "reply_markup": {"inline_keyboard": msg_kb}},
    }, None)
checks["the callback hands on the message's own keyboard"] = (
    captured.get("kbd") == msg_kb)

os.unlink(_tmp.name)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
if failed:
    print(f"      → first keyboard: {t}")
    print(f"      → second keyboard: {d2}")
    print(f"      → handed on: {captured}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
