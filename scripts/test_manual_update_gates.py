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
    """`how` picks the way the head fails: by answering no, or by raising.

    Both arms matter and only one was covered. A mutation sweep on
    11.10.2026 switched off the `if cur_group:` inside the engine's
    `except Exception` and no procedure went red — so a head whose
    update *threw* (a daemon that went away mid-recreate, a rename that
    timed out) let the rest of the stack update against a head that is
    not there. That is the shape of failure a group exists to prevent,
    and it was the arm nobody drove.
    """

    def __init__(self, how="return"):
        self.calls = []
        self.how = how

    def netns_target_name(self, name):
        return None

    def update_container(self, name, image, **kw):
        self.calls.append(name)
        if name != "head":
            return (True, "ok")
        if self.how == "raise":
            raise RuntimeError("the daemon went away mid-recreate")
        return (False, "boom")


def _run(how):
    """One batch through the real engine loop; who got updated?"""
    up = _Updater(how)
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
    # The batch says which container it is busy with while it is busy
    # with it; a stub missing the two calls takes the update down with
    # an AttributeError instead of testing anything.
    bot.mark_updating = lambda *a, **k: None
    bot.clear_updating = lambda *a, **k: None
    bot._remove_from_pending = lambda names: None
    bot._restart_group_dependents = lambda *a, **k: "cascade"
    # run_updates now delegates the per-container loop to the shared engine
    # (#2) — bind the real method so we still exercise the actual gate logic.
    bot._process_update_batch = lambda *a, **k: UpdateEngine._process_update_batch(bot, *a, **k)

    # unsorted on purpose — run_updates should sort into group order
    updates = [{"name": "dep2", "image": "i"}, {"name": "head", "image": "i"},
               {"name": "dep1", "image": "i"}]
    TelegramBot.run_updates(bot, up, updates=updates)
    return up.calls


def main():
    _returned = _run("return")
    _raised = _run("raise")
    checks = {
        "head was attempted first (group order)": _returned[:1] == ["head"],
        "dep1 skipped after head failed (group_aborted)":
            "dep1" not in _returned,
        "dep2 skipped after head failed (group_aborted)":
            "dep2" not in _returned,
        "only the failed head was actually updated": _returned == ["head"],
        # The other failure arm: the head did not answer no, it threw.
        "a head whose update RAISED also aborts the group":
            _raised == ["head"],
    }
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("returned:", _returned, "raised:", _raised)
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
