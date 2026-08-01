#!/usr/bin/env python3
"""Interactive Discord bot — slash commands onto the shared update engine.

This is the second interactive front-end, and it exists cheaply only
because the update orchestration moved out of `TelegramBot` first: it
talks to `UpdateEngine`, the host registry and the checkers, and knows
nothing about Telegram. If the two ever disagree about what `/update`
means, that's a bug in one of them, not two implementations drifting.

Two Discord constraints shape everything here:

* **Three seconds.** An interaction must be acknowledged within three
  seconds or the user sees "application did not respond". Almost nothing
  we do fits — listing containers shells out to the container CLI, an
  update takes minutes. So every command that touches Docker defers
  first and edits its answer afterwards.
* **Ephemeral by default.** Container lists and update output name
  internal hosts and services. Broadcasting that to a channel because
  someone typed `/status` is a privacy leak by default, so replies are
  visible only to the person who asked unless a command opts out.
"""

import threading

from discord_gateway import DiscordGateway
from discord_rest import DiscordREST, DiscordRESTError

#: Slash-command definitions, registered on startup. `type: 1` is a
#: CHAT_INPUT command; option `type: 3` is a string.
COMMANDS = [
    {"name": "status", "description": "Show container status", "type": 1,
     "options": [
         {"name": "container", "description": "Limit to one container",
          "type": 3, "required": False},
         {"name": "host", "description": "Limit to one host",
          "type": 3, "required": False},
     ]},
    {"name": "check", "description": "Check for container updates now",
     "type": 1,
     "options": [
         {"name": "host", "description": "Limit to one host",
          "type": 3, "required": False},
     ]},
    {"name": "updates", "description": "List pending updates", "type": 1},
    {"name": "hosts", "description": "List the hosts this instance manages",
     "type": 1},
    # ── state toggles ─────────────────────────────────────────────
    # Discord validates the whole registration as one document: a single
    # optional-before-required option makes the bulk PUT fail and the bot
    # ends up with NO commands at all. So required options come first in
    # every entry below, and `scripts/test_discord_bot.py` asserts it.
    {"name": "pin", "description": "Pin a container so it is never updated",
     "type": 1,
     "options": [
         {"name": "container", "description": "Container to pin",
          "type": 3, "required": True},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "unpin", "description": "Remove a container's update pin",
     "type": 1,
     "options": [
         {"name": "container", "description": "Container to unpin",
          "type": 3, "required": True},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "autoupdate",
     "description": "Toggle auto-update for a container", "type": 1,
     "options": [
         {"name": "container", "description": "Container to toggle",
          "type": 3, "required": True},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "protect",
     "description": "Toggle stop-protection for a container", "type": 1,
     "options": [
         {"name": "container", "description": "Container to toggle",
          "type": 3, "required": True},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "cooldown",
     "description": "Set a container's update cooldown in seconds",
     "type": 1,
     "options": [
         {"name": "container", "description": "Container to configure",
          "type": 3, "required": True},
         {"name": "seconds", "description": "Cooldown seconds (0 clears it)",
          "type": 4, "required": True},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "maintenance",
     "description": "Show or set maintenance mode (e.g. 2h, 30m, forever, off)",
     "type": 1,
     "options": [
         {"name": "duration",
          "description": "2h, 30m, 1d, forever or off — omit to just show",
          "type": 3, "required": False},
     ]},
    # ── reads ─────────────────────────────────────────────────────
    {"name": "history", "description": "Recent update history", "type": 1,
     "options": [
         {"name": "container", "description": "Limit to one container",
          "type": 3, "required": False},
     ]},
    {"name": "events",
     "description": "Recent container events (crash, OOM, health flips)",
     "type": 1},
    {"name": "logs", "description": "Tail a container's logs", "type": 1,
     "options": [
         {"name": "container", "description": "Container to read logs from",
          "type": 3, "required": True},
         {"name": "lines", "description": "How many lines to tail (default 30)",
          "type": 4, "required": False},
         {"name": "host", "description": "Limit to one host",
          "type": 3, "required": False},
     ]},
    {"name": "groups", "description": "List the configured container groups",
     "type": 1},
    {"name": "settings", "description": "Show the effective settings",
     "type": 1},
]

#: `/logs` tail bounds. Discord's 2000-character ceiling makes anything
#: much larger pointless, and an unbounded value would let one command
#: pull a gigabyte of log text through the container CLI.
LOG_LINES_DEFAULT = 30
LOG_LINES_MAX = 200


class DiscordBot:
    """Owns the gateway connection and answers interactions.

    Constructed with the same objects `main.py` already built, so nothing
    here duplicates state: one engine, one registry, one lock.
    """

    def __init__(self, config, store, engine, hosts=None, checker=None,
                 log=print):
        self.config = config
        self.store = store
        self.engine = engine
        self.hosts = hosts
        self.checker = checker
        self.log = log
        self.token = getattr(config, "discord_bot_token", "") or ""
        self.application_id = getattr(config, "discord_app_id", "") or ""
        self.guild_id = getattr(config, "discord_guild_id", "") or ""
        self.rest = DiscordREST(self.token, log=log) if self.token else None
        self.gateway = None
        self._thread = None

    @property
    def enabled(self):
        """A token alone is enough to connect; registering commands also
        needs the application id, so both are required to be useful."""
        return bool(self.token and self.application_id)

    # ── lifecycle ─────────────────────────────────────────────────
    def start(self):
        if not self.enabled:
            return False
        try:
            me = self.rest.me()
            self.log(f"Discord bot authenticated as {me.get('username', '?')}")
        except DiscordRESTError as e:
            # A bad token is a configuration problem, not a transient
            # one — say so plainly instead of reconnecting forever.
            self.log(f"Discord bot disabled: token rejected ({e})")
            return False
        try:
            self.rest.register_commands(self.application_id, COMMANDS,
                                        guild_id=self.guild_id or None)
            where = f"guild {self.guild_id}" if self.guild_id else "globally"
            self.log(f"Discord slash commands registered {where}")
        except DiscordRESTError as e:
            # Not fatal: the commands may already be registered from a
            # previous run, and the gateway half still works.
            self.log(f"Discord command registration failed: {e}")

        self.gateway = DiscordGateway(self.token, on_event=self._on_event,
                                      log=self.log)
        self._thread = threading.Thread(target=self.gateway.run_forever,
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self.gateway:
            self.gateway.stop()

    # ── events ────────────────────────────────────────────────────
    def _on_event(self, name, data):
        if name != "INTERACTION_CREATE":
            return
        # type 2 = APPLICATION_COMMAND. Everything else (buttons, modals)
        # is not wired up yet and is better ignored than half-answered.
        if data.get("type") != 2:
            return
        # Acknowledge inside the three-second window, then do the work on
        # another thread. The gateway loop must not block: heartbeats go
        # out from it, and a stalled loop reads to Discord as a dead
        # client.
        try:
            self.rest.interaction_response(data["id"], data["token"],
                                           deferred=True)
        except DiscordRESTError as e:
            self.log(f"Discord: could not acknowledge interaction: {e}")
            return
        threading.Thread(target=self._run_command, args=(data,),
                         daemon=True).start()

    def _run_command(self, data):
        token = data["token"]
        try:
            text = self._dispatch(data)
        except Exception as e:
            self.log(f"Discord command error: {e}")
            text = f"Something went wrong: {str(e)[:200]}"
        try:
            self.rest.edit_original_response(self.application_id, token,
                                             text or "(no output)")
        except DiscordRESTError as e:
            self.log(f"Discord: could not deliver the answer: {e}")

    # ── commands ──────────────────────────────────────────────────
    @staticmethod
    def _options(data):
        opts = (data.get("data") or {}).get("options") or []
        return {o["name"]: o.get("value") for o in opts}

    def _hosts_for(self, name):
        """Resolve a host option to a list of hosts, or None when the
        name isn't one we manage. Mirrors the Telegram rule: no host
        given means every host for read commands."""
        if not self.hosts:
            return [None]
        if not name:
            return list(self.hosts)
        host = self.hosts.get(name)
        return [host] if host else None

    def _write_hosts_for(self, name):
        """`_hosts_for` for a command that CHANGES something.

        Same resolution, different no-host default: reads look everywhere,
        writes stay on the local host. Turning auto-update on for `nginx`
        because you didn't name a host must not turn it on for every
        host's `nginx` — that is the #7 state collision, and the Telegram
        side already draws the line here (`_resolve_targets(write=True)`).
        """
        if not self.hosts:
            return [None]
        if not name:
            # On a single-host install this is the same one-element list
            # `_hosts_for` returns, so nothing about that path changes.
            return [self.hosts.local] if self.hosts.is_multi else list(self.hosts)
        host = self.hosts.get(name)
        return [host] if host else None

    def _checker_for(self, host):
        if host is None or getattr(host, "is_local", False):
            return self.checker
        return host.checker

    def _backend_for(self, host):
        """Container CLI bound to `host`. The local host reuses the
        checker's own backend, so the single-host path stays the one the
        rest of the project already exercises."""
        if host is None or getattr(host, "is_local", False):
            return getattr(self.checker, "backend", None)
        return getattr(host, "backend", None)

    def _store_for(self, host):
        """Per-host container state (pins, auto-update, protect, cooldowns,
        groups) for `host`.

        Always through `update_engine.host_store` — never `self.store`
        directly. The raw store holds every host's keys at once, so writing
        a pin through it pins that name on all of them; routing through
        `host_store` is what keeps one host's toggles out of another's.
        Single-host installs get the raw store back from it unchanged.
        """
        from update_engine import host_store
        return host_store(self, host)

    def _label(self, host):
        if host is None or not self.hosts or not self.hosts.is_multi:
            return ""
        return f" @{host.name}"

    def _resolve_container(self, partial, backend):
        """Partial container name → `(full_name, error)`, mirroring the
        Telegram resolver: running *and* stopped (you want `/logs` of the
        container that just died), minus our `_old` rollback leftovers."""
        if backend is None:
            return None, "No container backend available for that host."
        try:
            result = backend.run(["ps", "-a", "--format", "{{.Names}}"])
        except Exception as e:
            return None, f"Could not list containers: {str(e)[:80]}"
        names = [n.strip() for n in (result.stdout or "").strip().split("\n")
                 if n.strip() and not n.strip().endswith("_old")]
        if partial in names:
            return partial, None
        matches = [n for n in names if n.lower().startswith(partial.lower())]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, ("Several containers match `%s`: %s" %
                          (partial, ", ".join(f"`{m}`" for m in matches)))
        return None, f"No container matches `{partial}`."

    def _dispatch(self, data):
        name = (data.get("data") or {}).get("name")
        opts = self._options(data)
        if name == "hosts":
            return self._cmd_hosts()
        if name == "status":
            return self._cmd_status(opts)
        if name == "check":
            return self._cmd_check(opts)
        if name == "updates":
            return self._cmd_updates()
        if name == "pin":
            return self._cmd_pin(opts, remove=False)
        if name == "unpin":
            return self._cmd_pin(opts, remove=True)
        if name == "autoupdate":
            return self._cmd_autoupdate(opts)
        if name == "protect":
            return self._cmd_protect(opts)
        if name == "cooldown":
            return self._cmd_cooldown(opts)
        if name == "maintenance":
            return self._cmd_maintenance(opts)
        if name == "history":
            return self._cmd_history(opts)
        if name == "events":
            return self._cmd_events()
        if name == "logs":
            return self._cmd_logs(opts)
        if name == "groups":
            return self._cmd_groups()
        if name == "settings":
            return self._cmd_settings()
        return f"Unknown command `{name}`."

    def _cmd_hosts(self):
        if not self.hosts or not self.hosts.is_multi:
            return "This instance manages a single host."
        lines = []
        for h in self.hosts:
            where = "local" if h.is_local else h.endpoint
            lines.append(f"• `{h.name}` — {where}")
        return "**Managed hosts**\n" + "\n".join(lines)

    def _cmd_status(self, opts):
        wanted = (opts.get("container") or "").strip().lower()
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        lines = []
        for host in targets:
            checker = self._checker_for(host)
            if checker is None:
                continue
            try:
                containers = checker.get_running_containers()
            except Exception as e:
                lines.append(f"⚠ `{getattr(host, 'name', 'local')}` unreachable: "
                             f"{str(e)[:80]}")
                continue
            for c in containers:
                if wanted and wanted not in c["name"].lower():
                    continue
                lines.append(f"• `{c['name']}`{self._label(host)} — {c['image']}")
        if not lines:
            return "No matching containers."
        # Discord rejects messages over 2000 characters, and a busy host
        # blows past that easily — truncate rather than fail the call.
        return self._clip("**Containers**\n" + "\n".join(lines))

    def _cmd_check(self, opts):
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        if self.engine.update_running:
            return "An update is running right now — try again once it finishes."
        found = []
        for host in targets:
            checker = self._checker_for(host)
            if checker is None:
                continue
            try:
                updates = checker.check_all()
            except Exception as e:
                found.append(f"⚠ `{getattr(host, 'name', 'local')}` check failed: "
                             f"{str(e)[:80]}")
                continue
            for u in updates:
                found.append(f"• `{u['name']}`{self._label(host)} — {u['image']}")
        if not found:
            return "✅ Everything is up to date."
        return self._clip("**Updates available**\n" + "\n".join(found))

    def _cmd_updates(self):
        import json
        import os
        path = getattr(self.config, "pending_file", "")
        if not path or not os.path.exists(path):
            return "No pending updates."
        try:
            with open(path) as f:
                pending = json.load(f)
        except (OSError, ValueError):
            return "No pending updates."
        if not isinstance(pending, list) or not pending:
            return "No pending updates."
        from container_store import LOCAL_HOST, entry_host
        lines = []
        for u in pending:
            if not isinstance(u, dict):
                continue
            host = entry_host(u)
            tag = "" if host == LOCAL_HOST else f" @{host}"
            lines.append(f"• `{u.get('name', '?')}`{tag} — {u.get('image', '?')}")
        return self._clip("**Pending updates**\n" + "\n".join(lines))

    # ── per-container state toggles ───────────────────────────────
    # All four share the same skeleton: resolve the target hosts with the
    # WRITE default, resolve the container on each host's own backend,
    # then flip the flag in that host's own state view. The per-host
    # resolution is the whole point — none of them may touch self.store.

    def _cmd_pin(self, opts, *, remove):
        arg = (opts.get("container") or "").strip()
        if not arg:
            return "Usage: `/unpin <container>`" if remove else \
                   "Usage: `/pin <container>`"
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        lines = []
        for host in targets:
            store = self._store_for(host)
            tag = self._label(host)
            pinned = store.get_pinned()
            if remove:
                # Unpin matches against the PIN LIST, not against what is
                # running: a container that was removed from the host can
                # still hold a stale pin, and refusing to unpin it because
                # `ps` no longer lists it would strand that entry.
                name = self._match_in(arg, pinned)
                if name.startswith("!"):
                    lines.append(name[1:] + tag)
                    continue
                pinned.remove(name)
                store.save_pinned(pinned)
                lines.append(f"📌 Unpinned `{name}`{tag}.")
                continue
            name, err = self._resolve_container(arg, self._backend_for(host))
            if err:
                lines.append(err + tag)
                continue
            if name in pinned:
                lines.append(f"📌 `{name}`{tag} is already pinned.")
                continue
            pinned.append(name)
            store.save_pinned(pinned)
            lines.append(f"📌 Pinned `{name}`{tag} — it will not be updated.")
        return self._clip("\n".join(lines) or "Nothing to do.")

    @staticmethod
    def _match_in(partial, names):
        """Resolve `partial` against an in-memory list. Returns the name,
        or an error string prefixed with `!` — the marker keeps the two
        apart without a second return value, since a container name can
        never start with `!`."""
        if partial in names:
            return partial
        matches = [n for n in names if n.lower().startswith(partial.lower())]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "!Several match `%s`: %s" % (
                partial, ", ".join(f"`{m}`" for m in matches))
        return f"!`{partial}` is not in that list."

    def _cmd_autoupdate(self, opts):
        arg = (opts.get("container") or "").strip()
        if not arg:
            return "Usage: `/autoupdate <container>`"
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        lines = []
        for host in targets:
            store = self._store_for(host)
            tag = self._label(host)
            name, err = self._resolve_container(arg, self._backend_for(host))
            if err:
                lines.append(err + tag)
                continue
            auto = store.get_autoupdate()
            if name in auto:
                auto.remove(name)
                store.save_autoupdate(auto)
                lines.append(f"⚡ Auto-update is now **off** for `{name}`{tag}.")
            else:
                auto.append(name)
                store.save_autoupdate(auto)
                lines.append(f"⚡ Auto-update is now **on** for `{name}`{tag}.")
        return self._clip("\n".join(lines) or "Nothing to do.")

    def _cmd_protect(self, opts):
        arg = (opts.get("container") or "").strip()
        if not arg:
            return "Usage: `/protect <container>`"
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        lines = []
        for host in targets:
            store = self._store_for(host)
            tag = self._label(host)
            name, err = self._resolve_container(arg, self._backend_for(host))
            if err:
                lines.append(err + tag)
                continue
            now_on = store.toggle_protect_stop(name)
            lines.append(f"🛡 Stop-protection is now **on** for `{name}`{tag}."
                         if now_on else
                         f"🛡 Stop-protection is now **off** for `{name}`{tag}.")
        return self._clip("\n".join(lines) or "Nothing to do.")

    def _cmd_cooldown(self, opts):
        arg = (opts.get("container") or "").strip()
        if not arg:
            return "Usage: `/cooldown <container> <seconds>`"
        raw = opts.get("seconds")
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            return f"`{raw}` is not a whole number of seconds."
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        lines = []
        for host in targets:
            store = self._store_for(host)
            tag = self._label(host)
            name, err = self._resolve_container(arg, self._backend_for(host))
            if err:
                lines.append(err + tag)
                continue
            # The store clamps to its own [0, 600] range and returns what
            # it actually stored, so report that rather than what was
            # asked for — otherwise `/cooldown x 9999` reads as accepted.
            applied = store.set_cooldown(name, seconds)
            lines.append(f"⏳ Cooldown for `{name}`{tag} set to {applied}s."
                         if applied else
                         f"⏳ Cooldown for `{name}`{tag} cleared.")
        return self._clip("\n".join(lines) or "Nothing to do.")

    def _cmd_maintenance(self, opts):
        """Show maintenance state, or set it.

        Setting it is a state change, but a contained one: it only pauses
        scheduling and auto-notifications. Nothing is started, stopped or
        updated by it, which is why it belongs with the read commands.
        """
        from maintenance import (disable as _disable, enable as _enable,
                                 format_remaining, get_state, parse_duration)
        arg = (opts.get("duration") or "").strip()
        if not arg:
            state = get_state(self.config)
            if not state.get("active"):
                return "🔧 Maintenance mode is **off** — updates run on schedule."
            if state.get("until_iso") == "forever":
                return "🔧 Maintenance mode is **on** until you turn it off."
            return ("🔧 Maintenance mode is **on** — "
                    f"{format_remaining(state)} remaining.")
        try:
            parsed = parse_duration(arg)
        except (ValueError, AttributeError):
            return (f"Could not read `{arg}` as a duration. "
                    "Try `2h`, `30m`, `1d`, `forever` or `off`.")
        if parsed is False:
            _disable(self.config)
            return "🔧 Maintenance mode is **off** — updates run on schedule."
        if parsed is None:
            _enable(self.config, hours=None)
            return "🔧 Maintenance mode is **on** until you turn it off."
        until = _enable(self.config, hours=parsed)
        return f"🔧 Maintenance mode is **on** until {until.strftime('%H:%M')}."

    # ── reads ─────────────────────────────────────────────────────
    def _cmd_history(self, opts):
        import json
        import os
        wanted = (opts.get("container") or "").strip().lower()
        path = getattr(self.config, "history_file", "")
        if not path or not os.path.exists(path):
            return "No update history yet."
        try:
            with open(path) as f:
                history = json.load(f)
        except (OSError, ValueError):
            return "No update history yet."
        if not isinstance(history, list) or not history:
            return "No update history yet."
        entries = [h for h in history if isinstance(h, dict)]
        if wanted:
            entries = [h for h in entries
                       if wanted in str(h.get("container", "")).lower()]
        if not entries:
            return f"No update history for `{opts.get('container')}`."
        lines = []
        for h in reversed(entries[-10:]):       # newest first
            icon = "✅" if h.get("success") else "❌"
            # Legacy v1.16.1 rows stored a different calendar glyph; the
            # Telegram side normalises it too, so both front-ends render
            # the same stored string.
            detail = str(h.get("detail", "")).replace("📅", "🗓️")
            line = f"{icon} `{h.get('container', '?')}` — {h.get('timestamp', '')}"
            if detail:
                line += f"\n    {detail}"
            lines.append(line)
        return self._clip("**Update history**\n" + "\n".join(lines))

    def _cmd_events(self, limit=15):
        import json
        import os
        path = getattr(self.config, "monitor_events_file", "")
        if not path or not os.path.exists(path):
            return "No container events recorded."
        try:
            with open(path) as f:
                events = json.load(f) or []
        except (OSError, ValueError):
            return "No container events recorded."
        if not isinstance(events, list) or not events:
            return "No container events recorded."
        lines = []
        for ev in reversed([e for e in events if isinstance(e, dict)][-limit:]):
            kind = ev.get("kind", "event")
            detail = ev.get("detail") or {}
            extra = ""
            if isinstance(detail, dict) and detail:
                extra = " (" + ", ".join(f"{k}={v}" for k, v in detail.items()) + ")"
            lines.append(f"`{ev.get('timestamp', '')}` **{kind}** "
                         f"`{ev.get('container', '?')}`{extra}")
        if not lines:
            return "No container events recorded."
        return self._clip("**Container events**\n" + "\n".join(lines))

    def _cmd_logs(self, opts):
        arg = (opts.get("container") or "").strip()
        if not arg:
            return "Usage: `/logs <container>`"
        try:
            tail = int(opts.get("lines") or LOG_LINES_DEFAULT)
        except (TypeError, ValueError):
            tail = LOG_LINES_DEFAULT
        tail = max(1, min(LOG_LINES_MAX, tail))
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        errors = []
        for host in targets:
            backend = self._backend_for(host)
            name, err = self._resolve_container(arg, backend)
            if err:
                errors.append(err + self._label(host))
                continue
            try:
                r = backend.run(["logs", "--tail", str(tail), name], timeout=10)
            except Exception as e:
                errors.append(f"Could not read `{name}`{self._label(host)} "
                              f"logs: {str(e)[:80]}")
                continue
            output = (r.stdout or "") or (r.stderr or "")
            if not output.strip():
                return f"`{name}`{self._label(host)} has produced no log output."
            # Budget the body BEFORE the fences go on: `_clip` cuts blind,
            # and a cut that lands inside a code fence leaves it unclosed
            # and mangles the whole message. Keep the tail — the newest
            # lines are the ones someone running /logs is after.
            body = output.strip()
            if len(body) > 1600:
                body = "…\n" + body[-1600:]
            return self._clip(f"**Logs — `{name}`{self._label(host)}** "
                              f"(last {tail})\n```\n{body}\n```")
        return self._clip("\n".join(errors) or f"No container matches `{arg}`.")

    def _cmd_groups(self):
        lines = []
        for host in self._hosts_for(None):
            store = self._store_for(host)
            tag = self._label(host)
            groups = store.get_groups() or {}
            if not groups:
                lines.append(f"No container groups configured{tag}.")
                continue
            for gid, g in groups.items():
                members = g.get("containers") or []
                rd = " 🔁" if g.get("restart_dependents") else ""
                if len(members) > 1:
                    body = ("👑 `" + members[0] + "` → "
                            + ", ".join(f"`{m}`" for m in members[1:]))
                elif members:
                    body = "`" + members[0] + "`"
                else:
                    body = "—"
                lines.append(f"• `{g.get('name', gid)}`{tag} "
                             f"({len(members)}){rd}\n  {body}")
        return self._clip("**Container groups**\n" + "\n".join(lines))

    @staticmethod
    def _on_off(value):
        return "on ✅" if value else "off"

    def _cmd_settings(self):
        """The instance's effective settings.

        Deliberately reads the store WHOLE rather than per host: this
        answers "how is Docksentry configured", not "what is set on box
        X". The per-host views are what `/pin` and friends report.
        """
        cfg = self.config
        on_off = self._on_off
        pinned = sorted(self.store.get_pinned()) if self.store else []
        auto = sorted(self.store.get_autoupdate()) if self.store else []
        exclude = list(getattr(cfg, "exclude_containers", None) or [])
        lines = [
            "**Settings**",
            f"🗓️ Schedule: `{getattr(cfg, 'cron_schedule', '?')}`",
            f"🌍 Language: `{getattr(cfg, 'language', '?')}`",
            f"🔄 Auto-selfupdate: {on_off(getattr(cfg, 'auto_selfupdate', False))}",
            f"🔍 Debug: {on_off(getattr(cfg, 'debug', False))}",
            f"🚫 Excluded: `{', '.join(exclude) or '-'}`",
            f"📌 Pinned: `{', '.join(pinned) or '-'}`",
            f"⚡ Auto-update: `{', '.join(auto) or '-'}`",
        ]
        if self.hosts and self.hosts.is_multi:
            lines.append(f"🖧 Hosts: `{', '.join(self.hosts.names)}`")
        try:
            from maintenance import get_state
            active = bool(get_state(cfg).get("active"))
        except Exception:
            active = False
        lines.append(f"🔧 Maintenance: {on_off(active)}")
        return self._clip("\n".join(lines))

    def _unknown_host(self, name):
        known = ", ".join(f"`{n}`" for n in self.hosts.names) if self.hosts else ""
        return f"Unknown host `{name}`. Managed hosts: {known}"

    #: Appended to a clipped message. Counted against the limit rather
    #: than added on top of it — otherwise a caller passing a limit close
    #: to 2000 would produce a message just over it, which Discord
    #: rejects outright, losing the entire answer instead of its tail.
    _CLIP_MARKER = "\n… (truncated)"

    @classmethod
    def _clip(cls, text, limit=1900):
        """Discord hard-rejects a message body over 2000 characters. A
        rejected send loses the whole answer, so clip with a marker
        instead — the user at least learns there was more."""
        if len(text) <= limit:
            return text
        room = max(0, limit - len(cls._CLIP_MARKER))
        cut = text[:room].rsplit("\n", 1)[0]
        return cut + cls._CLIP_MARKER
