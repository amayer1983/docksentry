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

The commands that CHANGE something add two more constraints:

* **Fifteen minutes.** A deferred interaction token dies after 15
  minutes, and an "update all" over a dozen containers can outlive that.
  An answer that can no longer be delivered as an edit is posted into the
  channel instead — see `_deliver`. Silently losing the result of a
  twenty-minute update is the one outcome that isn't acceptable.
* **Ask first.** `/stop` and `/updateall` don't act on the first
  invocation: they hand back a button, and only the button press does
  anything. Discord makes a slash command a single keystroke away from
  the wrong container.
"""

import secrets
import threading
import time

from discord_gateway import DiscordGateway
from discord_rest import DiscordREST, DiscordRESTError
from errfmt import clip
import container_info
import selfrestart
import selfupdate

#: Discord-side gating, applied to every command below by
#: `_harden_commands()`. Two flags, and they are defence in depth — the
#: real check is `_authorized()`, because these can be overridden by a
#: server admin and say nothing about *which* server:
#:
#: * `default_member_permissions: "0"` — visible only to members with
#:   Administrator, until a server admin deliberately grants it further.
#:   Without it Discord's default is `@everyone`.
#: * `dm_permission: False` — never usable in a DM. A DM has no guild, so
#:   `_authorized()` would refuse it anyway; this stops it being offered
#:   in the first place.
DEFAULT_PERMISSIONS = "0"


def _harden_commands(commands):
    """Return `commands` with the Discord-side gating applied to each."""
    out = []
    for c in commands:
        c = dict(c)
        c.setdefault("default_member_permissions", DEFAULT_PERMISSIONS)
        c.setdefault("dm_permission", False)
        out.append(c)
    return out


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
    {"name": "backup", "description":
     "Send a backup of settings, groups and pins as a file", "type": 1},
    # The seven below existed on Telegram only. Two front ends that
    # answer different questions is a support burden nobody signed up
    # for — @NotRetarded found the gap in the notifications (#61) and it
    # is the same gap in the command list.
    {"name": "help", "description": "What every command does", "type": 1},
    # The five the Web UI could do and neither chat could.
    {"name": "note", "description": "Attach a note to a container", "type": 1,
     "options": [
         {"name": "container", "description": "Container name", "type": 3,
          "required": True},
         {"name": "text", "description": "The note (omit to clear)",
          "type": 3, "required": False},
              {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
]},
    {"name": "trustrunning", "description":
     "Accept running-but-unhealthy for a container", "type": 1,
     "options": [
         {"name": "container", "description": "Container name", "type": 3,
          "required": True},
              {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
]},
    {"name": "askmajor", "description":
     "Ask before applying a major update", "type": 1,
     "options": [
         {"name": "container", "description": "Container name", "type": 3,
          "required": True},
              {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
]},
    {"name": "testchannel", "description":
     "Send a test notification to every channel", "type": 1},
    {"name": "changelog", "description":
     "What is new in versions ahead of yours", "type": 1},
    {"name": "selfupdate", "description":
     "Update Docksentry itself", "type": 1,
     "options": [
         {"name": "version", "description":
          "A version to pin, or `previous` (default: latest)",
          "type": 3, "required": False},
     ]},
    {"name": "debug", "description": "Toggle debug logging", "type": 1},
    {"name": "lang", "description":
     "Switch the language Docksentry answers in", "type": 1,
     "options": [
         {"name": "code", "description": "e.g. en, de", "type": 3,
          "required": True},
     ]},
    {"name": "setlink", "description":
     "Set the changelog / repo link for a container", "type": 1,
     "options": [
         {"name": "container", "description": "Container name",
          "type": 3, "required": True},
         {"name": "url", "description": "Link (omit to clear)",
          "type": 3, "required": False},
              {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
]},
    {"name": "audit", "description":
     "Audit container inspect coverage", "type": 1,
     "options": [
         {"name": "container", "description": "Container to audit",
          "type": 3, "required": True},
     ]},
    # Option type 11 is ATTACHMENT — Discord uploads the file for us and
    # hands back an id we resolve out of `data.resolved.attachments`.
    {"name": "restore", "description":
     "Restore settings, groups and pins from a backup file", "type": 1,
     "options": [
         {"name": "file", "description": "A Docksentry backup (.json)",
          "type": 11, "required": True},
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
     # Both arguments optional, which gives the same three behaviours
     # Telegram has always had from one command: no container lists what
     # is set, a container alone shows its value, both together set it.
     "description": "Show, set or list per-container update cooldowns",
     "type": 1,
     "options": [
         {"name": "container", "description": "Container to configure",
          "type": 3, "required": False},
         {"name": "seconds", "description": "Cooldown seconds (0 clears it)",
          "type": 4, "required": False},
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
    # ── the commands that change things ───────────────────────────
    # Same rule as above: required options first, or the whole bulk
    # registration is rejected and the bot ends up with no commands.
    # `host` is optional on all of them and defaults to the LOCAL host,
    # never to "all of them" — see `_write_hosts_for`.
    {"name": "update",
     "description": "Update one container that has a pending update",
     "type": 1,
     "options": [
         {"name": "container", "description": "Container to update",
          "type": 3, "required": True},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "updateall",
     "description": "Update every container with a pending update (asks first)",
     "type": 1,
     "options": [
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    # No container means Docksentry itself, which is why this option is
    # optional here and required on /stop and /start. Same rule as
    # Telegram's bare `/restart`.
    {"name": "restart", "description":
     "Restart a container — or Docksentry itself, with no container",
     "type": 1,
     "options": [
         {"name": "container", "description":
          "Container to restart (empty restarts Docksentry)",
          "type": 3, "required": False},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "stop", "description": "Stop a container (asks first)",
     "type": 1,
     "options": [
         {"name": "container", "description": "Container to stop",
          "type": 3, "required": True},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "start", "description": "Start a stopped container", "type": 1,
     "options": [
         {"name": "container", "description": "Container to start",
          "type": 3, "required": True},
         {"name": "host", "description": "Host to act on (default: local)",
          "type": 3, "required": False},
     ]},
    {"name": "cleanup", "description": "Remove unused images and build cache",
     "type": 1},
    {"name": "checkimages",
     "description": "How much space /cleanup would free (dry-run)", "type": 1,
     "options": [
         {"name": "host", "description": "Limit to one host",
          "type": 3, "required": False},
     ]},
]

#: Options Discord should suggest values for. One seam rather than a flag
#: repeated on ~30 option dicts, so the next command to grow a `host`
#: cannot be added without it.
#:
#: Why at all: nothing in Discord's UI tells you what a free-text option
#: will accept. @NotRetarded had to work out that the local host is called
#: `local` (#57) — reasonable to guess with one host, not with five. The
#: information was never secret, it just wasn't where the typing happens:
#: `/hosts` has listed them all along.
AUTOCOMPLETE_OPTIONS = ("host", "container")

def _mark_autocomplete(commands):
    """Set `autocomplete` on every option we can suggest values for."""
    for cmd in commands:
        for opt in cmd.get("options") or []:
            if opt["name"] in AUTOCOMPLETE_OPTIONS:
                opt["autocomplete"] = True
    return commands


_mark_autocomplete(COMMANDS)

#: `/logs` tail bounds. Discord's 2000-character ceiling makes anything
#: much larger pointless, and an unbounded value would let one command
#: pull a gigabyte of log text through the container CLI.
LOG_LINES_DEFAULT = 30
LOG_LINES_MAX = 200

#: Commands whose work is measured in minutes rather than seconds. They
#: get a "this may take a while" edit as soon as the worker thread starts,
#: so the user isn't looking at a spinner with no explanation — and so the
#: warning about the 15-minute window arrives BEFORE it's relevant.
SLOW_COMMANDS = ("update", "updateall", "cleanup")

#: What a command handler returns when it has already answered for
#: itself. `/backup` has to: an attachment cannot be added by editing a
#: deferred response, so the file goes as a followup message and there is
#: nothing left for the usual delivery to say. Returning "" would not do
#: — `_deliver` turns an empty answer into "(no output)", which would
#: post a second, puzzling message under the file.
ANSWERED = object()

#: How long a confirmation button stays pressable. Deliberately the same
#: 15 minutes Discord gives the interaction token: past that the button is
#: dead on Discord's side anyway, and a stale "are you sure?" that still
#: works an hour later is its own hazard.
CONFIRM_TTL = 15 * 60

#: Life of a deferred interaction token, and the safety margin we keep
#: from it. Past `DEFER_TOKEN_TTL - DEFER_TOKEN_MARGIN` seconds we don't
#: even try to edit — the PATCH would 401 and the answer would be lost.
DEFER_TOKEN_TTL = 15 * 60
DEFER_TOKEN_MARGIN = 60

#: How many interactions may be in flight at once.
#:
#: Every command runs on its own thread and most of them shell out to the
#: container CLI, so "one thread per interaction, no cap" means a burst of
#: slash commands becomes a burst of concurrent `docker` subprocesses —
#: on a small box that is the whole machine. Four is comfortably more than
#: a household Discord server ever needs concurrently and small enough
#: that the worst case is still a working host.
MAX_COMMAND_WORKERS = 4

#: Threads reserved for saying "busy" to interactions that found no worker
#: slot. Separate from the pool above on purpose: the refusal is one HTTP
#: POST and must not queue behind a twenty-minute `/updateall`, and it has
#: its own bound so a flood cannot spawn threads through the back door.
MAX_REFUSAL_WORKERS = 8

#: How long `stop()` waits for in-flight commands before giving up on
#: them. A SIGTERM during a Discord-triggered `/update` used to kill the
#: process mid-recreate, because every worker was a daemon thread and
#: nothing joined it. Bounded, because a shutdown that never finishes is
#: its own failure — the container runtime will SIGKILL us regardless.
SHUTDOWN_GRACE = 15.0


class Reply(str):
    """A command answer that may carry Discord message components.

    It IS a string — every caller that just wants the text (`_clip`, the
    2000-character assertions, the whole read-command surface) keeps
    working unchanged, and only `_deliver` looks for `.components`. The
    alternative, making `_dispatch` return a pair, would have rippled
    through every existing command and every test for the sake of the two
    commands that need a button.
    """

    def __new__(cls, text, components=None):
        obj = super().__new__(cls, text)
        obj.components = list(components or [])
        return obj


class DiscordBot:
    """Owns the gateway connection and answers interactions.

    Constructed with the same objects `main.py` already built, so nothing
    here duplicates state: one engine, one registry, one lock.
    """

    def __init__(self, config, store, engine, hosts=None, checker=None,
                 log=print, telegram=None, broadcast=None,
                 selfupdate_ctx=None):
        self.config = config
        self.store = store
        self.engine = engine
        self.hosts = hosts
        self.checker = checker
        self.log = log
        # Every user-facing line comes from the shared translations, the
        # same ones Telegram reads (#63). Discord used to carry its own
        # hardcoded English: one instance then answered German in Telegram
        # and English here, for the same question — a difference in
        # CONTENT, not in presentation, which is the line the owner drew.
        # Resolved per call rather than held, so /lang applies at once.
        #: The Telegram bot, when one is running. What is still borrowed
        #: from it, counted rather than remembered — `test_discord_borrows
        #: _exist.py` scans this file and prints the list:
        #:
        #:   * `_handle_selfupdate`  — /selfupdate;
        #:   * `_run_queued_selfupdate` — handing a queued self-update on
        #:     to its runner after we release the shared update lock, the
        #:     same two-step the Web UI does. Without it a /selfupdate
        #:     queued behind a Discord-triggered update would sit there
        #:     until some other front end released the lock next;
        #:   * `_restart_policy` + `restart_self` — /restart with no
        #:     container named;
        #:   * `t` — WRITTEN, not read: /lang replaces the translator.
        #:
        #: Each is machinery that is not Telegram's and has not been
        #: pulled into the core yet (#63). The list shrinks with every
        #: extraction step; the announcement seam left it in this one.
        self.telegram = telegram
        #: The all-channel seam (`broadcast.Broadcast`), the same
        #: instance the Telegram bot holds. What `/testchannel`
        #: speaks through — it used to borrow `bot.announce`,
        #: which made an all-channel seam look like Telegram's
        #: (#63). Not to be confused with `self.announce` below,
        #: which posts to this bot's own Discord channel.
        self.broadcast = broadcast
        #: Set by a bare /restart, read once its answer is posted.
        #: See `_shutdown_if_asked` for why it cannot go down itself.
        self._shutdown_after_answer = False
        #: The shared self-update machine (`selfupdate.Context`),
        #: the same one the Telegram bot drives.
        self.selfupdate_ctx = selfupdate_ctx
        #: Pending confirmations, token → record. Written on `/stop` and
        #: `/updateall`, claimed by the button press. A plain dict: the
        #: single-use property comes from `dict.pop`, which is atomic
        #: under the GIL — no second lock, and nothing here may ever be
        #: confused with the one update mutex the engine owns.
        self._confirmations = {}
        #: The interaction worker pool. These are NOT the update mutex and
        #: never touch it: `UpdateEngine._update_lock` remains the one lock
        #: in the process that serialises updates, and a worker that wants
        #: it takes that one. What lives here is the pool's own
        #: bookkeeping — how many workers may run, and which are running so
        #: `stop()` can wait for them.
        self._worker_sem = threading.BoundedSemaphore(MAX_COMMAND_WORKERS)
        self._refusal_sem = threading.BoundedSemaphore(MAX_REFUSAL_WORKERS)
        self._workers = set()
        self._workers_lock = threading.Lock()
        self.token = getattr(config, "discord_bot_token", "") or ""
        self.application_id = getattr(config, "discord_app_id", "") or ""
        self.guild_id = getattr(config, "discord_guild_id", "") or ""
        self.rest = DiscordREST(self.token, log=log) if self.token else None
        self.gateway = None
        self._thread = None
        #: Why the last `start()` returned False, as `(code, detail)`.
        #: Codes are a closed set — "disabled", "guild", "token" — so the
        #: Web UI can translate them; `detail` carries whatever the API
        #: actually said and is shown verbatim next to the translation.
        #:
        #: `start()` already logged every one of these, which was enough
        #: while the credentials could only come from the environment: you
        #: edited compose, recreated the container, and the log was right
        #: in front of you. Once they can be typed into the settings form
        #: the console is somewhere else entirely, and "nothing happened"
        #: is the worst possible answer to a mistyped token.
        self.last_error = None
        #: Same shape, for a start that succeeded with something worth
        #: saying anyway — currently only a failed command registration.
        self.last_warning = None

    @property
    def t(self):
        """The translator for the configured language, in Discord's markup.

        The shared strings are written in Telegram's markdown, where bold
        is `*one*` — Discord reads that as italic and wants `**two**`.
        Converting here is the whole job of a connection: the sentence is
        the core's, the markup is ours. Backticked spans are left alone,
        so a `*` inside code stays a `*`.
        """
        from i18n import get_translator
        base = get_translator(getattr(self.config, "language", "en") or "en")

        def t(key, **kw):
            return self._tg_bold_to_discord(base(key, **kw))

        return t

    @staticmethod
    def _tg_bold_to_discord(text):
        """`*bold*` → `**bold**`, leaving `**already**` and code spans be."""
        import re as _re
        parts = _re.split(r"(`[^`]*`)", text or "")
        for i in range(0, len(parts), 2):        # outside backticks only
            parts[i] = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)",
                               r"**\1**", parts[i])
        return "".join(parts)

    @property
    def enabled(self):
        """A token alone is enough to connect; registering commands also
        needs the application id, so both are required to be useful."""
        return bool(self.token and self.application_id)

    def _replies_private(self):
        """Whether a command answer stays visible only to whoever asked.

        Private by default: a container listing names your internal
        services and a Discord server can have members in it who should
        not be reading that, so the default is the one that cannot
        embarrass anyone.

        @NotRetarded asked for the choice (#57), and his reasoning is
        better than the flag: an ephemeral answer also tidies up after
        itself, which some people want and others find infuriating when
        they wanted a record. Whose channel it is decides that, not us.
        """
        return not bool(getattr(self.config, "discord_public_replies", False))

    def announce(self, text):
        """Say something into the configured channel, unprompted.

        The bot could only ever answer before this: every slash reply is
        ephemeral, so "bot started" and crash alerts had nowhere to go.
        Best-effort and silent on failure — an announcement that fails
        must never take down the thing it was announcing.
        """
        channel = (getattr(self.config, "discord_bot_channel", "") or "").strip()
        if not channel or not self.rest:
            return
        try:
            self.rest.create_message(channel, self._clip(text))
        except Exception as e:
            self.log(f"Discord: could not post to the configured channel: {e}")

    # ── lifecycle ─────────────────────────────────────────────────
    def start(self):
        self.last_error = None
        self.last_warning = None
        if not self.enabled:
            self.last_error = ("disabled", "")
            return False
        if not (self.guild_id or "").strip():
            # Refusing to start is the honest behaviour. Connecting would
            # register commands globally and then reject every single one
            # in `_authorized`, which looks exactly like "the bot is
            # broken" — and if the app is Public in Discord's portal, the
            # commands would still be invitable elsewhere.
            self.log("Discord bot disabled: DISCORD_GUILD_ID is not set. "
                     "It is required — it is what restricts the bot to your "
                     "server. Copy it from Discord (Developer Mode on, "
                     "right-click the server name → Copy Server ID).")
            self.last_error = ("guild", "")
            return False
        # Timed, all of it: @NotRetarded's bot took SEVEN minutes from
        # container start to first answer, with a log that explained none
        # of them (#63). Whichever step is slow next time now says so.
        _t = time.monotonic()
        try:
            me = self.rest.me()
            self.log(f"Discord bot authenticated as {me.get('username', '?')} "
                     f"({time.monotonic() - _t:.1f}s)")
        except DiscordRESTError as e:
            # A bad token is a configuration problem, not a transient
            # one — say so plainly instead of reconnecting forever.
            self.log(f"Discord bot disabled: token rejected ({e})")
            self.last_error = ("token", str(e))
            return False
        _t = time.monotonic()
        try:
            self.rest.register_commands(self.application_id,
                                        _harden_commands(COMMANDS),
                                        guild_id=self.guild_id or None)
            where = f"guild {self.guild_id}" if self.guild_id else "globally"
            self.log(f"Discord slash commands registered {where} "
                     f"({time.monotonic() - _t:.1f}s)")
        except DiscordRESTError as e:
            # Not fatal: the commands may already be registered from a
            # previous run, and the gateway half still works.
            self.log(f"Discord command registration failed: {e}")
            # Not an error — the gateway half still works and the commands
            # may already be registered from a previous run — but the one
            # case where it matters is a first-time setup, where it means
            # the slash commands never appeared. Kept apart from
            # `last_error` so "the bot is up" stays a straight answer.
            self.last_warning = ("register", str(e))

        self.gateway = DiscordGateway(self.token, on_event=self._on_event,
                                      log=self.log)
        self._thread = threading.Thread(target=self.gateway.run_forever,
                                        daemon=True)
        self._thread.start()
        # The one thing the bot could never say. Only when a channel was
        # configured — silence is the old behaviour and stays the default.
        from version import VERSION as _V
        self.announce(self.t("bot_connected", version=_V))
        return True

    def stop(self, timeout=SHUTDOWN_GRACE):
        """Stop listening, then wait (briefly) for work already running.

        The gateway goes first so nothing new arrives. Then we wait for
        the in-flight command workers, because SIGTERM landing in the
        middle of a Discord-triggered `/update` used to kill the process
        between `docker stop` and `docker run` — the container simply
        stayed down. The wait is bounded: past `timeout` we say what is
        still running and let the shutdown continue, since the runtime's
        own kill timer is not negotiable.
        """
        if self.gateway:
            self.gateway.stop()
        deadline = self._now() + timeout
        while True:
            with self._workers_lock:
                live = [t for t in self._workers if t.is_alive()]
            if not live:
                return
            remaining = deadline - self._now()
            if remaining <= 0:
                self.log(f"Discord: {len(live)} command(s) still running after "
                         f"{timeout:.0f}s — shutting down anyway")
                return
            live[0].join(timeout=min(remaining, 0.5))

    # ── the interaction worker pool ───────────────────────────────
    def _start_worker(self, fn, *args):
        """Run `fn(*args)` on a pooled thread, or return None when every
        slot is taken. The caller has to answer the interaction itself in
        that case — see `_refuse_busy`."""
        if not self._worker_sem.acquire(blocking=False):
            return None
        thread = threading.Thread(target=self._worker_body, args=(fn, args),
                                  daemon=True)
        with self._workers_lock:
            self._workers.add(thread)
        thread.start()
        return thread

    def _worker_body(self, fn, args):
        try:
            fn(*args)
        except Exception as e:                      # never lose a slot
            self.log(f"Discord worker crashed: {e}")
        finally:
            with self._workers_lock:
                self._workers.discard(threading.current_thread())
            self._worker_sem.release()

    def _refuse_busy(self, data):
        """Tell an interaction we have no capacity for it.

        Answering matters: silence looks identical to a broken bot, and
        the user has no way to know their `/update` did nothing. This is
        an immediate (undeferred) ephemeral reply, so it is one HTTP call
        — but it is still HTTP, so it still cannot happen on the gateway
        loop, hence a thread. Those are bounded too; if even the refusal
        pool is saturated the interaction times out on Discord's side,
        which at least is not a lie about having done something.
        """
        if not self._refusal_sem.acquire(blocking=False):
            self.log("Discord: no capacity left even to refuse an "
                     "interaction — it will time out")
            return

        def _say():
            try:
                self.rest.interaction_response(data["id"], data["token"],
                                               self.t("chan_busy_capacity"),
                                               ephemeral=self._replies_private())
            except Exception as e:
                self.log(f"Discord: could not refuse an interaction: {e}")
            finally:
                self._refusal_sem.release()

        threading.Thread(target=_say, daemon=True).start()

    # ── events ────────────────────────────────────────────────────
    #: Injectable clock. Everything that measures elapsed time here uses
    #: it, so a test can exercise the 15-minute expiry path without
    #: sitting out fifteen minutes.
    _now = staticmethod(time.monotonic)

    def _authorized(self, data):
        """May this interaction drive Docksentry?

        Two layers, mirroring the Telegram front-end (`_check_auth`), for
        the simple reason that both drive the same engine and it would be
        indefensible for one to be locked down and the other open:

        1. **Guild match.** The interaction must come from the configured
           `DISCORD_GUILD_ID`. This is REQUIRED — with no guild set the
           bot refuses everything. That is deliberate and fail-closed:
           without it, commands are registered globally, and if the
           application is left "Public" in Discord's portal *anyone* can
           invite the bot to their own server and drive these containers
           from it. There is no safe "unset" for this.

        2. **Optional user allow-list.** With `DISCORD_ALLOWED_USERS` set,
           the invoking user's id must be in it. Lets the bot live in a
           shared server while only a few people can stop a database.

        Denials are silent unless debug is on: an unauthorised stranger
        learns nothing, and a busy shared server doesn't fill the log.
        """
        want_guild = (self.guild_id or "").strip()
        if not want_guild:
            if getattr(self.config, "debug", False):
                self.log("Discord: interaction refused — no server ID is "
                         "set (DISCORD_GUILD_ID), so no interaction can be "
                         "trusted")
            return False
        if str(data.get("guild_id") or "") != want_guild:
            if getattr(self.config, "debug", False):
                self.log(f"Discord: interaction refused — server "
                         f"{data.get('guild_id')} != {want_guild}")
            return False

        allowed = getattr(self.config, "discord_allowed_users", None) or []
        if not allowed:
            return True
        # In a guild the invoker is `member.user`; the `user` key only
        # appears in DMs, which we refuse anyway. Read both so a future
        # DM path can't silently bypass the list.
        member = data.get("member") or {}
        user = member.get("user") or data.get("user") or {}
        uid = str(user.get("id") or "")
        if uid and uid in [str(a) for a in allowed]:
            return True
        if getattr(self.config, "debug", False):
            self.log(f"Discord: interaction refused — user {uid or '?'} not in "
                     f"DISCORD_ALLOWED_USERS")
        return False

    def _on_event(self, name, data):
        if name != "INTERACTION_CREATE":
            return
        kind = data.get("type")
        # 2 = APPLICATION_COMMAND (a slash command), 3 = MESSAGE_COMPONENT
        # (our confirmation buttons), 4 = APPLICATION_COMMAND_AUTOCOMPLETE
        # (someone typing into a `host` or `container` option). Everything
        # else — modals — is not wired up and is better ignored than
        # half-answered.
        if kind not in (2, 3, 4):
            return
        if kind == 4:
            # Same authorisation as everything else, and the same silence
            # when it fails: suggesting host names to a stranger would
            # give away the estate this bot manages.
            if not self._authorized(data):
                return
            # No acknowledgement to send first — for autocomplete the
            # choices ARE the response, and there is no deferring it.
            # Off the gateway loop all the same, because building the
            # container list shells out to the container CLI.
            self._start_worker(self._autocomplete, data)
            return
        if kind == 3 and not self._is_ours(data):
            # A component we never sent. Not acknowledging it is the
            # honest answer: it isn't ours to claim.
            return
        # Authorisation, before anything else happens with this
        # interaction — including acknowledging it. An unauthorised
        # interaction gets no reply at all: answering would confirm the
        # bot is listening and tell a stranger which server it serves.
        if not self._authorized(data):
            return
        # Acknowledge inside the three-second window, then do the work on
        # another thread. The gateway loop must not block: heartbeats go
        # out from it, and a stalled loop reads to Discord as a dead
        # client. The clock for the token's 15-minute life starts HERE,
        # not when the worker gets around to running.
        started = self._now()
        # The acknowledgement is an HTTP call, so it does NOT belong on
        # this loop either. `interaction_response` retries and honours a
        # 429 `retry_after` of up to a minute — worst case it blocks here
        # for longer than Discord's ~41 s heartbeat interval, and the
        # connection we are trying to answer on gets dropped underneath
        # us. Hand the whole thing over, ack included, and return
        # immediately so the next heartbeat goes out on time.
        worker = self._run_command if kind == 2 else self._run_component
        # Bounded: one thread per interaction with no cap turns a flood of
        # commands into a flood of concurrent container-CLI subprocesses.
        if self._start_worker(self._ack_then, worker, data, started) is None:
            self._refuse_busy(data)

    def _ack_then(self, worker, data, started):
        """Acknowledge the interaction, then run it. Off the gateway loop.

        The three-second deadline still applies, but a thread start is
        microseconds — the budget is spent on the HTTP round trip either
        way, and here it costs nobody a heartbeat.
        """
        try:
            # The DEFERRED acknowledgement carries the flag too. Discord
            # fixes an answer's visibility at the acknowledgement — the
            # later edit cannot change it — so setting it only on the
            # immediate path would leave every deferred command (which is
            # most of them, since anything slow defers) private no matter
            # what the switch says.
            _t = self._now()
            self.rest.interaction_response(data["id"], data["token"],
                                           deferred=True,
                                           ephemeral=self._replies_private())
            _ack = self._now() - _t
            if _ack > 2.0:
                # Discord's window is three seconds from the interaction,
                # and the retry-on-429 inside interaction_response can eat
                # far more than that. When it does, the user saw "did not
                # respond" — the log should say why, not leave a silence.
                self.log(f"Discord: acknowledgement took {_ack:.1f}s "
                         f"(window is 3s — the user may have seen "
                         f"'did not respond')")
        except DiscordRESTError as e:
            self.log(f"Discord: could not acknowledge interaction: {e}")
            return
        worker(data, started)

    @staticmethod
    def _is_ours(data):
        """True for a component interaction carrying one of our own
        custom_ids. Anything else in the channel belongs to another bot."""
        cid = (data.get("data") or {}).get("custom_id") or ""
        return str(cid).startswith("ds:")

    def _run_command(self, data, started=None):
        started = self._now() if started is None else started
        name = (data.get("data") or {}).get("name")
        if name in SLOW_COMMANDS:
            self._warn_slow(data)
        try:
            text = self._dispatch(data)
        except Exception as e:
            self.log(f"Discord command error: {e}")
            text = self.t("chan_something_wrong", error=clip(e))
        self._deliver(data, started, text)
        self._shutdown_if_asked()

    def _run_component(self, data, started=None):
        """Handle a button press. Same shape as a slash command: work on
        a thread, answer through this interaction's own token — which is
        freshly minted, so a confirmed update gets a full new 15 minutes
        rather than what was left of the original command's."""
        started = self._now() if started is None else started
        cid = str((data.get("data") or {}).get("custom_id") or "")
        action = cid.split(":")[1] if cid.count(":") >= 2 else ""
        if action in SLOW_COMMANDS:
            self._warn_slow(data)
        try:
            text = self._on_component(cid, data)
        except Exception as e:
            self.log(f"Discord component error: {e}")
            text = self.t("chan_something_wrong", error=clip(e))
        self._deliver(data, started, text)
        self._shutdown_if_asked()

    def _warn_slow(self, data):
        """Fill the deferred answer in with a heads-up before the slow
        work starts. Best effort: failing to say "this takes a while" must
        never stop the thing that takes a while."""
        try:
            self.rest.edit_original_response(self.application_id,
                                             data["token"], self.t("chan_working_on_it"))
        except DiscordRESTError as e:
            self.log(f"Discord: could not post the progress note: {e}")
        except Exception:
            pass

    def _shutdown_if_asked(self):
        """Go down, if the command that just answered asked to.

        A bare /restart cannot arm the shutdown itself: its reply is only
        RETURNED from the handler, and the post to Discord happens after
        that. Arming a 1.5-second timer there would race the very message
        telling you the restart is happening. So the command sets the
        flag, `_deliver` gets the answer out, and this fires afterwards —
        the same "message first, then go" order Telegram has, which there
        is free because its message is sent inside the handler.
        """
        if not getattr(self, "_shutdown_after_answer", False):
            return
        self._shutdown_after_answer = False
        selfrestart.go_down()

    def _deliver(self, data, started, text):
        """Deliver `text` as the answer to `data`'s interaction.

        Normally that's an edit of the deferred response. But the token
        behind it is only valid for 15 minutes, and `/updateall` over a
        dozen containers can take longer than that — the PATCH then fails
        and the entire result is lost, which is the worst possible
        outcome for the command that just recreated your containers. So
        past the window (or if the edit fails anyway) the answer goes
        into the channel as a normal message addressed to whoever asked.

        That message is NOT ephemeral — it can't be, an interaction is
        the only thing Discord lets us answer privately. Losing the
        result outright is the worse of the two, and it only happens on
        the runs that genuinely took a quarter of an hour.

        Which is exactly why the public fallback is reserved for a token
        that is genuinely gone. Falling back on *any* edit failure means
        a transient 500 on a `/logs` reply publishes a log tail to
        everyone in the channel — the reply was ephemeral because its
        contents are nobody else's business, and a Discord hiccup is not
        a reason to change that. Anything other than an expired token is
        logged and dropped.
        """
        if text is ANSWERED:
            return
        body = str(text) or "(no output)"
        if self._now() - started < DEFER_TOKEN_TTL - DEFER_TOKEN_MARGIN:
            try:
                self.rest.edit_original_response(
                    self.application_id, data["token"], body,
                    components=getattr(text, "components", None) or [])
                return
            except DiscordRESTError as e:
                if not self._token_expired(e):
                    self.log(f"Discord: could not deliver the answer ({e}); "
                             "it was a private reply, so it is dropped "
                             "rather than posted to the channel")
                    return
                self.log(f"Discord: the interaction token is no longer valid "
                         f"({e}) — answering in the channel instead")
        else:
            self.log("Discord: interaction token expired mid-command — "
                     "answering in the channel instead")
        self._post_to_channel(data, body)

    @staticmethod
    def _token_expired(err):
        """True when `err` says the interaction token itself is dead, as
        opposed to Discord merely having refused this one request.

        Discord answers an expired webhook token with 401 (error 50027,
        "Invalid Webhook Token"), and 404/10015 once the webhook is gone
        entirely. Everything else — 429, 5xx, a network error — is
        transient or is our own bug, and neither is a reason to make a
        private answer public. Unrecognised shapes count as NOT expired:
        the failure mode of guessing wrong here is a leak.
        """
        import json
        status = getattr(err, "status", None)
        if status == 401:
            return True
        if status != 404:
            return False
        try:
            code = json.loads(getattr(err, "body", "") or "").get("code")
        except (ValueError, AttributeError, TypeError):
            return False
        return code in (10015, 50027)

    def _post_to_channel(self, data, body):
        channel = data.get("channel_id") or (data.get("channel") or {}).get("id")
        if not channel:
            self.log("Discord: no channel to fall back to — answer lost:\n"
                     + body[:500])
            return
        user = self._invoker(data)
        prefix = f"<@{user}> " if user else ""
        try:
            self.rest.create_message(
                channel, self._clip(f"{prefix}{self.t("chan_late_result")}\n{body}"))
        except DiscordRESTError as e:
            self.log(f"Discord: channel fallback failed too: {e}")

    @staticmethod
    def _invoker(data):
        """The user id behind an interaction. Guild interactions carry it
        under `member`, DMs under `user`."""
        member = data.get("member") or {}
        user = member.get("user") or data.get("user") or {}
        return user.get("id")

    # ── confirmations ─────────────────────────────────────────────
    # `/stop` and `/updateall` are one keystroke away from stopping the
    # wrong database or updating a box you weren't looking at, so neither
    # acts on the first invocation: they park the resolved parameters
    # under a token and hand back a button carrying it. Only the press
    # runs anything, and only once — `dict.pop` claims the token.

    def _new_confirmation(self, action, params, data):
        now = self._now()
        for tok, rec in list(self._confirmations.items()):
            if now - rec["created"] > CONFIRM_TTL:
                self._confirmations.pop(tok, None)
        token = secrets.token_hex(8)
        self._confirmations[token] = {
            "action": action,
            "params": params,
            "user": self._invoker(data),
            # The original interaction's token, so the press can strip the
            # buttons off the message it came from.
            "origin": data.get("token"),
            "created": now,
        }
        return token

    @staticmethod
    def _confirm_components(action, token, label):
        """One danger button and one cancel, in an action row. Style 4 is
        DANGER (red), 2 is SECONDARY."""
        return [{"type": 1, "components": [
            {"type": 2, "style": 4, "label": label,
             "custom_id": f"ds:{action}:{token}"},
            {"type": 2, "style": 2, "label": "Cancel",
             "custom_id": f"ds:cancel:{token}"},
        ]}]

    def _on_component(self, custom_id, data):
        """Resolve a button press to an action, or explain why not.

        Everything is re-derived from the stored parameters rather than
        from a captured closure: minutes can pass between the question
        and the answer, and the host registry or the pending list may
        have moved on. An unknown host still errors here, exactly as it
        would have on the command itself.
        """
        parts = str(custom_id).split(":", 2)
        if len(parts) != 3 or parts[0] != "ds":
            return self.t("chan_unknown_button")
        _, action, token = parts
        if action == "cancel":
            self._confirmations.pop(token, None)
            return self.t("chan_cancelled")
        rec = self._confirmations.get(token)
        if rec is None or rec["action"] != action:
            return self.t("chan_confirm_expired")
        # Bind to the asker before claiming the token: a failed identity
        # check must leave the confirmation pressable by its owner.
        #
        # Fail CLOSED. The old `rec["user"] and who and who != ...` skipped
        # the whole check whenever either id was missing, which is the one
        # case where it matters: an interaction we cannot attribute is
        # precisely the one that must not be allowed to stop a database.
        # Both ids have to be present and equal.
        who = self._invoker(data)
        if not who or not rec.get("user") or who != rec["user"]:
            return self.t("chan_confirm_not_yours")
        if self._now() - rec["created"] > CONFIRM_TTL:
            self._confirmations.pop(token, None)
            return self.t("chan_confirm_expired")
        # Claim it. From here the button is spent whatever happens —
        # a press that fails is not an invitation to press again.
        rec = self._confirmations.pop(token, None)
        if rec is None:
            return self.t("chan_confirm_expired")
        self._retire_buttons(rec)
        if action == "stop":
            return self._do_stop(rec["params"])
        if action == "updateall":
            return self._do_updateall(rec["params"])
        if action == "restore":
            return self._run_restore(rec["params"])
        return self.t("chan_confirm_expired")

    def _retire_buttons(self, rec):
        """Strip the buttons off the message that asked the question, so
        a spent confirmation doesn't sit there looking pressable. Best
        effort — the original token may already have expired, and that
        must not stop the action the user just confirmed."""
        origin = rec.get("origin")
        if not origin:
            return
        try:
            self.rest.edit_original_response(
                self.application_id, origin, "⏳ Confirmed — working on it…",
                components=[])
        except DiscordRESTError as e:
            self.log(f"Discord: could not retire the confirmation: {e}")
        except Exception:
            pass

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
        # Through the core, which is what finally gives Discord `@all`:
        # this used to look the name up directly and had no branch for the
        # sentinel, so `host: all` came back as an unknown host (#63).
        import container_flags
        from hosts import ALL_HOSTS
        token = None
        if name:
            token = ALL_HOSTS if name.strip().lower() == "all" else name
        targets, fatal = container_flags.targets_for_write(self.hosts, token)
        return None if fatal else targets

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
            return None, self.t("chan_no_backend")
        try:
            result = backend.run(["ps", "-a", "--format", "{{.Names}}"])
        except Exception as e:
            return None, self.t("chan_list_failed", error=str(e)[:80])
        names = [n.strip() for n in (result.stdout or "").strip().split("\n")
                 if n.strip() and not n.strip().endswith("_old")]
        if partial in names:
            return partial, None
        matches = [n for n in names if n.lower().startswith(partial.lower())]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, self.t("resolve_multiple",
                                names=", ".join(f"`{m}`" for m in matches))
        return None, self.t("resolve_not_found", name=partial)

    # ── autocomplete ─────────────────────────────────────────────────
    def _autocomplete(self, data):
        """Answer the suggestion request for whichever option has focus.

        Three seconds, no deferring, and a late answer is dropped by
        Discord without telling anyone — so everything here stays cheap
        and every failure ends as "no suggestions" rather than an error
        in the user's face. Typing the value by hand still works: these
        are suggestions, not `choices`, which would forbid anything else.
        """
        d = data.get("data") or {}
        focused = next((o for o in d.get("options") or [] if o.get("focused")),
                       None)
        if focused is None:
            return
        typed = str(focused.get("value") or "")
        try:
            if focused.get("name") == "host":
                names = self._host_names()
            else:
                names = self._container_names(self._options(data).get("host"))
        except Exception as e:                                # pragma: no cover
            self.log(f"Discord autocomplete failed: {e}")
            names = []
        # Discord does no filtering of its own for autocomplete — the
        # whole point is that the app decides what matches. Substring, not
        # prefix: people reach for the distinctive part of a name
        # ("sentry") rather than its prefix ("docksentry-").
        low = typed.lower()
        hits = [n for n in names if low in n.lower()] if low else list(names)
        try:
            self.rest.interaction_autocomplete(
                data["id"], data["token"],
                [{"name": n[:100], "value": n[:100]} for n in hits[:25]])
        except DiscordRESTError as e:
            self.log(f"Discord: could not answer autocomplete: {e}")

    def _host_names(self):
        """Names accepted by a `host` option, local first.

        Single-host installs included, deliberately: `local` being the
        name of the machine Docksentry runs on is exactly what wasn't
        obvious, and one suggestion says it better than any description.
        """
        return list(self.hosts.names) if self.hosts else []

    def _container_names(self, host_name=None):
        """Container names on `host_name`, or on the local host when the
        host option hasn't been filled in yet.

        Not every host: this runs while someone is typing, and walking
        five hosts' container lists inside three seconds is a race we
        would lose. Fill the host in first and the list follows it.
        """
        hosts = self._hosts_for(host_name) if host_name else None
        host = hosts[0] if hosts else (self.hosts.local if self.hosts else None)
        backend = self._backend_for(host)
        if backend is None:
            return []
        result = backend.run(["ps", "-a", "--format", "{{.Names}}"])
        return [n.strip() for n in (result.stdout or "").strip().split("\n")
                if n.strip() and not n.strip().endswith("_old")]

    def _dispatch(self, data):
        name = (data.get("data") or {}).get("name")
        opts = self._options(data)
        # Audit trail (v2.1) — one seam for all 19 slash commands, so the
        # 20th cannot be added without one.
        try:
            audit = getattr(self, "audit", None)
            if audit is not None:
                who = (((data.get("member") or {}).get("user") or {})
                       or (data.get("user") or {}))
                audit.record("discord",
                             who.get("username") or who.get("id") or "?",
                             f"/{name}", (opts or {}).get("name", ""), opts)
        except Exception:
            pass
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
        if name == "backup":
            return self._cmd_backup(data)
        if name == "help":
            return self._cmd_help()
        if name == "note":
            return self._cmd_note(opts)
        if name in ("trustrunning", "askmajor"):
            return self._cmd_flag(name, opts)
        if name == "testchannel":
            return self._cmd_testchannel()
        if name == "changelog":
            return self._cmd_changelog()
        if name == "selfupdate":
            return self._cmd_selfupdate(opts)
        if name == "debug":
            return self._cmd_debug()
        if name == "lang":
            return self._cmd_lang(opts)
        if name == "setlink":
            return self._cmd_setlink(opts)
        if name == "audit":
            return self._cmd_audit(opts)
        if name == "restore":
            return self._cmd_restore(opts, data)
        if name == "update":
            return self._cmd_update(opts)
        if name == "updateall":
            return self._cmd_updateall(opts, data)
        if name == "restart" and not opts.get("container"):
            # No container named: restart Docksentry itself. Same rule as
            # Telegram's bare `/restart`, and the same refusal when the
            # container has no restart policy to bring it back.
            return self._cmd_restart_self()
        if name in ("restart", "stop", "start"):
            return self._cmd_lifecycle(name, opts, data)
        if name == "cleanup":
            return self._cmd_cleanup()
        if name == "checkimages":
            return self._cmd_checkimages(opts)
        return self.t("chan_unknown_command", name=name)

    def _cmd_hosts(self):
        if not self.hosts or not self.hosts.is_multi:
            return self.t("hosts_single")
        lines = []
        for h in self.hosts:
            where = "local" if h.is_local else h.endpoint
            lines.append(f"• `{h.name}` — {where}")
        return "**Managed hosts**\n" + "\n".join(lines)

    def _cmd_status(self, opts):
        """Overview, or a single container's detail.

        Both halves are the owner's "one assembly" rule made real (#2):
        the detail is collected and rendered by the same code Telegram's
        `/status <name>` uses — `status_render` — so the two front ends
        cannot drift apart field by field, which is exactly what
        @NotRetarded photographed. The only difference either is allowed
        is the bold marker.
        """
        import status_render
        wanted = (opts.get("container") or "").strip()
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        # ── one container: the full detail ──────────────────────────
        # Falls through to the substring-filtered overview when the name
        # does not resolve to exactly one container — `container:ngx`
        # matching three things keeps listing three things, as it always
        # has, rather than answering "not found".
        if wanted:
            for host in targets:
                backend = self._backend_for(host)
                name, err = self._resolve_container(wanted, backend)
                if err:
                    continue
                info = container_info.state(backend, name)
                if not info:
                    continue
                probe = ""
                if info.get("health") == "unhealthy":
                    try:
                        probe = self._checker_for(host)._health_output(
                            name, entries=1)
                    except Exception:
                        probe = ""
                detail = status_render.collect(
                    name, info,
                    stats=container_info.stats(backend, name)
                    if info.get("running") else None,
                    store=self._store_for(host),
                    probe=probe,
                    disk=container_info.disk_facts(backend, name))
                return "\n".join(status_render.lines(
                    detail, bold="**", host_tag=self._label(host)))
            # Not resolved anywhere: the overview below, narrowed.

        # ── everyone: the overview ──────────────────────────────────
        lines = []
        for host in targets:
            checker = self._checker_for(host)
            if checker is None:
                continue
            try:
                # include_self: /status is a read, and a status that
                # hides the very container answering it confused its
                # reporter twice over. The update path keeps the filter
                # that protects PID 1 (#16).
                containers = checker.get_running_containers(include_self=True)
            except Exception as e:
                lines.append(self.t("host_unreachable_short",
                                    host=getattr(host, "name", "local"),
                                    error=clip(e)))
                continue
            tag = self._label(host)
            for c in containers:
                nm = c["name"]
                if wanted and wanted.lower() not in nm.lower():
                    continue
                si = container_info.state(self._backend_for(host), nm)
                if si:
                    lines.append(status_render.overview_line(
                        nm, si, host_tag=tag))
                else:
                    lines.append(f"⚪ `{nm}`{tag} — `{c.get('image', '?')}`")
        if not lines:
            return self.t("chan_no_containers")
        return self._clip("\n".join(lines))

    def _cmd_check(self, opts):
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        if self.engine.update_running:
            return self.t("web_check_one_busy")
        found = []
        for host in targets:
            checker = self._checker_for(host)
            if checker is None:
                continue
            try:
                updates = checker.check_all()
            except Exception as e:
                found.append(self.t("host_check_failed",
                                    host=getattr(host, "name", "local"),
                                    error=str(e)[:80]))
                continue
            for u in updates:
                found.append(f"• `{u['name']}`{self._label(host)} — {u['image']}")
        if not found:
            return self.t("chan_all_up_to_date")
        return self._clip("**Updates available**\n" + "\n".join(found))

    def _cmd_updates(self):
        import json
        import os
        path = getattr(self.config, "pending_file", "")
        if not path or not os.path.exists(path):
            return self.t("no_pending_updates")
        try:
            with open(path) as f:
                pending = json.load(f)
        except (OSError, ValueError):
            return self.t("no_pending_updates")
        if not isinstance(pending, list) or not pending:
            return self.t("no_pending_updates")
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

    def _render(self, outcome):
        """A core Outcome as Discord text.

        This is the whole of what a connection does with a result: pick
        the wording from the shared keys, add the host tag if there is
        one, join into a single reply because an interaction is one
        editable answer. Telegram renders the same Outcome as one message
        per host — same facts, different shape, which is the split (#63).
        """
        import container_flags
        if outcome.fatal is not None:
            return self.t(outcome.fatal.key, **outcome.fatal.params)
        lines = []
        for r in outcome.replies:
            tag = self._label(r.host) if r.host is not None else ""
            body = self.t(r.key, **r.params) if r.key else r.text
            if r.items:
                rows = [container_flags.item_parts(i) for i in r.items]
                body += "\n" + "\n".join(
                    f"• `{n}`" + (f": {d}" if d else "") for n, d in rows)
            lines.append(body + tag)
        return "\n".join(lines) or self.t("chan_nothing_to_do")

    def _cmd_pin(self, opts, *, remove):
        """Pin or unpin. The work is `container_flags`; this renders it.

        `/unpin` matches against the pin LIST rather than against what is
        running — a container removed from the host can still hold a
        stale pin, and refusing to lift it because `ps` no longer lists
        it would strand the entry. That rule is one field in the core's
        spec table now, not a comment in two files (#63).
        """
        import container_flags
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("chan_usage_unpin" if remove else "chan_usage_pin")
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        out = container_flags.apply_flag(
            container_flags.FLAGS["unpin" if remove else "pin"], targets,
            store_for=self._store_for, backend_for=self._backend_for,
            partial=arg)
        return self._clip(self._render(out))

    def _match_in(self, partial, names):
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
            return "!" + self.t("resolve_multiple",
                            names=", ".join(f"`{m}`" for m in matches))
        return "!" + self.t("unpin_not_found", name=partial)

    def _cmd_autoupdate(self, opts):
        import container_flags
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("chan_usage_autoupdate")
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        return self._clip(self._render(container_flags.apply_flag(
            container_flags.FLAGS["autoupdate"], targets,
            store_for=self._store_for, backend_for=self._backend_for,
            partial=arg)))

    def _cmd_protect(self, opts):
        import container_flags
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("chan_usage_protect")
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        return self._clip(self._render(container_flags.apply_flag(
            container_flags.FLAGS["protect"], targets,
            store_for=self._store_for, backend_for=self._backend_for,
            partial=arg)))

    def _cmd_cooldown(self, opts):
        """Set it, show it, or — with no container — list them all.

        The number is parsed in the core, before any host is touched, so
        a value that will not parse writes nothing anywhere (#63).
        """
        import container_flags
        arg = (opts.get("container") or "").strip()
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        return self._clip(self._render(container_flags.set_cooldown(
            targets, store_for=self._store_for,
            backend_for=self._backend_for, partial=arg or None,
            seconds=opts.get("seconds"))))

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
                return self.t("chan_maint_off")
            if state.get("until_iso") == "forever":
                return self.t("chan_maint_on")
            return self.t("chan_maint_remaining", remaining=format_remaining(state))
        try:
            parsed = parse_duration(arg)
        except (ValueError, AttributeError):
            return self.t("chan_bad_duration", value=arg)
        if parsed is False:
            _disable(self.config)
            return self.t("chan_maint_off")
        if parsed is None:
            _enable(self.config, hours=None)
            return self.t("chan_maint_on")
        until = _enable(self.config, hours=parsed)
        return self.t("chan_maint_on_until",
                      until=until.strftime("%H:%M"))

    # ── reads ─────────────────────────────────────────────────────
    def _cmd_history(self, opts):
        """The update log, read by the core and laid out here (#63)."""
        import container_flags
        rows, err = container_flags.update_history(
            getattr(self.config, "history_file", ""),
            wanted=(opts.get("container") or "").strip())
        if err is not None:
            return self.t(err.key, **err.params)
        lines = []
        for h in rows:
            icon = "✅" if h.get("success") else "❌"
            line = f"{icon} `{h.get('container', '?')}` — {h.get('timestamp', '')}"
            if h.get("detail"):
                line += f"\n    {h['detail']}"
            lines.append(line)
        return self._clip(self.t("history_title") + "\n" + "\n".join(lines))

    def _cmd_events(self, limit=15):
        import json
        import os
        path = getattr(self.config, "monitor_events_file", "")
        if not path or not os.path.exists(path):
            return self.t("events_empty")
        try:
            with open(path) as f:
                events = json.load(f) or []
        except (OSError, ValueError):
            return self.t("events_empty")
        if not isinstance(events, list) or not events:
            return self.t("events_empty")
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
            return self.t("events_empty")
        return self._clip("**Container events**\n" + "\n".join(lines))

    def _cmd_logs(self, opts):
        """Which host has it, and what it said — decided in the core; the
        fences and the length budget are Discord's (#63)."""
        import container_flags
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("logs_usage")
        try:
            tail = int(opts.get("lines") or LOG_LINES_DEFAULT)
        except (TypeError, ValueError):
            tail = LOG_LINES_DEFAULT
        tail = max(1, min(LOG_LINES_MAX, tail))
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        name, host, body, reply = container_flags.read_logs(
            targets, backend_for=self._backend_for, partial=arg, tail=tail)
        if reply is not None:
            return self._clip(self.t(reply.key, **reply.params)
                              + (self._label(reply.host)
                                 if reply.host is not None else ""))
        # Budget the body BEFORE the fences go on: `_clip` cuts blind, and
        # a cut inside a code fence leaves it unclosed and mangles the
        # whole message. Keep the tail — the newest lines are the ones
        # someone running /logs is after.
        if len(body) > 1600:
            body = "…\n" + body[-1600:]
        return self._clip(self.t("logs_title", name=name)
                          + self._label(host) + f"\n```\n{body}\n```")

    def _cmd_groups(self):
        lines = []
        for host in self._hosts_for(None):
            store = self._store_for(host)
            tag = self._label(host)
            groups = store.get_groups() or {}
            if not groups:
                lines.append(self.t("groups_empty") + tag)
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

    def _cmd_backup(self, data):
        """Hand the backup over as a file (#2).

        @famewolf asked for this on Telegram — "no more having to jump
        from webui to webui" — and @NotRetarded asked why Discord did not
        have it about four hours later. Same bundle, same builder, a
        different transport.

        The answer is a *followup*, not the usual edited reply: an
        attachment cannot be added by editing a deferred response, so the
        file goes as its own message on the interaction's webhook. The
        return value is empty because the followup has already said
        everything — returning text too would post the answer twice.

        The bundle carries webhook URLs and the Web UI password hash. The
        channel is already the trusted one (the guild check and the
        allow-list gate every command), but that is worth saying out loud
        rather than shipping quietly.
        """
        import backup as _backup
        from version import VERSION as _V
        try:
            payload = _backup.payload(self.config, self.store, _V)
            fname = _backup.filename(self.config)
        except Exception as e:
            return self.t("backup_failed", error=str(e)[:150])
        try:
            self.rest.upload_followup(
                self.application_id, data.get("token"),
                fname, payload,
                self.t("chan_backup_caption"))
        except Exception as e:
            self.log(f"Discord: backup upload failed: {e}")
            return self.t("chan_backup_upload_failed")
        try:
            _backup.write_local(self.config, self.store, _V)
        except Exception:
            pass
        return ANSWERED

    #: A backup bundle is small. Anything much larger is not one, and
    #: downloading it to find that out is somebody else's bandwidth.
    RESTORE_MAX_BYTES = 2 * 1024 * 1024

    def _cmd_restore(self, opts, data):
        """Restore from an attached backup — the Discord half (#2).

        Same shape as Telegram's: fetch, check it is one of ours, report
        what it would overwrite, and ask. The press restores. Discord
        already has that pattern for `/stop` and `/updateall`, so this
        reuses it rather than inventing a second kind of confirmation.

        Discord has uploaded the file by the time we see the
        interaction, so there is a URL to read rather than a two-step
        fetch. It is a CDN link with a signed query string and a short
        life — no token of ours goes near it.
        """
        att = ((data.get("data") or {}).get("resolved") or {}).get(
            "attachments") or {}
        chosen = att.get(str(opts.get("file"))) or {}
        name = str(chosen.get("filename") or "")
        size = int(chosen.get("size") or 0)
        url = chosen.get("url") or ""
        if not name.lower().endswith(".json"):
            return self.t("restore_not_json")
        if size > self.RESTORE_MAX_BYTES:
            return self.t("chan_file_too_large",
                          size=f"{size / 1024 / 1024:.1f}")
        # `json` is imported here because this module has no module-level
        # import of it — every other user does the same. Leaving it out is
        # what actually broke `/restore` for @NotRetarded (#2): the
        # original code caught the resulting NameError in the same `try`
        # as the download and reported "I could not read that attachment",
        # so the real fault was invisible and I diagnosed the CDN instead.
        # His second screenshot settled it — once the messages told the
        # truth, the failure moved past the fetch and named itself.
        #
        # The User-Agent below is still right, and still not the cause.
        # The attachment comes from Discord's CDN rather than the API, and
        # every other request this bot makes identifies itself; this one
        # did not. Kept as correctness, not as a fix.
        import json
        import urllib.error
        import urllib.request
        from discord_rest import USER_AGENT
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read(self.RESTORE_MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            self.log(f"Discord: attachment fetch returned {e.code}: {url[:120]}")
            return self.t("chan_attachment_http", code=e.code)
        except Exception as e:
            self.log(f"Discord: could not fetch the attachment: {e}")
            return self.t("chan_attachment_failed", error=str(e)[:120])
        try:
            bundle = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self.log(f"Discord: attachment was not JSON: {e}")
            return self.t("restore_not_json") + f" `{str(e)[:100]}`"
        if not isinstance(bundle, dict) or "schema_version" not in bundle:
            return self.t("restore_not_a_backup")

        parts = [k for k in ("settings", "groups", "pinned", "autoupdate",
                             "notes", "links", "update_windows", "ask_major")
                 if bundle.get(k)]
        token = self._new_confirmation("restore", {"bundle": bundle}, data)
        return Reply(
            self.t("restore_offer", name=name,
                   instance=bundle.get("instance") or "?",
                   made=str(bundle.get("generated_at") or "?")[:16],
                   version=bundle.get("docksentry_version") or "?",
                   parts=", ".join(parts) or "—"),
            self._confirm_components("restore", token, "♻️ Restore"))

    def _cmd_help(self):
        """The command list, from the same table Discord registers.

        Built from COMMANDS rather than written out, so a command added
        without a description here is impossible.
        """
        # No argument list. With 31 commands the full form came to 1972
        # characters against Discord's ceiling of 2000 — the smoke test
        # that dispatches every command caught it, four characters short
        # of a reply that would simply have been refused. Discord's own
        # picker shows the parameters as you type anyway, which is the
        # one thing it does better than a help text.
        lines = ["**Docksentry commands**", ""]
        for c in sorted(COMMANDS, key=lambda x: x["name"]):
            lines.append(f"`/{c['name']}` — {c['description']}")
        lines.append("")
        lines.append("📖 <https://github.com/amayer1983/docksentry>")
        return self._clip("\n".join(lines))

    def _cmd_changelog(self):
        """The changelog, decided by the core and laid out for Discord.

        Both front ends ask `changelog.report` what to say and render its
        three cases; only the markdown differs. They used to decide
        separately, and drifted — Discord printed raw GitHub markdown and
        never showed what your current version brought, which
        @NotRetarded caught by putting the two side by side (#63).
        """
        import changelog
        from version import VERSION
        ok, content = changelog.fetch()
        if not ok:
            return self.t("changelog_fetch_failed", error=clip(content))
        rep = changelog.report(content, VERSION)
        if rep["kind"] == "unknown":
            return self.t("changelog_up_to_date", version=VERSION)
        if rep["kind"] == "current":
            v, d, body = rep["current"]
            return self._clip(
                self.t("changelog_current", version=v, date=d)
                + "\n\n" + changelog.render_body(body, bold="**"))
        entries = rep["entries"]
        parts = [self.t("changelog_title", count=len(entries), current=VERSION)]
        total, truncated = len(parts[0]), False
        cap = 1800
        for v, d, body in entries:
            chunk = (f"\n**v{v}** — {d}\n"
                     f"{changelog.render_body(body, bold='**')}")
            if total + len(chunk) > cap:
                truncated = True
                break
            parts.append(chunk)
            total += len(chunk)
        msg = "\n".join(parts)
        if truncated:
            msg += ("\n\n… (truncated — full changelog at "
                    "<https://github.com/amayer1983/docksentry/blob/main/"
                    "CHANGELOG.md>)")
        return msg

    def _cmd_selfupdate(self, opts):
        """Start a self-update. Reports through the shared seam, so what
        it finds lands here too — not only on Telegram (#63)."""
        ctx = getattr(self, "selfupdate_ctx", None)
        if ctx is None:
            return self.t("chan_not_available")
        target = (opts.get("version") or "").strip() or None
        # In the background so this reply goes out now: the self-update
        # blocks through the pull and recreate, and then the swap helper
        # stops this process.
        #
        # It used to hand the work to the Telegram bot's method — which
        # first called `bot.check_selfupdate`, a name that does not exist
        # (an AttributeError that left the command broken the whole
        # time), and then, once fixed, reported to Telegram: twelve of
        # its thirteen messages had no second recipient, so this answered
        # "started" and went quiet. Both are gone with the extraction.
        #
        # Replies come back HERE — to the Discord channel — because that
        # is where the person asked. Routing every report through the
        # all-channel seam instead was the overcorrection: one /selfupdate
        # then answered in Telegram AND Discord, fourteen times over. The
        # events (found an update, restarting) still reach every channel.
        import threading
        threading.Thread(target=selfupdate.start, args=(ctx, target),
                         kwargs={"reply": self.announce},
                         daemon=True).start()
        return self.t("chan_selfupdate_started")

    def _cmd_debug(self):
        self.config.debug = not getattr(self.config, "debug", False)
        self.config.save_persistent()
        state = "on" if self.config.debug else "off"
        return self.t("chan_debug_toggled", status=state)

    def _cmd_lang(self, opts):
        from i18n import available_languages, get_translator
        code = (opts.get("code") or "").strip().lower()
        langs = available_languages()
        if code not in langs:
            return self.t("chan_lang_unknown", code=code,
                          langs=", ".join(sorted(langs)))
        self.config.language = code
        self.config.save_persistent()
        bot = getattr(self, "telegram", None)
        if bot is not None:
            bot.t = get_translator(code)
        return self.t("lang_changed")

    def _cmd_setlink(self, opts):
        """The link write, decided in the core like the other per-container
        writes; this only renders it (#63)."""
        import container_flags
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("setlink_usage")
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        return self._clip(self._render(container_flags.set_link(
            targets, store_for=self._store_for,
            backend_for=self._backend_for, partial=arg,
            url=(opts.get("url") or "").strip())))

    def _cmd_audit(self, opts):
        """Which non-default inspect fields a recreate would not restore.

        The finding is the core's — two implementations of a finding is
        two findings — and it sweeps every host now. This one looked at
        the first target only, so a container on the second managed host
        came back as not found while Telegram found it (#63).
        """
        import container_flags
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("audit_usage")
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        name, host, findings, err = container_flags.audit_container(
            targets, backend_for=self._backend_for,
            checker_for=self._checker_for, partial=arg)
        if err is not None:
            return self.t(err.key, **err.params) + (
                self._label(err.host) if err.host is not None else "")
        host_keys = findings.get("host_unknown") or []
        cfg_keys = findings.get("config_unknown") or []
        dropped = findings.get("host_dropped") or []
        if not host_keys and not cfg_keys and not dropped:
            return self.t("audit_clean", name=name) + self._label(host)
        out = [self.t("chan_audit_header", name=name) + self._label(host)]
        if dropped:
            out.append(self.t("audit_section_dropped"))
            out += [f"  • `{k}`" for k in dropped]
        if host_keys:
            out.append(self.t("audit_section_host"))
            out += [f"  • `{k}`" for k in host_keys]
        if cfg_keys:
            out.append(self.t("audit_section_config"))
            out += [f"  • `{k}`" for k in cfg_keys]
        return "\n".join(out)[:1800]

    def _cmd_note(self, opts):
        import container_flags
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("note_usage")
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        return self._clip(self._render(container_flags.set_note(
            targets, store_for=self._store_for,
            backend_for=self._backend_for, partial=arg,
            text=(opts.get("text") or "").strip())))

    def _cmd_flag(self, which, opts):
        """`/trustrunning` and `/askmajor` — same skeleton, one toggle."""
        import container_flags
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("chan_usage_container", command=which)
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        return self._clip(self._render(container_flags.apply_flag(
            container_flags.FLAGS[which], targets,
            store_for=self._store_for, backend_for=self._backend_for,
            partial=arg)))

    def _cmd_testchannel(self):
        """More useful from a chat than from the Web UI: you are already
        standing where the message has to arrive."""
        seam = getattr(self, "broadcast", None)
        if seam is None:
            return self.t("chan_not_available")
        seam.announce(self.t("testchannel_message"))
        return self.t("chan_testchannel_sent")

    def _cmd_restart_self(self):
        """Restart Docksentry, if something will bring it back.

        This used to ask the Telegram bot (`bot._restart_policy()`), and
        the answer was always the same: that method falls back to
        `self.checker` when called without one, the Telegram bot has no
        such attribute, and so every bare /restart from Discord refused
        with "I cannot tell which container I am" — and then advised
        adding a restart policy the container almost certainly already
        had. Both bots ask the neutral core now, each with its own
        checker (#63).
        """
        name, why = selfrestart.policy(
            self._backend_for(None), self.checker,
            lang=getattr(self.config, "language", "en"))
        if not name:
            return self.t("restart_no_policy", detail=why)
        selfrestart.record_request(self.config, by="discord")
        # NOT armed here. Telegram's message is already sent by the time
        # it goes down; this answer is only RETURNED — it still has to be
        # posted to Discord after this method ends. So the shutdown waits
        # for delivery, below.
        self._shutdown_after_answer = True
        return self.t("restart_going_down", policy=name)

    def _run_restore(self, params):
        """Apply a confirmed bundle. Returns the reply."""
        import backup as _backup
        from config import PERSISTENT_KEYS as _PK
        try:
            restored, errors, _dropped = _backup.restore(
                params["bundle"], self.config, self.store, _PK)
        except Exception as e:
            return self.t("restore_failed", error=str(e)[:150])
        note = ("\n⚠️ " + "; ".join(errors)) if errors else ""
        # Offer the restart rather than describing it — the same point
        # the owner made about Telegram's wording. `/restart` with no
        # container is the button here.
        return (self.t("restore_done",
                       parts=", ".join(restored) or "—", errors=note)
                + "\n\n" + self.t("chan_restore_restart_hint"))

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
        lines.append(self.t("chan_maintenance_line", state=on_off(active)))
        return self._clip("\n".join(lines))

    # ── the commands that change things ───────────────────────────
    # Everything below either holds the engine's update mutex while it
    # works, or refuses to work because something else holds it. There is
    # exactly ONE such lock in the process — `UpdateEngine._update_lock`,
    # the same object `TelegramBot.run_updates` and the Web UI's update
    # button claim — and nothing in this file ever constructs another. A
    # second lock would put a Discord `/update` and a scheduled
    # auto-update inside the same container's recreate at the same time,
    # which is the #53 window all over again.
    #
    # And when the lock is held we say so and stop. No queue, no wait: a
    # slash command that silently sits on a mutex for six minutes and
    # then acts is worse than one that tells you to try again.

    def _lock(self):
        return self.engine._update_lock

    def _release_lock(self):
        """Release the shared mutex, then hand on any self-update that
        queued up behind us — the same two-step every other front-end
        does (`web_ui`: release, then `bot._run_queued_selfupdate()`).
        Without the handoff a `/selfupdate` queued behind a
        Discord-triggered update would sit there until some other
        front-end happened to release the lock next."""
        self._lock().release()
        ctx = getattr(self, "selfupdate_ctx", None)
        if ctx is None:
            return
        try:
            selfupdate.run_queued(ctx)
        except Exception as e:
            self.log(f"Discord: queued self-update handoff failed: {e}")

    # ── pending_updates.json ──────────────────────────────────────
    # The file holds every managed host's entries in one flat list, each
    # carrying its `host` key, so everything here matches on the
    # (host, name) PAIR — two boxes may both have an `nginx` pending and
    # updating one must not make the other's entry disappear.

    def _pending(self):
        import json
        import os
        path = getattr(self.config, "pending_file", "")
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [u for u in data if isinstance(u, dict) and u.get("name")]

    def _drop_pending(self, keys):
        """Remove the given `(host, name)` pairs from the pending file
        (atomic write; the file is deleted when nothing is left, which is
        what every other reader treats as "nothing pending")."""
        import json
        import os
        from container_store import atomic_write_json, entry_host
        path = getattr(self.config, "pending_file", "")
        if not path or not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        wanted = {tuple(k) for k in keys}
        remaining = [u for u in data
                     if not isinstance(u, dict)
                     or (entry_host(u), u.get("name")) not in wanted]
        if remaining:
            atomic_write_json(path, remaining)
        else:
            try:
                os.remove(path)
            except OSError:
                pass

    def _pending_for(self, host):
        from container_store import entry_host
        from update_engine import host_name_of
        name = host_name_of(host)
        return [u for u in self._pending() if entry_host(u) == name]

    # ── /update ───────────────────────────────────────────────────
    def _cmd_update(self, opts):
        """Update one container that has a pending update.

        The name is matched against the PENDING LIST rather than against
        `docker ps`: `/update` only ever means "apply the update we
        already found", so a container that has none should say so
        instead of resolving fine and then doing nothing.
        """
        from update_engine import host_name_of
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("update_usage")
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        # Resolve everything BEFORE claiming the lock. An unknown host or
        # an unmatched name must not hold the mutex for the time it takes
        # to find that out — and must leave the containers untouched.
        jobs, errors = [], []
        for host in targets:
            tag = self._label(host)
            entries = self._pending_for(host)
            if not entries:
                errors.append(self.t("no_pending_updates") + tag)
                continue
            name = self._match_in(arg, [u["name"] for u in entries])
            if name.startswith("!"):
                errors.append(name[1:] + tag)
                continue
            jobs.append((host, next(u for u in entries if u["name"] == name)))
        if not jobs:
            return self._clip("\n".join(errors)
                              or self.t("chan_no_pending_for", name=arg))
        if not self._lock().acquire(blocking=False):
            return self.t("chan_update_busy")
        try:
            lines = list(errors)
            for host, target in jobs:
                checker = self._checker_for(host)
                if checker is None:
                    lines.append("No checker available" + self._label(host))
                    continue
                # Through the shared batch engine, not a bare
                # update_container: the group cascade, the netns snapshot
                # and the notifier results all live there, and a
                # front-end that bypassed it would quietly behave
                # differently from the other two.
                results, _ok, _major = self.engine._process_update_batch(
                    [target], checker, auto=False)
                lines.extend(results)
                self._drop_pending([(host_name_of(host), target["name"])])
        finally:
            self._release_lock()
        return self._clip("**Update**\n" + "\n".join(lines))

    # ── /updateall ────────────────────────────────────────────────
    def _cmd_updateall(self, opts, data):
        """Ask before updating everything pending. The confirmation is
        the whole point: this recreates every container on the host, and
        a slash command is one keystroke."""
        from update_engine import host_name_of
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        preview, approved = [], []
        for host in targets:
            for u in self._pending_for(host):
                preview.append(f"• `{u['name']}`{self._label(host)} — "
                               f"{u.get('image', '?')}")
                # What the user is being shown is what the press may act
                # on — nothing else. Minutes can pass before the button
                # is pressed and a scheduled check runs in that window.
                approved.append((host_name_of(host), u["name"]))
        if not preview:
            return self.t("no_pending_updates")
        token = self._new_confirmation("updateall",
                                       {"host": opts.get("host"),
                                        "approved": approved}, data)
        where = self._label(targets[0]).strip() or "this host"
        prompt = (f"⚠ Update **{len(preview)}** container(s) on {where}?\n"
                  + "\n".join(preview)
                  + "\n\n" + self.t("chan_updateall_confirm"))
        return Reply(self._clip(prompt),
                     self._confirm_components(
                         "updateall", token,
                         f"Update {len(preview)} container(s)"[:80]))

    def _do_updateall(self, params):
        """Run the update the user actually approved.

        NOT "whatever is pending now". The prompt listed N containers and
        that list is what was agreed to; the pending file is re-read here
        because entries can DISAPPEAR (another front-end updated one), but
        anything that appeared since — a scheduled check finding four more
        while the question sat unanswered — was never approved and is not
        touched. Approving 3 and getting 9 is not a confirmation.
        """
        from update_engine import host_name_of
        targets = self._write_hosts_for(params.get("host"))
        if targets is None:
            return self._unknown_host(params.get("host"))
        approved = params.get("approved")
        approved_set = {tuple(k) for k in approved} if approved else None
        batches, skipped = [], 0
        for host in targets:
            entries = self._pending_for(host)
            if approved_set is not None:
                keep = [u for u in entries
                        if (host_name_of(host), u["name"]) in approved_set]
                skipped += len(entries) - len(keep)
                entries = keep
            if entries:
                batches.append((host, entries))
        gone = 0
        if approved_set is not None:
            still_there = sum(len(e) for _h, e in batches)
            gone = len(approved_set) - still_there
        if not batches:
            if approved_set:
                return self.t("chan_nothing_left")
            return self.t("no_pending_updates")
        note = ""
        if gone > 0:
            note = "\n" + self.t("chan_skipped_not_pending", count=gone,
                                  total=len(approved_set))
        if skipped > 0:
            note += "\n" + self.t("chan_newly_pending", count=skipped)
        if not self._lock().acquire(blocking=False):
            return self.t("chan_update_busy")
        try:
            lines = []
            for host, entries in batches:
                checker = self._checker_for(host)
                if checker is None:
                    lines.append("No checker available" + self._label(host))
                    continue
                # One call per host: the engine resolves per-host STATE
                # from each entry's `host` key, but the checker that
                # actually recreates is a single parameter — a batch must
                # not mix hosts.
                results, _ok, _major = self.engine._process_update_batch(
                    entries, checker, auto=False)
                lines.extend(results)
                self._drop_pending([(host_name_of(host), u["name"])
                                    for u in entries])
        finally:
            self._release_lock()
        # The note goes at the TOP, not after the results: `_clip` cuts the
        # tail, and "I did not update the four that turned up while you were
        # deciding" is the part that must survive a long answer.
        return self._clip("**Update all**" + note + "\n" + "\n".join(lines))

    # ── /start, /stop, /restart ───────────────────────────────────
    def _cmd_lifecycle(self, action, opts, data):
        import lifecycle
        arg = (opts.get("container") or "").strip()
        if not arg:
            return self.t("chan_usage_container", command=action)
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        if action != "stop":
            return self._run_lifecycle(action, arg, targets)

        # `/stop` asks first, and the refusals run BEFORE the button is
        # offered: being asked "are you sure?" about a stop-protected
        # container and THEN refused is a worse answer than being told
        # straight away. Telegram asks the same question now, from the
        # same plan, in the same words (#63).
        outcome, work = lifecycle.plan(
            "stop", targets, backend_for=self._backend_for,
            checker_for=self._checker_for, store_for=self._store_for,
            partial=arg, update_running=self.engine.update_running)
        if outcome.fatal is not None or not work:
            return self._clip(self._render(outcome))
        token = self._new_confirmation(
            "stop", {"host": opts.get("host"), "container": arg}, data)
        question = lifecycle.confirm_question("stop", work, partial=arg)
        # `_render` of an empty outcome says "nothing to do", which is
        # exactly wrong here — there IS something to do, we are asking
        # about it.
        head = self._render(outcome) if outcome.replies else ""
        body = self.t(question.key, **question.params)
        names = [n for _h, ns in work for n in ns]
        label = (f"Stop {names[0]}" if len(names) == 1
                 else f"Stop {len(names)} containers")
        return Reply(
            self._clip("\n".join(x for x in (head, body) if x)),
            self._confirm_components("stop", token, label[:80]))

    def _do_stop(self, params):
        targets = self._write_hosts_for(params.get("host"))
        if targets is None:
            return self._unknown_host(params.get("host"))
        # The raw argument, not a resolved name: a glob has to survive
        # the round trip, and re-resolving on the press is what makes the
        # guards run against the world as it is now rather than as it was
        # when the question was asked.
        return self._run_lifecycle("stop", params.get("container") or "",
                                   targets)

    def _run_lifecycle(self, action, arg, targets):
        """The guards, the CLI call and the wording all live in the core
        now — this only renders. Globs came along with it: `/stop web*`
        worked in Telegram and not here, because the matching sat in the
        other front end (#40, @LeeNX)."""
        import lifecycle
        return self._clip(self._render(lifecycle.act(
            action, targets, backend_for=self._backend_for,
            checker_for=self._checker_for, store_for=self._store_for,
            partial=arg, update_running=self.engine.update_running)))

    def _protected_msg(self, name):
        """Why a stop was refused — the shared sentence Telegram uses for
        the same refusal, so the two cannot word it differently (#63).
        An instance method now, because reaching the translations needs
        the configured language."""
        return self.t("lifecycle_refused_protected", name=name)

    # `_is_protected` was here so `/stop` could refuse before offering a
    # button. `lifecycle.plan` runs that check now, together with the
    # other two, so the question and the refusal come from one place.
    def _cmd_cleanup(self):
        """Guarded image cleanup, on every managed host.

        `image prune -a` filters on image CREATION time, so an image
        built upstream days ago but pulled seconds ago is fair game —
        pruning inside an update's pull→run window would delete the image
        that update is about to run. Hence the same mutex, and hence
        "busy" means skip rather than wait: the next cleanup simply runs
        it.

        This used to answer for the local host only, on the grounds that
        cleanup is a write and writes stay local. The grounds were sound
        and the conclusion still wrong: @famewolf's dockmox was the box
        that was full, and it was not the local one (#2). Telegram walked
        them all; this did not. It does now, through the same core.
        """
        import container_flags
        # `_hosts_for(None)` — every host, the read-command default. A
        # cleanup IS a write, and writes stay local everywhere else; this
        # is the deliberate exception, because the box you need to clean
        # is the one that filled up, and that is rarely the local one.
        return self._clip(self._render(container_flags.cleanup(
            self._hosts_for(None), checker_for=self._checker_for,
            guarded_run=self._cleanup_guarded)))

    def _cleanup_guarded(self, checker):
        """`(ok, message)` from a prune, or `(None, busy)` if an update
        flow holds the mutex — the same contract as the Telegram bot's
        `cleanup_guarded`, which the scheduler and the Web UI also use."""
        if not self._lock().acquire(blocking=False):
            return None, self.t("cleanup_busy")
        try:
            return checker.cleanup_images()
        finally:
            self._release_lock()

    @staticmethod
    def _human_size(num):
        """Sizes exactly as the other chat prints them.

        This used to divide by 1024 while Telegram divided by 1000, so
        the same 224 MB of reclaimable images read as "214 MB" here and
        "224 MB" there — one number, two answers, depending on which app
        you had open. `_human_bytes` is the one that knows why: docker
        writes "8.534MB", which reads as 8534 MB wherever a dot is the
        thousands separator (#63).
        """
        from update_checker import UpdateChecker
        return UpdateChecker._human_bytes(num)

    def _cmd_checkimages(self, opts):
        """Dry-run counterpart to `/cleanup`: how much it would free right
        now. The per-host measurement is the core's; the header, the sizes
        and the trailing hint are Discord's shape (#63)."""
        import container_flags
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        replies, total = container_flags.reclaimable(
            targets, checker_for=self._checker_for)
        if not replies:
            return self.t("chan_no_backend")
        lines = []
        for r in replies:
            tag = (self._label(r.host) or " (local)") if r.ok else ""
            if r.key == "chan_reclaim_some":
                lines.append(self.t(r.key, tag=tag,
                                    size=self._human_size(
                                        r.values["bytes"])))
            elif r.key == "chan_reclaim_none":
                lines.append(self.t(r.key, tag=tag))
            else:
                lines.append(self.t(r.key, **r.params))
        if total > 0:
            lines.append(self.t("chan_autocleanup_on")
                         if getattr(self.config, "disk_warn_auto_cleanup",
                                    False)
                         else self.t("chan_run_cleanup_hint"))
        return self._clip(self.t("chan_reclaimable_header") + "\n"
                          + "\n".join(lines))

    def _unknown_host(self, name):
        known = ", ".join(f"`{n}`" for n in self.hosts.names) if self.hosts else ""
        return self.t("host_unknown", name=name, hosts=known)

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
