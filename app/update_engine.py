#!/usr/bin/env python3
"""UpdateEngine - neutral owner of the update-orchestration mutex.

v2 groundwork. The whole update flow (manual "Update all", single-container
updates, the scheduler's auto-update pass, self-updates, guarded cleanup)
serializes on ONE `threading.Lock` plus two state flags. Historically those
lived on `TelegramBot`, so any consumer that wanted to coordinate had to
reach into the bot's privates — awkward once a second front-end (Web UI,
later Discord) needs the same coordination.

This is the first, deliberately smallest step of moving the orchestration
into a neutral engine: the engine now OWNS the lock and the two flags, and
`TelegramBot` mirrors them through properties so every existing call site
keeps working unchanged. No orchestration logic moves yet — the engine is
purely the lock/flag owner in this step.

The one hard invariant: there is exactly ONE `Lock()` object. The bot's
`_update_lock` is a read-only view onto `self.engine._update_lock`; nothing
constructs a second lock. A second lock would reopen the #53 TOCTOU window
(double-recreate), so identity here is load-bearing.
"""

import threading


class UpdateEngine:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        # Single mutex guarding ALL update flows — manual "Update all"
        # (run_updates, bot thread), single-container update
        # (_run_single_update, bot thread), major-confirm update, AND the
        # scheduler's auto-update pass (handle_autoupdates, scheduler
        # thread). acquire(blocking=False) makes the check-and-claim atomic
        # (the old bool had a TOCTOU window) and covers every entry point.
        self._update_lock = threading.Lock()
        # A /selfupdate requested while the lock is held (container batch
        # in progress) is queued instead of killing the batch mid-flight
        # (#2, @famewolf). Holds the 1-tuple `(target,)` or None.
        self._queued_selfupdate = None
        # True once the helper container is launched — the process is
        # about to be stopped, so the wrapper keeps the update lock held
        # (nothing may start an update in the final seconds).
        self._swap_in_flight = False

    @property
    def update_running(self):
        """True while any update flow holds the lock. Read-only view kept
        for the /check race-guard and any external callers."""
        locked = self._update_lock.acquire(blocking=False)
        if locked:
            self._update_lock.release()
            return False
        return True
