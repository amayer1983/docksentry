#!/usr/bin/env python3
"""Test that the MANUAL update path (run_updates) honours the same group
gates as the auto path (#2, @famewolf — the two had drifted).

Asserts, driving run_updates with a stubbed updater/store (no Docker):
  - updates are processed in group order;
  - once a group member fails, the rest of that group is SKIPPED
    (update_container is NOT called for them) — the group_aborted gate.

Pure logic. Exits non-zero on any failure.
"""
import sys, os, types, threading, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from update_engine import UpdateEngine
from i18n import get_translator


class _Store:
    def get_groups(self):
        return {"g": {"name": "G", "containers": ["head", "dep1", "dep2"],
                      "restart_dependents": True, "wait_seconds": 0}}


class _Updater:
    def __init__(self):
        self.calls = []

    def netns_target_name(self, name):
        return None

    def update_container(self, name, image, **kw):
        self.calls.append(name)
        return (False, "boom") if name == "head" else (True, "ok")


def main():
    up = _Updater()
    bot = types.SimpleNamespace()
    bot._update_lock = threading.Lock()
    bot._run_queued_selfupdate = lambda: None
    bot.config = types.SimpleNamespace(pending_file=os.path.join(tempfile.mkdtemp(), "p.json"))
    bot.store = _Store()
    bot.notifier = None
    bot.t = get_translator("en")
    bot.send_message = lambda *a, **k: None
    bot._enrich_with_source_url = lambda u: None
    bot._display_name = lambda u: u["name"]
    bot._maybe_cooldown = lambda *a, **k: None
    bot._remove_from_pending = lambda names: None
    bot._restart_group_dependents = lambda *a, **k: "cascade"
    # run_updates now delegates the per-container loop to the shared engine
    # (#2) — bind the real method so we still exercise the actual gate logic.
    bot._process_update_batch = lambda *a, **k: UpdateEngine._process_update_batch(bot, *a, **k)

    # unsorted on purpose — run_updates should sort into group order
    updates = [{"name": "dep2", "image": "i"}, {"name": "head", "image": "i"},
               {"name": "dep1", "image": "i"}]
    TelegramBot.run_updates(bot, up, updates=updates)

    checks = {
        "head was attempted first (group order)": up.calls[:1] == ["head"],
        "dep1 skipped after head failed (group_aborted)": "dep1" not in up.calls,
        "dep2 skipped after head failed (group_aborted)": "dep2" not in up.calls,
        "only the failed head was actually updated": up.calls == ["head"],
    }
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("calls:", up.calls)
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
