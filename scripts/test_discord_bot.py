#!/usr/bin/env python3
"""Discord slash commands onto the shared engine (v2.0 Discord bot).

The point of this front-end is that it reuses the update engine and the
host registry rather than reimplementing them, so the tests here are
about the Discord-shaped edges: the three-second acknowledgement, the
2000-character limit, host resolution, and unreachable hosts.

Gateway and REST are stubbed — those have their own tests against real
sockets. No network, no Discord, no token.
"""
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from discord_bot import DiscordBot, COMMANDS   # noqa: E402
from container_backend import ContainerBackend   # noqa: E402

checks = {}


class FakeREST:
    def __init__(self):
        self.calls = []
        self.edit_fails = False
        #: `(status, body)` for the failure `edit_original_response` raises
        #: when `edit_fails` is on. Defaults to the expired-token 401,
        #: which is the only failure allowed to make a private answer
        #: public — everything else must not.
        self.edit_error = None

    def me(self):
        return {"username": "bot"}

    def register_commands(self, app_id, cmds, guild_id=None):
        self.calls.append(("register", app_id, guild_id, len(cmds)))

    def interaction_response(self, iid, itok, content=None, **kw):
        self.calls.append(("ack", iid, kw.get("deferred", False), content))

    def edit_original_response(self, app_id, itok, content, **kw):
        if self.edit_fails:
            from discord_rest import DiscordRESTError
            status, body = self.edit_error or (401, "Invalid Webhook Token")
            raise DiscordRESTError(status, body)
        self.calls.append(("edit", content, kw.get("components")))

    def create_message(self, channel_id, content, **kw):
        self.calls.append(("channel", channel_id, content))


class FakeBackend(ContainerBackend):
    """Just enough container CLI for the resolver, `/logs` and the
    lifecycle commands. Records every argv so a test can assert what was
    actually asked for — several checks below are precisely "and nothing
    was asked for at all".

    Subclasses the real backend rather than duck-typing it. It used to
    define `run` and nothing else, which meant it answered a call the
    production object would have answered differently: `/logs` was
    changed to go through `backend.logs()` — the method that merges the
    two output streams — and this stand-in had no such method, so the
    seven `/logs` checks failed on a stub gap rather than on anything
    about Discord. Inheriting means the argv these checks assert on is
    built by the same code the real bot builds it with, and a fake that
    is missing a method now cannot pass by accident."""

    def __init__(self, names, log_text=None):
        self.names = list(names)
        self.log_text = log_text
        self.calls = []

    def run(self, args, **kw):
        self.calls.append(list(args))
        if args and args[0] == "ps":
            return types.SimpleNamespace(
                returncode=0, stdout="\n".join(self.names) + "\n", stderr="")
        if args and args[0] == "logs":
            text = self.log_text
            if text is None:
                text = f"line one for {args[-1]}\nline two\n"
            return types.SimpleNamespace(returncode=0, stdout=text, stderr="")
        if args and args[0] in ("start", "restart"):
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="nope")


class FakeChecker:
    def __init__(self, containers, fail=False):
        self._c = containers
        self.fail = fail
        self.backend = FakeBackend([c["name"] for c in containers])
        # Lifecycle / cleanup bookkeeping for the stage-2 checks.
        self.stopped = []
        self.cleanups = 0
        self.reclaim = 0
        self.self_named = None      # name _would_kill_self says yes to
        self.labels = {}            # name -> {"protect": True/False}

    def get_running_containers(self, include_self=False):
        # Mirrors the real signature — /status passes include_self=True
        # since the self-hiding fix (#2), and a fake without the
        # parameter answered that call with a TypeError, which the
        # caller's per-host try read as "host unreachable". Every host
        # in the overview reported dead because of a stub gap.
        if self.fail:
            raise OSError("host down")
        return self._c

    def check_all(self):
        if self.fail:
            raise OSError("host down")
        return [c for c in self._c if c.get("update")]

    # ── lifecycle ────────────────────────────────────────────────
    def _would_kill_self(self, name):
        return name == self.self_named

    def _stop_container(self, name):
        self.stopped.append(name)
        return True, "stopped"

    def get_container_labels(self, name):
        return self.labels.get(name, {})

    @staticmethod
    def label_bool(labels, key):
        return labels.get(key) if labels else None

    # ── images ───────────────────────────────────────────────────
    def reclaimable_bytes(self):
        return self.reclaim

    def cleanup_images(self):
        self.cleanups += 1
        return True, "Cleanup done: 1.2 GB reclaimed"


class FakeEngine:
    """The real engine's lock discipline without the container work.

    `_update_lock` is a REAL `threading.Lock`, because half the point of
    the stage-2 checks is that the Discord bot claims that one object and
    refuses when it can't — a stub that always said yes would prove
    nothing at all.
    """

    def __init__(self):
        self._update_lock = threading.Lock()
        self.update_running = False
        self.batches = []

    def _process_update_batch(self, updates, checker, *, auto):
        self.batches.append(([u["name"] for u in updates], checker, auto))
        return ([f"✅ `{u['name']}`: updated" for u in updates],
                len(updates), [])


def _host(name, containers, local=False, fail=False, store=None):
    checker = FakeChecker(containers, fail)
    return types.SimpleNamespace(
        name=name, is_local=local, endpoint=f"tcp://{name}:2375",
        checker=checker, backend=checker.backend, store=store)


class FakeRegistry:
    def __init__(self, hosts):
        self.hosts = hosts

    def __iter__(self):
        return iter(self.hosts)

    def __len__(self):
        return len(self.hosts)

    @property
    def is_multi(self):
        return len(self.hosts) > 1

    @property
    def local(self):
        return next((h for h in self.hosts if h.is_local), self.hosts[0])

    @property
    def names(self):
        return [h.name for h in self.hosts]

    def get(self, n):
        return next((h for h in self.hosts if h.name == n), None)


LOCAL = [{"name": "web", "image": "nginx:1", "update": False}]
NAS = [{"name": "web", "image": "nginx:1", "update": True},
       {"name": "plex", "image": "plex:2", "update": False}]

cfg = types.SimpleNamespace(discord_bot_token="t", discord_app_id="app",
                            discord_guild_id="g", pending_file="",
                            # /debug and /lang persist their change, like
                            # their Telegram twins have always done.
                            language="en", debug=False,
                            save_persistent=lambda: None)
engine = FakeEngine()
reg = FakeRegistry([_host("local", LOCAL, local=True), _host("nas", NAS)])

bot = DiscordBot(cfg, None, engine, hosts=reg,
                 checker=reg.hosts[0].checker, log=lambda *_: None)
bot.rest = FakeREST()

# ── the command set is well-formed for Discord ───────────────────────
checks["every command has a name"] = all(c.get("name") for c in COMMANDS)
checks["every command has a description"] = all(
    c.get("description") for c in COMMANDS)
checks["descriptions fit Discord's 100-char limit"] = all(
    len(c["description"]) <= 100 for c in COMMANDS)
checks["names are lowercase (Discord rejects capitals)"] = all(
    c["name"] == c["name"].lower() for c in COMMANDS)
checks["options are typed"] = all(
    o.get("type") for c in COMMANDS for o in c.get("options", []))

# Discord validates a bulk command registration as ONE document: a single
# malformed entry makes the whole PUT fail, so the bot ends up with no
# commands at all rather than with one broken one. These assertions are
# the cheap version of that server-side validation.
checks["option names are lowercase"] = all(
    o["name"] == o["name"].lower()
    for c in COMMANDS for o in c.get("options", []))
checks["option descriptions fit the 100-char limit"] = all(
    0 < len(o.get("description", "")) <= 100
    for c in COMMANDS for o in c.get("options", []))
checks["command names fit the 32-char limit"] = all(
    len(c["name"]) <= 32 for c in COMMANDS)
checks["option names fit the 32-char limit"] = all(
    len(o["name"]) <= 32 for c in COMMANDS for o in c.get("options", []))
checks["names use no characters Discord rejects"] = all(
    all(ch.isalnum() or ch in "-_" for ch in c["name"]) for c in COMMANDS)
checks["command names are unique"] = (
    len({c["name"] for c in COMMANDS}) == len(COMMANDS))
checks["option names are unique within a command"] = all(
    len({o["name"] for o in c.get("options", [])}) == len(c.get("options", []))
    for c in COMMANDS)


def _required_first(cmd):
    """Discord rejects a command whose optional option precedes a required
    one — and rejects the whole registration with it."""
    seen_optional = False
    for opt in cmd.get("options", []):
        if opt.get("required"):
            if seen_optional:
                return False
        else:
            seen_optional = True
    return True


checks["required options come before optional ones"] = all(
    _required_first(c) for c in COMMANDS)
# 3 STRING, 4 INTEGER, 5 BOOLEAN, 6 USER, 7 CHANNEL, 8 ROLE,
# 9 MENTIONABLE, 10 NUMBER, 11 ATTACHMENT. 11 arrived with `/restore`,
# which takes the backup file as an attachment — Discord uploads it and
# hands back an id to resolve.
checks["option types are ones Discord knows"] = all(
    o["type"] in (3, 4, 5, 6, 7, 8, 9, 10, 11)
    for c in COMMANDS for o in c.get("options", []))
# The bad-ordering check has to be able to fail, or it proves nothing.
checks["…and that check would catch a bad ordering"] = not _required_first(
    {"options": [{"name": "a", "required": False},
                 {"name": "b", "required": True}]})

# ── an interaction is acknowledged before the work starts ────────────
# Discord kills the interaction after three seconds, so the ack must not
# wait for the container CLI.
bot._on_event("INTERACTION_CREATE",
              {"guild_id": "g", "type": 2, "id": "1", "token": "tk",
               "data": {"name": "hosts"}})
# The acknowledgement now happens on a worker thread — deliberately, so
# an HTTP call can never stall the gateway's heartbeat loop. That makes
# "did it ack?" a question about another thread, so wait for it instead
# of reading the list and hoping the scheduler was kind.
def _await_calls(fake_rest, kind, count=1, timeout=5.0):
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        hits = [c for c in fake_rest.calls if c[0] == kind]
        if len(hits) >= count:
            return hits
        _time.sleep(0.01)
    return [c for c in fake_rest.calls if c[0] == kind]


acks = _await_calls(bot.rest, "ack")
checks["interaction is acknowledged immediately"] = len(acks) == 1
# …and the loop is free again straight away: the ack is not on it.
import inspect as _inspect   # noqa: E402
checks["the ack is dispatched off the gateway loop"] = (
    "_ack_then" in _inspect.getsource(DiscordBot._on_event))
checks["acknowledgement is deferred (buys 15 minutes)"] = acks[0][2] is True

# ── host resolution ──────────────────────────────────────────────────
checks["no host given → every host"] = len(bot._hosts_for(None)) == 2
checks["named host → just that one"] = (
    [h.name for h in bot._hosts_for("nas")] == ["nas"])
checks["unknown host → None, never a fallback"] = bot._hosts_for("zzz") is None

out = bot._dispatch({"data": {"name": "status", "options": []}})
checks["status lists every host's containers"] = (
    "`web`" in out and "`plex`" in out)
checks["remote entries are labelled with their host"] = "@nas" in out
checks["local entries carry no label"] = "web` — nginx:1\n" in out or "@nas" in out

out = bot._dispatch({"data": {"name": "status",
                              "options": [{"name": "host", "value": "nas"}]}})
checks["host option narrows the listing"] = "plex" in out
out = bot._dispatch({"data": {"name": "status",
                              "options": [{"name": "host", "value": "nope"}]}})
checks["unknown host is reported, nothing is listed"] = "Unknown host" in out
checks["…and it names the hosts that do exist"] = "`nas`" in out

out = bot._dispatch({"data": {"name": "status",
                              "options": [{"name": "container", "value": "plex"}]}})
checks["container option filters"] = "plex" in out and "web" not in out

# ── check reports per host, and refuses during an update ─────────────
out = bot._dispatch({"data": {"name": "check", "options": []}})
checks["check finds the pending update"] = "web" in out and "@nas" in out
engine.update_running = True
out = bot._dispatch({"data": {"name": "check", "options": []}})
checks["check backs off while an update runs"] = "update is running" in out
engine.update_running = False

# ── an unreachable host must not sink the whole answer ───────────────
reg2 = FakeRegistry([_host("local", LOCAL, local=True),
                     _host("dead", [], fail=True)])
bot2 = DiscordBot(cfg, None, engine, hosts=reg2,
                  checker=reg2.hosts[0].checker, log=lambda *_: None)
bot2.rest = FakeREST()
out = bot2._dispatch({"data": {"name": "status", "options": []}})
checks["a dead host is reported, the rest still lists"] = (
    "unreachable" in out and "`web`" in out)

# ── Discord's 2000-character ceiling ─────────────────────────────────
long_list = [{"name": f"container-{i:03}", "image": "some/image:tag",
              "update": False} for i in range(300)]
reg3 = FakeRegistry([_host("local", long_list, local=True)])
bot3 = DiscordBot(cfg, None, engine, hosts=reg3,
                  checker=reg3.hosts[0].checker, log=lambda *_: None)
out = bot3._dispatch({"data": {"name": "status", "options": []}})
checks["a long listing is clipped below the 2000 limit"] = len(out) < 2000
checks["clipping says it clipped"] = "truncated" in out

# ── single host: no host labels anywhere ─────────────────────────────
reg4 = FakeRegistry([_host("local", LOCAL, local=True)])
bot4 = DiscordBot(cfg, None, engine, hosts=reg4,
                  checker=reg4.hosts[0].checker, log=lambda *_: None)
out = bot4._dispatch({"data": {"name": "status", "options": []}})
checks["single host output carries no host marker"] = "@" not in out
out = bot4._dispatch({"data": {"name": "hosts", "options": []}})
checks["/hosts says so plainly on a single host"] = "single host" in out

# ── a token alone isn't enough to be useful ──────────────────────────
half = DiscordBot(types.SimpleNamespace(discord_bot_token="t",
                                        discord_app_id="", discord_guild_id=""),
                  None, engine, log=lambda *_: None)
checks["token without application id is disabled"] = half.enabled is False
checks["both present → enabled"] = bot.enabled is True


# ═════════════════════════════════════════════════════════════════════
# Stage-1 command surface: reads and state toggles.
#
# These run against a REAL ContainerStore in a temp dir, not a stub. The
# bug this whole section exists to keep out is #7 state collision — a pin
# set through Discord on one host landing on every host — and a stubbed
# store would happily "pass" while doing exactly that.
# ═════════════════════════════════════════════════════════════════════
import json                                          # noqa: E402
import shutil                                        # noqa: E402
import tempfile                                      # noqa: E402

from container_store import ContainerStore, HostScopedStore   # noqa: E402

TMP = tempfile.mkdtemp(prefix="docksentry-discord-")


def _p(fn):
    return os.path.join(TMP, fn)


scfg = types.SimpleNamespace(
    pinned_file=_p("pinned.json"), autoupdate_file=_p("auto.json"),
    update_windows_file=_p("windows.json"),
    ask_before_major_file=_p("major.json"),
    trust_running_file=_p("trust.json"), cooldown_file=_p("cooldown.json"),
    protect_stop_file=_p("protect.json"),
    major_pending_file=_p("major_pending.json"), groups_file=_p("groups.json"),
    notes_file=_p("notes.json"), links_file=_p("links.json"))
real_store = ContainerStore(scfg)

scfg2 = types.SimpleNamespace(
    discord_bot_token="t", discord_app_id="app", discord_guild_id="g",
    pending_file=_p("pending.json"), history_file=_p("history.json"),
    monitor_events_file=_p("events.json"),
    maintenance_file=_p("maintenance.json"),
    cron_schedule="0 3 * * *", language="en", auto_selfupdate=False,
    debug=False, exclude_containers=["watchtower"],
    # `/debug` and `/lang` persist their change, exactly as their
    # Telegram twins always have. The smoke test below dispatches every
    # command, which is how that requirement surfaced at all.
    save_persistent=lambda: None)


def _state_host(name, names, local=False):
    h = _host(name, [{"name": n, "image": "img:1", "update": False}
                     for n in names], local=local)
    h.store = HostScopedStore(real_store, name)
    return h


sreg = FakeRegistry([_state_host("local", ["web", "db"], local=True),
                     _state_host("nas", ["web", "plex"])])
sbot = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                  checker=sreg.hosts[0].checker, log=lambda *_: None)


#: Who is running these commands. Confirmations are bound to the person
#: who asked for them and the ownership check fails CLOSED, so every
#: interaction below has to carry an identity — an anonymous button press
#: is refused, which is a property asserted further down.
ASKER = "u-asker"


def call(name, _user=ASKER, **opts):
    return sbot._dispatch({
        "member": {"user": {"id": _user}},
        "data": {"name": name,
                 "options": [{"name": k, "value": v} for k, v in opts.items()]}})


def press(custom_id, user=ASKER):
    """Press a confirmation button as `user`."""
    data = {} if user is None else {"member": {"user": {"id": user}}}
    return sbot._on_component(custom_id, data)


LOCAL_STORE = sreg.get("local").store
NAS_STORE = sreg.get("nas").store

# ── /pin, and the per-host isolation that is the point of it ─────────
out = call("pin", container="web")
checks["/pin confirms the pin"] = "Pinned `web`" in out
checks["/pin writes to the local host"] = LOCAL_STORE.get_pinned() == ["web"]
checks["/pin with no host does NOT touch the other host"] = (
    NAS_STORE.get_pinned() == [])

out = call("pin", container="web", host="nas")
checks["/pin host:nas pins there"] = NAS_STORE.get_pinned() == ["web"]
checks["…and says which host it acted on"] = "@nas" in out
checks["…while the local pin is untouched"] = LOCAL_STORE.get_pinned() == ["web"]
checks["the two pins are separate keys in the raw store"] = (
    sorted(real_store.get_pinned()) == ["nas/web", "web"])

out = call("pin", container="web")
checks["pinning twice is reported, not duplicated"] = (
    "already pinned" in out and LOCAL_STORE.get_pinned() == ["web"])

before = sorted(real_store.get_pinned())
out = call("pin", container="web", host="zzz")
checks["/pin on an unknown host errors"] = "Unknown host" in out
checks["…and changes nothing at all"] = sorted(real_store.get_pinned()) == before
checks["…and never falls back to local"] = "Pinned" not in out

# ── /unpin ───────────────────────────────────────────────────────────
out = call("unpin", container="web")
checks["/unpin removes the local pin"] = LOCAL_STORE.get_pinned() == []
checks["…and leaves the other host pinned"] = NAS_STORE.get_pinned() == ["web"]
checks["/unpin confirms"] = "Unpinned `web`" in out
out = call("unpin", container="web")
checks["/unpin of something unpinned says so"] = "not in that list" in out
out = call("unpin", container="web", host="zzz")
checks["/unpin on an unknown host errors"] = "Unknown host" in out
checks["…and the real pin survives"] = NAS_STORE.get_pinned() == ["web"]
call("unpin", container="web", host="nas")
checks["/unpin host:nas clears it there"] = NAS_STORE.get_pinned() == []

# ── /autoupdate toggles and reports the NEW state ────────────────────
out = call("autoupdate", container="web")
checks["/autoupdate turns it on"] = (
    "**on**" in out and LOCAL_STORE.get_autoupdate() == ["web"])
checks["/autoupdate does not leak to the other host"] = (
    NAS_STORE.get_autoupdate() == [])
out = call("autoupdate", container="web")
checks["/autoupdate toggles back off"] = (
    "**off**" in out and LOCAL_STORE.get_autoupdate() == [])
out = call("autoupdate", container="plex", host="nas")
checks["/autoupdate host:nas targets that host"] = (
    NAS_STORE.get_autoupdate() == ["plex"])
checks["…and the local host stays empty"] = LOCAL_STORE.get_autoupdate() == []
call("autoupdate", container="plex", host="nas")
out = call("autoupdate", container="web", host="zzz")
checks["/autoupdate on an unknown host errors"] = "Unknown host" in out

# ── /protect ─────────────────────────────────────────────────────────
# `get_protect_stop` is a set on the scoped view and a list on the raw
# store, so compare sorted — the assertion is about membership, not shape.
out = call("protect", container="db")
checks["/protect turns protection on"] = (
    "**on**" in out and sorted(LOCAL_STORE.get_protect_stop()) == ["db"])
checks["/protect does not leak across hosts"] = (
    sorted(NAS_STORE.get_protect_stop()) == [])
out = call("protect", container="db")
checks["/protect toggles back off"] = (
    "**off**" in out and sorted(LOCAL_STORE.get_protect_stop()) == [])
out = call("protect", container="plex", host="nas")
checks["/protect host:nas targets that host"] = (
    sorted(NAS_STORE.get_protect_stop()) == ["plex"])
call("protect", container="plex", host="nas")
out = call("protect", container="db", host="zzz")
checks["/protect on an unknown host errors"] = "Unknown host" in out

# ── /cooldown ────────────────────────────────────────────────────────
out = call("cooldown", container="web", seconds=45)
checks["/cooldown sets the value"] = LOCAL_STORE.get_cooldown("web") == 45
checks["…and says so"] = "45s" in out
checks["/cooldown does not leak across hosts"] = NAS_STORE.get_cooldown("web") == 0
out = call("cooldown", container="web", seconds=9999)
checks["/cooldown reports the CLAMPED value, not the asked-for one"] = (
    "600s" in out and "9999" not in out)
out = call("cooldown", container="web", seconds=0)
checks["/cooldown 0 clears it"] = (
    "cleared" in out and LOCAL_STORE.get_cooldown("web") == 0)
out = call("cooldown", container="web", seconds="not-a-number")
checks["/cooldown rejects a non-number"] = "whole number" in out
out = call("cooldown", container="web", seconds=30, host="nas")
checks["/cooldown host:nas targets that host"] = NAS_STORE.get_cooldown("web") == 30
checks["…and the local one is still clear"] = LOCAL_STORE.get_cooldown("web") == 0
out = call("cooldown", container="web", seconds=10, host="zzz")
checks["/cooldown on an unknown host errors"] = "Unknown host" in out
checks["…and writes nothing"] = NAS_STORE.get_cooldown("web") == 30

# ── an unknown CONTAINER is refused per host, nothing is written ─────
out = call("pin", container="nosuchthing")
checks["an unknown container is reported"] = "No container matches" in out
checks["…and nothing is pinned"] = LOCAL_STORE.get_pinned() == []

# ── /history ─────────────────────────────────────────────────────────
out = call("history")
# Compared against the shared translation rather than a copy of its
# wording: Discord reads the same keys Telegram does now (#63), so the
# sentence lives in one place and a test that re-typed it would be the
# second place it could drift.
import json as _json
_en = _json.load(open(os.path.join(os.path.dirname(__file__), "..", "app",
                                   "lang", "en.json"), encoding="utf-8"))
checks["/history with no file says so"] = _en["history_empty"] in out
with open(scfg2.history_file, "w") as f:
    json.dump([
        {"timestamp": "2026-07-01 10:00:00", "container": "web",
         "image": "nginx:1", "success": True, "detail": "1 → 2"},
        {"timestamp": "2026-07-02 11:00:00", "container": "db",
         "image": "pg:15", "success": False, "detail": "pull failed"},
    ], f)
out = call("history")
checks["/history lists entries"] = "`web`" in out and "`db`" in out
checks["/history marks failures"] = "❌" in out and "✅" in out
checks["/history is newest first"] = out.index("`db`") < out.index("`web`")
out = call("history", container="web")
checks["/history filters by container"] = "`web`" in out and "`db`" not in out
out = call("history", container="zzz")
checks["/history says when the filter matches nothing"] = (
    "No update history for" in out)

# ── /events ──────────────────────────────────────────────────────────
out = call("events")
checks["/events with no file says so"] = "No container events" in out
with open(scfg2.monitor_events_file, "w") as f:
    json.dump([
        {"timestamp": "2026-07-01 10:00:00", "kind": "oom",
         "container": "web", "detail": {}},
        {"timestamp": "2026-07-01 10:05:00", "kind": "unhealthy",
         "container": "db", "detail": {"streak": 3}},
    ], f)
out = call("events")
checks["/events lists events"] = "oom" in out and "unhealthy" in out
checks["/events is newest first"] = out.index("unhealthy") < out.index("oom")
checks["/events renders the detail payload"] = "streak=3" in out

# ── /logs ────────────────────────────────────────────────────────────
out = call("logs", container="web")
checks["/logs returns the log text"] = "line one for web" in out
checks["/logs fences the output"] = out.count("```") == 2
checks["/logs defaults to 30 lines"] = ["logs", "--tail", "30", "web"] in \
    sreg.get("local").backend.calls
out = call("logs", container="web", lines=5)
checks["/logs honours the lines option"] = ["logs", "--tail", "5", "web"] in \
    sreg.get("local").backend.calls
out = call("logs", container="web", lines=99999)
checks["/logs clamps an absurd line count"] = any(
    c[:2] == ["logs", "--tail"] and c[2] == "200"
    for c in sreg.get("local").backend.calls)
out = call("logs", container="plex", host="nas")
checks["/logs host:nas reads from that host"] = (
    "line one for plex" in out
    and any(c[0] == "logs" for c in sreg.get("nas").backend.calls))
checks["…and the remote log is labelled"] = "@nas" in out
out = call("logs", container="web", host="zzz")
checks["/logs on an unknown host errors"] = "Unknown host" in out
out = call("logs", container="nosuchthing")
checks["/logs of an unknown container is reported"] = "No container matches" in out

# a log longer than Discord allows must come back clipped AND fenced
noisy = _state_host("noisy", ["chatty"], local=True)
noisy.backend.log_text = "an extremely wordy log line\n" * 400
nreg = FakeRegistry([noisy])
nbot = DiscordBot(scfg2, real_store, engine, hosts=nreg,
                  checker=noisy.checker, log=lambda *_: None)
out = nbot._dispatch({"data": {"name": "logs",
                               "options": [{"name": "container",
                                            "value": "chatty"}]}})
checks["a long log stays under Discord's 2000 limit"] = len(out) < 2000
checks["…and its code fence is still closed"] = out.count("```") == 2

# ── /groups ──────────────────────────────────────────────────────────
out = call("groups")
checks["/groups with none configured says so"] = "No container groups" in out
LOCAL_STORE.save_group("media", "Media Stack", ["web", "db"],
                       restart_dependents=True)
out = call("groups")
checks["/groups lists the group"] = "Media Stack" in out
checks["/groups shows the members"] = "`web`" in out and "`db`" in out
checks["/groups marks the leader"] = "👑" in out
checks["a group is per host — the other host has none"] = (
    "No container groups configured @nas" in out)
checks["…and the group carries its host marker"] = "@local" in out
LOCAL_STORE.delete_group("media")

# ── /settings ────────────────────────────────────────────────────────
out = call("settings")
checks["/settings shows the schedule"] = "0 3 * * *" in out
checks["/settings shows excludes"] = "watchtower" in out
checks["/settings lists the managed hosts"] = "`local, nas`" in out
checks["/settings stays under the 2000 limit"] = len(out) < 2000

# ── /maintenance ─────────────────────────────────────────────────────
out = call("maintenance")
checks["/maintenance reports off by default"] = "**off**" in out
out = call("maintenance", duration="2h")
checks["/maintenance 2h turns it on"] = "**on**" in out
out = call("maintenance")
checks["…and the state is read back"] = "**on**" in out and "remaining" in out
out = call("maintenance", duration="forever")
checks["/maintenance forever has no end time"] = "until you turn it off" in out
out = call("maintenance", duration="off")
checks["/maintenance off turns it off"] = "**off**" in out
out = call("maintenance", duration="banana")
checks["/maintenance rejects nonsense"] = "Could not read" in out
out = call("maintenance")
checks["…and a rejected duration changed nothing"] = "**off**" in out


# ═════════════════════════════════════════════════════════════════════
# Stage-2 command surface: the commands that CHANGE things.
#
# Three properties carry most of the weight here:
#   * one shared lock — held elsewhere means refuse, never queue;
#   * writes default to the LOCAL host;
#   * an unknown host changes NOTHING, on any of them.
# Each is asserted by looking at what was actually done (the engine's
# batch list, the checker's stop list, the pending file), not at the
# wording of the reply.
# ═════════════════════════════════════════════════════════════════════
LOCAL_CHECKER = sreg.get("local").checker
NAS_CHECKER = sreg.get("nas").checker


def _set_pending(entries):
    with open(scfg2.pending_file, "w") as f:
        json.dump(entries, f)


def _read_pending():
    if not os.path.exists(scfg2.pending_file):
        return []
    with open(scfg2.pending_file) as f:
        return json.load(f)


def _pending_keys():
    return sorted((u.get("host", "local"), u["name"]) for u in _read_pending())


BOTH_WEB = [{"name": "web", "image": "nginx:2", "host": "local"},
            {"name": "web", "image": "nginx:2", "host": "nas"},
            {"name": "plex", "image": "plex:3", "host": "nas"}]

# ── /update ──────────────────────────────────────────────────────────
_set_pending(BOTH_WEB)
engine.batches.clear()
out = call("update", container="web")
checks["/update runs the container through the shared batch engine"] = (
    len(engine.batches) == 1 and engine.batches[0][0] == ["web"])
checks["/update is a manual update, never the auto path"] = (
    engine.batches[0][2] is False)
checks["/update with no host acts on the LOCAL host only"] = (
    engine.batches[0][1] is LOCAL_CHECKER)
checks["/update drops just that host's pending entry"] = (
    _pending_keys() == [("nas", "plex"), ("nas", "web")])
checks["/update reports the result"] = "updated" in out

engine.batches.clear()
out = call("update", container="web", host="nas")
checks["/update host:nas updates there"] = (
    len(engine.batches) == 1 and engine.batches[0][1] is NAS_CHECKER)
checks["…and only that entry leaves the pending file"] = (
    _pending_keys() == [("nas", "plex")])

# a container with nothing pending is told so, not silently ignored
engine.batches.clear()
out = call("update", container="db")
checks["/update of a container with no pending update says so"] = (
    "No pending updates" in out or "not in that list" in out)
checks["…and runs nothing"] = engine.batches == []

# unknown host → nothing runs, nothing is written
_set_pending(BOTH_WEB)
engine.batches.clear()
out = call("update", container="web", host="zzz")
checks["/update on an unknown host errors"] = "Unknown host" in out
checks["…and starts no update"] = engine.batches == []
checks["…and leaves the pending file untouched"] = (
    _pending_keys() == [("local", "web"), ("nas", "plex"), ("nas", "web")])

# the lock is held → refuse, do not queue, do not wait
engine._update_lock.acquire()
engine.batches.clear()
out = call("update", container="web")
checks["/update refuses while the shared lock is held"] = (
    "already running" in out)
checks["…and starts nothing"] = engine.batches == []
checks["…and the pending file is untouched"] = (
    _pending_keys() == [("local", "web"), ("nas", "plex"), ("nas", "web")])
engine._update_lock.release()
checks["…and the refusal did not swallow the lock"] = (
    engine._update_lock.acquire(blocking=False))
engine._update_lock.release()

# ── /updateall: confirmation required ────────────────────────────────
engine.batches.clear()
reply = call("updateall")
checks["/updateall does not act on the first invocation"] = engine.batches == []
checks["…it hands back a button"] = bool(getattr(reply, "components", None))
checks["…and previews what it would do"] = "`web`" in reply

row = reply.components[0]
checks["the button lives in an action row"] = row["type"] == 1
confirm_id = row["components"][0]["custom_id"]
cancel_id = row["components"][1]["custom_id"]
checks["the confirm button is styled as dangerous"] = (
    row["components"][0]["style"] == 4)
checks["custom_id is namespaced to this bot"] = confirm_id.startswith(
    "ds:updateall:")
checks["custom_id fits Discord's 100-char limit"] = len(confirm_id) <= 100

# it must NOT have offered the nas entries — writes stay local
checks["/updateall with no host previews only the local host"] = (
    "@nas" not in reply)

# the press is what acts
out = press(confirm_id)
checks["the button press runs the update"] = (
    len(engine.batches) == 1 and engine.batches[0][0] == ["web"])
checks["…on the local host only"] = engine.batches[0][1] is LOCAL_CHECKER
checks["…clearing only the local pending entry"] = (
    _pending_keys() == [("nas", "plex"), ("nas", "web")])

# and only once
engine.batches.clear()
out = press(confirm_id)
checks["a spent confirmation cannot be pressed again"] = (
    "expired or was already used" in out)
checks["…and nothing runs the second time"] = engine.batches == []

# cancel
_set_pending(BOTH_WEB)
reply = call("updateall")
cancel_id = reply.components[0]["components"][1]["custom_id"]
engine.batches.clear()
out = press(cancel_id)
checks["Cancel changes nothing"] = (
    "Cancelled" in out and engine.batches == []
    and len(_read_pending()) == 3)
checks["…and burns the token with it"] = "expired or was already used" in \
    press(reply.components[0]["components"][0]["custom_id"])

# a custom_id we never issued is not ours to answer
checks["an unknown token is refused"] = "expired or was already used" in \
    press("ds:updateall:deadbeef")
checks["a foreign custom_id is not claimed"] = (
    "don't recognise" in press("other-bot:go"))
checks["…and the action in the custom_id has to match the record"] = (
    "expired or was already used" in
    press("ds:stop:" + call("updateall").components[0]["components"][0][
        "custom_id"].split(":")[2]))

# unknown host on /updateall: no confirmation is even created
before_tokens = len(sbot._confirmations)
out = call("updateall", host="zzz")
checks["/updateall on an unknown host errors"] = "Unknown host" in out
checks["…and offers no button at all"] = not getattr(out, "components", None)
checks["…and parks no confirmation"] = len(sbot._confirmations) == before_tokens

# confirming while the lock is held: refuse, run nothing
_set_pending(BOTH_WEB)
reply = call("updateall")
confirm_id = reply.components[0]["components"][0]["custom_id"]
engine.batches.clear()
engine._update_lock.acquire()
out = press(confirm_id)
checks["a confirmed /updateall still refuses while the lock is held"] = (
    "already running" in out)
checks["…and updates nothing"] = engine.batches == []
engine._update_lock.release()

# the confirmation is bound to the person who asked
who = {"member": {"user": {"id": "111"}}, "data": {"name": "updateall",
                                                   "options": []}}
mine = sbot._dispatch(who)
mine_id = mine.components[0]["components"][0]["custom_id"]
engine.batches.clear()
out = sbot._on_component(mine_id, {"member": {"user": {"id": "222"}}})
checks["someone else cannot press my confirmation"] = "Only the person" in out
checks["…and it runs nothing"] = engine.batches == []
out = sbot._on_component(mine_id, {"member": {"user": {"id": "111"}}})
checks["…while the asker still can"] = len(engine.batches) == 1

# an expired confirmation is dead even if the button survives
_set_pending(BOTH_WEB)
reply = call("updateall")
stale_id = reply.components[0]["components"][0]["custom_id"]
stale_tok = stale_id.split(":")[2]
sbot._confirmations[stale_tok]["created"] -= 3600
engine.batches.clear()
out = press(stale_id)
checks["an expired confirmation is refused"] = "expired" in out
checks["…and an expired one runs nothing"] = engine.batches == []

# ── /stop, and stop-protection ───────────────────────────────────────
LOCAL_CHECKER.stopped.clear()
NAS_CHECKER.stopped.clear()
reply = call("stop", container="db")
checks["/stop asks before it acts"] = bool(getattr(reply, "components", None))
checks["…and stops nothing yet"] = LOCAL_CHECKER.stopped == []
checks["…and names what it would stop"] = "`db`" in reply
stop_id = reply.components[0]["components"][0]["custom_id"]
checks["the stop button carries the stop action"] = stop_id.startswith("ds:stop:")
out = press(stop_id)
checks["the press stops it"] = LOCAL_CHECKER.stopped == ["db"]
checks["…and confirms it"] = "Stopped `db`" in out

# writes stay local: `web` exists on both hosts
LOCAL_CHECKER.stopped.clear()
NAS_CHECKER.stopped.clear()
reply = call("stop", container="web")
press(reply.components[0]["components"][0]["custom_id"])
checks["/stop with no host stops only the local container"] = (
    LOCAL_CHECKER.stopped == ["web"] and NAS_CHECKER.stopped == [])
LOCAL_CHECKER.stopped.clear()
reply = call("stop", container="plex", host="nas")
press(reply.components[0]["components"][0]["custom_id"])
checks["/stop host:nas stops there"] = (
    NAS_CHECKER.stopped == ["plex"] and LOCAL_CHECKER.stopped == [])
NAS_CHECKER.stopped.clear()

# unknown host: no button, nothing stopped
before_tokens = len(sbot._confirmations)
out = call("stop", container="web", host="zzz")
checks["/stop on an unknown host errors"] = "Unknown host" in out
checks["…and offers no confirmation"] = (
    not getattr(out, "components", None)
    and len(sbot._confirmations) == before_tokens)
checks["…and stops nothing"] = (
    LOCAL_CHECKER.stopped == [] and NAS_CHECKER.stopped == [])

# stop-protection is honoured, and refused BEFORE the button
LOCAL_STORE.toggle_protect_stop("db")
out = call("stop", container="db")
checks["/stop refuses a protected container"] = "protected" in out
checks["…without even offering the button"] = not getattr(out, "components", None)
checks["…and a protected container is not stopped"] = LOCAL_CHECKER.stopped == []
# and again at confirm time, in case the flag was set while we asked
reply = call("stop", container="web")
LOCAL_STORE.toggle_protect_stop("web")
out = press(reply.components[0]["components"][0]["custom_id"])
checks["protection set after the question is still honoured"] = (
    "protected" in out and LOCAL_CHECKER.stopped == [])
LOCAL_STORE.toggle_protect_stop("web")
LOCAL_STORE.toggle_protect_stop("db")

# a `docksentry.protect` label wins over the stored toggle, as elsewhere
LOCAL_CHECKER.labels["db"] = {"protect": True}
out = call("stop", container="db")
checks["a protect LABEL refuses the stop too"] = "protected" in out
LOCAL_CHECKER.labels.pop("db")

# ── /start and /restart act straight away ────────────────────────────
lbackend = sreg.get("local").backend
lbackend.calls.clear()
out = call("restart", container="web")
checks["/restart restarts"] = "Restarted `web`" in out
checks["…with a graceful timeout"] = ["restart", "--time", "30", "web"] in \
    lbackend.calls
out = call("start", container="db")
checks["/start starts"] = "Started `db`" in out and ["start", "db"] in \
    lbackend.calls
nbackend = sreg.get("nas").backend
nbackend.calls.clear()
lbackend.calls.clear()
out = call("restart", container="web", host="nas")
checks["/restart host:nas acts there only"] = (
    any(c[0] == "restart" for c in nbackend.calls)
    and not any(c[0] == "restart" for c in lbackend.calls))
out = call("restart", container="web", host="zzz")
checks["/restart on an unknown host errors"] = "Unknown host" in out
checks["…and touches no container"] = not any(
    c[0] == "restart" for c in lbackend.calls)

# no lifecycle action while an update runs
lbackend.calls.clear()
engine.update_running = True
out = call("restart", container="web")
checks["/restart is refused while an update runs"] = "refused" in out
checks["…and issues no command"] = not any(c[0] == "restart"
                                           for c in lbackend.calls)
out = call("start", container="web")
checks["/start is refused too"] = "refused" in out
engine.update_running = False

# never stop or restart Docksentry itself
LOCAL_CHECKER.self_named = "web"
lbackend.calls.clear()
out = call("restart", container="web")
checks["/restart refuses to restart Docksentry itself"] = (
    "Docksentry itself" in out and not any(c[0] == "restart"
                                           for c in lbackend.calls))
reply = call("stop", container="web")
out = press(reply.components[0]["components"][0]["custom_id"])
checks["/stop refuses to stop Docksentry itself"] = (
    "Docksentry itself" in out and LOCAL_CHECKER.stopped == [])
LOCAL_CHECKER.self_named = None

# ── /cleanup ─────────────────────────────────────────────────────────
LOCAL_CHECKER.cleanups = 0
out = call("cleanup")
checks["/cleanup runs the prune"] = LOCAL_CHECKER.cleanups == 1
checks["…and reports what it freed"] = "1.2 GB" in out
engine._update_lock.acquire()
out = call("cleanup")
checks["/cleanup skips while the update lock is held"] = "skipped" in out
checks["…and prunes nothing"] = LOCAL_CHECKER.cleanups == 1
engine._update_lock.release()
checks["…and left the lock alone"] = engine._update_lock.acquire(blocking=False)
engine._update_lock.release()

# cleanup is a write: it stays on the local host
NAS_CHECKER.cleanups = 0
call("cleanup")
checks["/cleanup never touches another host"] = NAS_CHECKER.cleanups == 0

# ── /checkimages ─────────────────────────────────────────────────────
LOCAL_CHECKER.reclaim = 3 * 1024 ** 3
NAS_CHECKER.reclaim = 400 * 1024 ** 2
out = call("checkimages")
checks["/checkimages is a read — it spans every host"] = (
    "3.0 GB" in out and "400 MB" in out)
checks["…and points at /cleanup when there is something to free"] = (
    "/cleanup" in out)
out = call("checkimages", host="nas")
checks["/checkimages host:nas answers for that host"] = (
    "400 MB" in out and "3.0 GB" not in out)
LOCAL_CHECKER.reclaim = 0
NAS_CHECKER.reclaim = 0
out = call("checkimages")
checks["/checkimages says so when there is nothing to reclaim"] = (
    "nothing to reclaim" in out)
out = call("checkimages", host="zzz")
checks["/checkimages on an unknown host errors"] = "Unknown host" in out

# ── the 15-minute interaction token ──────────────────────────────────
# A deferred token is dead after 15 minutes and an "update all" over a
# dozen containers can outlive it. The result must go somewhere.
dbot = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                  checker=LOCAL_CHECKER, log=lambda *_: None)
dbot.rest = FakeREST()
inter = {"id": "9", "token": "tk", "channel_id": "chan",
         "member": {"user": {"id": "42"}}, "data": {"name": "hosts"}}

dbot._deliver(inter, dbot._now(), "fresh answer")
checks["a fresh interaction is answered by editing it"] = (
    dbot.rest.calls[-1][0] == "edit")

dbot.rest.calls.clear()
dbot._deliver(inter, dbot._now() - 20 * 60, "late answer")
checks["an answer past the 15-minute window is not edited"] = not any(
    c[0] == "edit" for c in dbot.rest.calls)
checks["…it is posted into the channel instead"] = (
    dbot.rest.calls[-1][0] == "channel" and dbot.rest.calls[-1][1] == "chan")
checks["…addressed to whoever asked"] = "<@42>" in dbot.rest.calls[-1][2]
checks["…carrying the result itself"] = "late answer" in dbot.rest.calls[-1][2]
checks["…and explaining why it is not ephemeral"] = (
    "15-minute" in dbot.rest.calls[-1][2])

# an edit that fails inside the window falls back the same way
dbot.rest.calls.clear()
dbot.rest.edit_fails = True
dbot._deliver(inter, dbot._now(), "rejected answer")
checks["a rejected edit falls back to the channel"] = (
    dbot.rest.calls[-1][0] == "channel"
    and "rejected answer" in dbot.rest.calls[-1][2])
dbot.rest.edit_fails = False

# with nowhere to post, the answer is logged rather than lost silently
logged = []
dbot2 = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                   checker=LOCAL_CHECKER, log=logged.append)
dbot2.rest = FakeREST()
dbot2._deliver({"id": "9", "token": "tk", "data": {"name": "hosts"}},
               dbot2._now() - 20 * 60, "orphan answer")
checks["an answer with no channel to fall back to is logged"] = any(
    "orphan answer" in m for m in logged)

# the channel fallback is clipped like everything else
dbot.rest.calls.clear()
dbot._deliver(inter, dbot._now() - 20 * 60, "x" * 5000)
checks["the channel fallback respects the 2000-char limit"] = (
    len(dbot.rest.calls[-1][2]) <= 2000)

# a slow command says so up front, before the slow part
dbot.rest.calls.clear()
dbot._run_command({"id": "9", "token": "tk", "channel_id": "chan",
                   "data": {"name": "cleanup", "options": []}}, dbot._now())
checks["a slow command warns before it starts"] = (
    dbot.rest.calls[0][0] == "edit" and "15-minute" in dbot.rest.calls[0][1])
checks["…and still delivers the real answer afterwards"] = (
    dbot.rest.calls[-1][0] == "edit" and "15-minute" not in dbot.rest.calls[-1][1])

# ── button presses are acknowledged inside the three seconds ─────────
abot = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                  checker=LOCAL_CHECKER, log=lambda *_: None)
abot.rest = FakeREST()
abot._on_event("INTERACTION_CREATE",
               {"guild_id": "g", "type": 3, "id": "5", "token": "tk", "channel_id": "c",
                "data": {"custom_id": "ds:cancel:nope"}})
acks = [c for c in abot.rest.calls if c[0] == "ack"]
checks["a button press is acknowledged immediately"] = len(acks) == 1
checks["…deferred, like a command"] = acks[0][2] is True

abot.rest.calls.clear()
abot._on_event("INTERACTION_CREATE",
               {"guild_id": "g", "type": 3, "id": "6", "token": "tk",
                "data": {"custom_id": "someone-elses-button"}})
checks["another bot's button is left alone"] = abot.rest.calls == []
abot._on_event("INTERACTION_CREATE",
               {"type": 4, "id": "7", "token": "tk", "data": {}})
checks["an interaction type we don't handle is ignored"] = abot.rest.calls == []

# releasing the shared lock hands a queued self-update on
handoffs = []
# The handoff goes through the shared self-update machine now, not
# through the Telegram bot (#63) — so that is what stands in for it.
import selfupdate  # noqa: E402
selfupdate.run_queued = lambda ctx: handoffs.append(1)
qbot = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                  checker=LOCAL_CHECKER, log=lambda *_: None,
                  selfupdate_ctx=object())
_set_pending([{"name": "web", "image": "nginx:2", "host": "local"}])
qbot._dispatch({"data": {"name": "update",
                         "options": [{"name": "container", "value": "web"}]}})
checks["a queued self-update is handed on after the lock is released"] = (
    handoffs == [1])
checks["…and the lock really was released"] = engine._update_lock.acquire(
    blocking=False)
engine._update_lock.release()

# ═════════════════════════════════════════════════════════════════════
# The interaction worker pool.
#
# One thread per interaction with no cap turns a burst of slash commands
# into a burst of concurrent container-CLI subprocesses — on a small box
# that is the whole machine. The pool is bounded, and an interaction that
# finds no slot still gets an answer: silence is indistinguishable from a
# broken bot, and the user has no way to know their /update did nothing.
# ═════════════════════════════════════════════════════════════════════
from discord_bot import MAX_COMMAND_WORKERS   # noqa: E402

checks["the worker pool has a small, positive bound"] = (
    isinstance(MAX_COMMAND_WORKERS, int) and 0 < MAX_COMMAND_WORKERS <= 16)

pbot = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                  checker=LOCAL_CHECKER, log=lambda *_: None)
pbot.rest = FakeREST()
_hold = threading.Event()
for _ in range(MAX_COMMAND_WORKERS):
    pbot._start_worker(lambda: _hold.wait(20))
checks["the pool refuses work once every slot is taken"] = (
    pbot._start_worker(lambda: None) is None)

pbot._on_event("INTERACTION_CREATE",
               {"guild_id": "g", "type": 2, "id": "flood", "token": "tk",
                "channel_id": "chan", "member": {"user": {"id": ASKER}},
                "data": {"name": "hosts", "options": []}})
_over = _await_calls(pbot.rest, "ack")
checks["an over-limit interaction is answered, not silently dropped"] = (
    len(_over) == 1 and "try again" in (_over[0][3] or ""))
checks["…and told plainly that nothing was started"] = (
    "Nothing was started" in (_over[0][3] or ""))
checks["…with an immediate reply, not a deferred promise it can't keep"] = (
    _over[0][2] is False)
checks["…and no command ran"] = not any(c[0] == "edit" for c in pbot.rest.calls)

_hold.set()
_freed = None
for _ in range(300):
    _freed = pbot._start_worker(lambda: None)
    if _freed is not None:
        break
    time.sleep(0.02)
checks["a finished worker hands its slot back"] = _freed is not None

# ── shutdown waits for work that is already running ──────────────────
# Every worker is a daemon thread and nothing joined them, so a SIGTERM
# during a Discord-triggered /update killed the process between
# `docker stop` and `docker run` — the container just stayed down.
wbot = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                  checker=LOCAL_CHECKER, log=lambda *_: None)
_finished = []


def _slow_command():
    time.sleep(0.4)
    _finished.append(True)


wbot._start_worker(_slow_command)
_t0 = time.monotonic()
wbot.stop()
checks["stop() waits for a command that is still running"] = _finished == [True]
checks["…and really waited rather than getting lucky"] = (
    time.monotonic() - _t0 >= 0.35)

# …but not forever. The runtime's kill timer is not negotiable, so a
# shutdown that hangs on one wedged worker is its own failure.
_stuck = threading.Event()
wbot._start_worker(lambda: _stuck.wait(30))
_t0 = time.monotonic()
wbot.stop(timeout=0.3)
checks["…while the wait is still bounded"] = time.monotonic() - _t0 < 5.0
_stuck.set()


# ═════════════════════════════════════════════════════════════════════
# An ephemeral answer stays ephemeral.
#
# Replies are private because their contents are nobody else's business —
# a /logs tail, a container list naming internal services. Falling back to
# a public channel message on ANY edit failure meant a transient Discord
# 500 published that to everyone. Only a genuinely dead token justifies
# it, because losing the result of a twenty-minute update is worse.
# ═════════════════════════════════════════════════════════════════════
_leaklog = []
lbot = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                  checker=LOCAL_CHECKER, log=_leaklog.append)
lbot.rest = FakeREST()
leak_inter = {"id": "9", "token": "tk", "channel_id": "chan",
              "member": {"user": {"id": "42"}}, "data": {"name": "logs"}}

lbot.rest.edit_fails = True
lbot.rest.edit_error = (500, "Internal Server Error")
lbot._deliver(leak_inter, lbot._now(), "PRIVATE log tail")
checks["a transient 500 does not publish a private answer"] = not any(
    c[0] == "channel" for c in lbot.rest.calls)
checks["…and it is logged rather than lost without trace"] = any(
    "could not deliver" in m for m in _leaklog)

lbot.rest.calls.clear()
lbot.rest.edit_error = (429, "rate limited")
lbot._deliver(leak_inter, lbot._now(), "PRIVATE rate-limited answer")
checks["a 429 does not publish one either"] = not any(
    c[0] == "channel" for c in lbot.rest.calls)

# An expired token is the one case that does fall back — that is what the
# whole channel-fallback path exists for.
lbot.rest.calls.clear()
lbot.rest.edit_error = (401, "Invalid Webhook Token")
lbot._deliver(leak_inter, lbot._now(), "the update result")
checks["an expired token still falls back to the channel"] = (
    lbot.rest.calls[-1][0] == "channel"
    and "the update result" in lbot.rest.calls[-1][2])
lbot.rest.calls.clear()
lbot.rest.edit_error = (404, '{"code": 10015, "message": "Unknown Webhook"}')
lbot._deliver(leak_inter, lbot._now(), "the other update result")
checks["…and so does a webhook Discord no longer knows"] = (
    lbot.rest.calls[-1][0] == "channel")
# A 404 that is NOT the webhook being gone is our own bug, not an expiry.
lbot.rest.calls.clear()
lbot.rest.edit_error = (404, '{"code": 10002, "message": "Unknown Application"}')
lbot._deliver(leak_inter, lbot._now(), "PRIVATE misrouted answer")
checks["a 404 for anything else is not treated as an expiry"] = not any(
    c[0] == "channel" for c in lbot.rest.calls)
lbot.rest.edit_fails = False


# ═════════════════════════════════════════════════════════════════════
# /updateall runs what was approved.
#
# The prompt lists N containers; the press used to re-read the pending
# file and update whatever was in it by then. A scheduled check landing in
# between meant the user approved 3 and 9 happened.
# ═════════════════════════════════════════════════════════════════════
_set_pending([{"name": "web", "image": "nginx:2", "host": "local"}])
engine.batches.clear()
reply = call("updateall")
_uid = reply.components[0]["components"][0]["custom_id"]
# …and now a scheduled check finds another one, while the question sits
# there unanswered.
_set_pending([{"name": "web", "image": "nginx:2", "host": "local"},
              {"name": "db", "image": "pg:16", "host": "local"}])
out = press(_uid)
checks["/updateall updates only the containers it previewed"] = (
    len(engine.batches) == 1 and engine.batches[0][0] == ["web"])
checks["…leaving what turned up afterwards pending"] = (
    _pending_keys() == [("local", "db")])
checks["…and saying that it did not touch them"] = "appeared after" in out

# The other direction: something on the approved list is already gone by
# the time the button is pressed. Update the rest, and say the set shrank.
_set_pending([{"name": "web", "image": "nginx:2", "host": "local"},
              {"name": "db", "image": "pg:16", "host": "local"}])
engine.batches.clear()
reply = call("updateall")
_uid = reply.components[0]["components"][0]["custom_id"]
_set_pending([{"name": "web", "image": "nginx:2", "host": "local"}])
out = press(_uid)
checks["/updateall says so when the approved set shrank"] = (
    "no longer pending" in out)
checks["…and updates what is left of it"] = (
    len(engine.batches) == 1 and engine.batches[0][0] == ["web"])

# …and when nothing at all is left, that is not "no pending updates".
_set_pending([{"name": "web", "image": "nginx:2", "host": "local"}])
reply = call("updateall")
_uid = reply.components[0]["components"][0]["custom_id"]
_set_pending([{"name": "db", "image": "pg:16", "host": "local"}])
engine.batches.clear()
out = press(_uid)
checks["/updateall with nothing approved left runs nothing"] = engine.batches == []
checks["…and explains that, rather than claiming nothing was pending"] = (
    "Nothing left to update" in out)
checks["…and leaves the unapproved entry alone"] = (
    _pending_keys() == [("local", "db")])


# ═════════════════════════════════════════════════════════════════════
# The confirmation ownership check fails CLOSED.
#
# `if rec["user"] and who and who != rec["user"]` skipped the check
# entirely whenever either id was missing — which is exactly the press
# that must not be allowed to stop a database.
# ═════════════════════════════════════════════════════════════════════
_set_pending(BOTH_WEB)
engine.batches.clear()
reply = call("updateall")
_aid = reply.components[0]["components"][0]["custom_id"]
out = press(_aid, user=None)
checks["a press we cannot attribute to anyone is refused"] = (
    "Only the person" in out)
checks["…and an unattributable press runs nothing"] = engine.batches == []
checks["…while a failed identity check leaves the button pressable"] = (
    "Update all" in press(_aid))

# A confirmation whose ASKER could not be identified is dead the same way:
# nobody can prove they are the person who asked for it.
_set_pending(BOTH_WEB)
engine.batches.clear()
anon = sbot._dispatch({"data": {"name": "updateall", "options": []}})
_anon_id = anon.components[0]["components"][0]["custom_id"]
out = press(_anon_id)
checks["a confirmation with no identified owner is not pressable"] = (
    "Only the person" in out)
checks["…and it too runs nothing"] = engine.batches == []

if os.path.exists(scfg2.pending_file):
    os.remove(scfg2.pending_file)
engine.batches.clear()

# ── every new command answers on a single-host install without a marker
one = _state_host("local", ["web"], local=True)
onereg = FakeRegistry([one])
onebot = DiscordBot(scfg2, real_store, engine, hosts=onereg,
                    checker=one.checker, log=lambda *_: None)


def call1(name, **opts):
    return onebot._dispatch({"data": {
        "name": name,
        "options": [{"name": k, "value": v} for k, v in opts.items()]}})


single_out = {
    "pin": call1("pin", container="web"),
    "unpin": call1("unpin", container="web"),
    "autoupdate": call1("autoupdate", container="web"),
    "protect": call1("protect", container="web"),
    "cooldown": call1("cooldown", container="web", seconds=5),
    "groups": call1("groups"),
    "logs": call1("logs", container="web"),
}
checks["single host: no command emits a host marker"] = not any(
    "@" in v for v in single_out.values())
checks["single host: state still lands in the store"] = (
    real_store.get_cooldown("web") == 5)
# The raw store is what a single-host install has always written to, so
# the key must stay unprefixed — a `local/web` key would be a migration.
checks["single host: keys stay unprefixed"] = "web" in real_store.get_cooldowns()
call1("cooldown", container="web", seconds=0)
call1("autoupdate", container="web")
call1("protect", container="web")

# ── nothing dispatches to a command the spec doesn't list ────────────
checks["every COMMANDS entry has a handler"] = all(
    "Unknown command" not in str(
        sbot._dispatch({"data": {"name": c["name"], "options": []}}))
    for c in COMMANDS)
checks["an unknown command still says so"] = "Unknown command" in sbot._dispatch(
    {"data": {"name": "definitely-not-a-command", "options": []}})

# ── every reply respects the 2000-character ceiling ──────────────────
_replies = [call(c["name"], **({"container": "web"}
                               if any(o["name"] == "container" and o.get("required")
                                      for o in c.get("options", []))
                               else {}),
                 **({"seconds": 1} if c["name"] == "cooldown" else {}))
            for c in COMMANDS if c["name"] not in ("check", "updates")]
checks["no reply exceeds Discord's 2000-character limit"] = all(
    len(r) <= 2000 for r in _replies)

shutil.rmtree(TMP, ignore_errors=True)


# ── authorisation: the hole the audit found ──────────────────────────
# The Telegram front-end has always checked CHAT_ID plus an optional user
# list. The Discord front-end drives the SAME engine and shipped with no
# check at all: any member of the server could /stop a database or /logs
# a container full of secrets. These assertions exist so that can never
# be true again.
import types as _t
from discord_bot import _harden_commands, DEFAULT_PERMISSIONS   # noqa: E402


def _auth_bot(guild="g", allowed=None, debug=False):
    cfg = _t.SimpleNamespace(discord_bot_token="t", discord_app_id="app",
                             discord_guild_id=guild, pending_file="",
                             discord_allowed_users=allowed or [], debug=debug)
    b = DiscordBot(cfg, None, _t.SimpleNamespace(update_running=False),
                   log=lambda *_: None)
    b.rest = FakeREST()
    return b


def _interaction(guild="g", user="u1"):
    return {"guild_id": guild, "type": 2, "id": "i", "token": "tk",
            "member": {"user": {"id": user}},
            "data": {"name": "hosts"}}


ab = _auth_bot()
checks["auth: an interaction from the configured guild passes"] = \
    ab._authorized(_interaction()) is True
checks["auth: another guild is refused"] = \
    ab._authorized(_interaction(guild="other")) is False
checks["auth: a missing guild (DM) is refused"] = \
    ab._authorized({"type": 2, "data": {}}) is False

# No guild configured at all → refuse everything. Fail-closed, because an
# unset guild means the commands were registered globally and a Public
# app could be invited to a stranger's server.
nb = _auth_bot(guild="")
checks["auth: no DISCORD_GUILD_ID → nothing is authorised"] = \
    nb._authorized(_interaction()) is False
checks["auth: …and the bot refuses to start at all"] = nb.start() is False

# Optional user allow-list, on top of the guild.
lb = _auth_bot(allowed=["u1", "u2"])
checks["auth: a listed user passes"] = lb._authorized(_interaction("g", "u1")) is True
checks["auth: an unlisted user is refused"] = \
    lb._authorized(_interaction("g", "u9")) is False
checks["auth: an unidentifiable user is refused when a list is set"] = \
    lb._authorized({"guild_id": "g", "type": 2, "data": {}}) is False
checks["auth: an empty list means anyone in the guild"] = \
    _auth_bot(allowed=[])._authorized(_interaction("g", "whoever")) is True

# An unauthorised interaction must not even be acknowledged — replying
# would confirm the bot is here and listening.
ub = _auth_bot()
ub._on_event("INTERACTION_CREATE", _interaction(guild="stranger"))
checks["auth: an unauthorised interaction gets no reply at all"] = ub.rest.calls == []

# Discord-side gating, as defence in depth.
hardened = _harden_commands(COMMANDS)
checks["auth: every command is admin-only by default"] = all(
    c["default_member_permissions"] == DEFAULT_PERMISSIONS for c in hardened)
checks["auth: no command is usable in a DM"] = all(
    c["dm_permission"] is False for c in hardened)
checks["auth: hardening does not mutate COMMANDS"] = all(
    "default_member_permissions" not in c for c in COMMANDS)


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
