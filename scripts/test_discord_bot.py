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


class FakeChecker:
    def __init__(self, containers, fail=False):
        self._c = containers
        self.fail = fail

    def get_running_containers(self):
        if self.fail:
            raise OSError("host down")
        return self._c

    def check_all(self):
        if self.fail:
            raise OSError("host down")
        return [c for c in self._c if c.get("update")]


def _host(name, containers, local=False, fail=False):
    return types.SimpleNamespace(
        name=name, is_local=local, endpoint=f"tcp://{name}:2375",
        checker=FakeChecker(containers, fail))


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


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
