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


class UpdateEngine:
    def __init__(self, config, store, link_resolver=None):
        self.config = config
        self.store = store
        # Neutral repo/changelog link resolution (#52). The engine owns the
        # single LinkResolver so its `_enrich_with_source_url` and the bot's
        # direct link calls resolve through the SAME instance — the bot sets
        # `self.link_resolver = self.engine.link_resolver` rather than
        # building a second one. `LinkResolver(store, config)` is the exact
        # shape the bot built before, so this is behaviour-preserving. Lazy
        # import mirrors the bot's proven pattern and sidesteps any cycle.
        from link_resolver import LinkResolver
        self.link_resolver = link_resolver or LinkResolver(store, config)
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
        # Notifier (Discord/webhook fan-out) — single-sourced here so the
        # orchestration methods that moved off the bot (_process_update_batch)
        # can reach it, while the bot mirrors it through a property. Set by
        # main.py after init (bot.notifier = Notifier(config), which writes
        # through to here). Defensive default so the attribute always exists.
        self.notifier = None

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

        recreated, restarted, failed = [], [], []
        for dep in dependents:
            try:
                nm = subprocess.run(
                    ["docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", dep],
                    capture_output=True, text=True, timeout=10,
                ).stdout.strip()
                if nm.startswith("container:"):
                    ok, _detail = checker.recreate_dependent(dep, head_name)
                    (recreated if ok else failed).append(dep)
                    if not ok:
                        print(f"Failed to recreate netns dependent {dep}: {_detail}")
                else:
                    r = subprocess.run(["docker", "restart", dep],
                                       capture_output=True, text=True, timeout=30)
                    (restarted if r.returncode == 0 else failed).append(dep)
                    if r.returncode != 0:
                        print(f"Failed to restart dependent {dep}: {r.stderr.strip()[:200]}")
            except subprocess.SubprocessError as e:
                failed.append(dep)
                print(f"Fixing dependent {dep} crashed: {e}")

        done = recreated + restarted
        ok_str = ", ".join(f"`{d}`" for d in done) if done else "—"
        if failed:
            return (f"🔁 `{head_name}` dependents: {len(done)} ok ({ok_str}), "
                    f"failed {len(failed)} ({', '.join(f'`{d}`' for d in failed)})")
        verb = "recreated/restarted" if recreated else "restarted"
        return f"🔁 `{head_name}` dependents {verb}: {ok_str}"

    def _maybe_cooldown(self, name, more_remaining):
        """After recreating `name`, pause for its configured update cooldown
        before the next container in the batch — but only when more updates
        follow. Lets a heavy (GPU/RAM) container's load peak settle so the
        next recreate doesn't contend for memory (#2, @famewolf)."""
        if not more_remaining:
            return
        cd = self.store.get_cooldown(name)
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
        is a list of (name, old_version, new_version) deferred for the
        confirmation prompt.
        """
        import time as _time
        self._enrich_with_source_url(updates)

        groups = self.store.get_groups() or {}
        group_position = {}  # container_name → (group_id, position)
        for gid, g in groups.items():
            for pos, cname in enumerate(g.get("containers") or []):
                group_position[cname] = (gid, pos)
        updates.sort(key=lambda u: (0,) + group_position[u["name"]]
                     if u["name"] in group_position else (1, "", 0))

        # Snapshot netns owners by NAME *before* any recreate (#2): a group
        # head recreated earlier in the batch changes ID, so sidecars still
        # referencing the old ID break — resolving to a stable name lets the
        # sidecars rejoin.
        for u in updates:
            tn = checker.netns_target_name(u["name"])
            if tn:
                u["netns_name"] = tn

        ask_major_list = self.store.get_ask_before_major() if auto else set()
        batch_names = {u["name"] for u in updates}
        results = []
        success_count = 0
        major_pending = []
        group_aborted = set()  # group_ids whose remaining members are skipped
        prev_group = None

        for idx, u in enumerate(updates):
            gp = group_position.get(u["name"])
            cur_group = gp[0] if gp else None

            # Skip the rest of a group whose earlier member already failed.
            if cur_group and cur_group in group_aborted:
                results.append(
                    f"⏭ {self._display_name(u)}: skipped (group `{cur_group}` aborted earlier)")
                continue

            # Inter-container wait when staying inside the same group.
            if cur_group and cur_group == prev_group:
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
            ask_this = ask_lab if ask_lab is not None else (u["name"] in ask_major_list)
            if auto and ask_this:
                is_major, old_ver, new_ver = self._is_major_bump(u, checker)
                if is_major:
                    self.store.add_pending_major(u["name"], {
                        "image": u["image"],
                        "old_version": old_ver,
                        "new_version": new_ver,
                        "compose": {k: u[k] for k in u if k.startswith("compose_")},
                    })
                    major_pending.append((u["name"], old_ver, new_ver))
                    results.append(
                        f"⏸ {self._display_name(u)}: major bump {old_ver} → {new_ver} — confirmation required")
                    prev_group = cur_group
                    continue

            try:
                compose_kwargs = {k: u[k] for k in u if k.startswith("compose_")}
                success, msg = checker.update_container(
                    u["name"], u["image"],
                    netns_name=u.get("netns_name"), **compose_kwargs)
                status = "✅" if success else "❌"
                results.append(f"{status} {self._display_name(u)}: {msg}")
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
                        deps = [d for d in members[1:] if d not in batch_names]
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
                    group_aborted.add(cur_group)
                    if is_head:
                        wait_s = max(int(grp.get("wait_seconds", 30) or 30), 30)
                        results.append("🔁 head rollback — dependents kicked: " +
                                       self._restart_group_dependents(
                                           u["name"], members[1:], checker, max_wait=wait_s))
            except Exception as e:
                results.append(f"❌ {self._display_name(u)}: {str(e)[:200]}")
                if cur_group:
                    group_aborted.add(cur_group)
                if self.notifier:
                    self.notifier.send_update_result(u["name"], u.get("image", "?"), False, str(e)[:200],
                                                     source_url=u.get("source_url", ""))
            prev_group = cur_group
            self._maybe_cooldown(u["name"], more_remaining=idx < len(updates) - 1)

        return results, success_count, major_pending

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
        if url:
            return f"[{u['name']}]({url})"
        return f"`{u['name']}`"

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
