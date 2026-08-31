#!/usr/bin/env python3
"""Operation coordination under the shared update mutex (#2, @famewolf).

v1.44.x serialized update flows and self-updates. This closes the remaining
uncoordinated docker-mutating operations:

1. cleanup: `docker image prune -a` filters on image CREATION time, so an
   image built upstream days ago but pulled seconds ago is prunable — running
   during an update's pull->run window would delete the image the update is
   about to run. cleanup_guarded() takes the mutex: busy -> (None, busy-msg).
2. lifecycle stop/start/restart: a stop during the post-update health wait
   reads as unhealthy and triggers a bogus rollback; refused while any update
   flow runs. The update machinery bypasses this method, so its own steps
   are never blocked.
3. a self-update queued while cleanup held the lock runs after cleanup.

Pure logic — prune and docker calls are stubbed. Exits non-zero on failure.
"""
import sys, os, threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from update_engine import UpdateEngine


class FakeChecker:
    def __init__(self):
        self.cleaned = 0
    def cleanup_images(self):
        self.cleaned += 1
        return True, "Reclaimed 1.2 GB"
    def _would_kill_self(self, name):
        return False


def make_bot():
    bot = TelegramBot.__new__(TelegramBot)
    # The lock and the two flags now live on the engine; the bot mirrors
    # them read-only (lock) / via setters (flags). Give the bare bot an
    # engine so `bot._update_lock` and the flag assignments below route to
    # the one Lock object — assertions still read `bot._update_lock` and
    # see exactly that object.
    engine = UpdateEngine.__new__(UpdateEngine)
    engine._update_lock = threading.Lock()
    engine._queued_selfupdate = None
    engine._swap_in_flight = False
    bot.engine = engine
    bot.sent = []
    bot.t = lambda key, **kw: key
    bot.send_message = lambda msg, **kw: bot.sent.append(msg)
    return bot


def main():
    checks = {}

    # ── 1. cleanup vs updates ──
    bot = make_bot()
    ck = FakeChecker()
    bot._update_lock.acquire()
    ok, msg = bot.cleanup_guarded(ck)
    checks["cleanup: busy -> (None, busy-msg)"] = ok is None and msg == "cleanup_busy"
    checks["cleanup: prune NOT run while busy"] = ck.cleaned == 0
    checks["cleanup: batch lock untouched"] = bot._update_lock.locked()
    bot._update_lock.release()
    ok, msg = bot.cleanup_guarded(ck)
    checks["cleanup: runs when free"] = ok is True and "Reclaimed" in msg
    checks["cleanup: exactly one prune"] = ck.cleaned == 1
    checks["cleanup: lock released after"] = not bot._update_lock.locked()

    # cleanup holds the mutex while pruning — an update flow trying to
    # start mid-prune must find it taken.
    bot2 = make_bot()
    seen = []
    class SlowChecker(FakeChecker):
        def cleanup_images(self):
            seen.append(bot2._update_lock.locked())
            return True, "ok"
    bot2.cleanup_guarded(SlowChecker())
    checks["cleanup: mutex held during prune"] = seen == [True]

    # ── 2. queued selfupdate runs after cleanup ──
    bot3 = make_bot()
    ran = []
    bot3._selfupdate_locked = lambda target=None, reply_to=None: ran.append(target)
    bot3._queued_selfupdate = ("1.9.9",)
    bot3.cleanup_guarded(FakeChecker())
    checks["queued selfupdate runs after cleanup"] = ran == ["1.9.9"]

    # ── 3. lifecycle actions vs updates ──
    bot4 = make_bot()
    ck4 = FakeChecker()
    bot4._is_protected = lambda name, checker: False
    bot4._update_lock.acquire()
    ok, msg = bot4._lifecycle_action("stop", "myapp", ck4)
    checks["lifecycle: stop refused while updating"] = ok is False and msg == "lifecycle_busy"
    ok, msg = bot4._lifecycle_action("restart", "myapp", ck4)
    checks["lifecycle: restart refused while updating"] = ok is False and msg == "lifecycle_busy"
    ok, msg = bot4._lifecycle_action("start", "myapp", ck4)
    checks["lifecycle: start refused while updating"] = ok is False and msg == "lifecycle_busy"
    bot4._update_lock.release()
    # Lock free: the guard must NOT trip — "unknown action" proves we got
    # past it without touching docker.
    ok, msg = bot4._lifecycle_action("nosuch", "myapp", ck4)
    checks["lifecycle: passes guard when free"] = ok is False and "unknown action" in msg

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
