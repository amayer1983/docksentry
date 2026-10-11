#!/usr/bin/env python3
"""`docksentry.auto` on the container beats both toggles.

`#42, @LeeNX` — his compose file is the source of truth, so a label on
the container has to win over the stored per-container switch *and* over
`AUTO_UPDATE_ALL`. `README.md` says it in one line: `auto=false` keeps a
container manual even under `AUTO_UPDATE_ALL`, `auto=true` opts it in
without touching the Web UI.

A mutation sweep on 11.10.2026 switched the label out of the decision
(`if lab is not None:` → `if False:`) and no procedure went red. With
`AUTO_UPDATE_ALL` on — which is the configuration the label exists to
override — that means his database gets updated unattended after he
wrote the line that says not to.

Driven through `handle_autoupdates`, because the promise is about what
the scheduler decides and not about a helper anyone could call. The
label answers come from a checker stand-in; the decision is the real one.
"""

import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import update_engine as _ue  # noqa: E402
from i18n import get_translator  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402

checks = {}


def _updated(labels, all_auto, stored=(), broken=False):
    """Who does the scheduler actually put through?

    `labels` maps container name → what `docksentry.auto` says (True,
    False or None for "no label"). `stored` is the per-container toggle
    the Web UI writes. `broken` makes the label lookup raise, which it
    can: it runs against a live daemon.
    """
    batched = []

    class _Checker:
        def get_container_labels(self, name):
            if broken:
                raise RuntimeError("daemon went away")
            return {"name": name}

        def label_bool(self, labels_dict, key):
            if key != "auto":
                return None
            return labels.get(labels_dict["name"])

    class _Cfg:
        language = "en"
        bot_token = ""
        chat_id = "1"

        def __init__(self):
            self.auto_update_all = all_auto
            self.pending_file = os.path.join(tempfile.mkdtemp(),
                                             "pending.json")

        def __getattr__(self, name):
            return ""

    class _Store:
        def get_autoupdate(self):
            return list(stored)

        def get_update_windows(self):
            return {}

        def get_groups(self):
            return {}

    bot = TelegramBot.__new__(TelegramBot)
    bot.config = _Cfg()
    bot.t = get_translator("en")
    bot.audit = types.SimpleNamespace(record=lambda *a, **kw: None)
    bot.hosts = None
    for n in ("send_message", "announce", "log", "_say_once"):
        setattr(bot, n, lambda *a, **kw: None)
    bot._policy_decision = lambda u, c: None
    bot._age_decision = lambda u, c: None
    bot._display_name = lambda u: u["name"]
    bot._store_for = lambda host: _Store()
    # The notification of what is *pending* is a different promise with
    # its own procedures, and reaching it wanted five more stubs
    # (`notifier`, the snapshot bookkeeping, source-url enrichment, the
    # group lookup). What is measured here is which containers reach
    # `_process_update_batch`, so the cut is above the notification
    # rather than through it.
    bot.notify_updates = lambda ups, auto=False: None
    bot._process_update_batch = lambda ups, checker, auto=False: (
        batched.extend(u["name"] for u in ups) or ([], 0, []))
    bot.engine = types.SimpleNamespace(
        _update_lock=threading.Lock(), _queued_selfupdate=None,
        _swap_in_flight=False,
        _enrich_with_source_url=lambda *a, **kw: None)

    real = _ue.host_store
    _ue.host_store = lambda scope, host: _Store()
    try:
        bot.handle_autoupdates(
            [{"name": n, "image": f"{n}:2", "host": "local"} for n in labels],
            _Checker())
    finally:
        _ue.host_store = real
    return sorted(batched)


# ── the label wins over AUTO_UPDATE_ALL ──────────────────────────────
# The case the feature exists for, and the one the sweep broke.
checks["auto=false keeps a container manual under AUTO_UPDATE_ALL"] = (
    _updated({"db": False, "web": None}, all_auto=True) == ["web"])

# …and the other direction, or the check above passes on a scheduler
# that updates nothing at all.
checks["…while an unlabelled one is still updated"] = (
    _updated({"web": None}, all_auto=True) == ["web"])

# ── the label wins over the stored toggle ────────────────────────────
checks["auto=true opts in without the Web UI switch"] = (
    _updated({"db": True}, all_auto=False, stored=()) == ["db"])
checks["…and auto=false overrides the switch being on"] = (
    _updated({"db": False}, all_auto=False, stored=("db",)) == [])

# ── no label: the previous behaviour, unchanged ──────────────────────
checks["with no label the stored switch decides"] = (
    _updated({"db": None}, all_auto=False, stored=("db",)) == ["db"])
checks["…and without it nothing is updated"] = (
    _updated({"db": None}, all_auto=False, stored=()) == [])

# ── a checker that cannot answer must not opt anything in ───────────
# The label lookup runs against a live daemon and can fail; failing
# towards "no label" keeps the stored switch in charge rather than
# silently enabling everything or nothing.
checks["a label lookup that fails falls back to the switch"] = (
    _updated({"db": None}, all_auto=False, stored=("db",),
             broken=True) == ["db"]
    and _updated({"db": None}, all_auto=False, stored=(),
                 broken=True) == [])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
