#!/usr/bin/env python3
"""Self-update vs. running container updates (#2, @famewolf).

A /selfupdate issued while a container batch was running restarted
Docksentry mid-batch: the batch died, its updates were re-offered after the
restart, and had the restart landed during a stop/rename/recreate it would
have orphaned a renamed `_old` container. Self-updates now coordinate with
every other update flow through the shared update lock:

1. lock held  -> /selfupdate is queued + announced, body never runs
2. batch done -> queued self-update runs exactly once
3. lock free  -> /selfupdate holds the lock while it runs (no batch can
   start mid-swap); released on no-update paths, kept once the helper
   container is launched
4. auto self-update skips the cycle when the lock is held

Pure logic — the selfupdate body and messaging are stubbed. Exits non-zero
on any failure.
"""
import sys, os, threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from update_engine import UpdateEngine
import selfupdate


def stub_body(fn):
    """Stand in for the self-update body.

    It used to be `bot._selfupdate_locked`, so each section stubbed it on
    its own bot. The body lives in the neutral `selfupdate` module now
    (#63) and `_handle_selfupdate` calls `selfupdate.run(self, target)`,
    so the seam to stand in for is that function. `fn` is called with the
    target only — the ctx is the bot, which every section already holds.
    """
    selfupdate.run = lambda ctx, target=None: fn(target)


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

    # ── 1. lock held → queued, body not run ──
    bot = make_bot()
    ran = []
    stub_body(lambda target=None: ran.append(target))
    bot._update_lock.acquire()
    bot._handle_selfupdate("1.2.3")
    checks["queued while batch runs"] = bot._queued_selfupdate == ("1.2.3",)
    checks["queue announced"] = "selfupdate_queued" in bot.sent
    checks["body not run while queued"] = ran == []
    checks["lock still held by batch"] = bot._update_lock.locked()

    # ── 2. batch finishes → queued selfupdate runs exactly once ──
    bot._update_lock.release()
    bot._run_queued_selfupdate()
    checks["queued run announced"] = "selfupdate_dequeued" in bot.sent
    checks["queued target passed through"] = ran == ["1.2.3"]
    checks["queue cleared"] = bot._queued_selfupdate is None
    bot._run_queued_selfupdate()
    checks["second run is a no-op"] = ran == ["1.2.3"]

    # ── 3. lock free → body runs under lock, released after ──
    bot2 = make_bot()
    held_during = []
    stub_body(lambda target=None: held_during.append(
        bot2._update_lock.locked()))
    bot2._handle_selfupdate()
    checks["body runs when lock free"] = held_during == [True]
    checks["lock released after no-update path"] = not bot2._update_lock.locked()

    # ── 3b. helper launched → lock deliberately kept ──
    bot3 = make_bot()
    def fake_swap(target=None, reply_to=None):
        bot3._swap_in_flight = True
    stub_body(fake_swap)
    bot3._handle_selfupdate()
    checks["lock kept once swap in flight"] = bot3._update_lock.locked()

    # ── 3c. queued None target works (plain /selfupdate) ──
    bot4 = make_bot()
    ran4 = []
    stub_body(lambda target=None: ran4.append(target))
    bot4._update_lock.acquire()
    bot4._handle_selfupdate()
    checks["plain selfupdate queues as (None,)"] = bot4._queued_selfupdate == (None,)
    bot4._update_lock.release()
    bot4._run_queued_selfupdate()
    checks["plain selfupdate dequeues with None"] = ran4 == [None]

    # ── 4. auto self-update skips when lock held ──
    bot5 = make_bot()
    inner = []
    bot5._check_selfupdate_auto_locked = lambda defer_check=False: (
        inner.append(defer_check) or True)
    bot5._update_lock.acquire()
    checks["auto skips while batch runs"] = bot5.check_selfupdate_auto() is False
    checks["auto inner not called"] = inner == []
    bot5._update_lock.release()
    checks["auto runs when free"] = bot5.check_selfupdate_auto(defer_check=True) is True
    checks["auto inner called with defer"] = inner == [True]
    checks["auto lock released when no swap"] = not bot5._update_lock.locked()

    # ── 4b. auto keeps lock when it swapped ──
    bot6 = make_bot()
    def fake_auto(defer_check=False):
        bot6._swap_in_flight = True
        return True
    bot6._check_selfupdate_auto_locked = fake_auto
    checks["auto applied"] = bot6.check_selfupdate_auto() is True
    checks["auto lock kept after swap"] = bot6._update_lock.locked()

    # ── 4c. batch flow CRASHED (exception unwind) → queued run cancelled ──
    # Per-container failures are normal results and don't stop the queued
    # run; but a flow-level exception means unknown state and an unreported
    # error — restarting there could kill the process before the error
    # message goes out. The queue must be dropped with an honest message.
    bot8 = make_bot()
    ran8 = []
    stub_body(lambda target=None: ran8.append(target))
    bot8._queued_selfupdate = (None,)
    try:
        try:
            raise ValueError("batch flow crashed")
        finally:
            bot8._run_queued_selfupdate()
    except ValueError:
        pass
    checks["crash: queued selfupdate NOT run"] = ran8 == []
    checks["crash: cancellation announced"] = "selfupdate_queue_cancelled" in bot8.sent
    checks["crash: queue cleared"] = bot8._queued_selfupdate is None
    checks["crash: original exception preserved"] = True  # reaching here proves it

    # ── 5. queue survives an exception in the queued run ──
    bot7 = make_bot()
    def boom(target=None, reply_to=None):
        raise RuntimeError("swap exploded")
    stub_body(boom)
    bot7._queued_selfupdate = (None,)
    try:
        bot7._run_queued_selfupdate()
        checks["queued-run exception contained"] = True
    except RuntimeError:
        checks["queued-run exception contained"] = False
    checks["lock not leaked on exception"] = not bot7._update_lock.locked()

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
