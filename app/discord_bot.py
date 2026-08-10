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
    {"name": "restart", "description": "Restart a container", "type": 1,
     "options": [
         {"name": "container", "description": "Container to restart",
          "type": 3, "required": True},
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
                 log=print, telegram=None):
        self.config = config
        self.store = store
        self.engine = engine
        self.hosts = hosts
        self.checker = checker
        self.log = log
        #: The Telegram bot, when one is running. Used for exactly one
        #: thing: handing a queued self-update on to its runner after we
        #: release the shared update lock, the same way the Web UI does
        #: (`bot._run_queued_selfupdate()`). Without it a /selfupdate
        #: queued behind a Discord-triggered update would sit there until
        #: some other front-end happened to release the lock next.
        self.telegram = telegram
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
        try:
            me = self.rest.me()
            self.log(f"Discord bot authenticated as {me.get('username', '?')}")
        except DiscordRESTError as e:
            # A bad token is a configuration problem, not a transient
            # one — say so plainly instead of reconnecting forever.
            self.log(f"Discord bot disabled: token rejected ({e})")
            self.last_error = ("token", str(e))
            return False
        try:
            self.rest.register_commands(self.application_id,
                                        _harden_commands(COMMANDS),
                                        guild_id=self.guild_id or None)
            where = f"guild {self.guild_id}" if self.guild_id else "globally"
            self.log(f"Discord slash commands registered {where}")
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
        self.announce(f"✅ Docksentry {_V} — bot connected.")
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

    _BUSY_WORKERS = ("⏳ Docksentry is already running as many commands as it "
                     "can at once. **Nothing was started** — try again in a "
                     "moment.")

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
                                               self._BUSY_WORKERS,
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
                self.log("Discord: interaction refused — DISCORD_GUILD_ID is "
                         "not set, so no interaction can be trusted")
            return False
        if str(data.get("guild_id") or "") != want_guild:
            if getattr(self.config, "debug", False):
                self.log(f"Discord: interaction refused — guild "
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
            self.rest.interaction_response(data["id"], data["token"],
                                           deferred=True,
                                           ephemeral=self._replies_private())
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
            text = f"Something went wrong: {str(e)[:200]}"
        self._deliver(data, started, text)

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
            text = f"Something went wrong: {str(e)[:200]}"
        self._deliver(data, started, text)

    _SLOW_NOTE = ("⏳ Working on it — this can take a few minutes.\n"
                  "If it runs past Discord's 15-minute reply window I'll "
                  "post the result in the channel instead.")

    def _warn_slow(self, data):
        """Fill the deferred answer in with a heads-up before the slow
        work starts. Best effort: failing to say "this takes a while" must
        never stop the thing that takes a while."""
        try:
            self.rest.edit_original_response(self.application_id,
                                             data["token"], self._SLOW_NOTE)
        except DiscordRESTError as e:
            self.log(f"Discord: could not post the progress note: {e}")
        except Exception:
            pass

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

    _LATE_NOTE = ("(this ran past Discord's 15-minute reply window, so "
                  "here's the result as a normal message)")

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
                channel, self._clip(f"{prefix}{self._LATE_NOTE}\n{body}"))
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

    _CONFIRM_GONE = ("That confirmation has expired or was already used — "
                     "run the command again if you still want it.")

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
            return "I don't recognise that button."
        _, action, token = parts
        if action == "cancel":
            self._confirmations.pop(token, None)
            return "Cancelled — nothing was changed."
        rec = self._confirmations.get(token)
        if rec is None or rec["action"] != action:
            return self._CONFIRM_GONE
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
            return "Only the person who ran the command can confirm it."
        if self._now() - rec["created"] > CONFIRM_TTL:
            self._confirmations.pop(token, None)
            return self._CONFIRM_GONE
        # Claim it. From here the button is spent whatever happens —
        # a press that fails is not an invitation to press again.
        rec = self._confirmations.pop(token, None)
        if rec is None:
            return self._CONFIRM_GONE
        self._retire_buttons(rec)
        if action == "stop":
            return self._do_stop(rec["params"])
        if action == "updateall":
            return self._do_updateall(rec["params"])
        return self._CONFIRM_GONE

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
        if name == "update":
            return self._cmd_update(opts)
        if name == "updateall":
            return self._cmd_updateall(opts, data)
        if name in ("restart", "stop", "start"):
            return self._cmd_lifecycle(name, opts, data)
        if name == "cleanup":
            return self._cmd_cleanup()
        if name == "checkimages":
            return self._cmd_checkimages(opts)
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
                # Same seam as the Web UI and Telegram: `backend.logs()`
                # merges stdout and stderr in write order. Building the
                # argv here bypassed that and dropped half the output.
                r = backend.logs(name, tail=tail, timeout=10)
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

    _BUSY = ("⏳ An update is already running — nothing was started. "
             "Try again once it finishes.")

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
        runner = getattr(self.telegram, "_run_queued_selfupdate", None)
        if runner is None:
            return
        try:
            runner()
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
            return "Usage: `/update <container>`"
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
                errors.append(f"No pending updates{tag or ' on this host'}.")
                continue
            name = self._match_in(arg, [u["name"] for u in entries])
            if name.startswith("!"):
                errors.append(name[1:] + tag)
                continue
            jobs.append((host, next(u for u in entries if u["name"] == name)))
        if not jobs:
            return self._clip("\n".join(errors)
                              or f"No pending update for `{arg}`.")
        if not self._lock().acquire(blocking=False):
            return self._BUSY
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
            return "No pending updates."
        token = self._new_confirmation("updateall",
                                       {"host": opts.get("host"),
                                        "approved": approved}, data)
        where = self._label(targets[0]).strip() or "this host"
        prompt = (f"⚠ Update **{len(preview)}** container(s) on {where}?\n"
                  + "\n".join(preview)
                  + "\n\nEach one is pulled and recreated. Press the button "
                    "to go ahead.")
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
                return ("Nothing left to update — every container you "
                        "approved has already been updated or is no longer "
                        "pending.")
            return "No pending updates."
        note = ""
        if gone > 0:
            note = (f"\nℹ {gone} of the {len(approved_set)} container(s) you "
                    "approved were no longer pending and were skipped.")
        if skipped > 0:
            note += (f"\nℹ {skipped} newly pending container(s) appeared after "
                     "you were asked and were NOT updated — run `/updateall` "
                     "again for those.")
        if not self._lock().acquire(blocking=False):
            return self._BUSY
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
        arg = (opts.get("container") or "").strip()
        if not arg:
            return f"Usage: `/{action} <container>`"
        targets = self._write_hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        if action != "stop":
            return self._run_lifecycle(action, arg, targets)
        # `/stop` asks first. Resolve and run the refusals BEFORE
        # offering the button: being asked "are you sure?" about a
        # container that is stop-protected — and then refused — is a
        # worse answer than being told straight away.
        # A write always resolves to exactly one host (that is what
        # `_write_hosts_for` is for), so there is one thing to confirm.
        host = targets[0]
        tag = self._label(host)
        name, err = self._resolve_container(arg, self._backend_for(host))
        if err:
            return self._clip(err + tag)
        checker = self._checker_for(host)
        if self._is_protected(name, checker, self._store_for(host)):
            return self._clip(self._protected_msg(name) + tag)
        token = self._new_confirmation(
            "stop", {"host": opts.get("host"), "container": name}, data)
        return Reply(
            self._clip(f"⚠ Stop `{name}`{tag}? It stays down until "
                       "something starts it again."),
            self._confirm_components("stop", token, f"Stop {name}"[:80]))

    def _do_stop(self, params):
        targets = self._write_hosts_for(params.get("host"))
        if targets is None:
            return self._unknown_host(params.get("host"))
        return self._run_lifecycle("stop", params.get("container") or "",
                                   targets)

    def _run_lifecycle(self, action, arg, targets):
        lines = []
        for host in targets:
            tag = self._label(host)
            backend = self._backend_for(host)
            name, err = self._resolve_container(arg, backend)
            if err:
                lines.append(err + tag)
                continue
            ok, msg = self._lifecycle_action(action, name,
                                             self._checker_for(host), backend,
                                             self._store_for(host))
            lines.append(("✅ " if ok else "❌ ") + msg + tag)
        return self._clip("\n".join(lines) or "Nothing to do.")

    @staticmethod
    def _protected_msg(name):
        return (f"`{name}` is protected from being stopped. "
                "Lift it with `/protect` first.")

    def _is_protected(self, name, checker, store):
        """True if `name` must not be stopped. A `docksentry.protect`
        container label wins over the stored toggle, exactly as it does
        on the Telegram and Web UI sides; a failing inspect falls back to
        the toggle, so a flaky host can never accidentally unprotect."""
        try:
            lab = checker.label_bool(checker.get_container_labels(name),
                                     "protect")
        except Exception:
            lab = None
        if lab is not None:
            return lab
        if store is None:
            return False
        return store.is_protect_stop(name)

    def _lifecycle_action(self, action, name, checker, backend, store):
        """Run stop/start/restart on an already-resolved container.
        Returns `(ok, message)` — same contract as the Telegram bot's
        method of the same name, and deliberately the same three
        refusals in the same order:

        * never stop or restart Docksentry itself (PID 1 dies before the
          recreate — #16),
        * nothing while an update flow runs, because a stop during the
          post-update health wait reads as unhealthy and triggers a bogus
          rollback of a good update,
        * never stop a stop-protected container (#38).

        This does NOT take the update lock. It doesn't need it — it
        checks that no update is running and then issues one CLI call —
        and taking it would let a `/restart` block the scheduler.
        """
        if checker is None or backend is None:
            return False, f"No container backend available for `{name}`."
        if action in ("stop", "restart") and checker._would_kill_self(name):
            return False, (f"Refusing to {action} `{name}` — that is "
                           "Docksentry itself.")
        if self.engine.update_running:
            return False, (f"An update is running — `{action}` is refused "
                           "until it finishes.")
        if action == "stop" and self._is_protected(name, checker, store):
            return False, self._protected_msg(name)
        if action == "stop":
            ok, detail = checker._stop_container(name)
            if ok:
                return True, f"Stopped `{name}`."
            return False, f"Could not stop `{name}`: {str(detail)[:120]}"
        if action == "start":
            try:
                r = backend.run(["start", name], timeout=30)
            except Exception as e:
                return False, f"Could not start `{name}`: {str(e)[:120]}"
            if r.returncode == 0:
                return True, f"Started `{name}`."
            return False, (f"Could not start `{name}`: "
                           f"{(r.stderr or '').strip()[:120]}")
        if action == "restart":
            # Graceful stop + start, with a generous timeout: gitlab and
            # gluetun both take their time coming down.
            try:
                r = backend.run(["restart", "--time", "30", name], timeout=120)
            except Exception as e:
                return False, f"Could not restart `{name}`: {str(e)[:120]}"
            if r.returncode == 0:
                return True, f"Restarted `{name}`."
            return False, (f"Could not restart `{name}`: "
                           f"{(r.stderr or '').strip()[:120]}")
        return False, f"Unknown action `{action}`."

    # ── /cleanup, /checkimages ────────────────────────────────────
    def _cmd_cleanup(self):
        """Guarded image cleanup on the local host.

        `image prune -a` filters on image CREATION time, so an image
        built upstream days ago but pulled seconds ago is fair game —
        pruning inside an update's pull→run window would delete the image
        that update is about to run. Hence the same mutex, and hence
        "busy" means skip rather than wait: the next cleanup simply runs
        it. No host option, because cleanup is a write and writes stay
        local.
        """
        host = (self._write_hosts_for(None) or [None])[0]
        checker = self._checker_for(host)
        if checker is None:
            return "No container backend available."
        if not self._lock().acquire(blocking=False):
            return ("🧹 An update is running right now — cleanup skipped. "
                    "The next one will pick it up.")
        try:
            ok, msg = checker.cleanup_images()
        finally:
            self._release_lock()
        if ok and "Nothing" in str(msg):
            return "🧹 Nothing to clean up."
        return self._clip(("✅ " if ok else "❌ ") + str(msg))

    @staticmethod
    def _human_size(num):
        """GB from ~1 GB up, MB below — "512 MB" reads better than
        "0.5 GB", same rule the Telegram reply uses."""
        gib = num / (1024 ** 3)
        if gib >= 1.0:
            return f"{gib:.1f} GB"
        return f"{num / (1024 ** 2):.0f} MB"

    def _cmd_checkimages(self, opts):
        """Dry-run counterpart to `/cleanup`: how much it would free
        right now, plus whether auto-cleanup is on. A read, so with no
        host given it answers for every host."""
        targets = self._hosts_for(opts.get("host"))
        if targets is None:
            return self._unknown_host(opts.get("host"))
        lines = []
        total = 0
        for host in targets:
            checker = self._checker_for(host)
            if checker is None:
                continue
            tag = self._label(host) or " (local)"
            try:
                reclaim = int(checker.reclaimable_bytes() or 0)
            except Exception as e:
                lines.append(f"⚠ `{getattr(host, 'name', 'local')}` check "
                             f"failed: {str(e)[:80]}")
                continue
            total += reclaim
            if reclaim <= 0:
                lines.append(f"🧹{tag} — nothing to reclaim.")
            else:
                lines.append(f"🧹{tag} — {self._human_size(reclaim)} "
                             "reclaimable.")
        if not lines:
            return "No container backend available."
        if total > 0:
            if getattr(self.config, "disk_warn_auto_cleanup", False):
                lines.append("Auto-cleanup is **on** — this happens by itself.")
            else:
                lines.append("Run `/cleanup` to free it.")
        return self._clip("**Reclaimable image space**\n" + "\n".join(lines))

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
