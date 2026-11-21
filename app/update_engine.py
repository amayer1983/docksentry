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

import contextlib
import threading
import time

from container_store import (LOCAL_HOST, HostScopedStore, entry_host,
                             host_key)
from errfmt import clip


def host_name_of(host):
    """The host NAME for `host`, which may be a `ManagedHost`, a plain
    name, or None (→ the local host)."""
    if host is None:
        return LOCAL_HOST
    if isinstance(host, str):
        return host or LOCAL_HOST
    return getattr(host, "name", None) or LOCAL_HOST


def host_store(owner, host):
    """The container-state view `owner` must use for `host` (#7).

    `owner` is anything carrying a `.store` and (optionally) a `.hosts`
    registry — the `UpdateEngine`, the `TelegramBot`, or one of the
    duck-typed stand-ins the tests drive the shared orchestration with.
    A free function rather than a method precisely because of that: the
    orchestration runs with several different kinds of `self`, and none
    of them should have to grow a resolver method to take part.

    **Single-host installs get the RAW store back**, not a
    `HostScopedStore(store, "local")`. The two are equivalent for keys —
    local keys are unprefixed either way — but not for reads: the scoped
    view *filters* lists and drops member-less groups. Handing back the
    raw object is what makes "a one-host install behaves exactly as it
    did, byte for byte" a structural property instead of a promise.

    With several hosts managed the registry's own per-host view is used
    (that is what `hosts.build_hosts` built it for); a host the registry
    doesn't know — a stale `pending_updates.json` entry naming a host
    that has since left `DOCKER_HOSTS` — still gets a correctly scoped
    view rather than silently writing to another host's keys.
    """
    store = owner.store
    registry = getattr(owner, "hosts", None)
    if registry is None or not getattr(registry, "is_multi", False):
        return store
    name = host_name_of(host)
    managed = registry.get(name)
    scoped = getattr(managed, "store", None) if managed is not None else None
    return scoped if scoped is not None else HostScopedStore(store, name)


def _wait(seconds, engine=None):
    """Sit out `seconds`, unless the engine was handed another way to wait.

    A module function rather than a method, because several of these
    methods are driven with a stand-in `self` — `test_cooldown.py` calls
    `UpdateEngine._maybe_cooldown(bot, …)` — and a stand-in has none of
    our methods. Those harnesses patch `time.sleep` and keep working,
    while an engine built with `sleep=` uses what it was given. Two of
    the suite's slowest procedures were sitting out real cooldowns to
    prove arithmetic.
    """
    import time as _t
    (getattr(engine, "_sleep", None) or _t.sleep)(seconds)


def _now(engine=None):
    """The engine's clock, for how long ago the last recreate finished.

    Monotonic, and injectable next to `_sleep` for the same reason: a
    check that a queued container waits the right number of seconds
    must not depend on how fast the machine running it is.
    """
    import time as _t
    return (getattr(engine, "_clock", None) or _t.monotonic)()


def _report_result(engine, name, image, success, detail, source_url=""):
    """One finished container, out through the seam.

    The engine used to hold `notifier` and call it directly, with no
    reference to `broadcast` anywhere in the file, so which channels
    heard a finished container was settled by whatever happened to be
    reachable rather than by anyone deciding. It has one now, set by
    `main.py` right after the seam is built. The seam keeps today's cadence
    exactly (a card per container on the plugins, the whole batch in one
    Telegram message) and puts that choice in one readable place.

    A module function, not a method: `test_update_policy.py` and
    `test_manual_update_gates.py` drive `_process_update_batch` with a
    stand-in `self` that has none of our methods, and a report is not
    worth an AttributeError.
    """
    seam = getattr(engine, "broadcast", None)
    if seam is not None:
        seam.update_result(name, image, success, detail,
                           source_url=source_url)
        return
    notifier = getattr(engine, "notifier", None)
    if notifier:
        notifier.send_update_result(name, image, success, detail,
                                    source_url=source_url)


class UpdateEngine:
    def _t(self, key, **kw):
        """A user-facing line, in the configured language.

        The batch report is read by people in a chat, so its words belong
        in the shared translations like everyone else's (#63). Resolved
        per call from `config.language`, so `/lang` applies at once — the
        same shape `UpdateChecker._t` uses.
        """
        from i18n import get_translator
        lang = getattr(getattr(self, "config", None), "language", "en") or "en"
        return get_translator(lang)(key, **kw)

    def __init__(self, config, store, link_resolver=None, hosts=None,
                 sleep=None):
        self.config = config
        self.store = store
        #: How the engine waits. Injectable for the same reason the
        #: Discord gateway and the REST client take one: the cooldown
        #: between recreates and the pause between containers in a group
        #: are real seconds, and the tests that exercise them were
        #: sitting out 170 of the suite's 362 seconds to prove
        #: arithmetic. Production passes nothing and gets time.sleep.
        self._sleep = sleep
        # Every managed host (#7). None — or a registry holding only the
        # local host — means single-host, and then `_store_for` hands back
        # the raw store for every lookup, so not one byte of what this
        # engine reads or writes changes.
        self.hosts = hosts
        # Neutral repo/changelog link resolution (#52). The engine owns the
        # single LinkResolver so its `_enrich_with_source_url` and the bot's
        # direct link calls resolve through the SAME instance — the bot sets
        # `self.link_resolver = self.engine.link_resolver` rather than
        # building a second one. `LinkResolver(store, config)` is the exact
        # shape the bot built before, so this is behaviour-preserving. Lazy
        # import mirrors the bot's proven pattern and sidesteps any cycle.
        from link_resolver import LinkResolver
        # The registry goes in too (#7): an update's `host` then reaches the
        # container reads that resolve its link, instead of every one of
        # them asking the machine Docksentry happens to run on.
        self.link_resolver = link_resolver or LinkResolver(store, config,
                                                           hosts=hosts)
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
        # Container updates tapped while the lock was held. Before this,
        # they were answered with "an update is already running" and then
        # thrown away, so tapping four containers in the notification ran
        # exactly one and silently dropped three — you had to come back
        # and tap each of them again once the previous had finished.
        #
        # Bounded, because a queue somebody can fill by tapping is a queue
        # somebody can fill by tapping. In memory on purpose and never
        # persisted: a restart losing it is honest and the entries are
        # still in the pending list, whereas a queue surviving a restart
        # would start updating containers on boot without anyone asking.
        # What must not happen is losing it *silently* — the drain says
        # what it is dropping and why.
        self._update_queue = []
        self._update_queue_lock = threading.Lock()
        #: The one thread working the queue off, or None (`kick_drain`).
        #: Read and set under `_update_queue_lock`. Read with getattr
        #: wherever it is read, so an engine assembled by hand without
        #: `__init__` simply has none running.
        self._drainer = None
        #: How many holds keep the drainer back (`hold_drain`), and the
        #: kick that landed while one did: None, or `(after,)`. Both under
        #: `_update_queue_lock`.
        self._drain_holds = 0
        self._held_kick = None
        #: The local host's checker, for an engine built without a host
        #: registry. The drainer needs one for a local key and has nobody
        #: to hand it one; with a registry (main.py always passes one)
        #: the registry's local host answers and this stays None.
        self.local_checker = None
        #: The shared self-update context (`selfupdate.Context`), set by
        #: main.py. The drainer hands a self-update that queued behind
        #: one of its entries on through it, as every front end does
        #: after its own release.
        self.selfupdate_ctx = None
        #: key -> when it was queued, for the 'already queued' answer
        #: and for /status. Cleared with the entry it belongs to.
        self._queued_at = {}
        # Which container the held lock is currently busy with, and what
        # it is moving to: `{host key: target version}`. `update_running`
        # above already says "an update is in flight" but cannot say for
        # whom, which is why the yellow "update" badge kept claiming an
        # update was merely *available* while the log said it was running
        # (#2, @LeeNX). This is that same fact with the name attached —
        # not a second source of truth: it is written only between the two
        # lines that call `update_container`, so it is empty whenever the
        # lock is free. In memory and never persisted, for the same reason
        # the queue is not: a restart mid-update leaves the crash-recovery
        # journal to sort it out, and a stale "updating…" surviving a
        # reboot would be a lie the UI could never clear.
        self._updating = {}
        # True once the helper container is launched — the process is
        # about to be stopped, so the wrapper keeps the update lock held
        # (nothing may start an update in the final seconds).
        self._swap_in_flight = False
        # Notifier (Discord/webhook fan-out) — single-sourced here so the
        # orchestration methods that moved off the bot (_process_update_batch)
        # can reach it, while the bot mirrors it through a property. Set by
        # main.py after init (bot.notifier = Notifier(config), which writes
        # through to here). Defensive default so the attribute always exists.
        self.notifier = None
        #: The all-channel seam. Set by main.py right after it is built;
        #: `_report_result` prefers it and falls back to `notifier` for
        #: the engines the tests construct on their own.
        self.broadcast = None

    #: Most anyone can queue by tapping. Ten manual updates back to back
    #: is already a long time to be holding someone's attention.
    UPDATE_QUEUE_MAX = 10

    def enqueue_update(self, key):
        """Queue a container key. Returns its 1-based position, or 0 when
        it is already queued, or -1 when the queue is full."""
        with self._update_queue_lock:
            if key in self._update_queue:
                return 0
            if len(self._update_queue) >= self.UPDATE_QUEUE_MAX:
                return -1
            self._update_queue.append(key)
            # When it went in, so the "already queued" answer can say
            # since when. Without it that message reads as a correction
            # — "you already pressed this" — to somebody pressing a
            # button for the first time, which is how a queue entry
            # stranded from the previous day looked on my
            # own instance on 23.10.2026.
            self._queued_at[key] = time.time()
            return len(self._update_queue)

    def enqueue_updates(self, keys):
        """Queue several keys as one request: all of them or none.

        `{key: answer}` with `enqueue_update`'s answers — the 1-based
        position, 0 for a key already queued, -1 for a key that needed a
        place it did not get. When the new keys do not all fit, none of
        them goes in and each of them gets -1: half a request on the
        queue is a request nobody made. A key named twice is one entry.
        """
        out = {}
        with self._update_queue_lock:
            new = []
            for k in dict.fromkeys(keys):
                if k in self._update_queue:
                    out[k] = 0
                else:
                    new.append(k)
            if len(self._update_queue) + len(new) > self.UPDATE_QUEUE_MAX:
                for k in new:
                    out[k] = -1
                return out
            now = time.time()
            for k in new:
                self._update_queue.append(k)
                self._queued_at[k] = now
                out[k] = len(self._update_queue)
        return out

    def take_queued_update(self):
        """Pop the next queued key, or None."""
        with self._update_queue_lock:
            if not self._update_queue:
                return None
            key = self._update_queue.pop(0)
            self._queued_at.pop(key, None)
            return key

    def drop_queued_updates(self, predicate=None):
        """Remove and return queued keys matching `predicate` (all if None)."""
        with self._update_queue_lock:
            keep, taken = [], []
            for k in self._update_queue:
                (taken if (predicate is None or predicate(k)) else keep).append(k)
            self._update_queue = keep
            for k in taken:
                self._queued_at.pop(k, None)
            return taken

    def queued_updates(self):
        """A snapshot of the queue as [(key, waiting_seconds), …], in order.

        A copy, deliberately: callers render it while the poll loop can
        be draining, and handing out the live list would let it change
        underneath a half-built message.
        """
        now = time.time()
        with self._update_queue_lock:
            return [(k, max(0.0, now - self._queued_at.get(k, now)))
                    for k in self._update_queue]

    def queued_waiting_seconds(self, key):
        """How long `key` has been queued, or None if it is not."""
        with self._update_queue_lock:
            if key not in self._update_queue:
                return None
            return max(0.0, time.time() - self._queued_at.get(key, time.time()))

    def has_queued_updates(self):
        with self._update_queue_lock:
            return bool(self._update_queue)

    def mark_updating(self, key, version=""):
        """Note that `key` is being updated right now, to `version`.

        `version` is best-effort: it comes from the pending entry's
        `new_version`, which only exists when the remote image carries an
        OCI version label. An empty string is a normal answer and the
        callers render "updating" without a target rather than inventing
        one.
        """
        with self._update_queue_lock:
            self._updating[key] = version or ""

    def clear_updating(self, key):
        """Forget `key` — the update finished, failed or was rolled back."""
        with self._update_queue_lock:
            self._updating.pop(key, None)

    @property
    def updating(self):
        """`{host key: target version}` for what is being updated now.

        A copy, so a caller iterating it cannot trip over the update
        thread finishing underneath it.
        """
        with self._update_queue_lock:
            return dict(self._updating)

    def _store_for(self, host):
        """Container state scoped to `host` (a `ManagedHost`, a host name,
        or None for the local one). See `host_store` for why single-host
        installs deliberately get the raw store back."""
        return host_store(self, host)

    @property
    def update_running(self):
        """True while any update flow holds the lock. Read-only view kept
        for the /check race-guard and any external callers.

        Only looks. It used to take the lock and give it straight back,
        and a real claim landing between those two calls was refused
        although nobody was updating."""
        return self._update_lock.locked()

    # ── The one door to the update lock ────────────────────────────────
    def claim_update(self):
        """Take the update lock, or return False. Never waits.

        Everything that changes containers goes through this one lock:
        the scheduler's unattended batch, a single tap, "Update all", a
        confirmed major, a cleanup, the scheduled self-update. Before the
        lock reached all of them, a cron tick could recreate the very
        container somebody was mid-updating from a chat (v1.23.1).

        Never waiting is the rule, not an optimisation: a flow that
        blocked here would sit on a cron thread for as long as somebody's
        update takes, and every caller has something better to do with a
        "no" — queue the tap, skip the tick, say "busy". What they say
        stays with them.
        """
        return self._update_lock.acquire(blocking=False)

    @property
    def update_claimed(self):
        """True while somebody holds the update lock. Only looks.

        For a caller that just wants to say "busy" before trying — the
        Web page's banner. The answer can be stale the moment it is
        returned; the claim that decides is still `claim_update`.
        """
        return self._update_lock.locked()

    def release_update(self, *, after=None, kick=True):
        """Give the update lock back, then run `after` (if given).

        Kept held while the self-update's helper is swapping the
        container: this process is about to be stopped and nothing may
        start an update in its last seconds.

        Nothing in here may raise before or instead of the release. The
        swap flag is read defensively because a bare read once raised
        inside a `finally` — `_swap_in_flight` had been orphaned out of
        `__init__` by an unrelated edit, the lock stayed held for the life
        of the process, and every later update queued behind a batch that
        had finished 40 minutes earlier. A release of a lock nobody holds
        is a bug somewhere else; it goes to the log instead of raising out
        of the caller's `finally` over whatever it was about to report.

        `after` runs outside any handler here, so when the release happens
        in an exception unwind it still sees `sys.exc_info()`.
        `selfupdate.run_queued` needs that: after a crashed batch it
        cancels the queued self-update instead of restarting on top of it.

        Then the queue (Q13): whatever was queued while this was held is
        worked off by the engine's one drainer, whoever released and on
        every install. After `after`, so when a self-update and
        containers both waited, the self-update goes first — the order
        it always had (my decision of 18.11.2026). `after` goes along to
        the drainer, whose own releases hand it on in turn. Those pass
        `kick=False`: the drain is already working the queue, and its
        next turn takes whatever came in.
        """
        try:
            swap = bool(getattr(self, "_swap_in_flight", False))
        except Exception:
            swap = False
        if not swap:
            try:
                self._update_lock.release()
            except RuntimeError:
                print("release_update: the update lock was not held "
                      "— nothing to give back")
        try:
            if after is not None:
                after()
        finally:
            if kick:
                self.kick_drain(after=after)

    @contextlib.contextmanager
    def update_claim(self, after=None):
        """`claim_update` / `release_update` as a `with` block.

        Yields whether the lock was claimed. Only a claim that won is
        released (and only then does `after` run); a crash inside gives
        the lock back and goes on.
        """
        claimed = self.claim_update()
        try:
            yield claimed
        finally:
            if claimed:
                self.release_update(after=after)

    # ── Claim, or say why not and queue ────────────────────────────────
    def claim_or_queue(self, container_key):
        """Claim the update lock for one container, or queue it.

        `(True, None, {})` when the lock was free: the caller now holds
        it and gives it back with `release_update`. Otherwise `(False,
        key, params)`, with the lock untouched and `key` what to tell
        the person:

          update_same_running    `container_key` is the one being
                                 updated right now — not a second job
          update_queue_full      {max}: it did not fit
          update_already_queued  {since_seconds}: it was on the queue
          update_queued          {pos}: it is on the queue now

        A key and its parameters, never a sentence: every connection
        adds the container's name and says it in its own words, and
        `since_seconds` stays raw so each renders it as it likes.
        """
        if self.claim_update():
            return True, None, {}
        # Tapping the SAME container again while it is running is not a
        # second piece of work — it is the same one. Queueing it meant
        # the drain picked it up after the update had finished and
        # emptied the pending list, and then reported "container not in
        # update list" directly under a green OK (seen on my local
        # instance, 08.10.2026).
        pos = (None if container_key in self.updating
               else self.enqueue_update(container_key))
        return (False,) + self._queue_answer(container_key, pos)

    def claim_or_queue_many(self, keys):
        """`claim_or_queue` for one request that names several containers.

        ONE claim for the whole request: `(True, [])` when the lock was
        free, and the caller runs every key under it — a batch never
        queues behind itself. Otherwise `(False, outcomes)`, one
        `(key, i18n key, params)` per distinct key in the order asked,
        with the same keys as `claim_or_queue`. The new keys go on the
        queue all together or not at all (`enqueue_updates`); when they
        do not fit, the request is answered as a whole: every key of it
        gets `update_queue_full`, the running one and one already
        waiting too (that one keeps its place on the queue). With
        nothing new to place, each key is answered for itself.
        """
        if self.claim_update():
            return True, []
        keys = list(dict.fromkeys(keys))
        running = self.updating
        # Onto the queue in the order the batch would have run them: the
        # drainer runs one key per run, so the batch's group sort never
        # sees two of them. Without this, the same Web bulk ran a group's
        # dependent before its head whenever the lock happened to be busy
        # (measured in T5 on 2354a6b: db,web of the group [web, db] ran
        # web → db on a free lock, db → web queued). The answers stay in
        # the order asked.
        positions = self.enqueue_updates(self._in_group_order(
            [k for k in keys if k not in running]))
        # My decision for batches (18.11.2026): a batch that does not
        # fit is answered as a whole with "queue full". Telling one key
        # of it "already queued" next to "queue full" for the rest reads
        # as if part of the request had gone through.
        if -1 in positions.values():
            return False, [(k,) + self._queue_answer(k, -1) for k in keys]
        return False, [(k,) + self._queue_answer(
            k, None if k in running else positions[k]) for k in keys]

    def _in_group_order(self, keys):
        """`keys` in the order they go on the queue: hosts as they first
        come up (the Web worker splits a selection the same way), and on
        each host the group members by group and place before everything
        else, as `_process_update_batch` sorts them. The rest keeps the
        order it came in, which is the selection. That is not quite what
        a free lock does: there the worker takes a host's containers in
        pending-file order, so containers in no group run in the order
        the pending file lists them, and the two match only when the
        selection does too. Groups and hosts match. A host whose groups cannot
        be read keeps its keys as they are — the order is a courtesy,
        not a reason to refuse the request."""
        from container_store import split_host_key
        hosts, by_host = [], {}
        for k in keys:
            h = split_host_key(k)[0]
            if h not in by_host:
                by_host[h] = []
                hosts.append(h)
            by_host[h].append(k)
        out = []
        for h in hosts:
            mine = by_host[h]
            try:
                groups = host_store(self, h).get_groups() or {}
            except Exception:                               # noqa: BLE001
                out.extend(mine)
                continue
            position = {}
            for gid, g in groups.items():
                for pos, cname in enumerate(g.get("containers") or []):
                    position[cname] = (gid, pos)

            def _rank(k):
                gp = position.get(split_host_key(k)[1])
                return (0,) + gp if gp else (1, "", 0)
            out.extend(sorted(mine, key=_rank))
        return out

    def _queue_answer(self, container_key, pos):
        """`(i18n key, params)` for a claim that lost. `pos` is what the
        queue said (`enqueue_update`), or None for the container that is
        being updated right now."""
        if pos is None:
            return "update_same_running", {}
        if pos == -1:
            return "update_queue_full", {"max": self.UPDATE_QUEUE_MAX}
        if pos == 0:
            # How long it has been sitting there, because "already in
            # the queue" alone reads as "you already pressed this" — and
            # the entry may have been stranded since another day (see
            # the Telegram sweep). A genuine double tap says "for 2s",
            # which is also the truth.
            waited = self.queued_waiting_seconds(container_key)
            return "update_already_queued", {
                "since_seconds": waited if waited is not None else 0}
        return "update_queued", {"pos": pos}

    # ── Working the queue off ───────────────────────────────────────────
    def drain_queue(self, run_one, *, group_of, say, last_group=None,
                    after=None):
        """Run whatever was queued while an update held the lock.

        `run_one(key)` updates one container and returns the group a
        failure of it takes down, or None. It runs under the drain's
        claim and must not give the lock back: the drain claims before
        it takes an entry, holds the claim through `run_one` and
        releases it after, handing `after` to `release_update` — one
        claim and one release per entry, and the drain is the only one
        that releases. `group_of(key)` names a key's group.
        `say(key, params)` tells the person; `params` carry raw container
        keys under "keys", each connection names them its own way:

          update_queue_dropped_selfupdate  {keys}: not run, a self-update
                                           restarts the process first
          update_queue_skipped_group       {group, keys}: group-mates of
                                           the failure, taken off
          entry_failed                     {key, error}: `run_one` raised.
                                           Not an i18n key — Telegram
                                           says the error raw.

        `last_group` is the group of the run that came before the drain.
        `after` is what every release hands on (a queued self-update), as
        it did when each run released for itself. Those releases do not
        kick the drainer (`kick=False`): this loop is the drain, and its
        next turn sees whatever was queued meanwhile.

        Since Q13 the one caller is the engine's drainer
        (`kick_drain` → `_drain_on_thread`), with the engine's own
        `_drain_one`, `_group_of_key` and `_say_drain`. Called anywhere
        else it would be a second drainer next to that one.

        Never raises because of `run_one`: a queue is a convenience and
        must not be able to take down the update that finished
        successfully before it.
        """
        handed_on = False
        while True:
            # A self-update waiting with the lock free was queued behind a
            # release that handed nothing on (`check_selfupdate_auto`
            # passes no `after`). Nobody else is going to start it, so the
            # drain does, through a claim and a release of its own, before
            # it looks at the queue: up to date it comes back and the
            # queue runs, a restart is named below. Once only — a hand-over
            # that leaves it waiting (no context to start it from) falls
            # through to the drop. Without this the queue was dropped
            # "because a self-update restarts first" and that self-update
            # stayed queued (Q13 review, measured).
            if (not handed_on and self._queued_selfupdate
                    and not getattr(self, "_swap_in_flight", False)):
                handed_on = True
                if self.claim_update():
                    self.release_update(after=after, kick=False)
                    continue
            if self._drop_for_selfupdate(say):
                return
            # A failed container takes its group-mates with it: the group
            # order exists because they depend on each other, and updating
            # the next one against a head that just failed is how you get
            # an app talking to a database that rolled back. Same rule the
            # automatic batch already follows.
            if last_group:
                self.drop_group_mates(last_group, group_of=group_of, say=say)
                last_group = None
            if not self.has_queued_updates():
                return
            # Claimed per entry rather than held across the whole queue,
            # so the scheduler and a queued self-update can still get in
            # between two entries — but not halfway through one. It used
            # to be taken and given straight back here, with `run_one`
            # claiming it again; anybody landing in that moment sent the
            # entry to the back of the queue with a second "queued at
            # position N" (Q10, measured in test_update_queue).
            if not self.claim_update():
                # Something else holds it. Nothing was taken, so the entry
                # keeps its place at the head; whoever holds the lock now,
                # or the next sweep, works the queue off.
                return
            key = self.take_queued_update()
            if key is None:
                # Emptied between the look and the claim.
                self.release_update(after=after, kick=False)
                return
            try:
                last_group = run_one(key)
            except BaseException as e:
                # Given back inside the handler, so `after` still sees the
                # crash: `selfupdate.run_queued` cancels a queued
                # self-update after one rather than restart on top of it.
                self.release_update(after=after, kick=False)
                if not isinstance(e, Exception):
                    raise
                say("entry_failed", {"key": key, "error": str(e)})
                last_group = None
                continue
            # Given back before anything is said: a `say` that raised
            # while the drain still held the claim kept the update lock
            # for the life of the process. A self-update queued during
            # this entry runs from this release, through `after`, and the
            # top of the loop then sees whether it is restarting.
            self.release_update(after=after, kick=False)

    def _drop_for_selfupdate(self, say):
        """True when a self-update is waiting or swapping, after taking
        the queue off and naming what was on it.

        A self-update restarts the process, which would take the rest of
        the queue with it. Stop and NAME what is not going to run —
        dropping work quietly is the bug this whole change exists to fix,
        and doing it here would be worse than never having queued at all.

        A swap under way (`_swap_in_flight`: the helper is launched and
        this process is about to be stopped) counts too, because that is
        the one moment "about to restart" is a fact. A self-update queued
        during an entry is only known to restart once it has run: up to
        date, or a failed pull, comes back without one, and then the rest
        of the queue runs. Named before it started, the rest was dropped
        for nothing under "about to restart" (Q10 review, measured).
        Not measured, read from `selfupdate.swap`: the helper's stop is
        meant to land inside its 30-second wait, so on a real restart the
        drain usually never gets back here, and the rest goes with the
        process unnamed, as before Q10. The entries stay in the pending
        list either way.
        """
        try:
            swapping = bool(getattr(self, "_swap_in_flight", False))
        except Exception:
            swapping = False
        if not self._queued_selfupdate and not swapping:
            return False
        left = self.drop_queued_updates()
        if left:
            say("update_queue_dropped_selfupdate", {"keys": left})
        return True

    def drop_group_mates(self, group, *, group_of=None, say=None):
        """Take `group`'s members off the queue and say which.

        A failed container takes its queued group-mates with it (see
        `drain_queue`). The drain does it before its next entry; a
        single update that failed does it itself before it lets go of
        the lock, so the drainer that release starts finds them gone.
        `group_of` and `say` default to the engine's own. Returns the
        keys it dropped.
        """
        group_of = group_of or self._group_of_key
        say = say or self._say_drain
        mates = self.drop_queued_updates(lambda k: group_of(k) == group)
        if mates:
            say("update_queue_skipped_group", {"group": group, "keys": mates})
        return mates

    # ── The one drainer (Q13) ───────────────────────────────────────────
    def kick_drain(self, after=None):
        """Work the queue off on a thread of its own, unless one already is.

        Every release of the lock ends here (`release_update`), and so
        do both sweeps, the Telegram poll loop's and the scheduler's
        (`sweep_queue(engine.kick_drain)`). Until Q13 the queue was
        worked off where a Telegram path happened to let go of the lock
        — the end of a single update, and the poll loop — so an install
        with Telegram switched off never ran a queued update at all.

        At most one drainer runs. The slot is taken under the queue's
        own lock, so two releases at the same moment start one thread,
        and a kick while it works starts nothing: the running one takes
        the new entry on its next turn. Its own releases do not kick
        (`kick=False`, see `drain_queue`). While a scheduled pass holds
        it back (`hold_drain`) a kick is kept, not started, and the end of
        the pass starts it.

        `after` is what each of the drainer's releases hands on, as the
        release that kicked it did (a front end's queued self-update);
        without one, the engine's own hand-over through the shared
        self-update context.

        Returns the thread it started, or None. Never raises: it runs
        from `release_update`, inside other flows' `finally`.
        """
        # An engine assembled by hand without a queue (most fixtures
        # build one with `__new__`) has nothing to work off.
        if not getattr(self, "_update_queue", None) or getattr(
                self, "_update_queue_lock", None) is None:
            return None
        try:
            with self._update_queue_lock:
                if getattr(self, "_drain_holds", 0):
                    # A scheduled pass is between two of its steps; the
                    # kick waits for its end (`hold_drain`).
                    self._held_kick = (after,)
                    return None
                if (getattr(self, "_drainer", None) is not None
                        or not self._update_queue):
                    return None
                t = threading.Thread(target=self._drain_on_thread,
                                     args=(after,), name="update-queue-drain",
                                     daemon=True)
                # Started under the lock and only then put in the slot,
                # so nobody reading the slot finds a thread that has not
                # started. `start` only waits for the thread to exist;
                # its first look at the queue waits for this lock, and so
                # does its giving the slot back.
                t.start()
                self._drainer = t
            return t
        except Exception as e:                              # noqa: BLE001
            print(f"Queue drain could not start: {e}")
            return None

    def hold_drain(self):
        """Keep the drainer back until `let_drain_go`, for one pass.

        The scheduler's pass is a row of claims: the automatic self-update
        check, `handle_autoupdates` once per host, the post-update cleanup
        and the disk-warning cleanup each claim and release on their own.
        Every one of those releases kicks the drainer, and a queued update
        then took the lock in the gap between two hosts: the next host's
        batch failed its claim and was "skipped" until the next cron match,
        usually a day later (Q13 review, measured). The lock is free in
        that gap, the pass is not done.

        So for the length of a pass a kick is kept, not started; the last
        `let_drain_go` starts it. Counted, because the deferred check after
        a restart walks the hosts on a thread of its own and can overlap a
        pass. A drainer that was already running goes on as before — this
        only stops a new one. Never raises.
        """
        lock = getattr(self, "_update_queue_lock", None)
        if lock is None:
            return
        try:
            with lock:
                self._drain_holds = getattr(self, "_drain_holds", 0) + 1
        except Exception as e:                              # noqa: BLE001
            print(f"Queue drain hold failed: {e}")

    def let_drain_go(self):
        """End one `hold_drain`; the last one starts the kick it kept.

        Returns the drainer it started, or None. Never raises.
        """
        lock = getattr(self, "_update_queue_lock", None)
        if lock is None:
            return None
        try:
            with lock:
                holds = getattr(self, "_drain_holds", 0)
                self._drain_holds = holds - 1 if holds > 0 else 0
                kept = getattr(self, "_held_kick", None)
                # While another hold is left, the kick stays kept. Without
                # this check `kick_drain` would keep it again (it looks at
                # the count itself), so no mutation of this line alone
                # shows; it saves the round trip.
                if self._drain_holds or kept is None:
                    return None
                self._held_kick = None
        except Exception as e:                              # noqa: BLE001
            print(f"Queue drain hold failed: {e}")
            return None
        return self.kick_drain(after=kept[0])

    def _drain_on_thread(self, after):
        """The drainer's body: `drain_queue` with the engine's own hooks.

        On its way out it gives the slot back and sweeps once. A drain
        that lost its claim to somebody else leaves the entry for that
        somebody's release — and if that release came while this thread
        still held the slot, its kick started nothing.
        """
        finished = False
        try:
            self.drain_queue(self._drain_one, group_of=self._group_of_key,
                             say=self._say_drain,
                             after=after or self._hand_on_selfupdate)
            finished = True
        except Exception as e:                              # noqa: BLE001
            print(f"Queue drain failed: {clip(e)}")
        finally:
            with self._update_queue_lock:
                self._drainer = None
        # Not after a drain that raised: the same entry would raise again
        # on a new thread, as often as this could start one.
        if finished:
            self.sweep_queue(lambda: self.kick_drain(after=after))

    def _drain_one(self, key):
        """One queued entry, under the drain's claim: `drain_queue`'s
        `run_one`. Returns the group its failure takes down, or None.

        What Telegram's single update did for a queued key, said through
        the broadcast seam instead of one chat: an unreadable pin list
        refuses it, `run_keys` runs it on its own host's checker, and
        the result is the batch's card for the plugins plus a line for
        Telegram (`Broadcast.update_line`, beside "Updating …", the
        cascade and "all done"). Whatever did not run is told to every
        channel — a queued update that quietly does nothing is the
        thing this queue exists to avoid.
        """
        seam = self._seam()
        checker = self._checker_for_key(key)
        if checker is not None:
            from update_checker import pin_list_unreadable
            if pin_list_unreadable(checker):
                seam.tell(self._t("update_refused_pin_list"))
                return None

        def starting(k, target):
            seam.update_line(self._t("update_single_starting",
                                     name=self._key_label(k)))

        def report(k, target, success, msg):
            seam.update_line(f"{'✅' if success else '❌'} "
                             f"{self._display_name(target)}: {msg}")

        results, _all_done = self.run_keys(
            [key], checker_for=self._checker_for_key,
            group_of=self._group_of_key,
            enrich=self._enrich_with_source_url,
            starting=starting, report=report,
            done=lambda: seam.update_line(self._t("update_all_done")),
            note=lambda k, line: seam.update_line(line))
        for k, target, _success, msg, last_group in results:
            if target is None:
                seam.tell(self._drain_skipped_text(k, msg))
                return None
            return last_group
        return None

    def _seam(self):
        """The broadcast seam main.py wires, or one over the notifier
        alone for an engine nobody wired."""
        seam = getattr(self, "broadcast", None)
        if seam is None:
            from broadcast import Broadcast
            seam = Broadcast(notifier=getattr(self, "notifier", None))
        return seam

    def _checker_for_key(self, key):
        """The checker of the host a queue key names, or None when no
        managed host is called that (run_keys then refuses the key)."""
        from container_store import split_host_key
        host_name = split_host_key(key)[0]
        hosts = getattr(self, "hosts", None) or ()
        if host_name in (None, "", LOCAL_HOST):
            for h in hosts:
                if getattr(h, "is_local", False):
                    return getattr(h, "checker", None)
            return getattr(self, "local_checker", None)
        for h in hosts:
            if getattr(h, "name", None) == host_name:
                return getattr(h, "checker", None)
        return None

    def _group_of_key(self, key):
        """The id of the group a queued container belongs to on its own
        host, or None — the lookup Telegram's `_group_of` does."""
        try:
            from container_store import split_host_key
            host_name, name = split_host_key(key)
            gid, _group = host_store(self, host_name).get_group_for_container(
                name)
            return gid or None
        except Exception:                                   # noqa: BLE001
            return None

    def _key_label(self, key):
        """`web` locally, `web @nas` on a remote host — how the buttons
        and the result lines name it."""
        from container_store import split_host_key
        host, name = split_host_key(key)
        return name if host == LOCAL_HOST else f"{name} @{host}"

    def _drain_skipped_text(self, key, why):
        """The sentence for a queued key `run_keys` ran nothing for."""
        if why == "no_pending_updates":
            return self._t("no_pending_updates")
        if why == "host_unknown":
            from container_store import split_host_key
            hosts = getattr(self, "hosts", None)
            names = [h.name for h in hosts] if hosts else [LOCAL_HOST]
            return self._t("host_unknown", name=split_host_key(key)[0],
                           hosts=", ".join(f"`{n}`" for n in names))
        # Off the queue, "not pending" almost always means the update
        # already happened while the entry waited. "Not in the update
        # list" there would put a warning under a green result.
        return self._t("update_already_done", name=self._key_label(key))

    def _say_drain(self, key, params):
        """What the drain reports, to every channel. Containers are
        named the short way, as Telegram's drain named them."""
        from container_store import split_host_key

        def short(k):
            return split_host_key(k)[1]
        if key == "entry_failed":
            text = f"❌ `{short(params['key'])}`: {params['error'][:200]}"
        else:
            kw = {k: v for k, v in params.items() if k != "keys"}
            kw["names"] = ", ".join(f"`{short(k)}`" for k in params["keys"])
            text = self._t(key, **kw)
        self._seam().tell(text)

    def _hand_on_selfupdate(self):
        """Start a self-update that queued behind a drained entry. The
        drainer's `after` when the kick brought none."""
        ctx = getattr(self, "selfupdate_ctx", None)
        if ctx is None:
            return
        try:
            import selfupdate
            selfupdate.run_queued(ctx)
        except Exception as e:                              # noqa: BLE001
            print(f"Queued self-update hand-over failed: {clip(e)}")

    def sweep_queue(self, drain):
        """Pick up queued work that nobody is going to drain.

        `drain()` works the queue off — `kick_drain` everywhere since
        Q13: the Telegram poll loop, the scheduler's `_after_tick` and
        the drainer on its way out all hand it in. This only decides
        whether to call it.

        The drain used to run in exactly one place: at the end of a
        Telegram single update, once it had let go of the lock. The lock
        itself is taken in many more — the scheduled run, the Web UI, a
        self-update and further Telegram paths among them. Queue
        something while one of THOSE is holding it and the entry waits
        for the next Telegram single update to finish, however long that
        takes.

        Measured on my own instance, 23.10.2026: three
        buttons pressed at 19:15:17, :21 and :22 (audit log). The second
        came back "queued — position 2" and the third "already in the
        queue", neither of which made sense for a first press. The queue
        had been holding an entry since an earlier day, and both
        portainers were then updated within the same minute once an
        unrelated 2.3 GB update happened to end. Nothing was lost; it
        simply sat there, and the person pressing the button was told
        something that read like a correction.

        Called once per poll cycle, so a stranded entry now waits
        seconds — and since Q13 once per scheduler pass too, which is
        what an install without Telegram has (30 to 60 s, see
        `Scheduler._after_tick`). Every release kicks the drainer
        anyway; the sweeps are for what a release does not cover
        (`selfupdate.start` gives the lock back by hand). Cheap when
        there is nothing to do: one lock, one truth test.

        Never raises — this runs inside the listener loop, and taking
        that down to retry a queued update would be a far worse trade.
        """
        try:
            if not self.has_queued_updates():
                return
            # Somebody is working: they will either drain it themselves
            # or leave it for the next sweep. Racy by nature and
            # harmless — the drain claims per entry and leaves the entry
            # where it is if it loses.
            if self.update_claimed:
                return
            drain()
        except Exception as e:                              # pragma: no cover
            print(f"Queue sweep failed: {e}")

    def _manages_host(self, name):
        """Whether `name` is a host this engine manages: the local one
        always, any other only while the registry holds it. No registry
        (a single-host install) knows nothing but the local host — the
        same reading `run_updates`' `_checker_of` refuses on."""
        if name in (None, "", LOCAL_HOST):
            return True
        return any(getattr(h, "name", None) == name
                   for h in (self.hosts or ()))

    def _wait_after_last_recreate(self, host_name, name):
        """Wait what the batch would have waited between the last
        recreate and this one: the cooldown of the container that ran
        last, plus its group's pause when `name` is in the same group on
        the same host — counted from when that recreate finished, so
        whatever time has passed since is not waited again.

        Inside one batch those two waits come from `_maybe_cooldown`
        after a container and the group pause before the next. A single
        update is a batch of one, and the drain runs every queued entry
        as one under its own claim, so without this neither wait ever
        happened between a tap and what was queued behind it. In the
        same order as the batch: the cooldown first, then the pause.
        """
        last = getattr(self, "_last_recreate", None)
        if not last:
            return
        l_host, l_group, l_name, at = last
        cooldown = host_store(self, l_host).get_cooldown(l_name) or 0
        pause = 0
        if l_group and host_name == l_host:
            groups = host_store(self, host_name).get_groups() or {}
            g = groups.get(l_group) or {}
            if name in (g.get("containers") or []):
                pause = int(g.get("wait_seconds", 0) or 0)
        elapsed = _now(self) - at
        cooldown_left = max(0, cooldown - elapsed)
        pause_left = max(0, cooldown + pause - elapsed) - cooldown_left
        if cooldown_left > 0:
            print(f"Update cooldown: waiting {cooldown_left:g}s after "
                  f"{l_name} before {name}")
            _wait(cooldown_left, self)
        if pause_left > 0:
            _wait(pause_left, self)

    def run_keys(self, keys, *, checker_for, group_of=None, enrich=None,
                 starting=None, report=None, done=None, note=None,
                 pending_file=None):
        """Update the containers behind `keys`, one after the other.

        The body of what used to be Telegram's single update, without the
        words: look each key up in the pending file, say it is in flight,
        run it through the shared batch (`_process_update_batch`, one
        entry, on `checker_for(key)`), take the entry out of the file,
        and stop saying it is in flight. The caller holds the update lock
        for the whole call; this neither claims nor releases it.

        Through the batch since Q11c, so a single update gets what
        "Update all" gives the same container: the netns snapshot, the
        cleanup after a full disk, a group head's dependents restarted,
        and its result reported to the other channels through the
        broadcast seam (the caller's `report` only says it in its own
        chat). And what the batch does between two of its entries is
        done between the last recreate and this one: the cooldown of the
        container that ran before, and the group's pause when both are
        in the same group (`_wait_after_last_recreate`). The drain runs
        one key per claim, so without that a queued group-mate never got
        the group's wait and a cooldown never held anything back.

        One batch per key, in the order given, not one per host: every
        caller passes a single key (a tap, a drained entry), and a queue
        keeps its own order. Group order inside a batch only means
        something with several entries.

        Two places where it is not what "Update all" does with the same
        containers. The prune "Update all" runs once per batch when the
        local disk is past DISK_WARN_PERCENT is left out here (once per
        key would be once per tap; a single update never had it). And a
        group-mate still queued behind a tapped head is not in the head's
        batch, so the head's cascade restarts it (and waits for the
        head's health) before its own update recreates it; "Update all"
        leaves in-batch members to their own update. Left so on purpose:
        the queued member's netns snapshot is taken only once the head
        has been recreated, and `netns_target_name` resolves the owner
        through the id the head's recreate retired — that is my reading
        of the code, not run against a daemon — so for a queued sidecar
        it is the cascade that puts it back onto the head first.

        Returns `(results, all_done)`. One result per key, in order:
        `(key, target, success, msg, last_group)`. `target` is the
        pending entry that ran, or None when nothing ran — then `msg` is
        `"no_pending_updates"` (no usable file at all), `"not_pending"`
        (the file has no entry for this key) or `"host_unknown"` (it has
        one, but for a host this engine does not manage) and `success` is
        None. A raising `update_container` is a failure with its error as
        `msg`, clipped the way the batch clips it (`errfmt.clip`; until
        Q11c the single update cut at 200 characters and lost the end).
        `last_group` is `group_of(key)` after a failure and None after a
        success — the group a failure takes out of the queue. `all_done`
        is True when an entry was taken out and the file is empty after.

        The hooks are the caller's, so the words and the stand-ins the
        tests put on a bot stay where they were:

        * `enrich([entry])` resolves the release link before the result
          is said (the result line and the other channels' card both
          carry it). Called before the key is marked or `starting` is,
          as before Q11c; the batch is handed a no-op in its place. A
          lookup that raises is logged and the key runs without a link;
        * `starting(key, target)` is called once the key is marked in
          flight, before the wait for the last recreate and the update.
          It is a hook rather than part of the result because the
          "Updating …" line has to go out while the update runs (and
          while it waits), not after it;
        * `report(key, entry, success, msg)` is called as soon as the
          batch has told the other channels, before the entry leaves the
          file: a file that cannot be rewritten (a full disk) must not
          swallow the report of an update that ran. `entry` is the
          batch's copy of the pending entry, with its link;
        * `note(key, line)` gets every other line the batch wrote for
          the key: a group head's cascade, the cleanup after a full
          disk. Without it they are only in the batch's return;
        * `done()` is called once, when the entry just taken out was the
          last one in the file — exactly when `all_done` comes back
          True, but while that key is still marked in flight. The "all
          done" line goes out there: a second tap on the same container
          while it is being sent must still be answered "the same one is
          running", not be queued (Q11a review, measured);
        * `group_of` is the caller's; without one no failure names a
          group (the engine has no group lookup of its own);
        * `pending_file` is the file to read and rewrite; this engine's
          own config names it when None.

        The file is read once, before the first key, and each write
        comes from that copy: whatever another writer puts in the file
        while several keys run is overwritten by the next key's write.
        One key per call, as Telegram calls it today, still has that gap
        across its whole `update_container` (minutes): an entry another
        writer takes out meanwhile comes back with this key's write
        (measured in the Q11a review). With one update at a time that
        never mattered; once hosts update in parallel (P1), two calls
        rewrite the shared file from their own copies, and this
        read-modify-write is where that has to be fixed. A host that
        stops answering is a failure like any other: the batch takes a
        netns snapshot that raises as that container's failure. A key
        that raises past its own report (the file write failing, a
        report raising twice) ends the call there: its in-flight mark
        goes, the keys after it do not run, and the error reaches the
        caller instead of any results.
        """
        import pending as _pending
        from container_store import split_host_key
        if enrich is None:
            enrich = self._enrich_with_source_url
        if group_of is None:
            def group_of(key):
                return None
        if pending_file is None:
            pending_file = self.config.pending_file
        results = []
        written = False
        updates = _pending.load(pending_file)
        for key in keys:
            try:
                if updates is None:
                    # No file, or one nothing can read (cut off mid-write,
                    # edited by hand): nothing is pending that this could
                    # act on. A file holding `[]` is a different answer —
                    # the last single update leaves exactly that — and
                    # goes on to "not pending".
                    results.append((key, None, None, "no_pending_updates",
                                    None))
                    continue
                host_name, name = split_host_key(key)
                target = next((u for u in updates
                               if _pending.key_of(u) == (host_name, name)),
                              None)
                if not target:
                    results.append((key, None, None, "not_pending", None))
                    continue
                if not self._manages_host(host_name):
                    # Pending, but for a host that has left DOCKER_HOSTS
                    # since (a button or a queued tap from before). What
                    # `checker_for` hands back for it is another machine:
                    # Telegram's falls back to the local checker, and the
                    # local container of the same name was updated (Q11b,
                    # measured in the callback matrix). Nothing runs, the
                    # entry stays. After the lookup on purpose: a key
                    # that is not pending still answers "not pending".
                    results.append((key, None, None, "host_unknown", None))
                    continue
                ck = checker_for(key)
                if ck is None:
                    # Nobody can reach that host: the same answer, before
                    # anything is marked or waited for.
                    results.append((key, None, None, "host_unknown", None))
                    continue
                # A copy, because the batch writes into its entries (the
                # link, the netns owner) and the file must not get those.
                # The link is looked up here, before anything is marked
                # or said, as before Q11c (left to the batch, it ran
                # after "Updating …"). A lookup that raises is logged and
                # the update goes on without the link: a missing link is
                # cosmetic. Until the Q11c follow-up it ended the call
                # with nothing said, the entry and the queue behind it
                # stuck (review, measured).
                entry = dict(target)
                try:
                    enrich([entry])
                except Exception as e:              # noqa: BLE001
                    print(f"Link lookup for {key} failed, updating "
                          f"without a link: {clip(e)}")
                # Say what is running, where the rest of the program can
                # see it: the Web UI's "updating" badge, and a second tap
                # that has to know this one is already in flight. Before
                # the wait below: the entry is off the queue and this
                # call's, so a tap during the wait is "the same one".
                self.mark_updating(key, target.get("new_version") or "")
                # Said before the wait, not after: a cooldown can be
                # minutes, and the chat would show nothing but the toast
                # until it was over (Q11c review). The Web UI's badge
                # already says "updating" from the mark above.
                if starting is not None:
                    starting(key, target)
                self._wait_after_last_recreate(host_name, name)
                # Through the shared batch (Q11c), so a tap gets what
                # "Update all" gives the same container: the netns
                # snapshot, the ENOSPC cleanup, the group head's cascade
                # and the report to the other channels. The link is on
                # `entry` already, so the batch looks up none. A netns
                # snapshot that raises is that container's failure there.
                outcome = []

                def on_result(u, success, msg, key=key, entry=entry):
                    outcome.append((success, msg))
                    if report is not None:
                        report(key, entry, success, msg)
                # What the batch raises outside its per-container guard
                # (a report that raised twice) ends the call, and the
                # entry stays in the file.
                lines, _n, _m = self._process_update_batch(
                    [entry], ck, auto=False, enrich=lambda us: None,
                    checker_of=lambda u, ck=ck: ck,
                    on_result=on_result, marks=False,
                    proactive_cleanup=False, snapshot_fails_entry=True)
                if not outcome:
                    # One entry, not auto, in a batch of its own: the
                    # batch skips it only when it refuses the checker,
                    # which `ck is None` above already answered. Kept so
                    # that a batch that ran nothing is never read as a
                    # result.
                    results.append((key, None, None, "host_unknown", None))
                    continue
                # The last word counts: a report that raised was told
                # again as a failure.
                success, msg = outcome[-1]
                last_group = None if success else group_of(key)
                if note is not None:
                    # What the batch said beside the result line: the
                    # cascade, the cleanup after a full disk.
                    said = {f"{'✅' if ok else '❌'} "
                            f"{self._display_name(entry)}: {m}"
                            for ok, m in outcome}
                    for line in lines:
                        if line not in said:
                            note(key, line)
                # Matched on (host, name): another host's same-named
                # entry must survive.
                updates = [u for u in updates
                           if _pending.key_of(u) != (host_name, name)]
                _pending.write(pending_file, updates)
                written = True
                results.append((key, target, success, msg, last_group))
                if not updates and done is not None:
                    # Nothing left in the file, so every key after this
                    # one is "not_pending": this is the all-done moment,
                    # said before the mark below goes.
                    done()
            finally:
                self.clear_updating(key)
        return results, (written and not updates)

    def cleanup_guarded(self, checker, *, after=None):
        """Image cleanup under the update lock (#2 follow-up, @famewolf).

        `docker image prune -a` filters on image CREATION time, so an
        image built upstream days ago but pulled seconds ago is fair
        game — pruning during an update's pull→run window would delete
        the image the update is about to run. Going through the same
        lock closes that window for every trigger: Telegram /cleanup,
        Discord /cleanup, the Web UI button, the disk-warning cleanup and
        the post-update one.

        Returns `(ok, msg)` from `cleanup_images`, or `(None,
        "cleanup_busy")` when an update flow holds the lock. That second
        one is the KEY, not the sentence: each chat says it in its own
        language. A skipped cleanup is never dangerous — the next trigger
        simply runs it — so the lock is not waited for, and callers
        decide how loudly to report the skip.

        `after` is the caller's self-update handoff; it runs once the
        lock is back, and only when this cleanup had it.
        """
        if not self.claim_update():
            return None, "cleanup_busy"
        try:
            return checker.cleanup_images()
        finally:
            self.release_update(after=after)

    # ── Neutral orchestration helpers ──────────────────────────────────
    # Telegram-agnostic building blocks (no send_message, no inline
    # keyboards) moved off TelegramBot (v2 groundwork). The bot keeps a
    # thin delegator for each so the update paths still living on the bot
    # (_process_update_batch, handle_autoupdates, notify_updates, …) keep
    # calling self._X, and so the tests that monkeypatch bot._X on an
    # instance keep shadowing them. Where a helper needs the registry
    # `checker`, it stays a PARAMETER — the engine holds no persistent
    # checker reference.

    def _is_major_bump(self, update, checker):
        """Detect whether the available update for `update` is a SemVer major
        bump.

        Strategy: parse the container's current image tag as SemVer; if that
        succeeds, query the registry for the highest matching SemVer tag and
        compare majors. Containers using `:latest` or non-SemVer tags can't
        be majored-detected reliably without pulling — the gate transparently
        becomes a no-op for those (returns (False, None, None)).

        Returns (is_major, current_tag, candidate_tag).
        """
        image = update.get("image", "")
        try:
            registry, repo, tag = checker._parse_image(image)
        except Exception:
            return False, None, None
        if not registry or not tag:
            return False, None, None
        cur = checker._parse_semver(tag)
        if cur is None:
            return False, None, None
        best_tag, best_parsed = checker.get_highest_semver_tag(registry, repo, tag)
        if not best_parsed:
            return False, None, None
        return best_parsed[0] > cur[0], tag, best_tag

    def _resolve_update_policy(self, name, checker):
        """The effective update policy for a container (v1.53.0, roadmap #2).

        Precedence: the `docksentry.policy` container label wins over the
        global `UPDATE_POLICY` env default. Valid values are `all`, `minor`
        and `patch`; anything else (or an unreadable label) falls back to
        `all` — fail-open, so a typo never silently freezes a container's
        updates. Mirrors the label-over-toggle precedence used for
        auto/ask-major (#42, @LeeNX)."""
        if checker is not None:
            try:
                labels = checker.get_container_labels(name) or {}
                raw = labels.get("docksentry.policy")
                if raw is not None:
                    v = raw.strip().lower()
                    return v if v in ("all", "minor", "patch") else "all"
            except Exception:
                pass
        pol = (getattr(self.config, "update_policy", "all") or "all").strip().lower()
        return pol if pol in ("all", "minor", "patch") else "all"

    @staticmethod
    def _policy_allows_level(policy, level):
        """Does `policy` permit auto-applying a `level` bump? `all` allows
        everything; `minor` allows minor+patch (blocks major); `patch`
        allows patch only. An unknown `level` (None — we couldn't classify
        the bump) is ALWAYS allowed: fail-open, never skip something we
        couldn't classify. An unknown policy also fails open to allow."""
        if level is None:
            return True
        if policy == "minor":
            return level in ("minor", "patch")
        if policy == "patch":
            return level == "patch"
        # "all" or any unrecognised policy → allow
        return True

    def _age_decision(self, u, checker):
        """Whether this update is too fresh to apply unattended.

        Returns None to allow, or `(days_old, required)` to hold back.

        Two independent reasons people ask for this, and the second is the
        one that makes it more than a preference. Risk deferral — "let
        someone else find the broken release first" — and supply chain: a
        compromised image is usually noticed within days, so not being the
        first to pull it is a real defence. It is the one gap in an
        otherwise complete safety chain here, which already has approval
        gates, semver caps, health-gated rollback, update windows and
        maintenance mode.

        The AUTO path only. A person pressing the button has decided; this
        is about what happens while nobody is watching — the same line as
        `docksentry.auto=false` versus monitor-only.

        `new_created` comes from the remote image's OCI config blob and is
        fetched for every container that has an update, so this costs
        nothing extra. When it is missing the update is ALLOWED: the gate
        cannot judge what it cannot see, and failing closed here would
        silently stop updates for every image whose registry does not
        expose a build date — the same trap `UPDATE_POLICY` fell into.
        """
        required = int(getattr(self.config, "min_image_age_days", 0) or 0)
        try:
            raw = (checker.get_container_labels(u["name"]) or {}).get(
                "docksentry.min-age")
            if raw is not None:
                required = max(0, int(str(raw).strip()))
        except Exception:
            # Broad on purpose. Reading a label means talking to the daemon,
            # and a daemon hiccup must not take the whole auto-update
            # decision with it — the global setting still applies. Narrower
            # catches let a RuntimeError through, which my own test found
            # before this shipped.
            pass
        if required <= 0:
            return None
        created = (u.get("new_created") or "").strip()
        if not created:
            return None
        from datetime import datetime, timezone
        try:
            built = datetime.strptime(created[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            return None
        days = (datetime.now(timezone.utc) - built).days
        if days >= required:
            return None
        return days, required

    def _policy_decision(self, u, checker):
        """Decide whether the update `u` is held back by its container's
        update policy on the AUTO path (v1.53.0). Returns None when the
        update is allowed, otherwise `(level, old, new)` describing the
        blocked bump for the "held back" message.

        Classification reuses the version info Docksentry already computes
        (`old_version` / `new_version`, set best-effort by check_all from
        OCI labels / semver tags). If either is missing we fall back to the
        image tag when it's a full semver, but keep it simple — an
        unclassifiable bump is allowed (fail-open)."""
        policy = self._resolve_update_policy(u["name"], checker)
        if policy == "all":
            return None
        old = (u.get("old_version") or "").strip()
        new = (u.get("new_version") or "").strip()
        # Best-effort fallback: derive `old` from a full-semver image tag
        # when the OCI-label version wasn't available.
        if not old and checker is not None:
            try:
                _, _, tag = checker._parse_image(u.get("image", ""))
                if tag and checker._parse_semver(tag):
                    old = tag
            except Exception:
                pass
        try:
            level = checker._bump_level(old, new)
        except Exception:
            level = None
        if self._policy_allows_level(policy, level):
            return None
        return (level, old, new)

    def _restart_group_dependents(self, head_name, dependents, checker, max_wait=30):
        """After the head of a group is recreated, bring its dependents back
        onto the new head. Waits up to `max_wait` for the head to be healthy.

        A netns sidecar (`network_mode: container:<head>`) can't just be
        `docker restart`-ed once the head was recreated — it still references
        the head's dead old ID and the restart fails (#8). Those are
        *recreated* against the head's current name (via
        checker.recreate_dependent). Non-netns group members are still just
        restarted (cheaper, sufficient).

        Returns a one-line user-facing result string for the update report.
        A health check or a dependent that raises ends up in that string,
        not as an exception: by the time this runs the head's result has
        been reported, and the batch would report it again as failed."""
        # Use the checker's canonical 3-tuple _wait_healthy (outcome, state,
        # health) — NOT a bot-local one. A stray bool-returning duplicate here
        # caused "cannot unpack non-iterable bool object" mid-cascade (#2,
        # @famewolf): the head updated fine, then the dependents kick crashed.
        #
        # A health check that raises is a head whose health is unknown,
        # and the dependents are fixed anyway, as for one that is not
        # healthy. It used to raise out of here into the batch's except
        # after the head's ✅ had gone out: a second, failing report for
        # an update that ran, and its group-mates dropped (review of
        # Q11c, measured with a host that stops answering right after
        # the recreate: TimeoutExpired from `_restart_count`).
        try:
            outcome, _, _ = checker._wait_healthy(head_name, max_wait)
        except Exception as e:                      # noqa: BLE001
            outcome = None
            print(f"⚠ {head_name}: health check failed ({clip(e)}) "
                  f"— fixing dependents anyway")
        if outcome is not None and outcome != "healthy":
            print(f"⚠ {head_name} not healthy ({outcome}) after {max_wait}s — fixing dependents anyway")

        backend = getattr(checker, "backend", None)
        if backend is None:
            import container_backend as _cb
            backend = _cb.default_backend()

        # `failed` carries (name, reason). The reason used to be printed to
        # the console and dropped from the notification, so the message said
        # "failed 1 (x)" and nothing else — @famewolf spent ten days with a
        # dead nzbhydra2 because the one line that could have told him what
        # was wrong went to a log he had no reason to open (#2).
        recreated, restarted, failed = [], [], []
        for dep in dependents:
            try:
                # Through the checker's OWN backend, not a hardcoded `docker`:
                # this runs against whichever CLI and whichever host that
                # checker drives. Hardcoding it meant a Podman install
                # (CONTAINER_CLI=podman) called a binary it may not have, and
                # a group on a remote host had its dependents restarted on
                # the LOCAL machine instead.
                nm = backend.inspect(
                    dep, fmt="{{.HostConfig.NetworkMode}}", timeout=10,
                ).stdout.strip()
                if not nm:
                    # The dependent is gone. Before reporting that, check
                    # whether it is gone because WE left it as `<dep>_old`
                    # — a recreate that was interrupted after the rename
                    # and before the rebuild leaves exactly that, and
                    # nothing used to pick it back up. @famewolf's
                    # nzbhydra2 sat like that for ten days while every run
                    # tried to restart a name that no longer existed (#2).
                    #
                    # Deliberately only `<dep>_old`, and only for a
                    # container we were asked about: a container somebody
                    # else named that way is theirs, not ours to move.
                    if getattr(checker, "recover_dependent", None) and \
                            checker.recover_dependent(dep):
                        restarted.append(dep)
                        print(f"Recovered {dep} from {dep}_old "
                              f"(left by an interrupted recreate)")
                        continue
                if nm.startswith("container:"):
                    ok, detail = checker.recreate_dependent(dep, head_name)
                    if ok:
                        recreated.append(dep)
                    else:
                        failed.append((dep, detail or "no reason given"))
                        print(f"Failed to recreate netns dependent {dep}: {detail}")
                else:
                    r = backend.restart(dep, timeout=30)
                    if r.returncode == 0:
                        restarted.append(dep)
                    else:
                        why = clip(r.stderr or "") or f"exit {r.returncode}"
                        failed.append((dep, why))
                        print(f"Failed to restart dependent {dep}: {why}")
            except Exception as e:                  # noqa: BLE001
                # Any exception, not only a subprocess one: a missing CLI
                # (FileNotFoundError) or a backend's own error is this
                # dependent's failure, with its reason. Left to raise, it
                # turned the head's report into a failure (see above).
                failed.append((dep, clip(e)))
                print(f"Fixing dependent {dep} crashed: {e}")

        done = recreated + restarted
        ok_str = ", ".join(f"`{d}`" for d in done) if done else "—"
        if failed:
            # The failure gets its own lines, above the successes and with
            # the reason attached. It used to be a suffix on the success
            # line — "9 ok (…), failed 1 (…)" — which reads as good news at
            # a glance and was missed for ten days by the person it
            # happened to.
            lines = [f"❌ `{head_name}` dependents: {len(failed)} FAILED"]
            for name, why in failed:
                lines.append(f"   • `{name}`: {why}")
            if done:
                lines.append(f"🔁 {len(done)} ok ({ok_str})")
            return "\n".join(lines)
        verb = "recreated/restarted" if recreated else "restarted"
        return f"🔁 `{head_name}` dependents {verb}: {ok_str}"

    def _maybe_cooldown(self, name, more_remaining, host=None):
        """After recreating `name`, pause for its configured update cooldown
        before the next container in the batch — but only when more updates
        follow. Lets a heavy (GPU/RAM) container's load peak settle so the
        next recreate doesn't contend for memory (#2, @famewolf).

        `host` picks whose cooldown applies (#7): two boxes may both run a
        `plex`, and only one of them is the one with 16 GB of RAM."""
        if not more_remaining:
            return
        cd = host_store(self, host).get_cooldown(name)
        if cd > 0:
            print(f"Update cooldown: waiting {cd}s after {name} before the next recreate")
            _wait(cd, self)

    def _process_update_batch(self, updates, checker, *, auto, enrich=None,
                              checker_of=None, on_result=None, marks=True,
                              proactive_cleanup=True,
                              snapshot_fails_entry=False):
        """Shared per-container update engine for BOTH the scheduled-auto
        path (handle_autoupdates) and the manual path (run_updates /
        "Update all"). This is the single source of truth that stops the
        two from drifting (#2, @famewolf): group-order sort + inter-member
        wait, the group-abort gate, the netns-owner-by-name snapshot,
        update_container, the restart-dependents cascade (success +
        head-rollback), per-container notifier results and the per-container
        cooldown all live here, once.

        `auto` toggles the one behaviour that is legitimately path-specific:
        the ask-before-major confirmation gate runs ONLY for auto — tapping
        "Update all" is itself the explicit "yes, including majors".

        `updates` is mutated in place (sorted, enriched, `netns_name` added).
        Returns (results, success_count, major_pending), where major_pending
        is a list of (name, old_version, new_version, host) deferred for the
        confirmation prompt.

        Every piece of state read here — groups, ask-before-major,
        pending-major, cooldowns — is resolved for the host the individual
        update belongs to (#7), taken from its `host` key. An entry without
        one (written by a pre-#7 version) is the local host, which is also
        why a single-host install resolves everything to the raw store and
        behaves exactly as before.

        The keyword hooks are for `run_keys`, which runs a single update
        through here (Q11c); "Update all" and the scheduler pass none:

        * `enrich(updates)` resolves the release links instead of this
          engine's own resolver (Telegram hands in its own, the tests a
          stand-in);
        * `checker_of(u)` is the checker for an entry, instead of the
          registry lookup below; None still refuses it;
        * `on_result(u, success, msg)` is called once a container's
          outcome is reported to the other channels, in both the
          returned and the raising case;
        * `marks=False` leaves the in-flight mark to the caller, who set
          it already and takes it away after its own last word;
        * `proactive_cleanup=False` leaves out the once-per-batch prune
          over a local disk past DISK_WARN_PERCENT (the cleanup after an
          ENOSPC failure stays). `run_keys` runs one batch per key, so
          "once per batch" would be once per tap and per drained entry —
          three prunes and three 🧹 lines for a tap with two entries
          queued behind it (Q11c review, measured). A single update
          never had that prune;
        * `snapshot_fails_entry=True` makes a netns snapshot that raises
          (a host that stops answering: TimeoutExpired from a remote
          backend) that container's failure, reported like a raising
          update, with nothing recreated. Without it the error leaves
          the batch before any container's guard, as it always has for
          the scheduler, the Web UI and Discord (T5 pins the Web UI's
          log line and the entry it leaves). Telegram's `run_updates`
          ("Update all", and /update <pattern> per host) asks for it
          since Q12, when its snapshot started reaching the remote
          host. For `run_keys` it
          left the tap at "Updating …" with nothing after it and the
          queue stuck (Q11c review, measured); before Q11c the single
          update took no snapshot and the same timeout came up inside
          `update_container`, where it was a failure.
        """
        (enrich or self._enrich_with_source_url)(updates)

        _host_of = entry_host
        _stores = {}
        _checkers = {}
        self._batch_cleaned = False

        def _checker_of(u):
            """The checker whose backend actually reaches this entry's host.

            The batch used to run EVERY entry through the one checker its
            caller passed. The scheduled path passes the right one per
            host, but the manual paths — "Update all", /updates — hand
            over the LOCAL checker with a mixed list. For an @dock8520
            entry that meant: the remote-compose guard read
            `backend.name`, saw "local", stood down — and `docker compose`
            ran the local copy of the file against the LOCAL daemon.
            @famewolf's dockmox pulled dock8520's 2.4 GB CUDA images onto
            its own disk until it was full (#2); his local /cleanup then
            reclaimed 14 GB of images that were never meant to be there.

            Falls back to the caller's checker for the local host and for
            anything the registry cannot resolve — never to silently
            acting on the wrong machine.
            """
            h = _host_of(u)
            if h in (None, "", LOCAL_HOST):
                return checker
            if h not in _checkers:
                resolved = None
                for host in (self.hosts or ()):
                    if getattr(host, "name", None) == h:
                        resolved = getattr(host, "checker", None)
                        break
                _checkers[h] = resolved
            # None means the registry does not know this host — the
            # caller decides whether to refuse. Returning the fallback
            # here would blur "resolved to the same checker the caller
            # passed" (the scheduled per-host path, correct) with
            # "nobody knows this host" (refuse) — the first version of
            # this did exactly that, and the wiring test caught the
            # scheduled path refusing everything it was asked to do.
            return _checkers[h]

        def _store_of(u):
            h = _host_of(u)
            if h not in _stores:
                _stores[h] = host_store(self, h)
            return _stores[h]

        _groups_cache = {}

        def _groups_of(u):
            """That host's groups. Groups don't span hosts (#7), so the
            member lists here are always about one box."""
            h = _host_of(u)
            if h not in _groups_cache:
                _groups_cache[h] = _store_of(u).get_groups() or {}
            return _groups_cache[h]

        # (host, container_name) → (group_id, position). Keyed by host as
        # well as name because two hosts may each have a `plex`, in their
        # own groups, at their own positions.
        group_position = {}
        for u in updates:
            for gid, g in _groups_of(u).items():
                for pos, cname in enumerate(g.get("containers") or []):
                    group_position[(_host_of(u), cname)] = (gid, pos)

        def _gp(u):
            return group_position.get((_host_of(u), u["name"]))

        updates.sort(key=lambda u: (0,) + _gp(u) if _gp(u) else (1, "", 0))

        # Snapshot netns owners by NAME *before* any recreate (#2): a group
        # head recreated earlier in the batch changes ID, so sidecars still
        # referencing the old ID break — resolving to a stable name lets the
        # sidecars rejoin.
        #
        # With `snapshot_fails_entry` a raise is kept per entry and
        # becomes that container's failure below, in its own guard. Not
        # "no netns name and go on": a sidecar recreated without its
        # owner's name is the #2 breakage.
        snapshot_failed = {}
        for u in updates:
            try:
                tn = checker.netns_target_name(u["name"])
            except Exception as e:
                if not snapshot_fails_entry:
                    raise
                snapshot_failed[(_host_of(u), u["name"])] = e
                continue
            if tn:
                u["netns_name"] = tn

        _ask_cache = {}

        def _ask_major_list(u):
            h = _host_of(u)
            if h not in _ask_cache:
                _ask_cache[h] = _store_of(u).get_ask_before_major()
            return _ask_cache[h]

        batch_names = {(_host_of(u), u["name"]) for u in updates}
        results = []
        success_count = 0
        major_pending = []
        # (host, group_id) pairs whose remaining members are skipped — a
        # failure on one host must not abort a same-named group on another.
        group_aborted = set()
        prev_group = None

        for idx, u in enumerate(updates):
            gp = _gp(u)
            u_host = _host_of(u)
            cur_group = gp[0] if gp else None
            abort_key = (u_host, cur_group)
            groups = _groups_of(u)

            # Skip the rest of a group whose earlier member already failed.
            if cur_group and abort_key in group_aborted:
                results.append(
                    f"⏭ {self._display_name(u)}: skipped (group `{cur_group}` aborted earlier)")
                continue

            # Inter-container wait when staying inside the same group.
            if cur_group and abort_key == prev_group:
                wait_s = int((groups.get(cur_group) or {}).get("wait_seconds", 0) or 0)
                if wait_s > 0:
                    _wait(wait_s, self)

            # Major-version confirmation gate — auto only (per-container
            # opt-in). A `docksentry.ask-major` label wins over the stored
            # toggle (#42, @LeeNX); no label → stored list as before.
            ask_lab = None
            if auto:
                try:
                    ask_lab = checker.label_bool(
                        checker.get_container_labels(u["name"]), "ask-major")
                except Exception:
                    ask_lab = None
            ask_this = (ask_lab if ask_lab is not None
                        else (auto and u["name"] in _ask_major_list(u)))
            if auto and ask_this:
                is_major, old_ver, new_ver = self._is_major_bump(u, checker)
                if is_major:
                    _store_of(u).add_pending_major(u["name"], {
                        "image": u["image"],
                        "old_version": old_ver,
                        "new_version": new_ver,
                        "compose": {k: u[k] for k in u if k.startswith("compose_")},
                    })
                    major_pending.append((u["name"], old_ver, new_ver, u_host))
                    results.append(
                        f"⏸ {self._display_name(u)}: major bump {old_ver} → {new_ver} — confirmation required")
                    prev_group = abort_key
                    continue

            try:
                compose_kwargs = {k: u[k] for k in u if k.startswith("compose_")}
                u_checker = (checker_of or _checker_of)(u)
                if u_checker is None:
                    # A remote entry whose host the registry no longer
                    # knows. Acting on it with the local checker is how
                    # dockmox filled its disk with dock8520's images —
                    # refusing is the only honest move left.
                    results.append(
                        f"❌ {self._display_name(u)}: host "
                        f"`{_host_of(u)}` is not managed any more — "
                        f"skipped rather than acting on the wrong machine")
                    prev_group = abort_key
                    continue
                if (u_host, u["name"]) in snapshot_failed:
                    # Its netns snapshot raised: reported, carded and
                    # group-aborted by the except below, as a raising
                    # update is; nothing is recreated.
                    raise snapshot_failed[(u_host, u["name"])]
                # Marked around this one call and nothing else, so the
                # window the UI reads is exactly the window the container
                # is being pulled and recreated in. `finally`, because a
                # failed update must clear it too — a badge stuck on
                # "updating…" after a rollback would be the same wrong
                # answer in the other direction.
                _u_key = host_key(_host_of(u), u["name"])
                if marks:
                    self.mark_updating(_u_key, u.get("new_version") or "")
                try:
                    success, msg = u_checker.update_container(
                        u["name"], u["image"],
                        netns_name=u.get("netns_name"), **compose_kwargs)
                finally:
                    if marks:
                        self.clear_updating(_u_key)
                status = "✅" if success else "❌"
                results.append(f"{status} {self._display_name(u)}: {msg}")

                # Disk pressure, handled where it happens (#2, @famewolf:
                # "It should never get to 'no space left on device' if
                # docksentry is doing its job. It needs to do cleanup on
                # a container by container basis as it updates them.")
                # Two triggers, honestly different in what they can know:
                #
                #  * reactive, any host: an update that just failed on
                #    ENOSPC gets that host's cleanup immediately — the
                #    only signal a remote host's disk gives us at all,
                #    since free space is a filesystem question the Docker
                #    API does not answer;
                #  * proactive, local only: between containers, the local
                #    disk is checked against DISK_WARN_PERCENT — locally
                #    we CAN see it coming. Once per batch: prune walks
                #    every image, and a batch that prunes after every
                #    container spends longer pruning than updating.
                #
                # cleanup_images carries the grace-hours filter, so a
                # just-pulled image for the NEXT entry is never eligible.
                try:
                    if not success and "no space left" in str(msg).lower():
                        _ok, _cmsg = u_checker.cleanup_images()
                        results.append(self._t(
                            "cleanup_enospc",
                            host=_host_of(u) or "local", message=_cmsg))
                    elif (proactive_cleanup
                          and getattr(self.config, "disk_warn_auto_cleanup", False)
                          and not getattr(self, "_batch_cleaned", False)):
                        # Measurement and cleanup on the SAME machine:
                        # get_disk_usage() reads the local data dir no
                        # matter which checker it hangs off, so the prune
                        # must go to the LOCAL checker too — on the
                        # scheduled per-host path `checker` is a remote
                        # one, and pruning a remote host over a local
                        # reading would be the routing bug all over again.
                        _lc = checker
                        for host in (self.hosts or ()):
                            if getattr(host, "is_local", False):
                                _lc = getattr(host, "checker", None) or checker
                                break
                        _pct, _free, _tot = _lc.get_disk_usage()
                        _thr = int(getattr(self.config, "disk_warn_percent",
                                           85) or 85)
                        if _pct and _pct >= _thr:
                            self._batch_cleaned = True
                            _ok, _cmsg = _lc.cleanup_images()
                            results.append(self._t(
                                "cleanup_between_updates",
                                pct=_pct, threshold=_thr, message=_cmsg))
                except Exception as _ce:
                    results.append(self._t("cleanup_attempt_failed",
                                           error=str(_ce)[:120]))
                _report_result(self, u["name"], u["image"], success, msg,
                               u.get("source_url", ""))
                if on_result is not None:
                    on_result(u, success, msg)

                grp = (groups.get(cur_group) or {}) if cur_group else {}
                members = grp.get("containers") or []
                is_head = bool(grp.get("restart_dependents") and members
                               and u["name"] == members[0] and len(members) > 1)
                if success:
                    success_count += 1
                    # Restart-dependents cascade. Members already IN this
                    # batch self-heal via their own update (recreated onto the
                    # head's new name through the netns snapshot above), so
                    # only out-of-batch sidecars need the explicit kick.
                    # A member queued behind a tapped head is out of
                    # batch too (run_keys runs one key per batch) and
                    # gets the kick before its own update — see run_keys.
                    if is_head:
                        deps = [d for d in members[1:]
                                if (u_host, d) not in batch_names]
                        if deps:
                            wait_s = max(int(grp.get("wait_seconds", 30) or 30), 30)
                            results.append(self._restart_group_dependents(
                                u["name"], deps, checker, max_wait=wait_s))
                elif cur_group:
                    # Failure aborts the remainder of this group. If the failed
                    # container is a restart_dependents head, its dependents'
                    # namespace was torn down when it stopped and the rollback
                    # only restored the head — kick ALL dependents (incl. the
                    # in-batch ones the group-abort gate would otherwise skip)
                    # so they re-attach to the rolled-back head (#27).
                    group_aborted.add(abort_key)
                    if is_head:
                        wait_s = max(int(grp.get("wait_seconds", 30) or 30), 30)
                        results.append("🔁 head rollback — dependents kicked: " +
                                       self._restart_group_dependents(
                                           u["name"], members[1:], checker, max_wait=wait_s))
            except Exception as e:
                results.append(f"❌ {self._display_name(u)}: {clip(e)}")
                if cur_group:
                    group_aborted.add(abort_key)
                _report_result(self, u["name"], u.get("image", "?"), False,
                               clip(e), u.get("source_url", ""))
                if on_result is not None:
                    on_result(u, False, clip(e))
            prev_group = abort_key
            # Remembered past the end of this batch: a single update run
            # right after it (a queued tap) waits what the next entry in
            # here would have waited — see `_wait_after_last_recreate`.
            self._last_recreate = (u_host, cur_group, u["name"], _now(self))
            self._maybe_cooldown(u["name"], more_remaining=idx < len(updates) - 1,
                                 host=u_host)

        return results, success_count, major_pending

    #: Leading glyph → what that line means. Every results-line this class
    #: produces starts with one of these, so counting them is counting
    #: outcomes rather than re-deriving them from the text.
    RESULT_GLYPHS = {"✅": "updated", "❌": "failed",
                     "⏸": "held", "⏭": "skipped"}

    @classmethod
    def count_results(cls, results):
        """`{outcome: n}` for a batch's result lines, zeroes omitted.

        The completion message names every container already, one line
        each — but @LeeNX's screenshot of a lost report (#56) is the
        reminder that those lines can be split across several Telegram
        messages, and then the outcome is spread over all of them. A
        count in the first line means the answer is readable from the
        first message whatever happens to the rest.

        Lines that are neither (the group-rollback note) are simply not
        counted; this reports outcomes per container, and that note is
        about a group.
        """
        counts = {}
        for line in results or ():
            head = line.strip()[:1]
            key = cls.RESULT_GLYPHS.get(head)
            if key:
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _enrich_with_source_url(self, updates):
        """Set u['source_url'] on each update via the shared LinkResolver.
        Kept as a thin instance method (not inlined at call sites) because
        the notifier docs point at it and several tests monkeypatch it on
        a bot instance to stub link resolution out."""
        self.link_resolver.enrich_with_source_url(updates)

    def _display_name(self, u):
        """Format a container name for Telegram messages: `[name](url)`
        when we have a source_url, plain ``code`` otherwise. Used by
        every results-line and notification builder so the same
        container renders consistently across "Updates Available",
        "Auto-update complete", "Update Result", history etc.
        """
        url = u.get("source_url") if isinstance(u, dict) else None
        name = f"[{u['name']}]({url})" if url else f"`{u['name']}`"
        # With several hosts managed (#7), say WHICH one — otherwise two
        # boxes both running `nginx` produce identical lines. Only remote
        # hosts get the marker: a single-host install tags everything
        # "local" and its messages stay exactly as they always were.
        host = u.get("host") if isinstance(u, dict) else None
        if host and host != LOCAL_HOST:
            return f"{name} @{host}"
        return name

    @staticmethod
    def _version_badge(u):
        """A ` 🔖 v_old → v_new` suffix for the "Updates Available" line when
        version info is known (#44, @LeeNX): the arrow when both old and new
        differ, otherwise whichever single version we have. Empty when none —
        many images don't set `org.opencontainers.image.version`."""
        old = (u.get("old_version") or "").strip()
        new = (u.get("new_version") or "").strip()
        if old and new and old != new:
            return f" 🔖 v{old} → v{new}"
        v = old or new
        return f" 🔖 v{v}" if v else ""
