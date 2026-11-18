#!/usr/bin/env python3
"""A scheduled auto-update waits for a manual update that is still running.

`handle_autoupdates` claims the shared update mutex before it touches a
container; if somebody else holds it, the pass is skipped and the next
tick retries. Without that, a cron tick recreates the very container a
user is updating from Telegram or the Web UI, and two recreates race.

The audit for 2.19.1 measured that nothing held this: with the
`auto_updates = []` in the "lock busy" branch removed, and again with the
`acquire` turned into `if True:`, every procedure stayed green.

Driven here with a real `TelegramBot` and so the real engine's lock. The
manual update is the real major-confirm flow (`_confirm_major_update`),
whose stand-in checker blocks inside `update_container` until told to go
on — so the lock is held by a genuine manual update, not by a test that
grabbed it. While it is in there, the scheduler's pass runs on the main
thread. What stands in is only the environment: the Telegram transport
(messages are collected, nothing goes out), the link lookup, and the
per-container batch, which records what it was handed instead of talking
to a daemon. The decision under test — run or skip — is untouched.

Counter-direction: the same pass with the lock free runs, and the lock is
free again afterwards. Exits non-zero on any failure.
"""
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from container_store import ContainerStore                   # noqa: E402
from telegram_bot import TelegramBot                         # noqa: E402

checks = {}


def make_bot():
    d = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(
        bot_token="x", chat_id="1", language="en", debug=False,
        container_cli="docker", auto_update_all=False, update_policy="all",
        channel_telegram_enabled=True, bot_label="", telegram_topic_id="",
        quiet_hours_start="", quiet_hours_end="",
        data_dir=d, pending_file=os.path.join(d, "p.json"),
        history_file=os.path.join(d, "h.json"),
        maintenance_file=os.path.join(d, "maintenance.json"))
    for n in ("pinned", "autoupdate", "update_windows", "ask_before_major",
              "trust_running", "cooldown", "protect_stop", "major_pending",
              "groups", "notes", "links"):
        setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
    store = ContainerStore(cfg)
    bot = TelegramBot(cfg, store)
    # The environment: no Telegram, no link lookup against a daemon.
    bot.said = []
    bot.send_message = lambda text, *a, **k: bot.said.append(text)
    bot.announce = lambda text, *a, **k: bot.said.append(text)
    bot.notify_updates = lambda upd, **k: None
    bot.notifier = None
    bot.link_resolver = types.SimpleNamespace(
        resolve_container_link=lambda *a, **k: "")
    # The per-container work: record what the auto pass was handed.
    bot.batches = []

    def batch(updates, checker, *, auto):
        bot.batches.append(([u["name"] for u in updates], auto))
        return ([], 0, [])
    bot._process_update_batch = batch
    return bot, store


class AutoChecker:
    """The host the scheduler hands over. No labels, so the stored
    auto-update list decides."""
    def get_container_labels(self, name):
        return {}

    def label_bool(self, labels, key):
        return None


class ManualChecker(AutoChecker):
    """The checker the manual update runs on; stops inside the recreate."""
    def __init__(self):
        self.inside = threading.Event()
        self.go_on = threading.Event()
        self.updated = []

    def update_container(self, name, image, **kw):
        self.updated.append(name)
        self.inside.set()
        self.go_on.wait(10)
        return True, "updated"


UPDATES = [{"name": "web", "image": "nginx:1.27", "host": "local"}]


def opt_in(store):
    if "web" not in store.get_autoupdate():
        store.toggle_auto("web")
    return "web" in store.get_autoupdate()


# ── a manual update holds the lock ───────────────────────────────────
bot, store = make_bot()
checks["setup: web is opted in to auto-update"] = opt_in(store)
store.add_pending_major("app", {"image": "app:2", "compose": {}})
manual = ManualChecker()
worker = threading.Thread(
    target=bot._confirm_major_update, args=(manual, "app"), daemon=True)
worker.start()
inside = manual.inside.wait(10)
checks["setup: the manual update is inside its recreate"] = (
    inside and manual.updated == ["app"])
checks["setup: …and holds the real update lock"] = bot._update_lock.locked()

auto_checker = AutoChecker()
bot.handle_autoupdates(list(UPDATES), auto_checker)
checks["lock held by a manual update: the auto pass touches nothing"] = (
    bot.batches == [])
checks["…and leaves the manual update's lock where it was"] = (
    bot._update_lock.locked())

manual.go_on.set()
worker.join(10)
checks["the manual update finishes and lets go"] = (
    not worker.is_alive() and not bot._update_lock.locked())

# ── counter-direction: the lock is free ──────────────────────────────
held_batches = list(bot.batches)
bot.batches.clear()
bot.handle_autoupdates(list(UPDATES), auto_checker)
checks["lock free: the auto pass runs, for web, as an auto update"] = (
    bot.batches == [(["web"], True)])
checks["…and lets go of the lock afterwards"] = not bot._update_lock.locked()

# A fresh bot, the lock free from the start — not only after a manual
# update happened to finish.
bot2, store2 = make_bot()
opt_in(store2)
bot2.handle_autoupdates(list(UPDATES), AutoChecker())
checks["a fresh bot with nothing running: the auto pass runs"] = (
    bot2.batches == [(["web"], True)])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
if bad:
    print(f"      while held: {held_batches}, after: {bot.batches}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
