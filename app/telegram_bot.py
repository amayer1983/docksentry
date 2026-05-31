#!/usr/bin/env python3
"""Telegram Bot - handles messages, callbacks, and notifications."""

import json
import socket
import subprocess
import os
import sys
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
    ("status",      "Container overview (add a name for details + action buttons)", "help_status"),
    ("check",       "Check for updates now",                                          "help_check"),
    ("updates",     "Show pending updates",                                           "help_updates"),
    ("cleanup",     "Remove unused images",                                           "help_cleanup"),
    ("start",       "Start a stopped container — /start <name>",                      "help_lifecycle"),
    ("stop",        "Stop a running container — /stop <name>",                        "help_lifecycle"),
    ("restart",     "Restart a container — /restart <name>",                          "help_lifecycle"),
    ("maintenance", "Pause auto-updates — /maintenance 2h or /maintenance off",       "help_maintenance"),
    ("history",     "Recent update history",                                          "help_history"),
    ("pin",         "Skip updates for a container — /pin <name>",                     "help_pin"),
    ("unpin",       "Re-enable updates — /unpin <name>",                              "help_unpin"),
    ("autoupdate",  "Toggle auto-update — /autoupdate <name>",                        "help_autoupdate"),
    ("selfupdate",  "Update the bot itself (add a version to pin)",                   "help_selfupdate"),
    ("changelog",   "What's new in versions ahead of yours",                          "help_changelog"),
    ("debug",       "Toggle debug mode",                                              "help_debug"),
    ("logs",        "Last 30 log lines — /logs <name>",                               "help_logs"),
    ("lang",        "Switch bot language — /lang en or /lang de",                     "help_lang"),
    ("settings",    "Show current settings",                                          "help_settings"),
    ("help",        "Show all commands",                                              "help_help"),
]


class TelegramBot:
    def __init__(self, config, container_store):
        self.config = config
        self.store = container_store
        self.running = True
        self.update_running = False
        self.notifier = None  # Set by main.py after init
        from i18n import get_translator
        self.t = get_translator(config.language)

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
        new outcome."""
        import json as _json
        from datetime import datetime as _dt
        entry = {
            "timestamp": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
            "container": container_name,
            "image": image,
            "success": True,
            "detail": f"🗓️ {old_created} → {new_created} (selfupdate)",
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
            with open(self.config.history_file, "w") as f:
                _json.dump(history, f, indent=2)
        except IOError as e:
            print(f"Failed to record selfupdate history: {e}")

    def _own_container_meta(self):
        """Return (own_name, own_image) for the running Docksentry
        container, or (None, None) when we can't figure it out (HOSTNAME
        env unset, docker inspect fails, …). Cached after first call —
        the answer doesn't change at runtime."""
        if hasattr(self, "_cached_own_meta"):
            return self._cached_own_meta
        hostname = os.environ.get("HOSTNAME", "")
        if not hostname:
            self._cached_own_meta = (None, None)
            return self._cached_own_meta
        try:
            r = subprocess.run(
                ["docker", "inspect", hostname],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                self._cached_own_meta = (None, None)
                return self._cached_own_meta
            cfg = json.loads(r.stdout)[0]
            name = (cfg.get("Name", "") or "").lstrip("/")
            image = cfg.get("Config", {}).get("Image", "") or ""
            self._cached_own_meta = (name, image)
            return self._cached_own_meta
        except (subprocess.SubprocessError, json.JSONDecodeError, IndexError, KeyError):
            self._cached_own_meta = (None, None)
            return self._cached_own_meta

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

        return {
            "name": cfg.get("Name", "").lstrip("/"),
            "state": status,
            "health": health,
            "uptime": uptime,
            "image": config.get("Image", "?"),
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

    def _resolve_container(self, partial):
        """Resolve a partial container name. Returns (full_name, error_msg)."""
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True
        )
        all_names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]

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
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # Telegram returns 4xx with a JSON body for parse errors,
            # rate-limit hints etc. Pass the parsed body to the caller so
            # the markdown-retry path in send_message can act on it
            # instead of treating it as a network failure.
            try:
                body = json.loads(e.read())
                if not (quiet_timeout and self._is_timeout(e)):
                    print(f"Telegram API {e.code}: {body.get('description', body)}")
                return body
            except Exception:
                print(f"Telegram API error: {e}")
                return None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
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
        if not os.path.exists(self.config.pending_file):
            self.send_message(self.t("no_pending_updates"))
            return

        with open(self.config.pending_file) as f:
            updates = json.load(f)

        target = next((u for u in updates if u["name"] == container_name), None)
        if not target:
            self.send_message(self.t("container_not_in_list", name=container_name))
            return

        self.send_message(self.t("update_single_starting", name=container_name))

        try:
            compose_kwargs = {k: target[k] for k in target if k.startswith("compose_")}
            success, msg = checker.update_container(target["name"], target["image"], **compose_kwargs)
            status = "✅" if success else "❌"
            self.send_message(f"{status} `{container_name}`: {msg}")
            if self.notifier:
                self.notifier.send_update_result(container_name, target["image"], success, msg)
        except Exception as e:
            self.send_message(f"❌ `{container_name}`: {str(e)[:200]}")
            if self.notifier:
                self.notifier.send_update_result(container_name, target.get("image", "?"), False, str(e)[:200])

        # Remove from pending list
        remaining = [u for u in updates if u["name"] != container_name]
        with open(self.config.pending_file, "w") as f:
            json.dump(remaining, f)

        if not remaining:
            self.send_message(self.t("update_all_done"))

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
        image = pending.get("image", "")
        compose = pending.get("compose", {}) or {}
        try:
            success, msg = checker.update_container(name, image, **compose)
        except Exception as e:
            success, msg = False, str(e)[:200]
        status = "✅" if success else "❌"
        self.send_message(f"{status} `{name}`: {msg}")
        if self.notifier:
            self.notifier.send_update_result(name, image, success, msg)
        if success:
            self.store.remove_pending_major(name)

    def _wait_healthy(self, name, max_seconds):
        """Poll `docker inspect` until the container is running (and, if it
        has a healthcheck, reports healthy). Returns True on success,
        False on timeout. Polls every second. Used by the restart-
        dependents cascade so we don't kick VPN-dependent containers
        before the VPN sidecar is actually up."""
        import time as _time
        for _ in range(max(1, int(max_seconds))):
            try:
                r = subprocess.run(
                    ["docker", "inspect", name],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    data = json.loads(r.stdout)[0]
                    state = data.get("State", {}) or {}
                    if state.get("Running"):
                        health = state.get("Health") or {}
                        if not health:
                            # No healthcheck configured — Running=true is
                            # the best signal we have.
                            return True
                        if health.get("Status") == "healthy":
                            return True
            except (subprocess.SubprocessError, json.JSONDecodeError, IndexError, KeyError):
                pass
            _time.sleep(1)
        return False

    def _restart_group_dependents(self, head_name, dependents, max_wait=30):
        """After updating a group's head container, restart its
        dependents. Waits up to `max_wait` seconds for the head to be
        healthy first — if it never gets there, restart the dependents
        anyway (with a log warning), because not restarting them leaves
        them stuck on a defunct network namespace which is usually worse
        than a slightly-too-early restart.

        Returns a one-line user-facing result string for the update
        report."""
        healthy = self._wait_healthy(head_name, max_wait)
        if not healthy:
            print(f"⚠ {head_name} not healthy after {max_wait}s — restarting dependents anyway")

        restarted = []
        failed = []
        for dep in dependents:
            try:
                r = subprocess.run(
                    ["docker", "restart", dep],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode == 0:
                    restarted.append(dep)
                else:
                    failed.append(dep)
                    print(f"Failed to restart dependent {dep}: {r.stderr.strip()[:200]}")
            except subprocess.SubprocessError as e:
                failed.append(dep)
                print(f"Restart of dependent {dep} crashed: {e}")

        if failed:
            return (f"🔁 `{head_name}` dependents: restarted {len(restarted)}, "
                    f"failed {len(failed)} ({', '.join(failed)})")
        return (f"🔁 `{head_name}` dependents restarted: {', '.join(f'`{d}`' for d in restarted)}")

    def handle_autoupdates(self, updates, checker):
        """Split updates into auto-update and manual, handle accordingly.

        Returns the number of containers that were successfully auto-updated
        (used by the scheduler to decide whether to follow up with cleanup).
        """
        import time as _time
        from update_window import is_window_open

        auto_list = self._get_autoupdate()
        ask_major_list = self.store.get_ask_before_major()
        windows = self.store.get_update_windows()
        groups = self.store.get_groups()

        auto_candidates = [u for u in updates if u["name"] in auto_list]
        # Filter out containers whose maintenance window is closed right now
        skipped_window = [u for u in auto_candidates
                          if not is_window_open(windows.get(u["name"]))]
        auto_updates = [u for u in auto_candidates if u not in skipped_window]
        manual_updates = [u for u in updates if u["name"] not in auto_list]
        success_count = 0
        major_pending_now = []
        group_aborted = set()  # group_ids whose remaining members must be skipped

        # ── Sort auto_updates by group order ────────────────────
        # Containers in a group are ordered as listed in the group; orphans
        # (containers in no group) keep their original order at the end.
        group_position = {}  # container_name → (group_id, position)
        for gid, g in groups.items():
            for pos, cname in enumerate(g.get("containers") or []):
                group_position[cname] = (gid, pos)

        def _sort_key(u):
            gp = group_position.get(u["name"])
            if gp is None:
                return (1, "", 0)  # orphans last
            return (0, gp[0], gp[1])
        auto_updates.sort(key=_sort_key)

        # Auto-update containers silently, respecting group order + wait
        if auto_updates:
            self.send_message(self.t("autoupdate_running", count=len(auto_updates)), auto=True)
            results = []
            prev_group = None
            for u in auto_updates:
                gp = group_position.get(u["name"])
                cur_group = gp[0] if gp else None

                # If a previous container in this group failed, skip remaining
                if cur_group and cur_group in group_aborted:
                    results.append(
                        f"⏭ `{u['name']}`: skipped (group `{cur_group}` aborted earlier)"
                    )
                    continue

                # Inter-container wait when staying inside the same group
                if cur_group and cur_group == prev_group:
                    wait_s = int((groups.get(cur_group) or {}).get("wait_seconds", 0) or 0)
                    if wait_s > 0:
                        _time.sleep(wait_s)

                # Major-version confirmation gate (per-container opt-in)
                if u["name"] in ask_major_list:
                    is_major, old_ver, new_ver = self._is_major_bump(u, checker)
                    if is_major:
                        self.store.add_pending_major(u["name"], {
                            "image": u["image"],
                            "old_version": old_ver,
                            "new_version": new_ver,
                            "compose": {k: u[k] for k in u if k.startswith("compose_")},
                        })
                        major_pending_now.append((u["name"], old_ver, new_ver))
                        results.append(
                            f"⏸ `{u['name']}`: major bump {old_ver} → {new_ver} — confirmation required"
                        )
                        prev_group = cur_group
                        continue
                try:
                    compose_kwargs = {k: u[k] for k in u if k.startswith("compose_")}
                    success, msg = checker.update_container(u["name"], u["image"], **compose_kwargs)
                    status = "✅" if success else "❌"
                    results.append(f"{status} `{u['name']}`: {msg}")
                    if success:
                        success_count += 1
                        # Restart-dependents cascade: if this container is
                        # the first ("head") member of a group flagged
                        # restart_dependents, wait for it to be healthy,
                        # then restart every other group member. Covers
                        # Gluetun-style "VPN sidecar restarts → all
                        # dependents lose connectivity" workflows.
                        if cur_group:
                            grp = groups.get(cur_group) or {}
                            members = grp.get("containers") or []
                            if (grp.get("restart_dependents")
                                    and members
                                    and u["name"] == members[0]
                                    and len(members) > 1):
                                deps = members[1:]
                                wait_s = int(grp.get("wait_seconds", 30) or 30)
                                restart_msg = self._restart_group_dependents(
                                    u["name"], deps, max_wait=max(wait_s, 30)
                                )
                                results.append(restart_msg)
                    elif cur_group:
                        # Failure aborts the remainder of this group
                        group_aborted.add(cur_group)
                    if self.notifier:
                        self.notifier.send_update_result(u["name"], u["image"], success, msg)
                except Exception as e:
                    results.append(f"❌ `{u['name']}`: {str(e)[:200]}")
                    if cur_group:
                        group_aborted.add(cur_group)
                    if self.notifier:
                        self.notifier.send_update_result(u["name"], u["image"], False, str(e)[:200])
                prev_group = cur_group
            self.send_message(self.t("autoupdate_done") + "\n\n" + "\n".join(results), auto=True)

            # Remove fully-processed auto-updated from pending. Major-pending
            # entries stay in pending so the user can also act on them via the
            # Web UI Update buttons; the dedicated confirm flow uses the
            # major-pending store independently.
            processed = {a["name"] for a in auto_updates if a["name"] not in {p[0] for p in major_pending_now}}
            remaining = [u for u in updates if u["name"] not in processed]
            with open(self.config.pending_file, "w") as f:
                json.dump(remaining, f)

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

    def notify_updates(self, updates, auto=False):
        if not updates:
            return
        names = []
        for u in updates:
            size = u.get('size', '?')
            created = u.get('created', '?')
            compose_tag = " 🐳" if u.get("compose_project") else ""
            names.append(f"• `{u['name']}` ({u['image']}){compose_tag}\n  📦 {size} | 🗓️ {self.t('current')}: {created}")
        text = self.t("updates_available") + "\n\n" + "\n".join(names)

        # One button per container + all/skip at the bottom
        keyboard = []
        for u in updates:
            size = u.get('size', '?')
            keyboard.append([
                {"text": f"🔄 {u['name']} ({size})", "callback_data": f"update_one:{u['name']}"}
            ])
        keyboard.append([
            {"text": self.t("update_all_btn"), "callback_data": "update_all"},
            {"text": self.t("manual_btn"), "callback_data": "update_skip"}
        ])

        reply_markup = {"inline_keyboard": keyboard}
        self.send_message(text, reply_markup, auto=auto)

        # Also notify external channels (notifier itself respects quiet hours)
        if self.notifier:
            self.notifier.send_updates_available(updates)

    def notify_no_updates(self):
        self.send_message(self.t("all_up_to_date"))

    def _resolve_selfupdate_target(self, current_image, target):
        """Resolve `target` ('previous' / 'X.Y.Z' / None) into a fully-
        qualified image ref to pull. Returns (image_ref, error_msg).

        On success, error_msg is None.
        """
        if not target:
            return current_image, None

        # Extract base ("registry/owner/repo") from current_image
        if ":" in current_image:
            base = current_image.rsplit(":", 1)[0]
        else:
            base = current_image

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

    def _handle_selfupdate(self, target=None):
        """Pull a target image and recreate own container.

        Args:
            target: optional version override:
                None       → whatever tag the container currently runs (usually :latest)
                "previous" → last released version older than the running one
                "X.Y.Z"    → a specific semver tag
        """
        hostname = os.environ.get("HOSTNAME", "")
        if not hostname:
            self.send_message(self.t("selfupdate_failed_id"))
            return

        # Get own container info
        result = subprocess.run(
            ["docker", "inspect", hostname],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            self.send_message(self.t("selfupdate_failed_container"))
            return

        config = json.loads(result.stdout)[0]
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
            self.send_message(self.t("selfupdate_up_to_date"))
            return

        new_id_short = new_id[:19]
        self.send_message(
            self.t("selfupdate_found") + "\n"
            + self.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
            + self.t("selfupdate_ids", old=old_id_short, new=new_id_short) + "\n\n"
            + self.t("selfupdate_restarting")
        )

        # Record in history BEFORE _do_selfupdate kills us — otherwise the
        # entry never gets written (#13).
        self._save_selfupdate_history(own_name, own_image, old_created, new_created)
        self._do_selfupdate(config, own_name, own_image)

    def _do_selfupdate(self, config, own_name, own_image):
        """Execute selfupdate via a temporary helper container on the host.

        The old approach (Popen + sys.exit) failed because Docker kills all
        processes inside a container when PID 1 exits. Instead, we launch a
        short-lived helper container that runs on the host and performs the
        stop/rename/run/cleanup sequence from outside.
        """
        # Rebuild run command from inspect
        run_args = ["--name", own_name]

        # Restart policy
        restart = config.get("HostConfig", {}).get("RestartPolicy", {})
        if restart.get("Name"):
            policy = restart["Name"]
            if restart.get("MaximumRetryCount", 0) > 0:
                policy += f":{restart['MaximumRetryCount']}"
            run_args.extend(["--restart", policy])

        # Network
        network_mode = config.get("HostConfig", {}).get("NetworkMode", "")
        if network_mode and network_mode != "default":
            run_args.extend(["--network", network_mode])

        # If the container inherits another container's network namespace
        # (Gluetun-style network_mode: "container:gluetun"), Docker rejects
        # per-container network options like -p / --hostname / --add-host.
        # See update_checker.py for the longer explanation. Unlikely to hit
        # here (Docksentry itself rarely sits behind a VPN sidecar), but
        # mirror the logic for consistency.
        shares_netns = network_mode.startswith(("container:", "service:"))

        # Env vars
        for env in config.get("Config", {}).get("Env", []):
            run_args.extend(["-e", env])

        # Mounts
        for mount in config.get("Mounts", []):
            if mount["Type"] == "bind":
                bind = f"{mount['Source']}:{mount['Destination']}"
                if not mount.get("RW", True):
                    bind += ":ro"
                run_args.extend(["-v", bind])
            elif mount["Type"] == "volume":
                bind = f"{mount['Name']}:{mount['Destination']}"
                if not mount.get("RW", True):
                    bind += ":ro"
                run_args.extend(["-v", bind])

        # Ports (skipped on shared netns — see comment above)
        if not shares_netns:
            ports = config.get("HostConfig", {}).get("PortBindings", {}) or {}
            for container_port, bindings in ports.items():
                if bindings:
                    for b in bindings:
                        host_ip = b.get("HostIp", "")
                        host_port = b.get("HostPort", "")
                        if host_ip:
                            run_args.extend(["-p", f"{host_ip}:{host_port}:{container_port}"])
                        else:
                            run_args.extend(["-p", f"{host_port}:{container_port}"])

        # Labels
        for key, value in config.get("Config", {}).get("Labels", {}).items():
            run_args.extend(["--label", f"{key}={value}"])

        # Security opts
        for opt in config.get("HostConfig", {}).get("SecurityOpt", []) or []:
            run_args.extend(["--security-opt", opt])

        # Build the full recreation command
        run_parts = " ".join(f'"{a}"' if " " in a or "=" in a else a for a in run_args)
        update_script = (
            f"sleep 3 && "
            f"docker stop {own_name} && "
            f"docker rename {own_name} {own_name}_old && "
            f"docker run -d {run_parts} {own_image} && "
            f"docker rm {own_name}_old"
        )

        # Launch a temporary helper container on the host that performs the swap.
        # This container survives because it runs independently on the Docker host.
        helper_name = f"{own_name}_updater"
        # Clean up any leftover helper from a previous attempt
        subprocess.run(["docker", "rm", "-f", helper_name],
                       capture_output=True, timeout=10)

        result = subprocess.run([
            "docker", "run", "-d",
            "--name", helper_name,
            "--rm",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "docker:cli",
            "sh", "-c", update_script
        ], capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            self.send_message(f"❌ Selfupdate failed: {result.stderr[:200]}")
            return

        # The helper container will stop us in ~3 seconds.
        # Just wait here — no sys.exit needed.
        print(f"Selfupdate helper started ({helper_name}). Waiting for shutdown...")
        import time
        time.sleep(30)

    def check_selfupdate_auto(self, defer_check=False):
        """Automatic selfupdate check — triggered by the scheduler when
        AUTO_SELFUPDATE=true.

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
        hostname = os.environ.get("HOSTNAME", "")
        if not hostname:
            return False

        result = subprocess.run(
            ["docker", "inspect", hostname],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return False

        config = json.loads(result.stdout)[0]
        own_image = config["Config"]["Image"]
        old_id = config["Image"]
        old_created = config.get("Created", "")[:10]

        # Pull latest silently
        pull = subprocess.run(
            ["docker", "pull", own_image],
            capture_output=True, text=True, timeout=300
        )
        if pull.returncode != 0:
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
            print("Auto selfupdate: already up to date.")
            return False

        # Notify and update
        own_name = config["Name"].lstrip("/")

        # Write deferred-check marker BEFORE triggering the restart so the
        # freshly-booted process can pick up where we left off. Best-effort
        # — if writing fails the worst case is the user has to wait until
        # the next cron tick for the container-update check.
        if defer_check:
            try:
                from datetime import datetime
                with open(self.config.deferred_check_file, "w") as f:
                    json.dump({
                        "trigger_time": datetime.now().isoformat(timespec="seconds"),
                        "reason": "post-selfupdate",
                    }, f)
            except OSError as e:
                print(f"Failed to write deferred-check marker: {e}")

        # Send a single combined notification when defer_check is on, so
        # the user sees one story instead of two unrelated messages.
        if defer_check:
            self.send_message(
                self.t("selfupdate_auto") + "\n"
                + self.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
                + self.t("selfupdate_restarting_then_check")
            )
        else:
            self.send_message(
                self.t("selfupdate_auto") + "\n"
                + self.t("selfupdate_dates", new=new_created, old=old_created) + "\n"
                + self.t("selfupdate_restarting")
            )

        # Record in history BEFORE _do_selfupdate kills us — otherwise the
        # entry never gets written (#13). Same data path as the manual
        # /selfupdate handler.
        self._save_selfupdate_history(own_name, own_image, old_created, new_created)

        # Reuse the selfupdate logic — this blocks for ~30s while the
        # helper container stops us. Caller should treat this as a
        # one-way call.
        self._do_selfupdate(config, own_name, own_image)
        return True

    def run_updates(self, updater):
        if self.update_running:
            self.send_message(self.t("update_already_running"))
            return

        pending_file = self.config.pending_file
        if not os.path.exists(pending_file):
            self.send_message(self.t("no_pending_updates"))
            return

        with open(pending_file) as f:
            updates = json.load(f)

        if not updates:
            self.send_message(self.t("no_pending_updates"))
            return

        self.update_running = True
        self.send_message(self.t("update_starting", count=len(updates)))

        results = []
        for u in updates:
            try:
                compose_kwargs = {k: u[k] for k in u if k.startswith("compose_")}
                success, msg = updater.update_container(u["name"], u["image"], **compose_kwargs)
                status = "✅" if success else "❌"
                results.append(f"{status} `{u['name']}`: {msg}")
                if self.notifier:
                    self.notifier.send_update_result(u["name"], u["image"], success, msg)
            except Exception as e:
                results.append(f"❌ `{u['name']}`: {str(e)[:200]}")
                if self.notifier:
                    self.notifier.send_update_result(u["name"], u.get("image", "?"), False, str(e)[:200])

        try:
            os.remove(pending_file)
        except OSError:
            pass

        self.send_message(self.t("update_result") + "\n\n" + "\n".join(results))
        self.update_running = False

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
            for (name, picker_desc, _help_key) in _BOT_COMMANDS
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

        # Register our command list with Telegram so users get the
        # native `/` autocomplete picker (with one-line descriptions per
        # command). Idempotent — Telegram just stores the latest set —
        # so calling on every boot is fine and means newly-added commands
        # surface without any setup step on the user's side.
        self._register_commands_with_telegram()

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
                        "allowed_updates": json.dumps(["callback_query", "message"]),
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

                    # Text commands
                    message = update.get("message", {})
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

        if data == "update_all":
            if msg_id and chat_id:
                self.remove_buttons(chat_id, msg_id)
            self.answer_callback(callback["id"], self.t("updates_starting_cb"))
            t = threading.Thread(target=self.run_updates, args=(checker,))
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

    def _handle_message(self, message, checker, scheduler):
        text = message.get("text", "")
        user_id = str(message.get("from", {}).get("id", ""))
        chat_id = message.get("chat", {}).get("id")

        if not self._check_auth(chat_id, user_id, kind="message"):
            return

        # `/status <name>` — per-container detail with inline action
        # buttons. The arg-less `/status` keeps the overview behaviour.
        if text.startswith("/status ") and len(text.split(maxsplit=1)) > 1:
            partial = text.split(maxsplit=1)[1].strip()
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
            lines.extend([
                f"*Image:* `{info['image']}`",
                f"*Ports:* {info['ports']}",
                f"*Volumes:* {info['volumes']}",
                f"*Restart policy:* `{info['restart_policy']}`",
            ])

            # Build inline keyboard based on current state.
            buttons = []
            if info["running"]:
                buttons.append({"text": self.t("lifecycle_btn_restart"),
                                "callback_data": f"lifecycle:restart:{resolved}"})
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
            ps = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"],
                capture_output=True, text=True
            )
            lines = [l for l in ps.stdout.strip().split("\n") if l]
            total = len(lines)
            healthy = 0
            unhealthy = 0
            running = 0
            containers = []

            for line in lines:
                parts = line.split("|", 2)
                name = parts[0] if len(parts) > 0 else "?"
                status_raw = parts[1] if len(parts) > 1 else "?"
                image = parts[2] if len(parts) > 2 else "?"

                # Parse uptime
                uptime = status_raw.replace("Up ", "").strip()

                # Determine health icon
                if "(healthy)" in status_raw:
                    icon = "🟢"
                    healthy += 1
                elif "(unhealthy)" in status_raw:
                    icon = "🔴"
                    unhealthy += 1
                elif "(health: starting)" in status_raw:
                    icon = "🟡"
                    running += 1
                else:
                    icon = "⚪"
                    running += 1

                # Clean up uptime display
                uptime = uptime.replace(" (healthy)", "").replace(" (unhealthy)", "").replace(" (health: starting)", "")

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

        elif text == "/check":
            self.send_message(self.t("checking_updates"))
            updates = checker.check_all(bot=self)
            if updates:
                self.notify_updates(updates)
            else:
                self.notify_no_updates()
            # If Docksentry itself is in the updates list, point the user
            # to /selfupdate (which is the right command — auto-updating
            # ourselves via the regular update flow doesn't work because
            # PID 1 can't replace its own container). Also hint at the
            # new /changelog so they can preview what's changed before
            # deciding. Requested by @famewolf in #2.
            own_name, _ = self._own_container_meta()
            if own_name and any(u.get("name") == own_name for u in updates):
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
            ok, msg = checker.cleanup_images()
            if ok and "Nothing" in msg:
                self.send_message(self.t("cleanup_none"))
            elif ok:
                self.send_message(f"✅ {msg}")
            else:
                self.send_message(f"❌ {msg}")

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
            resolved, err = self._resolve_container(parts[1].strip())
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

        elif text == "/help" or text == "/start":
            from version import VERSION
            # /help iterates the same _BOT_COMMANDS table that the
            # Telegram picker derives from — dedup'd by help_key so
            # commands that share a help line (start/stop/restart all
            # land under help_lifecycle) only show once.
            seen = set()
            command_lines = []
            for (_name, _picker_desc, help_key) in _BOT_COMMANDS:
                if help_key is None or help_key in seen:
                    continue
                seen.add(help_key)
                command_lines.append(self.t(help_key))
            self.send_message(
                self.t("help_title", version=VERSION) + "\n\n"
                + self.t("help_autocomplete_hint") + "\n\n"
                + self.t("help_commands") + "\n"
                + "\n".join(command_lines) + "\n\n"
                + self.t("help_docs_footer")
            )
