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
import selfupdate  # noqa: E402
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


class FakeBackend:
    """Enough to resolve a name — and no more, so a guard that fails to
    trip shows up as a docker call that should never have happened."""
    def run(self, argv, timeout=None):
        import types as _t
        if argv[:1] == ["ps"]:
            return _t.SimpleNamespace(returncode=0, stdout="myapp\n", stderr="")
        raise AssertionError(f"guard let {argv!r} through")


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
    # The body moved into the neutral `selfupdate` module (#63), so the
    # seam to stand in for is `selfupdate.run(ctx, target)`, not a method
    # on the bot.
    selfupdate.run = lambda ctx, target=None, reply_to=None: ran.append(target)
    bot3._queued_selfupdate = ("1.9.9",)
    bot3.cleanup_guarded(FakeChecker())
    checks["queued selfupdate runs after cleanup"] = ran == ["1.9.9"]

    # ── 3. lifecycle actions vs updates ──
    # The refusal itself moved into `lifecycle.act` (#63) and is pinned in
    # test_lifecycle_core. What belongs HERE is the coordination half: the
    # update lock is what makes `update_running` true, and the bot passes
    # that through rather than deciding for itself.
    import lifecycle
    bot4 = make_bot()
    ck4 = FakeChecker()
    bot4._update_lock.acquire()
    checks["lifecycle: the lock is what says 'busy'"] = bot4.update_running is True

    def act(action):
        o = lifecycle.act(action, [None],
                          backend_for=lambda h: FakeBackend(),
                          checker_for=lambda h: ck4,
                          store_for=lambda h: None,
                          partial="myapp",
                          update_running=bot4.update_running)
        r = o.fatal or o.replies[0]
        return r.ok, r.key

    for action in ("stop", "restart", "start"):
        ok, key = act(action)
        checks[f"lifecycle: {action} refused while updating"] = (
            ok is False and key == "lifecycle_busy")

    bot4._update_lock.release()
    checks["lifecycle: the lock releasing clears it"] = bot4.update_running is False
    # Lock free: the guard must NOT trip — "unknown action" proves we got
    # past it without touching docker.
    ok, key = act("nosuch")
    checks["lifecycle: passes guard when free"] = (
        ok is False and key == "chan_unknown_action")

    # And the Telegram branch really does hand the lock's answer over,
    # rather than having quietly kept a guard of its own.
    tb = open(os.path.join(os.path.dirname(__file__), "..", "app",
                           "telegram_bot.py"), encoding="utf-8").read()
    checks["lifecycle: the bot passes update_running to the core"] = (
        tb.count("update_running=self.update_running") >= 2)

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
