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
]


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

    def _checker_for(self, host):
        if host is None or getattr(host, "is_local", False):
            return self.checker
        return host.checker

    def _label(self, host):
        if host is None or not self.hosts or not self.hosts.is_multi:
            return ""
        return f" @{host.name}"

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

    def _unknown_host(self, name):
        known = ", ".join(f"`{n}`" for n in self.hosts.names) if self.hosts else ""
        return f"Unknown host `{name}`. Managed hosts: {known}"

    @staticmethod
    def _clip(text, limit=1900):
        """Discord hard-rejects a message body over 2000 characters. A
        rejected send loses the whole answer, so clip with a marker
        instead — the user at least learns there was more."""
        if len(text) <= limit:
            return text
        cut = text[:limit].rsplit("\n", 1)[0]
        return cut + "\n… (truncated)"
