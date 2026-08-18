#!/usr/bin/env python3
"""AUTO_UPDATE_ALL global auto-update (#45, @NotRetarded).

Without it, only per-container opt-ins (auto_list) are auto-applied; the rest
are reported as manual. With AUTO_UPDATE_ALL=true, every checked container is
auto-updated and nothing is left to the manual notification.

Drives handle_autoupdates with a stubbed _process_update_batch that captures
which containers were routed to auto vs manual. No Docker. Exits non-zero on
any failure.
"""
import sys, os, types, threading, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from update_engine import UpdateEngine
from i18n import get_translator


def run(all_auto, auto_list):
    cap = {"auto": [], "manual": []}
    bot = types.SimpleNamespace()
    bot.config = types.SimpleNamespace(
        auto_update_all=all_auto,
        pending_file=os.path.join(tempfile.mkdtemp(), "p.json"))
    bot.store = types.SimpleNamespace(
        get_update_windows=lambda: {},
        get_autoupdate=lambda: set(auto_list))
    bot._update_lock = threading.Lock()
    bot._run_queued_selfupdate = lambda: None
    bot._get_autoupdate = lambda: set(auto_list)
    bot.t = get_translator("en")
    # `announce` is the seam every unattended message goes through
    # now (#61) — a fake bot that lacks it is a fake bot the code
    # under test cannot talk to.
    bot.send_message = lambda *a, **k: None
    bot.announce = lambda *a, **k: None
    bot.notify_updates = lambda upd, **k: cap["manual"].extend(u["name"] for u in upd)
    # Update-policy gate (v1.53.0) — bind the real resolution/decision so the
    # gate runs; with no UPDATE_POLICY set it resolves to "all" (allow all),
    # so this test's auto/manual routing is unchanged.
    bot._resolve_update_policy = lambda n, c: UpdateEngine._resolve_update_policy(bot, n, c)
    bot._policy_allows_level = UpdateEngine._policy_allows_level
    bot._policy_decision = lambda u, c: UpdateEngine._policy_decision(bot, u, c)
    # Same shape as the policy gate beside it: the auto path consults both,
    # and a stub missing one takes the whole decision down with an
    # AttributeError rather than testing anything.
    bot._age_decision = lambda u, c: UpdateEngine._age_decision(bot, u, c)

    def fake_batch(updates, checker, *, auto):
        cap["auto"] = [u["name"] for u in updates]
        return ([], 0, [])
    bot._process_update_batch = fake_batch

    updates = [{"name": "a", "image": "i"}, {"name": "b", "image": "i"}]
    TelegramBot.handle_autoupdates(bot, updates, checker=None)
    return cap


def main():
    on = run(True, [])
    off = run(False, ["a"])
    checks = {
        "AUTO_UPDATE_ALL on: every container auto-updated": sorted(on["auto"]) == ["a", "b"],
        "AUTO_UPDATE_ALL on: nothing left as manual": on["manual"] == [],
        "off: only the opt-in container auto-updated": off["auto"] == ["a"],
        "off: the rest reported as manual": off["manual"] == ["b"],
    }
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("auto/manual:", on, off)
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
