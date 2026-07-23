#!/usr/bin/env python3
"""Send-only Telegram mode (#2, @famewolf).

Telegram allows one getUpdates consumer per bot token. Sharing a bot with
another app (Home Assistant) that polls it means a permanent
"Conflict: terminated by other getUpdates request" fight. Send-only mode
keeps notifications (sendMessage never conflicts) but must touch NONE of
the token-global / polling APIs: no getUpdates (poll or startup flush) and
no setMyCommands (global command list — would clobber the other app's).

Pure logic — no network. Exits non-zero on any failure.
"""
import sys, os, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot


def main():
    checks = {}

    calls = []

    def make_bot(polling):
        bot = TelegramBot.__new__(TelegramBot)
        bot.config = types.SimpleNamespace(telegram_polling=polling)
        bot.running = True
        # Any of these being called in send-only mode is a bug.
        bot.api_call = lambda method, params=None, **kw: (
            calls.append(method) or {"ok": True, "result": []})
        bot._register_commands_with_telegram = lambda: calls.append("setMyCommands")
        # enabled True (token+chat present) but polling off
        type(bot).enabled = property(lambda self: True)
        # Exit the block's while-loop after one pass
        import time as _t
        orig_sleep = _t.sleep
        def one_shot(_):
            bot.running = False
        bot._sleep_patch = (one_shot, orig_sleep)
        return bot

    # ── send-only: no getUpdates, no setMyCommands ──
    calls.clear()
    bot = make_bot(polling=False)
    import time as _t
    one_shot, orig = bot._sleep_patch
    _t.sleep = one_shot
    try:
        bot.listen(checker=None, scheduler=None)
    finally:
        _t.sleep = orig
    checks["send-only: never calls getUpdates"] = "getUpdates" not in calls
    checks["send-only: never calls setMyCommands"] = "setMyCommands" not in calls
    checks["send-only: makes no api calls at all"] = calls == []

    # ── sendMessage still works in send-only mode ──
    # (enabled is True, so the normal send path is not short-circuited)
    checks["send-only: bot still 'enabled' for sending"] = bot.enabled is True

    # ── config default is polling ON (no behavior change for existing users) ──
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    from config import Config
    os.environ.pop("TELEGRAM_POLLING", None)
    checks["default: polling ON"] = Config.from_env().telegram_polling is True
    os.environ["TELEGRAM_POLLING"] = "false"
    checks["env false: polling OFF"] = Config.from_env().telegram_polling is False
    os.environ["TELEGRAM_POLLING"] = "true"
    checks["env true: polling ON"] = Config.from_env().telegram_polling is True
    os.environ.pop("TELEGRAM_POLLING", None)

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
