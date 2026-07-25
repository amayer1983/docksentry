#!/usr/bin/env python3
"""Telegram Bot - handles messages, callbacks, and notifications."""

import json
import shlex
import socket
import subprocess
import os
import threading
import urllib.error
import urllib.request
import urllib.parse


# ── Single source of truth for all bot commands ────────────────────────
# Every command Docksentry exposes is described here ONCE. The two
# downstream consumers — Telegram's `setMyCommands` (the `/`-autocomplete
# picker) and the `/help` output — both derive from this list. Adding a
# new command is a one-line edit; the picker and /help update in lockstep.
#
# Fields:
#   name        — the slash command without the leading `/`
#   picker_desc — short single-line description for the Telegram picker
#                 (Telegram's UI only shows one line, ≤ ~80 chars works
#                 best on mobile). Plain English — Telegram's picker is
#                 keyed to the *user's Telegram client* language, not
#                 the bot's `config.language`, so EN reaches everyone.
#   help_key    — i18n key for the `/help` output line. Multiple
#                 commands can share the same key when the help text
#                 covers them as a group (start/stop/restart all share
#                 `help_lifecycle`). Set to None to omit from `/help`.
#
# The order here is the order both surfaces render in.
_BOT_COMMANDS = [
    # (cmd-name, picker-description, summary-help-key, detail-help-key)
    # detail-help-key drives `/help <cmd>` (#15) — a deeper per-command
    # help block with synopsis, parameters, examples, side effects.
    # Multiple commands can share a detail key (lifecycle, pin/unpin).
    ("status",      "Container overview (add a name for details + action buttons)", "help_status",      "help_detail_status"),
    ("check",       "Check for updates (add a name/glob to scope)",                   "help_check",       "help_detail_check"),
    ("update",      "Update a container or glob — /update <name|*>",                   "help_update",      "help_detail_update"),
    ("updates",     "Show pending updates",                                           "help_updates",     "help_detail_updates"),
    ("cleanup",     "Remove unused images",                                           "help_cleanup",     "help_detail_cleanup"),
    ("checkimages", "How much space /cleanup would free (dry-run)",                    "help_checkimages", "help_detail_checkimages"),
    ("start",       "Start a stopped container — /start <name>",                      "help_lifecycle",   "help_detail_lifecycle"),
    ("stop",        "Stop a running container — /stop <name>",                        "help_lifecycle",   "help_detail_lifecycle"),
    ("restart",     "Restart a container — /restart <name>",                          "help_lifecycle",   "help_detail_lifecycle"),
    ("maintenance", "Pause auto-updates — /maintenance 2h or /maintenance off",       "help_maintenance", "help_detail_maintenance"),
    ("history",     "Recent update history",                                          "help_history",     "help_detail_history"),
    ("events",      "Recent container events (crashes, OOM, health flips)",           "help_events",      "help_detail_events"),
    ("groups",      "Show container groups — /groups or /groups <name>",              "help_groups",      "help_detail_groups"),
    ("pin",         "Skip updates for a container — /pin <name>",                     "help_pin",         "help_detail_pin"),
    ("unpin",       "Re-enable updates — /unpin <name>",                              "help_unpin",       "help_detail_pin"),
    ("autoupdate",  "Toggle auto-update — /autoupdate <name>",                        "help_autoupdate",  "help_detail_autoupdate"),
    ("cooldown",    "Per-container update cooldown — /cooldown <name> <seconds>",      "help_cooldown",    "help_detail_cooldown"),
    ("protect",     "Protect a container from Stop — /protect <name>",                 "help_protect",     "help_detail_protect"),
    ("setlink",     "Set repo/changelog link — /setlink <name> <url>",                 "help_setlink",     "help_detail_setlink"),
    ("audit",       "Audit container inspect coverage — /audit <name>",                "help_audit",       "help_detail_audit"),
    ("selfupdate",  "Update the bot itself (add a version to pin)",                   "help_selfupdate",  "help_detail_selfupdate"),
    ("changelog",   "What's new in versions ahead of yours",                          "help_changelog",   "help_detail_changelog"),
    ("debug",       "Toggle debug mode",                                              "help_debug",       "help_detail_debug"),
    ("logs",        "Last 30 log lines — /logs <name>",                               "help_logs",        "help_detail_logs"),
    ("lang",        "Switch bot language — /lang en or /lang de",                     "help_lang",        "help_detail_lang"),
    ("settings",    "Show current settings",                                          "help_settings",    "help_detail_settings"),
    ("help",        "Show all commands (or /help <cmd> for details)",                 "help_help",        "help_detail_help"),
]


class TelegramBot:
    def __init__(self, config, container_store):
        self.config = config
        self.store = container_store
        self.running = True
        # Single mutex guarding ALL update flows — manual "Update all"
        # (run_updates, bot thread), single-container update
        # (_run_single_update, bot thread), major-confirm update, AND the
        # scheduler's auto-update pass (handle_autoupdates, scheduler
        # thread). Before v1.23.1 only the manual paths checked a plain
        # `update_running` bool; the scheduler's auto-update ignored it
        # entirely, so a cron tick could recreate the same container the
        # user was mid-updating. A Lock with acquire(blocking=False)
        # makes the check-and-claim atomic (the old bool had a TOCTOU
        # window) and covers every entry point.
        import threading as _threading
        self._update_lock = _threading.Lock()
        # A /selfupdate requested while the lock is held (container batch
        # in progress) is queued instead of killing the batch mid-flight
        # (#2, @famewolf): the restart aborted running updates, and if it
        # had landed during a stop/rename/recreate it would have left a
        # renamed `_old` orphan. Holds the 1-tuple `(target,)` or None.
        self._queued_selfupdate = None
        # True once the helper container is launched — the process is
        # about to be stopped, so the wrapper keeps the update lock held
        # (nothing may start an update in the final seconds).
        self._swap_in_flight = False
        # Per-notification snapshots of the "Updates Available" container
        # list, keyed by a short token carried in the "Update all"
        # button's callback_data (v1.23.3). Before this, "Update all"
        # carried no reference to WHICH notification it came from — it
        # just re-read the global pending_updates.json at click time. So
        # tapping "Update all" on yesterday's notification (which showed
        # one container) updated whatever the latest check had since
        # written to pending (e.g. five containers). Reported by
        # @famewolf in #2. Capped FIFO so the dict can't grow unbounded.
        self._update_snapshots = {}
        self._snapshot_seq = 0
        self.notifier = None  # Set by main.py after init
        from i18n import get_translator
        self.t = get_translator(config.language)

    @property
    def update_running(self):
        """True while any update flow holds the lock. Read-only view kept
        for the /check race-guard and any external callers."""
        locked = self._update_lock.acquire(blocking=False)
        if locked:
            self._update_lock.release()
            return False
        return True

    @property
    def enabled(self):
        """True when both BOT_TOKEN and CHAT_ID are configured. When False,
        send_message / api_call / listen are no-ops — Docksentry runs
        headless (Web UI + Discord/Webhook only)."""
        return bool(self.config.bot_token and self.config.chat_id)

    def stop(self):
        self.running = False

    def _save_selfupdate_history(self, container_name, image, old_created, new_created):
        """Record a Docksentry self-update in update_history.json so it
        shows up in /history and the Web UI history page alongside
        regular container updates. Reported missing by @famewolf in #13.

        Written BEFORE the helper container restarts us (since we won't
        be alive to write after). Detail uses the same date-arrow format
        as regular container updates so the Web UI doesn't need special
        rendering. success=True is assumed — if the helper fails, the
        next manual /selfupdate will create a follow-up entry with the
        new outcome.

        We know the OLD version (from `version.VERSION`) at this point
        but NOT the NEW version (the new image is pulled but our
        process hasn't restarted yet). Write `v{old} → ?` as a
        placeholder; main.py's post-boot fixup patches the `?` with
        the freshly-booted process's VERSION as part of resuming the
        deferred check (#22)."""
        import json as _json
        from datetime import datetime as _dt
        from version import VERSION as _CUR_VERSION
        entry = {
            "timestamp": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            "container": container_name,
            "image": image,
            "success": True,
            "detail": f"🗓️ {old_created} → {new_created} (selfupdate v{_CUR_VERSION} → ?)",
        }
        try:
            history = []
            if os.path.exists(self.config.history_file):
                try:
                    with open(self.config.history_file) as f:
                        history = _json.load(f)
                except (_json.JSONDecodeError, IOError):
                    history = []
            history.append(entry)
            history = history[-100:]
            # Atomic write (v1.22.1) — see container_store.atomic_write_json
            from container_store import atomic_write_json
            atomic_write_json(self.config.history_file, history, indent=2)
        except OSError as e:
            print(f"Failed to record selfupdate history: {e}")

    def _own_container_meta(self):
        """Return (own_name, own_image) for the running Docksentry
        container, or (None, None) when we can't figure it out (HOSTNAME
        env unset, docker inspect fails, …). Cached after first call —
        the answer doesn't change at runtime."""
        if hasattr(self, "_cached_own_meta"):
            return self._cached_own_meta
        # Robust self-resolution (#41) — works where $HOSTNAME isn't a
        # directly inspect-resolvable reference (e.g. QNAP Container Station).
        from update_checker import UpdateChecker as _UC
        cfg = _UC.inspect_self()
        if not cfg:
            self._cached_own_meta = (None, None)
            return self._cached_own_meta
        name = (cfg.get("Name", "") or "").lstrip("/")
        image = cfg.get("Config", {}).get("Image", "") or ""
        self._cached_own_meta = (name, image)
        return self._cached_own_meta

    def _resolve_container_link(self, name, image=""):
        """Return the URL that should wrap `name` in update
        notifications, or empty string when no link is available.

        Priority order:
          1. Manual override stored via Web UI (`container_links.json`).
             Lets users point at the actual changelog of containers
             whose images don't ship OCI labels (redis, postgres,
             nginx-proxy-manager, …).
          2. `org.opencontainers.image.source` OCI label (gold standard).
          3. `org.opencontainers.image.url` OCI label (fallback).
          4. Registry overview heuristic (Hub / ghcr.io / quay.io /
             lscr.io → fleet.linuxserver.io) from the image reference.

        Reuses the v1.18.3 `/changelog <container>` helpers so the
        notification-link feature gets the same coverage as that
        command (~67 % auto-detection rate without any user setup).
        """
        # 1. Manual override
        manual = self.store.get_link(name)
        if manual:
            return manual
        # 2 + 3. OCI labels
        url, kind = self._container_source_url(name)
        if url and kind in ("source", "url"):
            return url
        # 4. Registry-overview heuristic — only when we have an image ref
        if image:
            return self._guess_registry_overview_url(image)
        return ""

    def _container_source_url(self, name):
        """Look up the upstream source URL for a container from its OCI
        labels. Returns (url, kind) where kind is:
          - "source": from `org.opencontainers.image.source` (the gold
                      standard — points at a real source repo)
          - "url":    fallback to `org.opencontainers.image.url`
                      (usually the product/landing page, less useful)
          - "none":   no usable label found

        Used by /changelog <container> to give the user a link to the
        upstream repo instead of trying (and frequently failing) to
        fetch + parse an arbitrary container's CHANGELOG file."""
        for label in ("org.opencontainers.image.source",
                      "org.opencontainers.image.url"):
            try:
                r = subprocess.run(
                    ["docker", "inspect", "--format",
                     "{{index .Config.Labels \"" + label + "\"}}", name],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    url = r.stdout.strip()
                    if url and url not in ("<no value>", "no value"):
                        kind = "source" if "source" in label else "url"
                        return url, kind
            except subprocess.SubprocessError:
                continue
        return "", "none"

    def _guess_registry_overview_url(self, image):
        """Heuristic for "where can the user look this up?" when the
        image has no OCI source label. Maps the image reference to its
        registry's overview page URL. Best-effort — at worst we say
        'check the registry's own page'."""
        # Strip tag
        ref = image.rsplit(":", 1)[0] if ":" in image else image
        # Docker Hub library/official ("redis" → docker.io/library/redis)
        if "/" not in ref:
            return f"https://hub.docker.com/_/{ref}"
        # GHCR
        if ref.startswith("ghcr.io/"):
            rest = ref[len("ghcr.io/"):]
            return f"https://github.com/{rest}/pkgs/container/{rest.split('/')[-1]}"
        # Quay
        if ref.startswith("quay.io/"):
            return f"https://quay.io/repository/{ref[len('quay.io/'):]}"
        # GitLab Container Registry (registry.gitlab.com / *.gitlab.io)
        if ref.startswith("registry.gitlab.com/"):
            return f"https://gitlab.com/{ref[len('registry.gitlab.com/'):]}"
        # LinuxServer (lscr.io) → fleet page
        if ref.startswith("lscr.io/"):
            return f"https://fleet.linuxserver.io/image?name={ref[len('lscr.io/'):]}"
        # Default: Docker Hub repo page (works for `user/image`)
        return f"https://hub.docker.com/r/{ref}"

    def _fetch_changelog(self):
        """Fetch CHANGELOG.md from GitHub raw. Returns (ok, text_or_error)."""
        url = "https://raw.githubusercontent.com/amayer1983/docksentry/main/CHANGELOG.md"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Docksentry/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return True, r.read().decode("utf-8", errors="replace")
        except Exception as e:
            return False, str(e)[:200]

    def _github_md_to_telegram(self, text):
        """Adapt GitHub-flavored Markdown for Telegram's classic Markdown
        parser. Mostly: GitHub `**bold**` collides with Telegram's
        `*bold*` (Telegram chokes on `**`), and Markdown headings aren't
        a Telegram concept. We also strip image-style `![alt](url)`
        embeds since Telegram won't inline them anyway."""
        import re
        # Heading levels → just bold (Telegram has no heading concept)
        text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
        # GitHub bold → Telegram bold
        text = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)
        # Strip stray "![alt](url)" image embeds
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
        return text

    def _parse_changelog_entries(self, text, after_version):
        """Parse CHANGELOG.md, return entries with version > after_version
        as list of (version, date, body) tuples in newest-first order."""
        import re
        pat = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\] - (\d{4}-\d{2}-\d{2})$", re.MULTILINE)
        entries = []
        matches = list(pat.finditer(text))
        for i, m in enumerate(matches):
            major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
            date = m.group(4)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            entries.append(((major, minor, patch), date, body))
        # Filter newer than current
        try:
            cur = tuple(int(x) for x in after_version.split(".")[:3])
        except ValueError:
            cur = (0, 0, 0)
        newer = [(f"{v[0]}.{v[1]}.{v[2]}", d, b) for v, d, b in entries if v > cur]
        return newer

    def _changelog_entry_for(self, text, version):
        """Return (version, date, body) for the exact `version` in the
        CHANGELOG, or None. Lets `/changelog` show what the version you're
        *already* on brought, when nothing newer exists (#2, @famewolf)."""
        import re
        try:
            want = tuple(int(x) for x in version.split(".")[:3])
        except ValueError:
            return None
        pat = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\] - (\d{4}-\d{2}-\d{2})$", re.MULTILINE)
        matches = list(pat.finditer(text))
        for i, m in enumerate(matches):
            v = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if v == want:
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                return (f"{v[0]}.{v[1]}.{v[2]}", m.group(4), text[start:end].strip())
        return None

    def _check_auth(self, chat_id, user_id, kind="message"):
        """Authorize an incoming Telegram message or callback.

        Two layers, in order:

        1. **Chat-origin match.** The incoming `chat.id` must equal the
           configured `CHAT_ID`. This is the right field to compare —
           in a 1:1 chat `chat.id == user.id`, in a group `chat.id` is
           the (negative) group ID. Comparing `from.id` (the previous
           behaviour) silently broke every group / topic setup because
           `from.id` is the *clicker's* personal user ID, never the
           group ID. Reported by @jayjay3108 in #2.

        2. **Optional user whitelist.** If `TELEGRAM_ALLOWED_USERS` is
           set, the sender's `from.id` must be in that list. Lets you
           use a group chat while restricting control to a handful of
           members.

        Returns True on success. On failure, logs the reason when
        debug mode is on (so users can self-diagnose) and returns
        False — silent in non-debug to avoid log spam from drive-by
        messages in shared groups.
        """
        chat_id = str(chat_id) if chat_id is not None else ""
        user_id = str(user_id) if user_id is not None else ""

        if chat_id != str(self.config.chat_id):
            if self.config.debug:
                print(f"Auth fail ({kind}): chat.id={chat_id} ≠ CHAT_ID={self.config.chat_id} (from user {user_id})")
            return False

        allowed = self.config.telegram_allowed_users or []
        # Normalize: env may give us a list, persistent storage may also
        # give us a list of strings or numbers depending on JSON-roundtrip.
        allowed_strs = [str(u).strip() for u in allowed if str(u).strip()]
        if allowed_strs and user_id not in allowed_strs:
            if self.config.debug:
                print(f"Auth fail ({kind}): user {user_id} not in TELEGRAM_ALLOWED_USERS={allowed_strs}")
            return False

        return True

    # Thin wrappers around ContainerStore — kept for backwards compatibility
    # with internal call sites in this file. New code should use self.store
    # directly.
    def _get_pinned(self):
        return self.store.get_pinned()

    def _save_pinned(self, pinned):
        self.store.save_pinned(pinned)

    def _get_autoupdate(self):
        return self.store.get_autoupdate()

    def _save_autoupdate(self, containers):
        self.store.save_autoupdate(containers)

    def _is_protected(self, name, checker):
        """True if `name` is protected from /stop. A `docksentry.protect`
        container label overrides the stored toggle when present (#42,
        @LeeNX) — GitOps-style config in the compose file; otherwise the
        bot/Web-UI toggle (#38) applies. Label failures fall back to the
        toggle so a flaky inspect can never accidentally unprotect."""
        try:
            lab = checker.label_bool(checker.get_container_labels(name), "protect")
        except Exception:
            lab = None
        return lab if lab is not None else self.store.is_protect_stop(name)

    @staticmethod
    def _help_alias(text):
        """`/cmd -?` → `/help cmd` (#15, @LeeNX). Returns text unchanged
        unless `-?` is the sole argument to a slash command, so a single
        /help code path serves both forms."""
        if text.startswith("/") and text.split()[1:] == ["-?"]:
            return "/help " + text.split()[0].lstrip("/")
        return text

    def _container_state(self, name):
        """Return a dict with current state of `name` for the per-container
        status output. Keys: state, health, uptime, image, ports, volumes,
        restart_policy. All values are strings (already formatted for
        display). Returns None if inspect fails."""
        try:
            r = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return None
            cfg = json.loads(r.stdout)[0]
        except (subprocess.SubprocessError, json.JSONDecodeError, IndexError):
            return None

        state = cfg.get("State", {}) or {}
        host = cfg.get("HostConfig", {}) or {}
        config = cfg.get("Config", {}) or {}

        status = state.get("Status", "?")
        health = (state.get("Health") or {}).get("Status", "")

        # Uptime from StartedAt
        started_at = state.get("StartedAt", "")
        uptime = "?"
        if state.get("Running") and started_at:
            try:
                from datetime import datetime as _dt, timezone as _tz
                s = _dt.fromisoformat(started_at.replace("Z", "+00:00"))
                delta = _dt.now(_tz.utc) - s
                total = int(delta.total_seconds())
                if total < 60:
                    uptime = f"{total}s"
                elif total < 3600:
                    uptime = f"{total // 60}m {total % 60}s"
                elif total < 86400:
                    uptime = f"{total // 3600}h {(total % 3600) // 60}m"
                else:
                    days = total // 86400
                    hrs = (total % 86400) // 3600
                    uptime = f"{days}d {hrs}h"
            except (ValueError, AttributeError):
                pass

        # Ports — only host-mapped ones
        port_lines = []
        for cport, bindings in (host.get("PortBindings") or {}).items():
            if bindings:
                for b in bindings:
                    hp = b.get("HostPort", "")
                    if hp:
                        port_lines.append(f"{hp}→{cport.split('/')[0]}")
        ports = ", ".join(port_lines) if port_lines else "—"

        # Volumes count
        mounts = cfg.get("Mounts", []) or []
        volumes = f"{len(mounts)}"

        restart = (host.get("RestartPolicy") or {}).get("Name", "no")

        # Resolved version + image hash (#36, @LeeNX — same data the Web UI
        # detail shows since #32). Both come from the already-fetched inspect,
        # so no extra docker call. The OCI version label is the honest answer
        # to "what version is this really?" beyond a rolling :latest tag.
        image_id = cfg.get("Image", "") or ""
        short_id = image_id[7:19] if image_id.startswith("sha256:") else image_id[:12]
        # Version from the RUNNING image's own label, not the container's:
        # pre-v1.43.0 recreates pinned stale version labels onto containers
        # (#46, @LeeNX — /status kept showing the old version after every
        # selfupdate). The image can't lie about itself; the container can.
        version = ""
        if image_id:
            r = subprocess.run(
                ["docker", "image", "inspect", "--format",
                 '{{index .Config.Labels "org.opencontainers.image.version"}}',
                 image_id],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                version = r.stdout.strip()
        if not version:
            version = ((config.get("Labels") or {}).get(
                "org.opencontainers.image.version") or "").strip()

        return {
            "name": cfg.get("Name", "").lstrip("/"),
            "state": status,
            "health": health,
            "uptime": uptime,
            "image": config.get("Image", "?"),
            "version": version,
            "short_id": short_id,
            "ports": ports,
            "volumes": volumes,
            "restart_policy": restart or "no",
            "running": bool(state.get("Running")),
        }

    def _lifecycle_action(self, action, name, checker):
        """Execute a lifecycle action (stop/start/restart) on a resolved
        container. Returns (ok: bool, message: str — already i18n'd).

        Reuses the v1.17.7 _would_kill_self guard for stop/restart so
        the bot can't stop / restart itself by accident (PID 1 would
        die before the recreate, same class of bug as #16).
        """
        if action in ("stop", "restart") and checker._would_kill_self(name):
            return False, self.t("lifecycle_refused_self", action=action, name=name)

        # No user lifecycle actions while any update flow runs (#2
        # follow-up, @famewolf's "other fringe cases"). A stop during the
        # post-update health wait reads as unhealthy and triggers a bogus
        # rollback of a good update; a restart can hit a container that is
        # mid stop/rename/recreate. The update machinery itself doesn't go
        # through this method (it calls _stop_container / docker restart
        # directly), so this can never block an update's own steps.
        if self.update_running:
            return False, self.t("lifecycle_busy", action=action)

        # Stop-protection (#38): a container the user marked must not be
        # stopped (e.g. the VPN/tunnel carrying their remote access).
        # Restart stays allowed — brief downtime during update/rollback is
        # acceptable, a permanent stop is the dangerous one.
        if action == "stop" and self._is_protected(name, checker):
            return False, self.t("lifecycle_refused_protected", name=name)

        if action == "stop":
            ok, detail = checker._stop_container(name)
            if ok:
                return True, self.t("lifecycle_stopped", name=name)
            return False, self.t("lifecycle_stop_failed", name=name, error=detail)

        if action == "start":
            try:
                r = subprocess.run(
                    ["docker", "start", name],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode == 0:
                    return True, self.t("lifecycle_started", name=name)
                return False, self.t("lifecycle_start_failed", name=name,
                                      error=(r.stderr or "").strip()[:200])
            except subprocess.SubprocessError as e:
                return False, self.t("lifecycle_start_failed", name=name, error=str(e)[:200])

        if action == "restart":
            # docker restart is graceful stop + start; use generous
            # timeout because some apps (gitlab, gluetun) take a while.
            try:
                r = subprocess.run(
                    ["docker", "restart", "--time", "30", name],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0:
                    return True, self.t("lifecycle_restarted", name=name)
                return False, self.t("lifecycle_restart_failed", name=name,
                                      error=(r.stderr or "").strip()[:200])
            except subprocess.SubprocessError as e:
                return False, self.t("lifecycle_restart_failed", name=name, error=str(e)[:200])

        return False, f"unknown action: {action}"

    def _resolve_container(self, partial, include_stopped=True):
        """Resolve a partial container name. Returns (full_name, error_msg).

        Looks at `docker ps -a` by default (running + stopped) — almost
        every partial-name lookup in the bot wants this. `/logs` of a
        stopped container shows you why it died; `/status` of a stopped
        container shows you its state; `/start` only makes sense for
        stopped ones. Originally we defaulted to running-only on the
        theory that surfacing dead containers in pickers would confuse
        users — but @famewolf's #25 made it clear that the opposite
        was true: confining the resolver to running containers was the
        thing that confused users, since "not found" doesn't
        distinguish absent from stopped.

        Filters out `_old`-suffix containers (our internal rollback
        leftovers from failed updates) so they never surface in the
        picker. Specific callers that genuinely need running-only can
        opt out via `include_stopped=False`.
        """
        cmd = ["docker", "ps", "--format", "{{.Names}}"]
        if include_stopped:
            cmd.insert(2, "-a")
        result = subprocess.run(cmd, capture_output=True, text=True)
        all_names = [
            n.strip() for n in result.stdout.strip().split("\n")
            if n.strip() and not n.strip().endswith("_old")
        ]

        # Exact match first
        if partial in all_names:
            return partial, None

        # Partial match (starts with)
        matches = [n for n in all_names if n.lower().startswith(partial.lower())]
        if len(matches) == 1:
            return matches[0], None
        elif len(matches) > 1:
            return None, self.t("resolve_multiple", names=", ".join(f"`{m}`" for m in matches))
        else:
            return None, self.t("resolve_not_found", name=partial)

    @staticmethod
    def _is_glob(s):
        """True if `s` looks like a glob pattern (vs. a plain/partial name)."""
        return any(c in s for c in "*?[")

    def _match_glob(self, pattern, include_stopped=True):
        """Return sorted container names matching a glob pattern (`*`, `?`,
        `[...]`), case-insensitively — running + stopped, minus our `_old`
        rollback leftovers. Empty list if nothing matches (#40, @LeeNX)."""
        import fnmatch
        cmd = ["docker", "ps", "--format", "{{.Names}}"]
        if include_stopped:
            cmd.insert(2, "-a")
        result = subprocess.run(cmd, capture_output=True, text=True)
        names = [n.strip() for n in result.stdout.strip().split("\n")
                 if n.strip() and not n.strip().endswith("_old")]
        pl = pattern.lower()
        return sorted(n for n in names if fnmatch.fnmatch(n.lower(), pl))

    def _select_containers(self, arg):
        """Resolve a /check or /update argument to a list of container names.
        Glob (`*?[`) → all matches; plain → single partial-resolved name.
        Returns (names, error_msg); error_msg is None on success (#40)."""
        if self._is_glob(arg):
            names = self._match_glob(arg)
            if not names:
                return [], self.t("glob_no_match", pattern=arg)
            return names, None
        resolved, err = self._resolve_container(arg)
        if err:
            return [], err
        return [resolved], None

    @staticmethod
    def _is_timeout(exc):
        """Return True if `exc` is a network timeout (vs. a real API error)."""
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return True
        if isinstance(exc, urllib.error.URLError):
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return True
            if "timed out" in str(reason).lower():
                return True
        return False

    def api_call(self, method, data=None, timeout=60, quiet_timeout=False):
        """Call Telegram Bot API.

        timeout         HTTP socket timeout in seconds.
        quiet_timeout   If True, suppress logging when the request times out.
                        Used by the getUpdates long-poll loop where timeouts
                        are expected on flaky networks and don't indicate a
                        real problem.
        Returns the parsed JSON response, or a sentinel-aware None on error.
        Callers can distinguish "API responded with not-ok" (dict with
        ok=False) from "request failed entirely" (None).

        When the bot is disabled (no BOT_TOKEN/CHAT_ID), this is a no-op.
        """
        if not self.enabled:
            return None
        url = f"https://api.telegram.org/bot{self.config.bot_token}/{method}"
        if data:
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(data).encode(),
                method="POST"
            )
        else:
            req = urllib.request.Request(url)
        # Bounded retry for transient network failures (timeout / connection
        # error). A single blip — e.g. the network still settling right after
        # a self-update restart — used to silently drop a notification while
        # Discord/webhook got through (#2, @NotRetarded). The long-poll
        # (quiet_timeout=True) is exempt: its timeouts are normal and it loops
        # anyway. Trade-off: a read-timeout AFTER Telegram already processed a
        # send can yield a duplicate message — accepted on purpose, a dropped
        # update/self-update notification is worse than a rare dupe. Only the
        # network layer is retried; HTTP 4xx (parse/rate-limit bodies) is
        # returned to the caller unchanged.
        import time as _t
        attempts = 1 if quiet_timeout else 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                # Telegram returns 4xx with a JSON body for parse errors,
                # rate-limit hints etc. Pass the parsed body to the caller so
                # the markdown-retry path in send_message can act on it
                # instead of treating it as a network failure. Not retried.
                try:
                    body = json.loads(e.read())
                    if not (quiet_timeout and self._is_timeout(e)):
                        print(f"Telegram API {e.code}: {body.get('description', body)}")
                    return body
                except Exception:
                    print(f"Telegram API error: {e}")
                    return None
            except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
                if attempt < attempts - 1:
                    _t.sleep(2 * (attempt + 1))  # 2s, then 4s
                    continue
                if not (quiet_timeout and self._is_timeout(e)):
                    print(f"Telegram API error: {e}")
                return None
            except Exception as e:
                print(f"Telegram API error: {e}")
                return None

    def send_message(self, text, reply_markup=None, auto=False):
        """Send a Telegram message.

        auto=False (default) — treats this as a direct response to the user
                  (e.g. answer to a /command, status output). Always sent.
        auto=True            — auto-notification (cron-triggered: updates,
                  cleanup, disk warning). Suppressed during quiet hours.
        """
        if auto:
            from quiet_hours import is_quiet_now
            if is_quiet_now(self.config):
                return None
            try:
                from maintenance import is_active as _maint_active
                if _maint_active(self.config):
                    return None
            except Exception:
                pass

        # Prepend optional bot label (e.g. "🖥 pve1") to distinguish
        # multiple Docksentry instances posting into a shared Telegram
        # group. Empty by default — no-op on single-host setups.
        label = (self.config.bot_label or "").strip()
        if label:
            text = f"{label} · {text}"

        data = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true"
        }
        if self.config.telegram_topic_id:
            data["message_thread_id"] = self.config.telegram_topic_id
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        result = self.api_call("sendMessage", data)
        # Only retry without Markdown when Telegram actively rejected the
        # message (ok=False, typically a parse error). Don't retry when the
        # request itself failed (None) — that's a network/timeout issue and
        # retrying immediately won't help.
        if result and not result.get("ok"):
            data.pop("parse_mode", None)
            result = self.api_call("sendMessage", data)
        return result

    def answer_callback(self, callback_id, text):
        self.api_call("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text
        })

    def remove_buttons(self, chat_id, message_id):
        self.api_call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps({"inline_keyboard": []})
        })

    def _remove_single_button(self, chat_id, message_id, callback_data):
        """Mark clicked button as done, keep remaining buttons."""
        keyboard = self._rebuild_keyboard_without(callback_data)
        self.api_call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": json.dumps(keyboard)
        })

    def _rebuild_keyboard_without(self, callback_data):
        """Rebuild keyboard marking the clicked container as done."""
        if not os.path.exists(self.config.pending_file):
            return {"inline_keyboard": []}

        with open(self.config.pending_file) as f:
            updates = json.load(f)

        keyboard = []
        for u in updates:
            btn_data = f"update_one:{u['name']}"
            if btn_data == callback_data:
                keyboard.append([{"text": f"✅ {u['name']}", "callback_data": "noop"}])
            else:
                keyboard.append([{"text": f"🔄 {u['name']}", "callback_data": btn_data}])

        remaining = [u for u in updates if f"update_one:{u['name']}" != callback_data]
        if remaining:
            keyboard.append([
                {"text": self.t("update_all_btn"), "callback_data": "update_all"},
                {"text": self.t("manual_btn"), "callback_data": "update_skip"}
            ])

        return {"inline_keyboard": keyboard}

    def _run_single_update(self, checker, container_name):
        """Update a single container."""
        # Claim the shared update mutex — before v1.23.1 this path took
        # no lock at all, so tapping "update searxng" while an "Update
        # all" was running recreated containers concurrently.
        if not self._update_lock.acquire(blocking=False):
            self.send_message(self.t("update_already_running"))
            return
        try:
            if not os.path.exists(self.config.pending_file):
                self.send_message(self.t("no_pending_updates"))
                return

            with open(self.config.pending_file) as f:
                updates = json.load(f)

            target = next((u for u in updates if u["name"] == container_name), None)
            if not target:
                self.send_message(self.t("container_not_in_list", name=container_name))
                return

            # Enrich source_url so both the inline send_message line and
            # the Discord/webhook send_update_result carry the link
            # (parity with the auto-update + bulk-update flows, fixed in
            # v1.19.2/v1.19.3 after @NotRetarded surfaced the gap).
            self._enrich_with_source_url([target])
            self.send_message(self.t("update_single_starting", name=container_name))

            try:
                compose_kwargs = {k: target[k] for k in target if k.startswith("compose_")}
                success, msg = checker.update_container(target["name"], target["image"], **compose_kwargs)
                status = "✅" if success else "❌"
                self.send_message(f"{status} {self._display_name(target)}: {msg}")
                if self.notifier:
                    self.notifier.send_update_result(container_name, target["image"], success, msg,
                                                     source_url=target.get("source_url", ""))
            except Exception as e:
                self.send_message(f"❌ {self._display_name(target)}: {str(e)[:200]}")
                if self.notifier:
                    self.notifier.send_update_result(container_name, target.get("image", "?"), False, str(e)[:200],
                                                     source_url=target.get("source_url", ""))

            # Remove from pending list (atomic write — v1.22.1)
            remaining = [u for u in updates if u["name"] != container_name]
            from container_store import atomic_write_json
            atomic_write_json(self.config.pending_file, remaining)

            if not remaining:
                self.send_message(self.t("update_all_done"))
        finally:
            self._update_lock.release()
            self._run_queued_selfupdate()

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

    def _confirm_major_update(self, checker, name):
        """Resume an update that was held back by the major-confirmation gate.
        Reads metadata from the pending-major store, runs update_container,
        clears the pending entry on success."""
        pending = self.store.get_pending_major().get(name)
        if not pending:
            self.send_message(f"⚠️ No pending major update for `{name}`.")
            return
        # Claim the shared update mutex (v1.23.1) — this path previously
        # took no lock, so confirming a major update could collide with
        # a concurrent "Update all" or scheduler auto-update pass.
        if not self._update_lock.acquire(blocking=False):
            self.send_message(self.t("update_already_running"))
            return
        try:
            image = pending.get("image", "")
            compose = pending.get("compose", {}) or {}
            # Resolve link once for both Telegram + Discord/webhook surfaces
            source_url = self._resolve_container_link(name, image)
            try:
                success, msg = checker.update_container(name, image, **compose)
            except Exception as e:
                success, msg = False, str(e)[:200]
            status = "✅" if success else "❌"
            display = f"[{name}]({source_url})" if source_url else f"`{name}`"
            self.send_message(f"{status} {display}: {msg}")
            if self.notifier:
                self.notifier.send_update_result(name, image, success, msg, source_url=source_url)
            if success:
                self.store.remove_pending_major(name)
        finally:
            self._update_lock.release()
            self._run_queued_selfupdate()

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

    def handle_autoupdates(self, updates, checker):
        """Split updates into auto-update and manual, handle accordingly.

        Returns the number of containers that were successfully auto-updated
        (used by the scheduler to decide whether to follow up with cleanup).

        The actual per-container work runs in the shared `_process_update_batch`
        engine (#2) — this method only does the auto-specific scaffolding:
        candidate selection (auto-list + maintenance windows), the mutex
        claim, pending-file bookkeeping, and the notification framing.
        """
        from update_window import is_window_open

        auto_list = self._get_autoupdate()
        windows = self.store.get_update_windows()

        # AUTO_UPDATE_ALL (#45, @NotRetarded): treat *every* checked container
        # as auto-update, not just the per-container opt-ins. Pinned/excluded
        # and label-opt-out containers never reach `updates` in the first
        # place, so this only auto-applies things that were already going to
        # be reported. Off by default — the per-container list still rules.
        all_auto = getattr(self.config, "auto_update_all", False)

        def _effective_auto(u):
            """Auto-update this container? A `docksentry.auto` label wins
            over both the stored per-container toggle and AUTO_UPDATE_ALL
            (#42, @LeeNX — compose file as source of truth): `auto=false`
            keeps a container manual even under AUTO_UPDATE_ALL, `auto=true`
            opts it in without touching the Web UI. No label → previous
            behaviour."""
            lab = None
            if checker is not None:
                try:
                    lab = checker.label_bool(
                        checker.get_container_labels(u["name"]), "auto")
                except Exception:
                    lab = None
            if lab is not None:
                return lab
            return all_auto or u["name"] in auto_list

        auto_candidates = [u for u in updates if _effective_auto(u)]
        # Filter out containers whose maintenance window is closed right now
        skipped_window = [u for u in auto_candidates
                          if not is_window_open(windows.get(u["name"]))]
        auto_updates = [u for u in auto_candidates if u not in skipped_window]
        manual_updates = [u for u in updates if u not in auto_candidates]
        success_count = 0
        major_pending_now = []

        # Claim the shared update mutex (v1.23.1): before, the scheduler's
        # auto-update loop ignored the lock entirely, so a cron tick could
        # recreate the very container a user was mid-updating from Telegram.
        # If a manual update holds the lock we skip auto this tick (next tick
        # retries); the manual flow handles those containers anyway. The
        # notification sections below run regardless — they don't touch
        # containers.
        auto_lock_held = False
        if auto_updates:
            if self._update_lock.acquire(blocking=False):
                auto_lock_held = True
            else:
                print("Auto-update skipped: another update flow is running "
                      "(will retry next tick)")
                auto_updates = []
        try:
            if auto_updates:
                self.send_message(self.t("autoupdate_running", count=len(auto_updates)), auto=True)
                results, success_count, major_pending_now = self._process_update_batch(
                    auto_updates, checker, auto=True)
                self.send_message(self.t("autoupdate_done") + "\n\n" + "\n".join(results), auto=True)

                # Remove fully-processed auto-updates from pending. Major-pending
                # entries stay in pending so the user can also act on them via
                # the Web UI Update buttons; the dedicated confirm flow uses the
                # major-pending store independently.
                major_names = {p[0] for p in major_pending_now}
                processed = {a["name"] for a in auto_updates if a["name"] not in major_names}
                remaining = [u for u in updates if u["name"] not in processed]
                # Atomic write — v1.22.1
                from container_store import atomic_write_json
                atomic_write_json(self.config.pending_file, remaining)
        finally:
            # Release the update mutex if we claimed it for this batch.
            # try/finally guarantees release even if a send_message /
            # atomic_write above raised — otherwise the lock would leak and
            # block every future update.
            if auto_lock_held:
                self._update_lock.release()
                self._run_queued_selfupdate()

        # Window-skipped: tell the user once so they're not surprised
        if skipped_window:
            names = ", ".join(f"`{u['name']}`" for u in skipped_window)
            self.send_message(
                f"⏰ Outside maintenance window — auto-update skipped for: {names}",
                auto=True,
            )

        # Major-confirm queue: send confirmation prompt(s)
        for name, old_ver, new_ver in major_pending_now:
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Confirm", "callback_data": f"confirm_major:{name}"},
                {"text": "❌ Skip", "callback_data": f"reject_major:{name}"},
            ]]}
            self.send_message(
                f"⚠️ *Major update for* `{name}`\n"
                f"  {old_ver} → *{new_ver}*\n\n"
                f"Major version bumps can break configs. Confirm to proceed.",
                reply_markup=keyboard,
                auto=True,
            )

        # Notify about remaining manual updates (this is auto-triggered from
        # scheduler — respect quiet hours)
        if manual_updates:
            self.notify_updates(manual_updates, auto=True)

        return success_count

    def _enrich_with_source_url(self, updates):
        """Set u['source_url'] on each update from the link store + OCI
        labels + registry fallback. Idempotent — safe to call multiple
        times (it overwrites). Used by every notification path so the
        Telegram markdown link, Discord embed and webhook payload all
        share the same resolved URL.
        """
        for u in updates:
            u["source_url"] = self._resolve_container_link(u["name"], u.get("image", ""))

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

    def notify_updates(self, updates, auto=False):
        if not updates:
            return
        # Enrich each update with a `source_url` once so all downstream
        # surfaces (Telegram markdown link, Discord embed, notifier
        # webhook payload) share the same value. Resolved in priority
        # order: manual override → OCI source label → image.url label →
        # registry overview heuristic (#20).
        self._enrich_with_source_url(updates)

        # v1.22.0: sort by Container Group position so the HEAD of each
        # group appears first, followed by its members in order, then
        # orphan containers (= not in any group) at the end. Reported by
        # @famewolf in #2: with a Gluetun+dependents stack, gluetun was
        # showing up LAST in the notification, making cascade-debugging
        # harder. Mirrors the existing sort in handle_autoupdates.
        groups = self.store.get_groups() or {}
        group_position = {}  # container_name → (group_id, position)
        for gid, g in groups.items():
            for pos, cname in enumerate(g.get("containers") or []):
                group_position[cname] = (gid, pos)
        def _sort_key(u):
            gp = group_position.get(u["name"])
            if gp is None:
                return (1, "", 0)  # orphans after groups
            return (0, gp[0], gp[1])
        updates = sorted(updates, key=_sort_key)

        names = []
        for u in updates:
            size = u.get('size', '?')
            created = u.get('created', '?')
            compose_tag = " 🐳" if u.get("compose_project") else ""
            # HEAD badge for the first member of a group (matches the
            # Web UI Groups page badge added in v1.21.0). Only emitted
            # when the user has at least two members in the group —
            # single-container groups have no HEAD semantics.
            head_badge = ""
            gp = group_position.get(u["name"])
            if gp and gp[1] == 0:
                gid = gp[0]
                if len(((groups.get(gid) or {}).get("containers") or [])) > 1:
                    head_badge = " 👑"
            # Container name becomes a markdown link when we have a
            # source URL — Telegram's parse_mode=Markdown renders
            # [text](url) as a tap-to-open hyperlink. Falls back to
            # plain `name` when no URL is available. Shared with the
            # result-message paths via _display_name().
            names.append(f"• {self._display_name(u)}{head_badge} ({u['image']}){compose_tag}{self._version_badge(u)}\n  📦 {size} | 🗓️ {self.t('current')}: {created}")
        text = self.t("updates_available") + "\n\n" + "\n".join(names)

        # Snapshot this notification's exact container set, keyed by a
        # short token carried in the "Update all" button. So tapping
        # "Update all" updates THESE containers, not whatever the latest
        # check has since written to pending_updates.json (#2, @famewolf).
        # We store a shallow copy of the dicts so a later pending-file
        # rewrite can't mutate the snapshot.
        self._snapshot_seq += 1
        token = str(self._snapshot_seq)
        self._update_snapshots[token] = [dict(u) for u in updates]
        # Cap the snapshot store (FIFO) so it can't grow without bound.
        if len(self._update_snapshots) > 20:
            oldest = sorted(self._update_snapshots, key=lambda k: int(k))[0]
            self._update_snapshots.pop(oldest, None)

        # One button per container + all/skip at the bottom
        keyboard = []
        for u in updates:
            size = u.get('size', '?')
            keyboard.append([
                {"text": f"🔄 {u['name']} ({size})", "callback_data": f"update_one:{u['name']}"}
            ])
        keyboard.append([
            {"text": self.t("update_all_btn"), "callback_data": f"update_all:{token}"},
            {"text": self.t("manual_btn"), "callback_data": "update_skip"}
        ])

        reply_markup = {"inline_keyboard": keyboard}
        self.send_message(text, reply_markup, auto=auto)

        # Also notify external channels (notifier itself respects quiet hours)
        if self.notifier:
            self.notifier.send_updates_available(updates)

    def notify_no_updates(self):
        self.send_message(self.t("all_up_to_date"))

    def _build_events_msg(self, limit=15):
        """`/events` reply — the most recent monitor events from the
        persisted log, rendered through the same monitor_* keys as the
        live notifications (#2). Newest first."""
        events = []
        path = getattr(self.config, "monitor_events_file", None)
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    events = json.load(f) or []
            except (ValueError, OSError):
                events = []
        if not events:
            return self.t("events_empty")
        lines = [self.t("events_header", count=min(limit, len(events)))]
        for ev in reversed(events[-limit:]):
            kind = ev.get("kind", "")
            try:
                msg = self.t(f"monitor_{kind}", name=ev.get("container", "?"),
                             **(ev.get("detail") or {}))
            except Exception:
                msg = f"{kind}: {ev.get('container', '?')}"
            lines.append(f"`{ev.get('timestamp', '')}` {msg}")
        return "\n".join(lines)

    def _build_checkimages_msg(self, reclaim_bytes, auto_cleanup):
        """`/checkimages` reply — how much `/cleanup` would free right now.
        Nothing-to-clean and auto-cleanup states each get a clear line so
        the reply is answer-in-glance, not a puzzle."""
        if reclaim_bytes <= 0:
            return self.t("checkimages_none")
        gib = reclaim_bytes / (1024 ** 3)
        # Show GB only from ~1 GB up — a "0.2 GB" or "150 MB" reads much
        # better than "0.2 GB", and 512 MB shouldn't get rounded to "0.5 GB".
        size = f"{gib:.1f} GB" if gib >= 1.0 else f"{reclaim_bytes / (1024 ** 2):.0f} MB"
        msg = self.t("checkimages_reclaimable", size=size)
        if auto_cleanup:
            msg += "\n" + self.t("checkimages_auto_on")
        else:
            msg += "\n" + self.t("checkimages_auto_off")
        return msg

    def _build_dry_run(self, updates, checker):
        """Read-only preview of what applying the pending updates WOULD do —
        the recreate path (compose vs standalone), dependents that would be
        restarted, and major-version jumps that would prompt confirmation.
        Performs no changes (roadmap dry-run, #2)."""
        self._enrich_with_source_url(updates)
        groups = self.store.get_groups() or {}
        # container_name → (group_id, position, members, restart_dependents)
        group_for = {}
        for gid, g in groups.items():
            conts = g.get("containers") or []
            for pos, cname in enumerate(conts):
                group_for[cname] = (gid, pos, conts, bool(g.get("restart_dependents")))

        lines = [self.t("dryrun_title")]
        for u in updates:
            name = u["name"]
            size = u.get("size", "?")
            created = u.get("created", "?")
            compose_tag = " 🐳" if u.get("compose_project") else ""
            block = [f"\n• {self._display_name(u)} ({u['image']}){compose_tag}"
                     f"\n  📦 {size} | 🗓️ {self.t('current')}: {created}"]
            # Update path — mirror _update_compose's own fallback rule: the
            # compose path only runs when the YAML is actually reachable from
            # inside Docksentry, otherwise it falls back to standalone.
            cfile = u.get("compose_file")
            if (u.get("compose_project") and u.get("compose_service")
                    and cfile and os.path.isfile(cfile)):
                block.append("  " + self.t("dryrun_path_compose",
                                           service=u.get("compose_service")))
            else:
                block.append("  " + self.t("dryrun_path_standalone"))
            # Dependents — only the HEAD (position 0) of a multi-member group
            # with restart_dependents triggers the cascade.
            gp = group_for.get(name)
            if gp and gp[1] == 0 and gp[3] and len(gp[2]) > 1:
                deps = ", ".join(f"`{d}`" for d in gp[2][1:])
                block.append("  " + self.t("dryrun_dependents", deps=deps))
            # Major-version jump — would be held for confirmation.
            try:
                is_major, cur_tag, cand_tag = self._is_major_bump(u, checker)
                if is_major:
                    block.append("  " + self.t("dryrun_major",
                                               old=cur_tag, new=cand_tag))
            except Exception:
                pass
            lines.append("\n".join(block))
        lines.append("\n" + self.t("dryrun_footer"))
        return "\n".join(lines)

    def _resolve_group(self, arg):
        """Resolve a /groups argument to (group_id, group_dict). Matches the
        opaque slug or the display name, case-insensitively, then falls back
        to a unique partial match. Returns (None, None) if nothing matches or
        the partial is ambiguous."""
        groups = self.store.get_groups() or {}
        if not groups:
            return None, None
        if arg in groups:
            return arg, groups[arg]
        al = arg.strip().lower()
        for gid, g in groups.items():
            if gid.lower() == al or (g.get("name", "") or "").lower() == al:
                return gid, g
        partial = [(gid, g) for gid, g in groups.items()
                   if al in gid.lower() or al in (g.get("name", "") or "").lower()]
        return partial[0] if len(partial) == 1 else (None, None)

    def _running_names(self):
        """Set of currently-running container names (one docker call)."""
        try:
            r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return set(r.stdout.split())
        except (subprocess.SubprocessError, OSError):
            pass
        return set()

    def _build_groups_list(self):
        """Read-only overview of all Container Groups (#2 roadmap /groups)."""
        groups = self.store.get_groups() or {}
        if not groups:
            return self.t("groups_none")
        lines = [self.t("groups_title")]
        for gid, g in groups.items():
            members = g.get("containers") or []
            rd = " 🔁" if g.get("restart_dependents") else ""
            if len(members) > 1:
                body = "👑 `" + members[0] + "` → " + ", ".join(f"`{m}`" for m in members[1:])
            elif members:
                body = "`" + members[0] + "`"
            else:
                body = "—"
            lines.append(f"\n• `{g.get('name', gid)}` ({len(members)}){rd}\n  {body}")
        lines.append("\n" + self.t("groups_list_hint"))
        return "\n".join(lines)

    def _build_group_detail(self, arg):
        """Detail for one group + an optional restart-dependents button.
        Returns (text, reply_markup)."""
        gid, g = self._resolve_group(arg)
        if not g:
            return self.t("groups_not_found", name=arg), None
        members = g.get("containers") or []
        rd = bool(g.get("restart_dependents"))
        running = self._running_names()
        lines = [self.t("group_detail_title", name=g.get("name", gid))]
        lines.append(self.t("group_detail_rd_on") if rd else self.t("group_detail_rd_off"))
        lines.append("")
        for i, m in enumerate(members):
            icon = "🟢" if m in running else "⚪"
            crown = " 👑" if i == 0 and len(members) > 1 else ""
            lines.append(f"{icon} `{m}`{crown}")
        markup = None
        if rd and len(members) > 1:
            markup = {"inline_keyboard": [[
                {"text": self.t("group_restart_deps_btn"),
                 "callback_data": f"restart_deps:{gid}"}]]}
        return "\n".join(lines), markup

    def _resolve_selfupdate_target(self, current_image, target):
        """Resolve `target` into a fully-qualified image ref to pull.
        Returns (image_ref, error_msg); error_msg is None on success.

        Accepted targets:
            None         → whatever tag the container currently runs
            "latest"     → <base>:latest  (v1.23.4)
            "stable"     → <base>:stable   (v1.23.4)
            "previous"   → last released version older than the running one
            "X.Y.Z"      → a specific semver tag
        """
        if not target:
            return current_image, None

        # Extract base ("registry/owner/repo") from current_image
        if ":" in current_image:
            base = current_image.rsplit(":", 1)[0]
        else:
            base = current_image

        # Moving tags: let the user jump from a pinned :X.Y.Z back onto
        # the rolling :latest / :stable line. Reported by @famewolf in #2
        # — his host was stuck on :1.19.0 and `/selfupdate latest` was
        # rejected, so there was no in-band way to rejoin latest.
        if target.lower() in ("latest", "stable"):
            return f"{base}:{target.lower()}", None

        if target.lower() == "previous":
            # Walk the upstream CHANGELOG for the latest version older
            # than what's currently running — gives the user a one-step
            # rollback target without having to look up version numbers.
            from version import VERSION
            ok, content = self._fetch_changelog()
            if not ok:
                return None, self.t("selfupdate_previous_fetch_failed", error=content)
            import re
            pat = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.MULTILINE)
            try:
                cur = tuple(int(x) for x in VERSION.split(".")[:3])
            except ValueError:
                cur = (0, 0, 0)
            best = None
            for m in pat.finditer(content):
                v = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if v < cur and (best is None or v > best):
                    best = v
            if not best:
                return None, self.t("selfupdate_previous_none", current=VERSION)
            target = f"{best[0]}.{best[1]}.{best[2]}"

        # Validate the resolved target is a clean semver — refuses
        # weird input ("latest", "1.2", "1.2.3-rc1") to avoid pulling a
        # tag that the helper container can't actually find.
        import re as _re
        if not _re.match(r"^\d+\.\d+\.\d+$", target):
            return None, self.t("selfupdate_invalid_version", version=target)
        return f"{base}:{target}", None

    def _latest_released_version(self):
        """Return the highest version in the upstream CHANGELOG as a
        (major, minor, patch) tuple, or None if it can't be fetched or
        parsed. Used to warn a user pinned to an old :X.Y.Z image tag
        that a newer release exists (#2)."""
        ok, content = self._fetch_changelog()
        if not ok:
            return None
        import re
        best = None
        for m in re.finditer(r"^## \[(\d+)\.(\d+)\.(\d+)\]", content, re.MULTILINE):
            v = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if best is None or v > best:
                best = v
        return best

    def _should_retag_moving(self, target, current_image, own_image):
        """True when the user explicitly asked to move onto a ROLLING tag
        (`latest`/`stable`) and the container is currently on a DIFFERENT
        tag — so even when the pulled digest is unchanged we should recreate
        to actually adopt the moving tag. Closes the SET/UNSET asymmetry:
        `/selfupdate <version>` can pin a tag, but returning to `:latest`
        used to require editing compose on the host because a same-digest
        result short-circuited to "up to date" (#2, @famewolf). False when
        already on that tag — no pointless recreate."""
        return target in ("latest", "stable") and own_image != current_image

    def _handle_selfupdate(self, target=None):
        """Pull a target image and recreate own container.

        Coordinates with every other update flow via `_update_lock`:
        - Lock held (a container batch is running): the self-update is
          QUEUED and runs automatically when the batch finishes — a
          restart mid-batch aborted the running updates (#2, @famewolf)
          and could have orphaned a container mid-recreate.
        - Lock free: held for the whole self-update, so no batch can
          start while the swap is in flight. Released on every no-update
          path; deliberately kept once the helper is launched (this
          process is about to be stopped).

        Args:
            target: optional version override:
                None       → whatever tag the container currently runs (usually :latest)
                "previous" → last released version older than the running one
                "X.Y.Z"    → a specific semver tag
        """
        if not self._update_lock.acquire(blocking=False):
            self._queued_selfupdate = (target,)
            self.send_message(self.t("selfupdate_queued"))
            return
        try:
            self._selfupdate_locked(target)
        finally:
            if not self._swap_in_flight:
                self._update_lock.release()

    def cleanup_guarded(self, checker):
        """Run image cleanup under the shared update mutex (#2 follow-up,
        @famewolf). `docker image prune -a` filters on image CREATION
        time, so an image built upstream days ago but pulled seconds ago
        is fair game — pruning during an update's pull→run window would
        delete the image the update is about to run. Serializing through
        the same lock closes that window for every trigger (Telegram
        /cleanup, Web UI button, disk-warning auto-cleanup, post-update
        auto-cleanup).

        Returns (ok, msg) from cleanup_images, or (None, busy-msg) when
        an update flow holds the lock — callers decide how loudly to
        report the skip. A skipped cleanup is never dangerous: the next
        trigger simply runs it.
        """
        if not self._update_lock.acquire(blocking=False):
            return None, self.t("cleanup_busy")
        try:
            return checker.cleanup_images()
        finally:
            self._update_lock.release()
            self._run_queued_selfupdate()

    def _run_queued_selfupdate(self):
        """Run a self-update that was queued behind a container batch.
        Called by every update flow right after it releases the lock;
        no-op when nothing is queued. Never raises (a queue hiccup must
        not mask the batch result it runs after).

        Per-container update FAILURES don't stop the queued run: they are
        normal batch results — reported, rolled back / left in place —
        and a Docksentry restart can't make them worse. But when we're
        called during an exception unwind (the batch flow itself crashed,
        state unknown, error not yet reported), restarting on top of that
        would be reckless and could kill the process before the error
        message ever goes out — so the queued self-update is CANCELLED
        with an honest message instead (#2 follow-up, @famewolf)."""
        q = self._queued_selfupdate
        if q is None:
            return
        self._queued_selfupdate = None
        import sys as _sys
        if _sys.exc_info()[0] is not None:
            try:
                self.send_message(self.t("selfupdate_queue_cancelled"))
            except Exception:
                pass
            return
        try:
            self.send_message(self.t("selfupdate_dequeued"))
            self._handle_selfupdate(q[0])
        except Exception as e:
            print(f"Queued selfupdate error: {e}")

    def _selfupdate_locked(self, target=None):
        """Body of _handle_selfupdate; only ever called with the update
        lock held (see wrapper above)."""
        # Resolve our own container robustly — handles hosts where $HOSTNAME
        # isn't a directly inspect-resolvable reference (e.g. QNAP Container
        # Station reports "no such object" for it; #41, @NotRetarded).
        from update_checker import UpdateChecker as _UC
        config = _UC.inspect_self()
        if not config:
            self.send_message(self.t("selfupdate_failed_container"))
            return
        own_name = config["Name"].lstrip("/")
        current_image = config["Config"]["Image"]
        own_image, err = self._resolve_selfupdate_target(current_image, target)
        if err:
            self.send_message(err)
            return

        # Get current image info
        old_created = config.get("Created", "")[:10]
        old_id_short = config["Image"][:19]

        self.send_message(
            self.t("selfupdate_checking", image=own_image) + "\n"
            + self.t("selfupdate_current_version", date=old_created) + "\n"
            + self.t("selfupdate_image_id", id=old_id_short)
        )

        # Pull latest
        pull = subprocess.run(
            ["docker", "pull", own_image],
            capture_output=True, text=True, timeout=300
        )
        if pull.returncode != 0:
            self.send_message(self.t("selfupdate_failed_pull", error=pull.stderr[:200]))
            return

        # Check if image actually changed
        new_inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}||{{.Created}}", own_image],
            capture_output=True, text=True
        )
        parts = new_inspect.stdout.strip().split("||")
        new_id = parts[0]
        new_created = parts[1][:10] if len(parts) > 1 else "?"
        old_id = config["Image"]

        if new_id == old_id:
            # Same image bits — but if the user explicitly asked to rejoin a
            # rolling tag (`/selfupdate latest`/`stable`) while the container
            # runs a DIFFERENT tag (e.g. a pinned :1.19.0 whose digest equals
            # :latest right now), force a re-tag recreate so the container's
            # image reference actually becomes the moving tag and future
            # updates track it again (#2, @famewolf).
            if self._should_retag_moving(target, current_image, own_image):
                self.send_message(self.t("selfupdate_retag",
                                         old=current_image, new=own_image))
                self._save_selfupdate_history(own_name, own_image,
                                              old_created, new_created)
                self._do_selfupdate(config, own_name, own_image)
                return
            # Up to date for the CURRENT image tag. But if the user ran a
            # plain /selfupdate while the container is on a fixed version
            # tag (e.g. :1.19.0) and a newer release exists, plain "up to
            # date" is misleading — they're stuck on an old version that
            # can't self-update past itself. Guide them. Reported by
            # @famewolf in #2 (his host was stuck on :1.19.0, still hit by
            # the pre-v1.22.0 config-loss bug, with no obvious way out).
            if target is None and ":" in current_image:
                import re
                tag = current_image.rsplit(":", 1)[1]
                m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", tag)
                if m:
                    cur_v = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    latest_v = self._latest_released_version()
                    if latest_v and latest_v > cur_v:
                        latest_str = ".".join(str(x) for x in latest_v)
                        self.send_message(self.t(
                            "selfupdate_old_tag", current=tag, latest=latest_str))
                        return
            self.send_message(self.t("selfupdate_up_to_date"))
            return

        new_id_short = new_id[:19]
        msg = (
            self.t("selfupdate_found") + "\n"
            + self._selfupdate_version_line(own_image)
            + self.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
            + self.t("selfupdate_ids", old=old_id_short, new=new_id_short) + "\n"
            + self.t("selfupdate_releases_link") + "\n\n"
            + self.t("selfupdate_restarting")
        )
        self.send_message(msg)
        # Also fan out to Discord / generic webhook so non-Telegram
        # users actually see self-update events (#19). Same pattern as
        # main.py's startup-message handling.
        if self.notifier and self.notifier.has_channels():
            self.notifier.send_message(msg)

        # Record in history BEFORE _do_selfupdate kills us — otherwise the
        # entry never gets written (#13).
        self._save_selfupdate_history(own_name, own_image, old_created, new_created)
        self._do_selfupdate(config, own_name, own_image)

    def _selfupdate_version_line(self, target_image):
        """The `v_old → v_new` line for self-update messages (#41 follow-up).
        Old = the running VERSION; new = the target image's
        org.opencontainers.image.version label. Returns "" (line omitted)
        when the new version can't be read — e.g. a pre-label image — so we
        never show a half-blank `v1.33.1 → v?` line."""
        from version import VERSION as _cur
        from update_checker import UpdateChecker as _UC
        new_ver = _UC.image_version_label(target_image)
        if not new_ver:
            return ""
        return self.t("selfupdate_versions", old=f"v{_cur}", new=f"v{new_ver}") + "\n"

    @staticmethod
    def _host_docker_socket(config):
        """Resolve the HOST path of the Docker/Podman socket this container
        talks to, from our own inspect Mounts. The self-update helper runs
        as a *separate* container and must mount the SAME host socket —
        hardcoding `/var/run/docker.sock` breaks rootless Podman / custom
        socket paths where the real host socket lives elsewhere (e.g.
        `/run/user/1002/podman/podman.sock` mapped to `/var/run/docker.sock`
        inside the container; #43, @LeeNX). Falls back to
        `/var/run/docker.sock` when it can't be determined."""
        inside = "/var/run/docker.sock"
        dh = os.environ.get("DOCKER_HOST", "")
        if dh.startswith("unix://"):
            inside = dh[len("unix://"):] or inside
        mounts = config.get("Mounts") or []
        for m in mounts:
            if m.get("Destination") == inside and m.get("Source"):
                return m["Source"]
        for m in mounts:
            if m.get("Destination") in ("/var/run/docker.sock", "/run/docker.sock") and m.get("Source"):
                return m["Source"]
        return "/var/run/docker.sock"

    def _write_selfupdate_marker(self, image):
        """Record that the imminent restart is a self-update so the next boot
        doesn't mislabel it as an external stop (#2, @famewolf).

        Writes to `self.config.selfupdate_marker_file` on the persistent data
        dir (survives the container recreate). Best-effort — a failure only
        costs a cosmetic "external restart" line, never the update itself.

        NB: this MUST read the path off `self.config` (the app Config), not
        off the docker-inspect dict that the selfupdate callers pass around as
        their local `config` — that dict has no such attribute, and the bug
        where the marker write silently `AttributeError`-ed for every
        self-update (manual *and* auto) since v1.26.2 was exactly that mixup.
        """
        try:
            import time as _time
            from container_store import atomic_write_json
            atomic_write_json(self.config.selfupdate_marker_file,
                              {"image": image, "ts": _time.time()})
        except Exception as e:
            print(f"Could not write selfupdate marker (non-fatal): {e}")

    @staticmethod
    def _host_mount_source(inspect_config, dest):
        """Host source path of the bind/volume mounted at container path
        `dest`, from our own inspect dict, or None. Lets the self-update
        helper mount the SAME host directory Docksentry uses for a path
        (e.g. /data) so it can leave output where the next boot reads it."""
        for m in (inspect_config.get("Mounts") or []):
            if m.get("Destination") == dest and m.get("Source"):
                return m["Source"]
        return None

    @staticmethod
    def _build_selfupdate_script(name, run_parts, image):
        """Shell run by the helper container to swap Docksentry's image.

        Recovery net (#43, @LeeNX): if the recreate `docker run` fails — e.g. a
        flag the runtime rejects, seen on rootless Podman — the old
        `stop && rename && run && rm` chain left Docksentry DEAD (renamed to
        `_old`, stopped, no new container). The run is now guarded: on failure
        we remove any partial new container, rename `_old` back and start it,
        so the bot survives on the previous version. `rm _old` runs only after
        a successful run, so a failed *cleanup* can't roll back a good update.
        """
        # Quote the container name and image before they land in the `sh -c`
        # script (run_parts is already shlex-quoted by the caller). Docker
        # restricts names/tags to safe characters so shlex.quote is normally
        # a no-op here, but quoting keeps the script robust and consistent
        # with the run-arg handling. `{qname}_old` concatenates fine in sh:
        # an unquoted name stays `ds_old`; a quoted one becomes `'x'_old`,
        # which the shell joins into a single token.
        qname = shlex.quote(name)
        qimage = shlex.quote(image)
        rollback = (
            f"docker rm -f {qname} 2>/dev/null; "
            f"docker rename {qname}_old {qname} 2>/dev/null; "
            f"docker start {qname}"
        )
        return (
            f"sleep 3 && "
            f"docker stop {qname} && "
            f"docker rename {qname} {qname}_old && "
            f"( docker run -d {run_parts} {qimage} || "
            f"( echo 'Selfupdate recreate failed — rolling back'; {rollback}; exit 1 ) ) && "
            f"docker rm {qname}_old"
        )

    def _do_selfupdate(self, config, own_name, own_image):
        """Execute selfupdate via a temporary helper container on the host.

        The old approach (Popen + sys.exit) failed because Docker kills all
        processes inside a container when PID 1 exits. Instead, we launch a
        short-lived helper container that runs on the host and performs the
        stop/rename/run/cleanup sequence from outside.
        """
        # Mark that the imminent restart is a self-update. The recreate sends
        # SIGTERM to our old container, which the signal handler records as a
        # generic external stop — without this marker the next boot would
        # mislabel the self-update as "external restart" (#2, @famewolf).
        self._write_selfupdate_marker(own_image)
        # Rebuild run command from inspect. Single source of truth via
        # UpdateChecker._build_run_args so the self-update path stays
        # in sync with the regular container-update path (#27): same
        # HostConfig coverage (capabilities, devices, sysctls, tmpfs,
        # extra hosts, DNS, init, shm, log config, ipc/pid/uts modes,
        # runtime, read-only rootfs, user) is preserved on recreate.
        # `_build_run_args` returns the FULL docker run argv including
        # `["docker", "run", "-d", "--name", own_name, ...]` and the
        # image at the end. We slice off the leading `docker run -d`
        # and the trailing image+cmd because the helper-container
        # update_script formats those separately.
        from update_checker import UpdateChecker as _UC
        # Fetch image's default Entrypoint/Cmd so _build_run_args can
        # avoid locking in the OLD image's tokens on update. Best-effort:
        # on failure we pass None which preserves pre-v1.19.0 behaviour.
        image_defaults = None
        try:
            ii = subprocess.run(
                ["docker", "image", "inspect", own_image],
                capture_output=True, text=True, timeout=10,
            )
            if ii.returncode == 0:
                data = json.loads(ii.stdout)
                if data:
                    icfg = data[0].get("Config") or {}
                    image_defaults = {
                        "Entrypoint": icfg.get("Entrypoint"),
                        "Cmd": icfg.get("Cmd"),
                    }
        except (subprocess.SubprocessError, json.JSONDecodeError,
                IndexError, ValueError):
            pass
        # The OLD image's Config, so inherited Env/Labels/User/WorkingDir/
        # StopSignal/Healthcheck aren't pinned onto the new one (#35).
        # `config` is our own container inspect, so .Image is the image we
        # are currently running — the one we're updating away from.
        inherited = _UC._image_config(config.get("Image") or "")
        full = _UC._build_run_args(config, own_image, own_name, image_defaults,
                                   inherited=inherited)
        # full = ["docker", "run", "-d", "--name", own_name, ...flags..., own_image, ...cmd...]
        # We need just the flags between "-d" and own_image:
        try:
            img_idx = full.index(own_image)
            run_args = full[3:img_idx]  # drop ["docker","run","-d"] and image+cmd
        except ValueError:
            # Defensive — should never happen since we passed own_image
            run_args = full[3:-1]

        # Build the full recreation command
        run_parts = " ".join(shlex.quote(a) for a in run_args)
        update_script = self._build_selfupdate_script(own_name, run_parts, own_image)

        # Launch a temporary helper container on the host that performs the swap.
        # This container survives because it runs independently on the Docker host.
        helper_name = f"{own_name}_updater"
        # Clean up any leftover helper from a previous attempt
        subprocess.run(["docker", "rm", "-f", helper_name],
                       capture_output=True, timeout=10)

        # v1.22.2: pre-pull `docker:cli` so the helper launch is clean.
        # Previously the implicit auto-pull by `docker run` would write
        # progress to stderr ("Unable to find image 'docker:cli' locally"
        # + layer download lines), and if the auto-pull went sideways
        # (slow registry, transient network blip) the helper-launch
        # subprocess would surface that stderr as the failure message
        # — confusing users when the selfupdate then succeeded anyway.
        # Reported by @NotRetarded in #2.
        helper_pull = subprocess.run(
            ["docker", "pull", "docker:cli"],
            capture_output=True, text=True, timeout=120,
        )
        if helper_pull.returncode != 0:
            self.send_message(
                f"❌ Selfupdate failed: couldn't pull helper image `docker:cli` — "
                f"{(helper_pull.stderr or helper_pull.stdout or 'unknown').strip()[:200]}"
            )
            return

        # Mount the SAME host socket Docksentry itself uses (not a hardcoded
        # path) so the helper works on rootless Podman / custom sockets (#43).
        host_sock = self._host_docker_socket(config)
        helper_args = [
            "docker", "run", "-d",
            "--name", helper_name,
            "--rm",
            "-v", f"{host_sock}:/var/run/docker.sock",
        ]
        # Also mount our /data host dir so the helper can drop its output
        # where the next boot reads it — a --rm helper's stderr otherwise
        # vanishes, which is why we've never seen WHY the podman recreate
        # fails (#43). When mounted, redirect the whole swap script's output
        # into it; the next boot surfaces it if the recreate rolled back.
        host_data = self._host_mount_source(config, self.config.data_dir)
        run_script = update_script
        if host_data:
            helper_args += ["-v", f"{host_data}:{self.config.data_dir}"]
            logpath = f"{self.config.data_dir}/selfupdate_helper.log"
            run_script = f"({update_script}) > {logpath} 2>&1"
        helper_args += ["docker:cli", "sh", "-c", run_script]
        result = subprocess.run(helper_args, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            self.send_message(f"❌ Selfupdate failed: {result.stderr[:200]}")
            return

        # The helper container will stop us in ~3 seconds. Keep the update
        # lock held from here on (see _handle_selfupdate wrapper): nothing
        # may start a container update in the final seconds of this process.
        self._swap_in_flight = True
        print(f"Selfupdate helper started ({helper_name}). Waiting for shutdown...")
        import time
        time.sleep(30)

    def check_selfupdate_auto(self, defer_check=False):
        """Automatic selfupdate check — triggered by the scheduler when
        AUTO_SELFUPDATE=true. Skips the cycle (returns False) when any
        update flow holds the lock — killing a manual batch mid-flight is
        the same hazard as the manual /selfupdate case (#2, @famewolf);
        the next scheduled tick simply retries. Holds the lock through
        its own pull+swap so no batch can start mid-self-update.
        """
        if not self._update_lock.acquire(blocking=False):
            print("Selfupdate check: skipped — another update flow is running.")
            return False
        try:
            return self._check_selfupdate_auto_locked(defer_check)
        finally:
            if not self._swap_in_flight:
                self._update_lock.release()

    def _check_selfupdate_auto_locked(self, defer_check=False):
        """Body of check_selfupdate_auto; only ever called with the
        update lock held (see wrapper above).

        When `defer_check` is True (called from a cron tick that is about
        to also run a container-update check), we write a deferred-check
        marker before triggering the restart. The freshly-booted process
        picks up that marker and runs the container-update check
        immediately, so the user gets a single linear story:
            "self-updating…" → restart → "checking your containers…"
        instead of running the check on the *old* code first and then
        killing the bot mid-conversation with a self-update.

        Returns:
            True  — a self-update was applied; this process is about to
                    be replaced by the helper container. Caller should
                    return out of the current cron tick.
            False — no update available, or the inspect/pull failed.
                    Caller continues with the rest of the tick.
        """
        # Robust self-resolution (see _handle_selfupdate / #41) — works even
        # where $HOSTNAME isn't directly inspect-resolvable.
        # Every exit below PRINTS its reason. This path is the ONLY
        # selfupdate channel on headless installs (Web UI button with
        # Telegram off), and its silent `return False`s made a failing
        # podman pull indistinguishable from "already up to date" —
        # @LeeNX chased exactly that ghost in #43/#46.
        from update_checker import UpdateChecker as _UC
        config = _UC.inspect_self()
        if not config:
            print("Selfupdate check: FAILED — can't inspect own container.")
            return False
        own_image = config["Config"]["Image"]
        old_id = config["Image"]
        old_created = config.get("Created", "")[:10]
        print(f"Selfupdate check: pulling {own_image} "
              f"(running {old_id[:19]}, created {old_created})...")

        pull = subprocess.run(
            ["docker", "pull", own_image],
            capture_output=True, text=True, timeout=300
        )
        if pull.returncode != 0:
            print(f"Selfupdate check: FAILED — pull of {own_image!r} "
                  f"returned {pull.returncode}: {pull.stderr.strip()[:300]}")
            return False

        # Check if image changed
        new_inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}||{{.Created}}", own_image],
            capture_output=True, text=True
        )
        parts = new_inspect.stdout.strip().split("||")
        new_id = parts[0]
        new_created = parts[1][:10] if len(parts) > 1 else "?"

        if new_id == old_id:
            print(f"Selfupdate check: already up to date ({new_id[:19]}).")
            return False
        print(f"Selfupdate check: update found {old_id[:19]} -> {new_id[:19]} "
              f"({old_created} -> {new_created}) — starting selfupdate.")

        # Notify and update
        own_name = config["Name"].lstrip("/")

        # Write deferred-check marker BEFORE triggering the restart so the
        # freshly-booted process can pick up where we left off. Best-effort
        # — if writing fails the worst case is the user has to wait until
        # the next cron tick for the container-update check.
        if defer_check:
            try:
                from datetime import datetime
                from container_store import atomic_write_json
                atomic_write_json(
                    self.config.deferred_check_file,
                    {
                        "trigger_time": datetime.now().isoformat(timespec="seconds"),
                        "reason": "post-selfupdate",
                    },
                )
            except OSError as e:
                print(f"Failed to write deferred-check marker: {e}")

        # Send a single combined notification when defer_check is on, so
        # the user sees one story instead of two unrelated messages.
        version_line = self._selfupdate_version_line(own_image)
        if defer_check:
            msg = (
                self.t("selfupdate_auto") + "\n"
                + version_line
                + self.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
                + self.t("selfupdate_releases_link") + "\n"
                + self.t("selfupdate_restarting_then_check")
            )
        else:
            msg = (
                self.t("selfupdate_auto") + "\n"
                + version_line
                + self.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
                + self.t("selfupdate_releases_link") + "\n"
                + self.t("selfupdate_restarting")
            )
        self.send_message(msg)
        # Fan out to Discord / generic webhook so non-Telegram users
        # see self-update events too (#19).
        if self.notifier and self.notifier.has_channels():
            self.notifier.send_message(msg)

        # Record in history BEFORE _do_selfupdate kills us — otherwise the
        # entry never gets written (#13). Same data path as the manual
        # /selfupdate handler.
        self._save_selfupdate_history(own_name, own_image, old_created, new_created)

        # Reuse the selfupdate logic — this blocks for ~30s while the
        # helper container stops us. Caller should treat this as a
        # one-way call.
        self._do_selfupdate(config, own_name, own_image)
        return True

    def run_updates(self, updater, updates=None):
        """Run a batch of container updates.

        When `updates` is provided (a list of update dicts from a
        notification snapshot, v1.23.3), exactly those containers are
        updated and only their names are removed from the pending file
        afterward — so "Update all" acts on the set the notification
        actually showed. When `updates` is None (legacy bare-"update_all"
        callback, or programmatic callers), the current pending file is
        read and the whole file is cleared on completion as before.
        """
        # Atomic claim of the shared update mutex. If any other update
        # flow (single-container, major-confirm, or the scheduler's
        # auto-update pass) holds it, bail. try/finally guarantees we
        # always release — the old `update_running = False` only ran at
        # the end, so an exception outside the inner loop would have left
        # the flag stuck True forever, blocking all future updates.
        if not self._update_lock.acquire(blocking=False):
            self.send_message(self.t("update_already_running"))
            return
        try:
            from_snapshot = updates is not None
            pending_file = self.config.pending_file
            if not from_snapshot:
                if not os.path.exists(pending_file):
                    self.send_message(self.t("no_pending_updates"))
                    return
                with open(pending_file) as f:
                    updates = json.load(f)

            if not updates:
                self.send_message(self.t("no_pending_updates"))
                return

            self.send_message(self.t("update_starting", count=len(updates)))

            # All per-container work (enrich, group-order sort, netns snapshot,
            # update, cascade, cooldown) runs in the shared engine (#2). The
            # manual path passes auto=False, which skips the maintenance-window
            # filter (already applied at candidate selection) and the
            # ask-before-major gate — tapping "Update all" is itself the
            # explicit "do it now / yes to majors".
            results, _sc, _mp = self._process_update_batch(updates, updater, auto=False)

            if from_snapshot:
                # Remove only the processed containers from pending —
                # the file may hold others the snapshot didn't include.
                self._remove_from_pending([u["name"] for u in updates])
            else:
                try:
                    os.remove(pending_file)
                except OSError:
                    pass

            self.send_message(self.t("update_result") + "\n\n" + "\n".join(results))
        finally:
            self._update_lock.release()
            self._run_queued_selfupdate()

    def _remove_from_pending(self, names):
        """Drop the given container names from pending_updates.json
        (atomic). No-op if the file is missing or unreadable."""
        pending_file = self.config.pending_file
        if not os.path.exists(pending_file):
            return
        try:
            with open(pending_file) as f:
                pending = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        name_set = set(names)
        remaining = [u for u in pending if u.get("name") not in name_set]
        from container_store import atomic_write_json
        if remaining:
            atomic_write_json(pending_file, remaining)
        else:
            try:
                os.remove(pending_file)
            except OSError:
                pass

    # Long-polling timing for getUpdates. Telegram holds the request open
    # for LONG_POLL_TIMEOUT seconds; the HTTP socket gets a generous extra
    # buffer so it doesn't trip first on slow networks (TLS handshake, DNS,
    # etc.). HTTP > Long-poll is required by Telegram's docs.
    LONG_POLL_TIMEOUT = 25
    LONG_POLL_HTTP_TIMEOUT = LONG_POLL_TIMEOUT + 15  # = 40s

    def _register_commands_with_telegram(self):
        """Push the bot's command list to Telegram via `setMyCommands`.

        Result: typing `/` in any chat with this bot pops up a native
        Telegram autocomplete picker, with the per-command descriptions
        below. This is the canonical Telegram command-discovery UX and
        removes the need for users to remember command names.

        Descriptions are short (≤ ~80 chars each) because Telegram only
        shows them in a single line in the picker. We use English for
        the picker entries — Telegram's autocomplete is based on the
        user's Telegram client language, not the bot's configured
        language, so EN reaches everyone. Per-language picker entries
        are possible via the `language_code` parameter but it'd be a
        big i18n maintenance burden for marginal benefit; skipped.

        Idempotent and safe to call on every boot."""
        if not self.enabled:
            return
        # Single source of truth at module top — _BOT_COMMANDS — drives
        # both the picker registration here and the /help output below.
        commands = [
            {"command": name, "description": picker_desc}
            for (name, picker_desc, _help_key, _detail_key) in _BOT_COMMANDS
        ]
        try:
            r = self.api_call("setMyCommands", {"commands": json.dumps(commands)})
            if r and r.get("ok"):
                print(f"Telegram command picker registered ({len(commands)} commands)")
            elif r:
                print(f"Telegram command picker: setMyCommands returned not-ok: {r.get('description', r)}")
        except Exception as e:
            # Non-fatal — bot keeps working without the picker.
            print(f"Telegram command picker registration failed (non-fatal): {e}")

    def listen(self, checker, scheduler):
        import time as _time
        self.start_time = _time.time()

        # Headless mode: no Telegram credentials. Don't poll, just block here
        # so the scheduler thread (and Web UI, if enabled) keep running.
        if not self.enabled:
            print("Telegram disabled (no BOT_TOKEN/CHAT_ID). Running headless.")
            while self.running:
                _time.sleep(1)
            return

        # Send-only mode (#2, @famewolf): notifications yes, polling no.
        # Telegram allows one getUpdates consumer per token, so a second
        # app sharing the bot (Home Assistant) fights us for it. We skip
        # BOTH the poll loop AND the startup flush (itself a getUpdates
        # call) AND setMyCommands (which is global per bot and would clobber
        # the other app's command list). sendMessage never conflicts, so
        # notifications keep flowing. Interactive commands are off — the
        # user drives Docksentry from the Web UI instead.
        if not getattr(self.config, "telegram_polling", True):
            print("Telegram in send-only mode (TELEGRAM_POLLING=false): "
                  "notifications on, interactive commands off. "
                  "Control Docksentry via the Web UI.")
            while self.running:
                _time.sleep(1)
            return

        # Register our command list with Telegram so users get the
        # native `/` autocomplete picker (with one-line descriptions per
        # command). Idempotent — Telegram just stores the latest set —
        # so calling on every boot is fine and means newly-added commands
        # surface without any setup step on the user's side.
        self._register_commands_with_telegram()

        # Query our own username via getMe and cache it. Needed for
        # `@botname` targeting in multi-bot groups (#25): if the user
        # writes `/check@dockmox-bot`, only the bot whose username is
        # `dockmox-bot` should respond — the v1.18.5 strip was too
        # aggressive and made *every* bot respond regardless.
        self.bot_username = ""
        try:
            r = self.api_call("getMe")
            if r and r.get("ok"):
                self.bot_username = (r.get("result") or {}).get("username", "") or ""
                if self.bot_username:
                    print(f"Bot identified as @{self.bot_username}")
        except Exception as e:
            print(f"getMe failed (non-fatal, @botname targeting disabled): {e}")

        # Flush old updates from queue to prevent replaying commands after restart
        flush = self.api_call("getUpdates", {"offset": -1, "timeout": 0})
        if flush and flush.get("ok") and flush.get("result"):
            offset = flush["result"][-1]["update_id"] + 1
            print(f"Flushed {len(flush['result'])} old updates from queue.")
        else:
            offset = 0

        print("Bot listener started. Waiting for Telegram messages...")

        while self.running:
            try:
                # Long-poll. Timeouts here are expected on flaky networks and
                # are NOT real errors — they're just "no new updates arrived
                # within the long-poll window". quiet_timeout=True suppresses
                # the noisy log line.
                result = self.api_call(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": self.LONG_POLL_TIMEOUT,
                        "allowed_updates": json.dumps(["callback_query", "message", "edited_message"]),
                    },
                    timeout=self.LONG_POLL_HTTP_TIMEOUT,
                    quiet_timeout=True,
                )

                if not result or not result.get("ok"):
                    import time
                    time.sleep(5)
                    continue

                for update in result.get("result", []):
                    offset = update["update_id"] + 1

                    # Callback buttons
                    callback = update.get("callback_query")
                    if callback:
                        self._handle_callback(callback, checker)
                        continue

                    # Text commands (incl. recalled-via-↑ edits, #15)
                    message = self._message_from_update(update)
                    if message:
                        self._handle_message(message, checker, scheduler)

            except Exception as e:
                print(f"Bot listener error: {e}")
                import time
                time.sleep(5)

        print("Bot listener stopped.")

    def _handle_callback(self, callback, checker):
        data = callback.get("data", "")
        user_id = str(callback["from"]["id"])
        msg_id = callback.get("message", {}).get("message_id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")

        if not self._check_auth(chat_id, user_id, kind="callback"):
            self.answer_callback(callback["id"], self.t("not_authorized"))
            return

        if data == "update_all" or data.startswith("update_all:"):
            # Tokenised form (v1.23.3): "update_all:<token>" updates the
            # exact container set that THIS notification showed, looked
            # up from the per-notification snapshot. Bare "update_all"
            # (no token) is the legacy form from notifications sent by
            # older versions still sitting in the chat — fall back to the
            # current pending file as before.
            snapshot = None
            if data.startswith("update_all:"):
                token = data.split(":", 1)[1]
                snapshot = self._update_snapshots.get(token)
                if snapshot is None:
                    # Snapshot evicted (FIFO) or lost to a bot restart —
                    # the notification is stale. Tell the user rather
                    # than silently updating the wrong (current) set.
                    self.answer_callback(callback["id"],
                                         self.t("update_snapshot_stale"))
                    if msg_id and chat_id:
                        self.remove_buttons(chat_id, msg_id)
                    self.send_message(self.t("update_snapshot_stale_msg"))
                    return
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            self.answer_callback(callback["id"], self.t("updates_starting_cb"))
            t = threading.Thread(target=self.run_updates,
                                 args=(checker,), kwargs={"updates": snapshot})
            t.start()
        elif data == "update_skip":
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            self.answer_callback(callback["id"], self.t("ok_manual_cb"))
            self.send_message(self.t("manual_message"))
            try:
                os.remove(self.config.pending_file)
            except OSError:
                pass
        elif data.startswith("update_one:"):
            container_name = data.split(":", 1)[1]
            self.answer_callback(callback["id"], f"Update {container_name}...")
            # Remove only this button, keep the rest
            if msg_id and chat_id:
                self._remove_single_button(chat_id, msg_id, data)
            t = threading.Thread(target=self._run_single_update, args=(checker, container_name))
            t.start()
        elif data.startswith("confirm_major:"):
            name = data.split(":", 1)[1]
            self.answer_callback(callback["id"], f"Confirming major update for {name}...")
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            t = threading.Thread(target=self._confirm_major_update,
                                 args=(checker, name))
            t.start()
        elif data.startswith("reject_major:"):
            name = data.split(":", 1)[1]
            self.answer_callback(callback["id"], f"Skipped major update for {name}.")
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            self.store.remove_pending_major(name)
            self.send_message(f"⏭ Major update for `{name}` skipped.")

        elif data.startswith("lifecycle:"):
            # Inline-button action under /status <name>. Format:
            # "lifecycle:<action>:<container_name>". Auth already
            # passed at the top of _handle_callback.
            try:
                _, action, target = data.split(":", 2)
            except ValueError:
                self.answer_callback(callback["id"], "Bad action")
                return
            if action not in ("start", "stop", "restart"):
                self.answer_callback(callback["id"], "Unknown action")
                return
            # Drop the buttons so the user can't double-fire while the
            # action runs.
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            self.answer_callback(callback["id"], self.t("lifecycle_running", action=action))
            ok, msg = self._lifecycle_action(action, target, checker)
            self.send_message(msg)

        elif data.startswith("restart_deps:"):
            # Manual restart-dependents cascade from /groups <name>.
            gid = data.split(":", 1)[1]
            g = self.store.get_group(gid)
            members = (g or {}).get("containers") or []
            if not g or len(members) < 2:
                self.answer_callback(callback["id"], "No dependents")
                return
            self.answer_callback(callback["id"], self.t("group_restarting_deps"))
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            head, deps = members[0], members[1:]
            wait = int(g.get("wait_seconds", 30) or 30)

            def _run():
                self.send_message(self._restart_group_dependents(head, deps, checker, max_wait=wait))
            threading.Thread(target=_run).start()

    @staticmethod
    def _message_from_update(update, now=None):
        """Pick the message to handle from a Telegram update. A normal
        `message` passes through. An `edited_message` is honoured only when
        the edit is recent (≤120s) — pressing ↑ in Telegram (Desktop) edits
        the last message instead of sending a new one, so a recalled
        `/command` arrives as an edit (#15, @famewolf); the recency guard
        keeps an old message edited for unrelated reasons from re-running.
        Returns {} when there's nothing actionable."""
        msg = update.get("message")
        if msg:
            return msg
        edited = update.get("edited_message")
        if edited:
            import time as _t
            ref = now if now is not None else _t.time()
            if ref - float(edited.get("edit_date", 0) or 0) <= 120:
                return edited
        return {}

    def _handle_message(self, message, checker, scheduler):
        text = message.get("text", "")
        user_id = str(message.get("from", {}).get("id", ""))
        chat_id = message.get("chat", {}).get("id")

        if not self._check_auth(chat_id, user_id, kind="message"):
            return

        # Telegram's group-multi-bot-disambiguation: in a group with
        # ≥ 2 bots, tapping a registered command in the picker sends
        # `/check@dockmox-bot` rather than `/check`. Three cases to
        # handle correctly (#21 + #25):
        #   1. No `@` in the command token → handle as usual.
        #   2. `@<our-username>` → strip and handle (we're the target).
        #   3. `@<some-other-bot>` → silent ignore (not for us).
        # The old v1.18.5 implementation stripped any `@` blindly,
        # which made every bot in the group respond regardless of who
        # was being addressed — defeating the point of targeted
        # addressing. User mentions later in the payload (e.g.
        # `/notify @someone hello`) are still preserved because we
        # only touch the first token.
        if text.startswith("/") and "@" in text.split(" ", 1)[0]:
            cmd, sep, rest = text.partition(" ")
            target = cmd.split("@", 1)[1]
            own = (self.bot_username or "").lower()
            if own and target.lower() != own:
                # Addressed to a different bot — stay silent.
                return
            text = cmd.split("@", 1)[0] + sep + rest

        # `-?` as a per-command help alias (#15, @LeeNX): `/protect -?`
        # behaves exactly like `/help protect`, routed through the canonical
        # /help path.
        text = self._help_alias(text)

        # `/status <name>` — per-container detail with inline action
        # buttons. The arg-less `/status` keeps the overview behaviour.
        if text.startswith("/status ") and len(text.split(maxsplit=1)) > 1:
            partial = text.split(maxsplit=1)[1].strip()
            # Glob → a compact one-line-per-match overview (#40, @LeeNX).
            # Read-only, so no action buttons; use /status <name> for the
            # full single-container detail.
            if self._is_glob(partial):
                names = self._match_glob(partial)
                if not names:
                    self.send_message(self.t("glob_no_match", pattern=partial))
                    return
                lines = [self.t("glob_status_header", count=len(names), pattern=partial)]
                for nm in names:
                    si = self._container_state(nm)
                    if not si:
                        continue
                    icon = "🟢" if si["running"] else ("⏸" if si["state"] == "paused" else "⏹")
                    health = f" ({si['health']})" if si["health"] else ""
                    lines.append(f"{icon} `{nm}` — {si['state']}{health}")
                self.send_message("\n".join(lines))
                return
            resolved, err = self._resolve_container(partial)
            if not resolved:
                self.send_message(err)
                return
            info = self._container_state(resolved)
            if not info:
                self.send_message(self.t("resolve_not_found", name=resolved))
                return
            # State / health label with color icon
            state_icon = "✅" if info["running"] else ("⏸" if info["state"] == "paused" else "⏹")
            state_text = info["state"]
            if info["health"]:
                state_text += f" ({info['health']})"
            uptime_line = f"⏱ *Uptime:* {info['uptime']}" if info["running"] else ""

            lines = [
                f"📊 *{info['name']}*  {state_icon}",
                f"*State:* `{state_text}`",
            ]
            if uptime_line:
                lines.append(uptime_line)
            lines.append(f"*Image:* `{info['image']}`")
            if info.get("version"):
                lines.append(f"*Version:* `{info['version']}`")
            if info.get("short_id"):
                lines.append(f"*Image ID:* `{info['short_id']}`")
            lines.extend([
                f"*Ports:* {info['ports']}",
                f"*Volumes:* {info['volumes']}",
                f"*Restart policy:* `{info['restart_policy']}`",
            ])

            # Build inline keyboard based on current state.
            buttons = []
            if info["running"]:
                buttons.append({"text": self.t("lifecycle_btn_restart"),
                                "callback_data": f"lifecycle:restart:{resolved}"})
                # Stop hidden for protected containers (#38). The callback is
                # also guarded in _lifecycle_action, so a stale button can't
                # slip a stop through either.
                if not self._is_protected(resolved, checker):
                    buttons.append({"text": self.t("lifecycle_btn_stop"),
                                    "callback_data": f"lifecycle:stop:{resolved}"})
            else:
                buttons.append({"text": self.t("lifecycle_btn_start"),
                                "callback_data": f"lifecycle:start:{resolved}"})

            self.send_message(
                "\n".join(lines),
                reply_markup={"inline_keyboard": [buttons]},
            )
            return

        if text == "/status":
            # Use docker inspect (not docker ps Status-string parsing) so health
            # detection works on both Docker and Podman. Podman's REST API does
            # not append `(healthy)` to the Status field — that's a Docker CLI
            # cosmetic — but State.Health.Status is consistently provided by
            # both. Reported by LeeNX in #28 for podman-compose containers.
            ids_p = subprocess.run(
                ["docker", "ps", "-q"],
                capture_output=True, text=True
            )
            ids = [i for i in ids_p.stdout.strip().split("\n") if i]
            inspected = []
            if ids:
                ins_p = subprocess.run(
                    ["docker", "inspect", *ids],
                    capture_output=True, text=True
                )
                try:
                    inspected = json.loads(ins_p.stdout) or []
                except (json.JSONDecodeError, ValueError):
                    inspected = []
            total = len(inspected)
            healthy = 0
            unhealthy = 0
            running = 0
            containers = []

            from datetime import datetime as _dt, timezone as _tz

            for cfg in inspected:
                name = (cfg.get("Name") or "?").lstrip("/")
                image = (cfg.get("Config") or {}).get("Image", "?")
                state = cfg.get("State") or {}
                health = (state.get("Health") or {}).get("Status", "")

                # Uptime from StartedAt — same logic as _container_state
                started_at = state.get("StartedAt", "")
                uptime = "?"
                if state.get("Running") and started_at:
                    try:
                        s = _dt.fromisoformat(started_at.replace("Z", "+00:00"))
                        delta = _dt.now(_tz.utc) - s
                        secs = int(delta.total_seconds())
                        if secs < 60:
                            uptime = f"{secs}s"
                        elif secs < 3600:
                            uptime = f"{secs // 60}m {secs % 60}s"
                        elif secs < 86400:
                            uptime = f"{secs // 3600}h {(secs % 3600) // 60}m"
                        else:
                            d = secs // 86400
                            h = (secs % 86400) // 3600
                            uptime = f"{d}d {h}h"
                    except (ValueError, AttributeError):
                        pass

                # Determine health icon from State.Health.Status (Docker+Podman)
                if health == "healthy":
                    icon = "🟢"
                    healthy += 1
                elif health == "unhealthy":
                    icon = "🔴"
                    unhealthy += 1
                elif health == "starting":
                    icon = "🟡"
                    running += 1
                else:
                    icon = "⚪"
                    running += 1

                containers.append(f"{icon} `{name}`\n     ⏱ {uptime} · 📦 `{image}`")

            # Summary line
            summary = f"📊 *{total}* {self.t('status_containers')}"
            if healthy:
                summary += f" · 🟢 {healthy}"
            if unhealthy:
                summary += f" · 🔴 {unhealthy}"
            if running:
                summary += f" · ⚪ {running}"

            # Bot uptime
            import time as _t
            bot_uptime_s = int(_t.time() - self.start_time)
            days = bot_uptime_s // 86400
            hours = (bot_uptime_s % 86400) // 3600
            mins = (bot_uptime_s % 3600) // 60
            if days > 0:
                bot_uptime = f"{days}d {hours}h {mins}m"
            elif hours > 0:
                bot_uptime = f"{hours}h {mins}m"
            else:
                bot_uptime = f"{mins}m"

            # Pinned & auto-update counts
            pinned_count = len(self._get_pinned())
            auto_count = len(self._get_autoupdate())

            header = (
                f"{self.t('container_status')}\n\n"
                f"{summary}\n"
                f"🤖 Bot Uptime: {bot_uptime}\n"
            )
            if pinned_count:
                header += f"📌 {self.t('status_pinned')}: {pinned_count}\n"
            if auto_count:
                header += f"⚡ {self.t('status_autoupdate')}: {auto_count}\n"

            header += f"\n{'─' * 28}\n\n"

            # Split into chunks if message exceeds Telegram's 4096 char limit
            full_msg = header + "\n".join(containers)
            if len(full_msg) <= 4000:
                self.send_message(full_msg)
            else:
                # Send header first, then containers in chunks
                self.send_message(header.rstrip())
                chunk = ""
                for c in containers:
                    if len(chunk) + len(c) + 1 > 3500:
                        self.send_message(chunk)
                        chunk = ""
                    chunk += c + "\n"
                if chunk.strip():
                    self.send_message(chunk)

        elif text in ("/check --dry-run", "/check dry-run", "/dryrun"):
            # Dry run: detect updates and show what applying them WOULD do
            # (path, dependents, major jumps) without changing anything.
            if self.update_running:
                self.send_message(self.t("update_already_running"))
                return
            self.send_message(self.t("checking_updates"))
            updates = checker.check_all(bot=self)
            if updates:
                self.send_message(self._build_dry_run(updates, checker))
            else:
                self.notify_no_updates()

        elif text.startswith("/check ") and len(text.split(maxsplit=1)) > 1:
            # /check <name|glob> — scope the check to selected containers (#40).
            if self.update_running:
                self.send_message(self.t("update_already_running"))
                return
            arg = text.split(maxsplit=1)[1].strip()
            names, err = self._select_containers(arg)
            if err:
                self.send_message(err)
                return
            self.send_message(self.t("checking_updates"))
            nameset = set(names)
            updates = [u for u in checker.check_all(bot=self) if u["name"] in nameset]
            if updates:
                self.notify_updates(updates)
            else:
                self.send_message(self.t("check_scoped_uptodate",
                                         count=len(names), pattern=arg))

        elif text == "/update" or (text.startswith("/update ") and len(text.split(maxsplit=1)) > 1):
            # /update <name|glob> — check then update only the matching
            # containers that actually have a pending update (#40). Bulk by
            # design; the user typing the pattern is the explicit go-ahead,
            # and run_updates carries all the group/guard gates + the mutex.
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                self.send_message(self.t("update_usage"))
                return
            if self.update_running:
                self.send_message(self.t("update_already_running"))
                return
            arg = parts[1].strip()
            names, err = self._select_containers(arg)
            if err:
                self.send_message(err)
                return
            self.send_message(self.t("checking_updates"))
            nameset = set(names)
            updates = [u for u in checker.check_all(bot=self) if u["name"] in nameset]
            if not updates:
                self.send_message(self.t("update_scoped_none", pattern=arg))
                return
            self.send_message(self.t("update_scoped_starting",
                                     count=len(updates), pattern=arg))
            threading.Thread(target=self.run_updates,
                             args=(checker,), kwargs={"updates": updates}).start()

        elif text == "/check":
            # Don't run a check while a manual update is in progress —
            # `check_all` would still see in-flight containers as
            # "available" (they're on the pre-pull digest until the
            # recreate lands), producing a misleading second "Updates
            # Available" notification a few seconds after the user
            # already tapped "Update all". `run_updates` itself is
            # single-instance protected so no data harm, but the UX
            # confusion was real. Reported by @famewolf in #26.
            if self.update_running:
                self.send_message(self.t("update_already_running"))
                return
            self.send_message(self.t("checking_updates"))
            updates = checker.check_all(bot=self)
            if updates:
                self.notify_updates(updates)
            else:
                self.notify_no_updates()
            # Docksentry-selfupdate hint (#2, @famewolf): the regular
            # `check_all` filters us out (get_running_containers → "Skipped
            # (self)") because auto-updating via the normal flow can't work
            # (PID 1 can't replace its own container). So checking the
            # updates list for our own name never matched — the hint hasn't
            # been surfacing since the self-filter existed. Ask the checker
            # directly (registry digest compare, no pull) and, if newer, tell
            # the user to run /selfupdate and hint at /changelog for preview.
            if checker.has_selfupdate_available():
                self.send_message(self.t("docksentry_update_hint"))

        elif text == "/changelog":
            from version import VERSION
            self.send_message(self.t("changelog_fetching"))
            ok, content = self._fetch_changelog()
            if not ok:
                self.send_message(self.t("changelog_fetch_failed", error=content))
                return
            new_entries = self._parse_changelog_entries(content, VERSION)
            if not new_entries:
                # Up to date — but show what the version you're ON brought,
                # so a post-/selfupdate "what did I just get?" is answerable
                # (#2, @famewolf). Fall back to the plain message if the
                # current version isn't in the changelog yet.
                cur = self._changelog_entry_for(content, VERSION)
                if cur:
                    v, d, body = cur
                    tg = self._github_md_to_telegram(body)
                    self.send_message(
                        self.t("changelog_current", version=v, date=d)
                        + "\n" + tg)
                else:
                    self.send_message(self.t("changelog_up_to_date", version=VERSION))
                return
            # Build the message entry-by-entry and stop at the cap so we
            # never truncate mid-`*bold*` (which would leave an unpaired
            # asterisk and force the Markdown-fallback retry path).
            header = self.t("changelog_title", count=len(new_entries), current=VERSION)
            parts = [header]
            total_len = len(header)
            truncated = False
            cap = 3800  # leaves headroom for truncation footer + BOT_LABEL
            for version, date, body in new_entries:
                tg_body = self._github_md_to_telegram(body)
                chunk = f"\n*v{version}* — {date}\n{tg_body}"
                if total_len + len(chunk) > cap:
                    truncated = True
                    break
                parts.append(chunk)
                total_len += len(chunk)
            msg = "\n".join(parts)
            if truncated:
                msg += "\n\n" + self.t(
                    "changelog_truncated",
                    url="https://github.com/amayer1983/docksentry/blob/main/CHANGELOG.md",
                )
            self.send_message(msg)

        elif text.startswith("/changelog "):
            # /changelog <container> — link-only (#14). We don't try to
            # fetch + parse arbitrary container changelogs because
            # projects use too many different formats (Keep-a-Changelog,
            # GitHub Releases, plain HISTORY.md, none at all) for the
            # result to be reliable. Instead, point the user at the
            # upstream source repo via OCI labels, or at the registry
            # overview page as a fallback. Honest > half-broken.
            parts = text.split(maxsplit=1)
            partial = parts[1].strip() if len(parts) > 1 else ""
            resolved, err = self._resolve_container(partial)
            if not resolved:
                self.send_message(err)
                return
            # Get the image ref for the registry fallback
            try:
                ir = subprocess.run(
                    ["docker", "inspect", "--format", "{{.Config.Image}}", resolved],
                    capture_output=True, text=True, timeout=5,
                )
                image_ref = ir.stdout.strip() if ir.returncode == 0 else ""
            except subprocess.SubprocessError:
                image_ref = ""
            source_url, kind = self._container_source_url(resolved)
            if kind == "source":
                self.send_message(self.t(
                    "changelog_container_source",
                    name=resolved, url=source_url,
                ))
            elif kind == "url":
                self.send_message(self.t(
                    "changelog_container_url_only",
                    name=resolved, url=source_url,
                ))
            else:
                fallback = self._guess_registry_overview_url(image_ref) if image_ref else ""
                if fallback:
                    self.send_message(self.t(
                        "changelog_container_registry_fallback",
                        name=resolved, url=fallback,
                    ))
                else:
                    self.send_message(self.t(
                        "changelog_container_none",
                        name=resolved,
                    ))

        elif text == "/updates":
            if os.path.exists(self.config.pending_file):
                with open(self.config.pending_file) as f:
                    pending = json.load(f)
                if pending:
                    names = [f"• `{u['name']}`" for u in pending]
                    self.send_message(self.t("pending_title") + "\n" + "\n".join(names))
                    return
            self.send_message(self.t("no_pending"))

        elif text == "/debug":
            self.config.debug = not self.config.debug
            self.config.save_persistent()
            status = self.t("debug_on") if self.config.debug else self.t("debug_off")
            self.send_message(self.t("debug_mode", status=status))

        elif text.startswith("/maintenance"):
            from maintenance import (
                enable as _maint_enable,
                disable as _maint_disable,
                parse_duration,
                get_state as _maint_state,
                format_remaining as _maint_remaining,
            )
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                # No arg → show current state
                st = _maint_state(self.config)
                if st.get("active"):
                    if st.get("until_iso") == "forever":
                        self.send_message(self.t("maintenance_active_forever"))
                    else:
                        self.send_message(self.t("maintenance_active_until",
                                                  remaining=_maint_remaining(st)))
                else:
                    self.send_message(self.t("maintenance_inactive"))
            else:
                arg = parts[1].strip()
                try:
                    parsed = parse_duration(arg)
                except (ValueError, AttributeError):
                    self.send_message(self.t("maintenance_usage"))
                    return
                if parsed is False:
                    _maint_disable(self.config)
                    self.send_message(self.t("maintenance_disabled"))
                elif parsed is None:
                    _maint_enable(self.config, hours=None)
                    self.send_message(self.t("maintenance_enabled_forever"))
                else:
                    until = _maint_enable(self.config, hours=parsed)
                    self.send_message(self.t("maintenance_enabled",
                                              until=until.strftime("%H:%M")))

        elif text == "/cleanup":
            self.send_message(self.t("cleanup_starting"))
            ok, msg = self.cleanup_guarded(checker)
            if ok is None:
                self.send_message(msg)
            elif ok and "Nothing" in msg:
                self.send_message(self.t("cleanup_none"))
            elif ok:
                self.send_message(f"✅ {msg}")
            else:
                self.send_message(f"❌ {msg}")

        elif text == "/checkimages":
            # Dry-run counterpart to /cleanup — how much would `/cleanup`
            # free right now (unused images / build cache), plus the
            # AUTO_CLEANUP status (#2, @famewolf: if you're not running
            # auto-cleanup, being able to check on demand is valuable).
            reclaim = checker.reclaimable_bytes()
            auto_on = bool(getattr(self.config, "disk_warn_auto_cleanup", False))
            self.send_message(self._build_checkimages_msg(reclaim, auto_on))

        elif text == "/events":
            # Telegram parity for the Web UI's Container Events section
            # (#2): same persisted log, same monitor_* message keys.
            self.send_message(self._build_events_msg())

        # Container lifecycle commands — start / stop / restart.
        # Same partial-name matching as /pin / /logs. Stop and restart
        # refuse on the Docksentry container itself (#16 / #17). Code
        # path is shared with the inline buttons in /status <name>.
        elif text.startswith("/stop ") or text.startswith("/start ") or text.startswith("/restart "):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                self.send_message(self.t("lifecycle_usage"))
                return
            action = parts[0][1:]  # strip leading "/"
            arg = parts[1].strip()
            # Glob → bulk action on every match (#40). Each goes through the
            # same _lifecycle_action, so the self-kill and protect-from-stop
            # guards still apply per container; results are aggregated.
            if self._is_glob(arg):
                names = self._match_glob(arg)
                if not names:
                    self.send_message(self.t("glob_no_match", pattern=arg))
                    return
                lines = [self.t("glob_action_header", action=action,
                                count=len(names), pattern=arg)]
                for nm in names:
                    ok, msg = self._lifecycle_action(action, nm, checker)
                    lines.append(("✅ " if ok else "❌ ") + msg)
                self.send_message("\n".join(lines))
                return
            # include_stopped is now the default — see _resolve_container
            # docstring for rationale (#25).
            resolved, err = self._resolve_container(arg)
            if not resolved:
                self.send_message(err)
                return
            ok, msg = self._lifecycle_action(action, resolved, checker)
            self.send_message(msg)

        elif text == "/selfupdate":
            self._handle_selfupdate()

        elif text.startswith("/selfupdate "):
            # /selfupdate <version> — pin to a specific release.
            # /selfupdate previous — last released version older than current.
            # See #12. The handler validates the target and refuses
            # malformed input before triggering the helper container.
            parts = text.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else None
            self._handle_selfupdate(target=target)

        elif text.startswith("/lang"):
            from i18n import available_languages, get_translator
            langs = available_languages()
            parts = text.split()
            if len(parts) == 2 and parts[1].lower() in langs:
                new_lang = parts[1].lower()
                self.config.language = new_lang
                self.config.save_persistent()
                self.t = get_translator(new_lang)
                self.send_message(self.t("lang_changed"))
            else:
                self.send_message(self.t("lang_usage") + f"\n\n📂 {', '.join(langs)}")

        elif text == "/history":
            if os.path.exists(self.config.history_file):
                with open(self.config.history_file) as f:
                    history = json.load(f)
                if history:
                    # Show last 10 entries, newest first
                    lines = []
                    for h in reversed(history[-10:]):
                        icon = "✅" if h["success"] else "❌"
                        # Normalize legacy v1.16.1 calendar glyph in stored
                        # detail strings — see CHANGELOG v1.16.2.
                        detail = h.get("detail", "").replace("📅", "🗓️")
                        lines.append(f"{icon} `{h['container']}` — {h['timestamp']}\n    {detail}")
                    self.send_message(self.t("history_title") + "\n\n" + "\n".join(lines))
                    return
            self.send_message(self.t("history_empty"))

        elif text.startswith("/pin"):
            parts = text.split()
            if len(parts) < 2:
                pinned = self._get_pinned()
                if pinned:
                    names = [f"• `{n}`" for n in pinned]
                    self.send_message(self.t("pin_list") + "\n" + "\n".join(names))
                else:
                    self.send_message(self.t("pin_empty"))
                return
            name, err = self._resolve_container(parts[1])
            if err:
                self.send_message(err)
                return
            pinned = self._get_pinned()
            if name not in pinned:
                pinned.append(name)
                self._save_pinned(pinned)
                self.send_message(self.t("pin_added", name=name))
            else:
                self.send_message(self.t("pin_already", name=name))

        elif text.startswith("/unpin"):
            parts = text.split()
            if len(parts) < 2:
                self.send_message(self.t("unpin_usage"))
                return
            # For unpin, match against pinned list too
            partial = parts[1]
            pinned = self._get_pinned()
            matches = [n for n in pinned if n.lower().startswith(partial.lower())]
            if partial in pinned:
                name = partial
            elif len(matches) == 1:
                name = matches[0]
            elif len(matches) > 1:
                self.send_message(self.t("resolve_multiple", names=", ".join(f"`{m}`" for m in matches)))
                return
            else:
                self.send_message(self.t("unpin_not_found", name=partial))
                return
            pinned.remove(name)
            self._save_pinned(pinned)
            self.send_message(self.t("unpin_removed", name=name))

        elif text.startswith("/autoupdate"):
            parts = text.split()
            if len(parts) < 2:
                auto_list = self._get_autoupdate()
                if auto_list:
                    names = [f"• `{n}`" for n in auto_list]
                    self.send_message(self.t("autoupdate_list") + "\n" + "\n".join(names))
                else:
                    self.send_message(self.t("autoupdate_empty"))
                return
            name, err = self._resolve_container(parts[1])
            if err:
                self.send_message(err)
                return
            auto_list = self._get_autoupdate()
            if name in auto_list:
                auto_list.remove(name)
                self._save_autoupdate(auto_list)
                self.send_message(self.t("autoupdate_off", name=name))
            else:
                auto_list.append(name)
                self._save_autoupdate(auto_list)
                self.send_message(self.t("autoupdate_on", name=name))

        elif text.startswith("/cooldown"):
            parts = text.split()
            if len(parts) < 2:
                cds = self.store.get_cooldowns()
                if cds:
                    lines = [f"• `{n}`: {s}s" for n, s in cds.items()]
                    self.send_message(self.t("cooldown_list") + "\n" + "\n".join(lines))
                else:
                    self.send_message(self.t("cooldown_empty"))
                return
            name, err = self._resolve_container(parts[1])
            if err:
                self.send_message(err)
                return
            if len(parts) < 3:
                self.send_message(self.t("cooldown_current", name=name,
                                         seconds=self.store.get_cooldown(name)))
                return
            try:
                secs = int(parts[2])
            except ValueError:
                self.send_message(self.t("cooldown_bad_value"))
                return
            applied = self.store.set_cooldown(name, secs)
            if applied:
                self.send_message(self.t("cooldown_set", name=name, seconds=applied))
            else:
                self.send_message(self.t("cooldown_cleared", name=name))

        elif text.startswith("/protect"):
            parts = text.split()
            if len(parts) < 2:
                names = self.store.get_protect_stop()
                if names:
                    self.send_message(self.t("protect_list") + "\n"
                                      + "\n".join(f"• `{n}`" for n in names))
                else:
                    self.send_message(self.t("protect_empty"))
                return
            name, err = self._resolve_container(parts[1])
            if err:
                self.send_message(err)
                return
            now_on = self.store.toggle_protect_stop(name)
            self.send_message(self.t("protect_on" if now_on else "protect_off", name=name))

        elif text.startswith("/audit"):
            # /audit <container> — run UpdateChecker._audit_inspect_coverage
            # against the target's docker inspect and report any non-
            # default HostConfig / Config fields we don't restore on
            # recreate. Mirror of the DEBUG-mode logging in v1.19.0 but
            # actionable from chat (#2 + audit story closure).
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                self.send_message(self.t("audit_usage"))
                return
            name, err = self._resolve_container(parts[1])
            if err:
                self.send_message(err)
                return
            try:
                r = subprocess.run(
                    ["docker", "inspect", name],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode != 0:
                    self.send_message(self.t("audit_inspect_failed", name=name))
                    return
                inspect = json.loads(r.stdout)[0]
            except (subprocess.SubprocessError, json.JSONDecodeError, IndexError):
                self.send_message(self.t("audit_inspect_failed", name=name))
                return
            from update_checker import UpdateChecker as _UC
            findings = _UC._audit_inspect_coverage(checker, inspect)
            host_keys = findings.get("host_unknown") or []
            cfg_keys = findings.get("config_unknown") or []
            if not host_keys and not cfg_keys:
                self.send_message(self.t("audit_clean", name=name))
                return
            lines = [self.t("audit_findings_header", name=name)]
            if host_keys:
                lines.append(self.t("audit_section_host"))
                lines.extend(f"  • `HostConfig.{k}`" for k in host_keys)
            if cfg_keys:
                lines.append(self.t("audit_section_config"))
                lines.extend(f"  • `Config.{k}`" for k in cfg_keys)
            lines.append(self.t("audit_footer"))
            self.send_message("\n".join(lines))

        elif text.startswith("/setlink"):
            # Telegram-side affordance for the per-container link store —
            # mirror of the Web UI Status page link icon. Without this
            # users had to leave Telegram to set a custom changelog /
            # repo URL. Requested by @famewolf + @NotRetarded in #2 for
            # CGNAT-hosted setups where the Web UI isn't easily reachable.
            #
            # Usage:
            #   /setlink <container> <url>    → save (validates http(s)://)
            #   /setlink <container>          → clear (back to OCI labels)
            #
            # The URL replaces the auto-resolved OCI source URL in both
            # the update-notification repo link AND /changelog output —
            # they share `container_store.get_link()`.
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                self.send_message(self.t("setlink_usage"))
                return
            partial = parts[1].strip()
            url = parts[2].strip() if len(parts) > 2 else ""
            name, err = self._resolve_container(partial)
            if err:
                self.send_message(err)
                return
            ok = self.store.set_link(name, url)
            if not ok:
                self.send_message(self.t("setlink_invalid"))
                return
            if url:
                self.send_message(self.t("setlink_set", name=name, url=url))
            else:
                self.send_message(self.t("setlink_cleared", name=name))

        elif text.startswith("/groups ") and len(text.split(maxsplit=1)) > 1:
            arg = text.split(maxsplit=1)[1].strip()
            body, markup = self._build_group_detail(arg)
            self.send_message(body, markup)

        elif text == "/groups":
            self.send_message(self._build_groups_list())

        elif text == "/settings":
            debug_status = self.t("debug_on") if self.config.debug else self.t("debug_off")
            auto_su = "ON ✅" if self.config.auto_selfupdate else "OFF"
            self.send_message(
                self.t("settings_title") + "\n\n"
                + f"🗓️ Schedule: `{self.config.cron_schedule}`\n"
                + f"🌍 {self.t('settings_language')}: `{self.config.language}`\n"
                + f"🔄 Auto-Selfupdate: {auto_su}\n"
                + f"🔍 Debug: {debug_status}\n"
                + f"🚫 Exclude: `{', '.join(self.config.exclude_containers) or '-'}`\n"
                + f"📌 Pinned: `{', '.join(self._get_pinned()) or '-'}`\n"
                + f"⚡ Auto-Update: `{', '.join(self._get_autoupdate()) or '-'}`"
            )

        elif text.startswith("/logs"):
            parts = text.split()
            if len(parts) < 2:
                self.send_message(self.t("logs_usage"))
                return
            name, err = self._resolve_container(parts[1])
            if err:
                self.send_message(err)
                return
            result = subprocess.run(
                ["docker", "logs", "--tail", "30", name],
                capture_output=True, text=True, timeout=10
            )
            output = result.stdout or result.stderr
            if output.strip():
                # Telegram message limit is 4096, truncate if needed
                if len(output) > 3500:
                    output = output[-3500:]
                self.send_message(self.t("logs_title", name=name) + f"\n```\n{output.strip()}\n```")
            else:
                self.send_message(self.t("logs_empty", name=name))

        elif text.startswith("/help ") or text.startswith("/start "):
            # /help <command> — per-command detailed help (#15, @famewolf).
            # Looks up the command in _BOT_COMMANDS and sends the
            # `detail_key`-translated block. Strips a leading `/` from
            # the arg so both `/help pin` and `/help /pin` work.
            parts = text.split(maxsplit=1)
            requested = parts[1].strip().lstrip("/").lower() if len(parts) > 1 else ""
            if not requested:
                # Falls through to the generic /help below — shouldn't
                # happen due to the startswith check but defensive.
                requested = None
            match = None
            for cmd in _BOT_COMMANDS:
                if cmd[0] == requested:
                    match = cmd
                    break
            if match is None:
                # Unknown command — list valid ones briefly so the user
                # can recover without scrolling /help.
                valid = ", ".join(f"`/{c[0]}`" for c in _BOT_COMMANDS)
                self.send_message(self.t("help_detail_unknown",
                                         cmd=requested, valid=valid))
                return
            self.send_message(self.t(match[3]))

        elif text == "/help" or text == "/start":
            from version import VERSION
            # /help iterates the same _BOT_COMMANDS table that the
            # Telegram picker derives from — dedup'd by help_key so
            # commands that share a help line (start/stop/restart all
            # land under help_lifecycle) only show once.
            seen = set()
            command_lines = []
            for (_name, _picker_desc, help_key, _detail_key) in _BOT_COMMANDS:
                if help_key is None or help_key in seen:
                    continue
                seen.add(help_key)
                command_lines.append(self.t(help_key))
            self.send_message(
                self.t("help_title", version=VERSION) + "\n\n"
                + self.t("help_autocomplete_hint") + "\n\n"
                + self.t("help_commands") + "\n"
                + "\n".join(command_lines) + "\n\n"
                + self.t("help_per_command_hint") + "\n\n"
                + self.t("help_docs_footer")
            )
