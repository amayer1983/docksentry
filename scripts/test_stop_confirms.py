#!/usr/bin/env python3
"""A stop asks first, in both chats, with the same question.

Discord asked; Telegram just stopped. The owner's call was that stop
should ask everywhere — a container that comes back up is a decision you
can take back, and a stopped one stays stopped until somebody notices.

The rule that makes the question worth asking: the refusals run BEFORE
the button is offered. Being asked "are you sure you want to stop
gluetun?", pressing yes, and only THEN being told gluetun is
stop-protected is a worse answer than being told straight away. That
holds for globs too — `/stop *` asks about the containers it will
actually stop, and says up front which ones it will not.

`start` and `restart` do not ask, on purpose.

The plan is not a promise: minutes can pass between the question and the
press, so the press re-runs `lifecycle.act` from scratch, guards and all,
rather than replaying a captured decision.
"""
import glob as _glob
import json
import os
import sys
import threading
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import lifecycle                                   # noqa: E402
from i18n import get_translator                    # noqa: E402
from telegram_bot import TelegramBot               # noqa: E402
from update_engine import UpdateEngine             # noqa: E402

checks = {}
NAMES = ["web", "web-db", "gluetun", "docksentry"]


class Checker:
    def __init__(self):
        self.stopped = []
        self.started = []

    def _would_kill_self(self, name):
        return name == "docksentry"

    def _stop_container(self, name):
        self.stopped.append(name)
        return True, ""

    def get_container_labels(self, name):
        return {}

    def label_bool(self, labels, key):
        return None


class Store:
    def is_protect_stop(self, name):
        return name == "gluetun"


class Backend:
    def __init__(self, checker):
        self.checker = checker

    def run(self, argv, timeout=None):
        if argv[:1] == ["ps"]:
            return types.SimpleNamespace(returncode=0,
                                         stdout="\n".join(NAMES), stderr="")
        if argv[:1] == ["start"]:
            self.checker.started.append(argv[1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def make_bot(lang="en"):
    ck = Checker()
    engine = UpdateEngine.__new__(UpdateEngine)
    engine._update_lock = threading.Lock()
    engine._queued_selfupdate = None
    engine._swap_in_flight = False
    bot = TelegramBot.__new__(TelegramBot)
    bot.engine = engine
    bot.t = get_translator(lang)
    bot.hosts = None
    bot.store = Store()
    bot._backend_for = lambda h: Backend(ck)
    bot._checker_for = lambda h, c=None: ck
    bot._store_for = lambda h: Store()
    bot._host_hint = lambda raw: ""
    bot._resolve_targets = lambda raw, write=False: (raw.strip(), None, None)
    bot._check_auth = lambda *a, **k: True
    bot.sent = []
    bot.send_message = lambda m, **k: bot.sent.append((m, k.get("reply_markup")))
    bot.toasts = []
    # "That is not your button" belongs in a toast, not in the chat — it
    # is an answer to the person who tapped, not news for the room.
    bot.answer_callback = lambda _id, text="": bot.toasts.append(text)
    bot.remove_buttons = lambda *a: None
    return bot, ck


def cmd(bot, text):
    bot.sent.clear()
    TelegramBot._handle_message(bot, {"text": text, "chat": {"id": 1},
                                      "from": {"id": 1}}, None, None)
    return list(bot.sent)


def buttons(rows):
    for _msg, kb in rows:
        if kb:
            return [b["callback_data"] for r in kb["inline_keyboard"] for b in r]
    return []


def press_as(bot, data, user="1"):
    bot.sent.clear()
    TelegramBot._handle_callback(bot, {"data": data, "id": "c",
                                       "from": {"id": user},
                                       "message": {"message_id": 9,
                                                   "chat": {"id": 1}}}, None)
    return list(bot.sent)


def press(bot, data):
    bot.sent.clear()
    TelegramBot._handle_callback(bot, {"data": data, "id": "c",
                                       "from": {"id": 1},
                                       "message": {"message_id": 9,
                                                   "chat": {"id": 1}}}, None)
    return list(bot.sent)


EN = json.load(open(os.path.join(APP, "lang", "en.json"), encoding="utf-8"))

# ── a single stop asks ───────────────────────────────────────────────
bot, ck = make_bot()
rows = cmd(bot, "/stop web")
checks["/stop asks before it stops"] = (
    EN["chan_confirm_stop"].split("{")[0].strip() in rows[0][0])
checks["…and stops nothing yet"] = ck.stopped == []
btns = buttons(rows)
checks["…offering a confirm and a cancel"] = (
    len(btns) == 2 and btns[0].startswith("stop_go:")
    and btns[1] == "stop_cancel")

out = press(bot, btns[0])
checks["the press is what stops it"] = ck.stopped == ["web"]
checks["…and it says so"] = "web" in str(out[-1][0])

# One press only: the token is spent, so a double tap cannot run twice.
out = press(bot, btns[0])
checks["a second press does nothing"] = ck.stopped == ["web"]
checks["…and says the confirmation is gone"] = (
    EN["chan_confirm_expired"].split("—")[0].strip() in str(out[-1][0]))

# Cancel means cancel.
bot, ck = make_bot()
btns = buttons(cmd(bot, "/stop web"))
press(bot, "stop_cancel")
checks["cancelling stops nothing"] = ck.stopped == []
press(bot, btns[0])
checks["…and the token is dead afterwards"] = ck.stopped == []

# ── refusals come before the question, not after ─────────────────────
bot, ck = make_bot()
rows = cmd(bot, "/stop gluetun")
checks["a protected container is refused straight away"] = (
    EN["lifecycle_refused_protected"].split("{")[0].strip() in rows[0][0])
checks["…with no button to press"] = buttons(rows) == []

rows = cmd(bot, "/stop docksentry")
checks["stopping Docksentry is refused straight away"] = (
    EN["lifecycle_refused_self"].split("{")[0].strip() in rows[0][0])
checks["…with no button either"] = buttons(rows) == []

rows = cmd(bot, "/stop nosuch")
checks["an unknown container is answered, not asked about"] = (
    buttons(rows) == [] and "nosuch" in rows[0][0])

# ── start and restart do not ask ─────────────────────────────────────
bot, ck = make_bot()
rows = cmd(bot, "/start web")
checks["/start does not ask"] = buttons(rows) == [] and ck.started == ["web"]

# ── globs ────────────────────────────────────────────────────────────
bot, ck = make_bot()
rows = cmd(bot, "/stop web*")
checks["a glob asks once, for the whole set"] = len(buttons(rows)) == 2
checks["…naming what it would stop"] = (
    "web" in rows[-1][0] and "web-db" in rows[-1][0])
checks["…and stops nothing yet"] = ck.stopped == []
press(bot, buttons(rows)[0])
checks["…until the press"] = sorted(ck.stopped) == ["web", "web-db"]

# `/stop *` must not ask about the two it is going to refuse.
bot, ck = make_bot()
rows = cmd(bot, "/stop *")
said = "\n".join(m for m, _ in rows)
question = rows[-1][0]
checks["`/stop *` says up front what it will skip"] = (
    "docksentry" in said and "gluetun" in said)
checks["…and asks only about the rest"] = (
    "2" in question and "docksentry" not in question
    and "gluetun" not in question)
press(bot, buttons(rows)[0])
checks["…and the press honours that"] = sorted(ck.stopped) == ["web", "web-db"]

bot, ck = make_bot()
rows = cmd(bot, "/stop nothing*")
checks["a glob matching nothing is answered, not asked about"] = (
    buttons(rows) == [])

# ── whose button is it, and for how long ─────────────────────────────
# Discord recorded the asker and a 15-minute TTL from the start. This
# side recorded neither: any authorised user could press somebody else's
# button, and a question left unanswered on Monday was still live on
# Friday when someone scrolled up and tapped it.
import time as _time  # noqa: E402

bot, ck = make_bot()
btns = buttons(cmd(bot, "/stop web"))
press_as(bot, btns[0], user="999")
checks["another user cannot press your confirmation"] = ck.stopped == []
checks["…and is told why, in a toast rather than in the chat"] = (
    any(EN["chan_confirm_not_yours"].split(".")[0] in t
        for t in bot.toasts) and bot.sent == [])
press(bot, btns[0])
checks["…and the button still works for the person who asked"] = (
    ck.stopped == ["web"])

# A record with no asker fails closed too — that is the shape that let
# Discord's own check be bypassed before it was tightened.
bot, ck = make_bot()
btns = buttons(cmd(bot, "/stop web"))
bot._pending_stops[btns[0].split(":", 1)[1]]["user"] = ""
press(bot, btns[0])
checks["a record with no asker is refused, not waved through"] = (
    ck.stopped == [])

bot, ck = make_bot()
btns = buttons(cmd(bot, "/stop web"))
rec = bot._pending_stops[btns[0].split(":", 1)[1]]
rec["created"] = _time.time() - (bot.STOP_CONFIRM_TTL + 1)
out = press(bot, btns[0])
checks["a confirmation older than the TTL is dead"] = ck.stopped == []
checks["…and says so"] = (
    EN["chan_confirm_expired"].split("—")[0].strip() in str(out[-1][0]))

bot, ck = make_bot()
btns = buttons(cmd(bot, "/stop web"))
bot._pending_stops[btns[0].split(":", 1)[1]]["created"] = (
    _time.time() - (bot.STOP_CONFIRM_TTL - 60))
press(bot, btns[0])
checks["…but one just inside it still works"] = ck.stopped == ["web"]

checks["the TTL matches Discord's"] = (
    TelegramBot.STOP_CONFIRM_TTL == 15 * 60)

# ── the question itself is shared ────────────────────────────────────
work = [(None, ["web"])]
checks["one container gets the singular question"] = (
    lifecycle.confirm_question("stop", work, partial="web").key
    == "chan_confirm_stop")
many = lifecycle.confirm_question("stop", [(None, ["a", "b", "c"])],
                                  partial="*")
checks["several get the plural one"] = many.key == "confirm_stop_many"
checks["…which counts them"] = many.params["count"] == 3
long = lifecycle.confirm_question("stop", [(None, [f"c{i}" for i in range(30)])],
                                  partial="*")
checks["…and does not list thirty names at you"] = (
    long.params["names"].endswith("…") and long.params["count"] == 30)

db = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
tb = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
checks["both chats ask from the same plan"] = (
    "lifecycle.plan(" in db and "lifecycle.plan(" in tb)
checks["…and phrase it from the same place"] = (
    "confirm_question(" in db and "confirm_question(" in tb)
checks["neither keeps its own protect check for it"] = (
    "def _is_protected" not in db)

# The press re-derives rather than replaying: `act` is what runs, not a
# stored list of names.
# Sliced at the branch boundary, not at a byte count: the first version
# of this took a fixed 1600-character window, and adding the TTL check
# pushed `lifecycle.act` out of it — the test failed on its own arbitrary
# number rather than on the behaviour it names.
i = tb.index('if data.startswith("stop_go:")')
block = tb[i:tb.index("\n        if data ==", i + 10)]
checks["the press re-runs the guards"] = "lifecycle.act(" in block
checks["…from the raw argument, not a captured name list"] = (
    'rec["arg"]' in block)

# ── the new keys exist everywhere ────────────────────────────────────
LANGS = sorted(_glob.glob(os.path.join(APP, "lang", "*.json")))
checks["all 16 languages are checked"] = len(LANGS) == 16
for key, needed in (("confirm_stop_many", ("{count}", "{pattern}", "{names}")),
                    ("confirm_stop_btn", ())):
    bad = []
    for f in LANGS:
        d = json.load(open(f, encoding="utf-8"))
        if key not in d or any(p not in d[key] for p in needed):
            bad.append(os.path.basename(f))
    checks[f"{key} is complete in every language"] = bad == []

# ── bare /stop and /restart answer, they do not go silent ────────────
# The lifecycle branch matched "/stop " with a trailing space, so a bare
# /stop fell through the dispatcher into nothing — while /logs and /audit
# answered with their usage line. It says what to type now.
bot, ck = make_bot()
rows = cmd(bot, "/stop")
checks["bare /stop answers with usage, not silence"] = (
    len(rows) >= 1 and EN["lifecycle_usage"].split("`")[0].strip()[:6]
    in rows[-1][0])
checks["…and offers no button"] = buttons(rows) == []
bot, ck = make_bot()
rows = cmd(bot, "/restart")
checks["bare /restart answers too"] = (
    len(rows) >= 1 and "restart" in rows[-1][0].lower())
# bare /start stays the greeting, not a lifecycle usage — it must NOT be
# swallowed by the lifecycle branch.
bot, ck = make_bot()
rows = cmd(bot, "/start")
checks["bare /start is not captured as a lifecycle usage"] = (
    not any(EN["lifecycle_usage"][:20] in m for m, _ in rows))

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
