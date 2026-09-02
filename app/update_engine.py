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

import subprocess
import threading

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

    def __init__(self, config, store, link_resolver=None, hosts=None):
        self.config = config
        self.store = store
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
            return len(self._update_queue)

    def take_queued_update(self):
        """Pop the next queued key, or None."""
        with self._update_queue_lock:
            return self._update_queue.pop(0) if self._update_queue else None

    def drop_queued_updates(self, predicate=None):
        """Remove and return queued keys matching `predicate` (all if None)."""
        with self._update_queue_lock:
            keep, taken = [], []
            for k in self._update_queue:
                (taken if (predicate is None or predicate(k)) else keep).append(k)
            self._update_queue = keep
            return taken

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
        for the /check race-guard and any external callers."""
        locked = self._update_lock.acquire(blocking=False)
        if locked:
            self._update_lock.release()
            return False
        return True

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

        Returns a one-line user-facing result string for the update report."""
        # Use the checker's canonical 3-tuple _wait_healthy (outcome, state,
        # health) — NOT a bot-local one. A stray bool-returning duplicate here
        # caused "cannot unpack non-iterable bool object" mid-cascade (#2,
        # @famewolf): the head updated fine, then the dependents kick crashed.
        outcome, _, _ = checker._wait_healthy(head_name, max_wait)
        if outcome != "healthy":
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
            except subprocess.SubprocessError as e:
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
            import time as _time
            print(f"Update cooldown: waiting {cd}s after {name} before the next recreate")
            _time.sleep(cd)

    def _process_update_batch(self, updates, checker, *, auto):
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
        """
        import time as _time
        self._enrich_with_source_url(updates)

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
        for u in updates:
            tn = checker.netns_target_name(u["name"])
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
                    _time.sleep(wait_s)

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
                u_checker = _checker_of(u)
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
                # Marked around this one call and nothing else, so the
                # window the UI reads is exactly the window the container
                # is being pulled and recreated in. `finally`, because a
                # failed update must clear it too — a badge stuck on
                # "updating…" after a rollback would be the same wrong
                # answer in the other direction.
                _u_key = host_key(_host_of(u), u["name"])
                self.mark_updating(_u_key, u.get("new_version") or "")
                try:
                    success, msg = u_checker.update_container(
                        u["name"], u["image"],
                        netns_name=u.get("netns_name"), **compose_kwargs)
                finally:
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
                    elif (getattr(self.config, "disk_warn_auto_cleanup", False)
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
                if self.notifier:
                    self.notifier.send_update_result(u["name"], u["image"], success, msg,
                                                     source_url=u.get("source_url", ""))

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
                if self.notifier:
                    self.notifier.send_update_result(u["name"], u.get("image", "?"), False, clip(e),
                                                     source_url=u.get("source_url", ""))
            prev_group = abort_key
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
