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


def make_engine():
    engine = UpdateEngine.__new__(UpdateEngine)
    engine._update_lock = threading.Lock()
    engine._queued_selfupdate = None
    engine._swap_in_flight = False
    return engine


def engine_guard_checks(checks):
    """The guard itself, on the engine, with no chat in sight.

    The scheduler and the Web UI reach the guard through the Telegram
    bot and Discord has its own entry, but the rule is one rule: take the
    lock without waiting, prune under it, give it back, then let the
    caller hand on a queued self-update. What the engine hands back when
    the lock is taken is the KEY — each chat says it in its own language.
    """
    guard = getattr(UpdateEngine, "cleanup_guarded", None)
    checks["engine: the cleanup guard lives on the engine"] = callable(guard)
    if not callable(guard):
        return
    eng = make_engine()
    afters = []

    def after():
        afters.append(eng._update_lock.locked())

    ck = FakeChecker()
    eng._update_lock.acquire()
    res = eng.cleanup_guarded(ck, after=after)
    checks["engine: busy -> (None, 'cleanup_busy'), the key, not a sentence"] = (
        res == (None, "cleanup_busy"))
    checks["engine: busy prunes nothing and hands nothing on"] = (
        ck.cleaned == 0 and afters == [])
    checks["engine: busy leaves the holder's lock held"] = eng._update_lock.locked()
    if eng._update_lock.locked():       # a broken guard may have given it back
        eng._update_lock.release()

    seen = []

    class Seeing(FakeChecker):
        def cleanup_images(self):
            seen.append(eng._update_lock.locked())
            return super().cleanup_images()
    res = eng.cleanup_guarded(Seeing(), after=after)
    checks["engine: free -> the prune's own answer"] = (
        res == (True, "Reclaimed 1.2 GB"))
    checks["engine: the prune runs under the lock"] = seen == [True]
    checks["engine: after() runs once, with the lock already back"] = (
        afters == [False])
    checks["engine: the lock is free afterwards"] = not eng._update_lock.locked()

    class Crashing:
        def cleanup_images(self):
            raise RuntimeError("prune died")
    try:
        eng.cleanup_guarded(Crashing(), after=after)
        raised = False
    except RuntimeError:
        raised = True
    checks["engine: a crashing prune still gives the lock back and runs after()"] = (
        raised and not eng._update_lock.locked() and afters == [False, False])
    checks["engine: after is optional"] = (
        eng.cleanup_guarded(FakeChecker()) == (True, "Reclaimed 1.2 GB")
        and not eng._update_lock.locked())


def delegator_language_checks(checks):
    """Telegram and Discord each turn the engine's key into a sentence,
    in their own language. One engine, one lock; Telegram set to German,
    Discord to French, so a delegator that borrowed the other's
    translator — or forgot to translate — shows up as the wrong text."""
    from discord_bot import DiscordBot
    from i18n import get_translator
    import types as _t

    tg = make_bot()
    tg.t = get_translator("de")
    eng = tg.engine
    dcfg = _t.SimpleNamespace(discord_bot_token="t", discord_app_id="app",
                              discord_guild_id="g", pending_file="",
                              discord_allowed_users=[], debug=False,
                              language="fr")
    marker = object()
    dc = DiscordBot(dcfg, None, eng, log=lambda *_: None,
                    selfupdate_ctx=marker)
    handed = []
    real_run_queued = selfupdate.run_queued
    selfupdate.run_queued = lambda ctx: handed.append(
        (ctx, eng._update_lock.locked()))
    try:
        de = get_translator("de")("cleanup_busy")
        fr = DiscordBot._tg_bold_to_discord(get_translator("fr")("cleanup_busy"))
        checks["languages: the two sentences really differ"] = (
            de != fr and "cleanup_busy" not in (de, fr))

        eng._update_lock.acquire()
        ck = FakeChecker()
        t_res = tg.cleanup_guarded(ck)
        d_res = dc._cleanup_guarded(ck)
        checks["languages: Telegram says busy in German"] = t_res == (None, de)
        checks["languages: Discord says busy in French"] = d_res == (None, fr)
        checks["languages: neither pruned, the holder still holds"] = (
            ck.cleaned == 0 and eng._update_lock.locked())
        checks["languages: a busy Discord cleanup hands nothing on"] = handed == []
        if eng._update_lock.locked():
            eng._update_lock.release()
        del handed[:]

        d_res = dc._cleanup_guarded(ck)
        checks["languages: Discord's free cleanup prunes once"] = (
            d_res == (True, "Reclaimed 1.2 GB") and ck.cleaned == 1)
        checks["languages: …then hands on its own queued self-update, "
               "lock already back"] = handed == [(marker, False)]
        checks["languages: …and the lock is free"] = not eng._update_lock.locked()
    finally:
        selfupdate.run_queued = real_run_queued
        if eng._update_lock.locked():
            eng._update_lock.release()


def main():
    checks = {}

    # ── 0. the guard on the engine, and each chat's own words ──
    engine_guard_checks(checks)
    delegator_language_checks(checks)

    # ── 1. cleanup vs updates ──
    bot = make_bot()
    ck = FakeChecker()
    bot._update_lock.acquire()
    ok, msg = bot.cleanup_guarded(ck)
    checks["cleanup: busy -> (None, busy-msg)"] = ok is None and msg == "cleanup_busy"
    checks["cleanup: prune NOT run while busy"] = ck.cleaned == 0
    checks["cleanup: batch lock untouched"] = bot._update_lock.locked()
    if bot._update_lock.locked():
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
