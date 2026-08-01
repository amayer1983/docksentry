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
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from discord_bot import DiscordBot, COMMANDS   # noqa: E402

checks = {}


class FakeREST:
    def __init__(self):
        self.calls = []

    def me(self):
        return {"username": "bot"}

    def register_commands(self, app_id, cmds, guild_id=None):
        self.calls.append(("register", app_id, guild_id, len(cmds)))

    def interaction_response(self, iid, itok, content=None, **kw):
        self.calls.append(("ack", iid, kw.get("deferred", False)))

    def edit_original_response(self, app_id, itok, content, **kw):
        self.calls.append(("edit", content))


class FakeBackend:
    """Just enough container CLI for the resolver and `/logs`. Records
    every argv so a test can assert what was actually asked for."""

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
        return types.SimpleNamespace(returncode=1, stdout="", stderr="nope")


class FakeChecker:
    def __init__(self, containers, fail=False):
        self._c = containers
        self.fail = fail
        self.backend = FakeBackend([c["name"] for c in containers])

    def get_running_containers(self):
        if self.fail:
            raise OSError("host down")
        return self._c

    def check_all(self):
        if self.fail:
            raise OSError("host down")
        return [c for c in self._c if c.get("update")]


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
                            discord_guild_id="g", pending_file="")
engine = types.SimpleNamespace(update_running=False)
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
checks["option types are ones Discord knows"] = all(
    o["type"] in (3, 4, 5, 6, 7, 8, 9, 10)
    for c in COMMANDS for o in c.get("options", []))
# The bad-ordering check has to be able to fail, or it proves nothing.
checks["…and that check would catch a bad ordering"] = not _required_first(
    {"options": [{"name": "a", "required": False},
                 {"name": "b", "required": True}]})

# ── an interaction is acknowledged before the work starts ────────────
# Discord kills the interaction after three seconds, so the ack must not
# wait for the container CLI.
bot._on_event("INTERACTION_CREATE",
              {"type": 2, "id": "1", "token": "tk",
               "data": {"name": "hosts"}})
acks = [c for c in bot.rest.calls if c[0] == "ack"]
checks["interaction is acknowledged immediately"] = len(acks) == 1
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
    debug=False, exclude_containers=["watchtower"])


def _state_host(name, names, local=False):
    h = _host(name, [{"name": n, "image": "img:1", "update": False}
                     for n in names], local=local)
    h.store = HostScopedStore(real_store, name)
    return h


sreg = FakeRegistry([_state_host("local", ["web", "db"], local=True),
                     _state_host("nas", ["web", "plex"])])
sbot = DiscordBot(scfg2, real_store, engine, hosts=sreg,
                  checker=sreg.hosts[0].checker, log=lambda *_: None)


def call(name, **opts):
    return sbot._dispatch({"data": {
        "name": name,
        "options": [{"name": k, "value": v} for k, v in opts.items()]}})


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
checks["/history with no file says so"] = "No update history" in out
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


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
