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

bot = TelegramBot.__new__(TelegramBot)
bot.config = types.SimpleNamespace(pending_file=_tmp.name)

# ── 1) single srv30 button: tap it → scope kept, no global leak ───────────
scoped = [
    [{"text": "🔄 dsentry-web @srv30 (20 MB)",
      "callback_data": "update_one:srv30/dsentry-web"}],
    [{"text": "🚀 Update all", "callback_data": "update_all:7"},
     {"text": "✋ Manual", "callback_data": "update_skip"}],
]
out = bot._rebuild_keyboard_without("update_one:srv30/dsentry-web", scoped)
d, t = datas(out), texts(out)

# no other host's container leaked in from the global pending file
assert not any("gitlab" in x for x in t), t
assert not any("paperless" in x for x in t), t
assert not any("@srv40" in x for x in t), t
assert "update_one:srv40/dsentry-web" not in d, d
# the tapped container is marked done and de-activated (keeps its label)
assert any(x.startswith("✅ dsentry-web @srv30") for x in t), t
assert "noop" in d, d
# it was the only container button → the all/manual control row is dropped
assert not any(x.startswith("update_all") for x in d), d
assert "update_skip" not in d, d
print("OK: single scoped button — no cross-host leak, control row dropped")

# ── 2) two srv30 buttons: tap one → other stays, token survives ───────────
scoped2 = [
    [{"text": "🔄 dsentry-web @srv30 (20 MB)",
      "callback_data": "update_one:srv30/dsentry-web"}],
    [{"text": "🔄 dsentry-api @srv30 (10 MB)",
      "callback_data": "update_one:srv30/dsentry-api"}],
    [{"text": "🚀 Update all", "callback_data": "update_all:7"},
     {"text": "✋ Manual", "callback_data": "update_skip"}],
]
out2 = bot._rebuild_keyboard_without("update_one:srv30/dsentry-web", scoped2)
d2, t2 = datas(out2), texts(out2)

assert "update_one:srv30/dsentry-api" in d2, d2          # sibling kept
assert any(x.startswith("✅ dsentry-web @srv30") for x in t2), t2
# control row kept, and STILL carries its snapshot token — not the bare
# global "update_all" the old rebuild degraded it to.
assert "update_all:7" in d2, d2
assert "update_all" not in d2, d2                        # never the bare form
assert "update_skip" in d2, d2
# still nothing from the global file
assert not any("gitlab" in x for x in t2), t2
print("OK: sibling button kept, update_all token preserved, no leak")

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
TelegramBot._handle_callback(bot2, {
    "data": "update_one:srv30/dsentry-web", "id": "c", "from": {"id": 1},
    "message": {"message_id": 9, "chat": {"id": 1},
                "reply_markup": {"inline_keyboard": msg_kb}},
}, None)
assert captured.get("kbd") == msg_kb, captured          # the message's own kb
print("OK: callback forwards the message keyboard, not a global read")

os.unlink(_tmp.name)
print("All update-button-scope tests passed.")
