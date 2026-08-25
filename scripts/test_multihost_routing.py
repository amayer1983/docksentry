#!/usr/bin/env python3
"""Multi-host command targeting in the Telegram bot (#7).

The rule under test:

  * read commands  (/check, /status, /updates) with no `@token` → EVERY host
  * write commands (/update, /start, /stop, /restart) with no `@token` → the
    LOCAL host only, plus a hint saying how to reach the others
  * `@<name>` narrows to that host, `@all` widens to all of them
  * an unknown host name is an error that executes nothing

…and, most importantly, that a single-host install (no registry at all, or a
registry holding only the local host) behaves EXACTLY as it did before #7:
same calls, same replies, no host suffixes, no extra lines.

No Telegram, no Docker, no network — the backends and checkers are fakes and
only their calls are inspected. Exits non-zero on any failure.
"""
import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from hosts import HostRegistry, ManagedHost          # noqa: E402
from telegram_bot import TelegramBot                 # noqa: E402

checks = {}


class _CP:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _inspect_json(name):
    return {
        "Name": "/" + name,
        "Config": {"Image": f"reg/{name}:1",
                   "Labels": {"org.opencontainers.image.version": "1.0.0"}},
        "State": {"Status": "running", "Running": True, "StartedAt": "",
                  "Health": {"Status": "healthy"}},
        "HostConfig": {"PortBindings": {}, "RestartPolicy": {"Name": "no"}},
        "Mounts": [],
        "Image": "sha256:" + "a" * 64,
    }


class FakeBackend:
    """Answers `ps` / `inspect` / lifecycle for a fixed container list."""

    def __init__(self, name, names):
        self.name = name
        self.names = list(names)
        self.calls = []

    def run(self, args, **kw):
        a = list(args)
        self.calls.append(a)
        if a[:2] == ["ps", "-q"]:
            return _CP("\n".join("id-" + n for n in self.names))
        if a[0] == "ps":
            return _CP("\n".join(self.names))
        if a[:2] == ["image", "inspect"]:
            return _CP("1.0.0\n")
        if a[0] == "inspect":
            refs = [x for x in a[1:] if x != "--format"]
            out = []
            for r in refs:
                n = r[3:] if r.startswith("id-") else r
                if n in self.names:
                    out.append(_inspect_json(n))
            return _CP(json.dumps(out))
        return _CP("")

    def lifecycle_calls(self):
        return [c for c in self.calls if c and c[0] in ("start", "stop", "restart")]


class DeadBackend:
    """A host that does not answer at all. `run` RAISES — the way a real
    unreachable host's `docker ps` runs into its timeout and raises,
    where an auth failure would merely return non-zero. This is the case
    that took `/status` down: the exception escaped the overview loop into
    the poll thread and the command produced nothing at all (srv20, #2)."""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def run(self, args, **kw):
        self.calls.append(list(args))
        import subprocess
        raise subprocess.TimeoutExpired(list(args), 30)

    def lifecycle_calls(self):
        return []


class FakeChecker:
    """Reports an update for every container in `outdated`."""

    def __init__(self, name, outdated=()):
        self.name = name
        self.outdated = set(outdated)
        self.check_calls = []
        self.stopped = []

    def check_all(self, bot=None, only=None):
        self.check_calls.append(sorted(only) if only else None)
        wanted = self.outdated if only is None else (self.outdated & set(only))
        return [{"name": n, "image": f"reg/{n}:1", "size": "10 MB",
                 "created": "2024", "host": self.name} for n in sorted(wanted)]

    def has_selfupdate_available(self):
        return False

    def _would_kill_self(self, name):
        return False

    def get_container_labels(self, name):
        return {}

    def label_bool(self, labels, key):
        return None

    def _stop_container(self, name):
        self.stopped.append(name)
        return True, ""


class FakeStore:
    def get_groups(self):
        return {}

    def is_protect_stop(self, name):
        return False

    def get_link(self, name):
        return ""

    def get_pinned(self):
        return []

    def get_autoupdate(self):
        return []


def make_host(name, names, outdated=(), is_local=False):
    backend = FakeBackend(name, names)
    checker = FakeChecker(name, outdated)
    return ManagedHost(name, backend, checker, FakeStore(),
                       endpoint="" if is_local else f"ssh://{name}",
                       is_local=is_local)


def make_bot(hosts=None):
    """A bot with `hosts` wired in. Returns (bot, local_checker, cfg)."""
    tmp = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(
        language="en", debug=False, container_cli="docker",
        pending_file=os.path.join(tmp, "pending.json"),
        history_file=os.path.join(tmp, "history.json"),
    )
    bot = TelegramBot(cfg, FakeStore(), hosts=hosts)
    local = hosts.local if hosts is not None else None
    bot.backend = local.backend if local is not None else FakeBackend("local", ["web", "db"])
    local_checker = local.checker if local is not None else FakeChecker("local", ["web"])
    bot.start_time = 0
    bot.bot_username = "dockbot"
    bot.sent = []
    bot.markups = []
    bot.send_message = lambda text, reply_markup=None, auto=False: (
        bot.sent.append(text), bot.markups.append(reply_markup))[0]
    bot.run_updates = lambda updater, updates=None: bot.sent.append(
        "RUN_UPDATES@%s:%s" % (updater.name, ",".join(u["name"] for u in (updates or []))))
    bot._enrich_with_source_url = lambda ups: None
    bot._check_auth = lambda *a, **k: True
    return bot, local_checker, cfg


def drive(bot, cmd, checker):
    """Run one command; return the list of reply texts."""
    bot.sent = []
    bot.markups = []
    bot._check_auth = lambda *a, **k: True
    bot.answer_callback = lambda *a: None
    bot.remove_buttons = lambda *a: None
    bot._handle_message({"text": cmd, "from": {"id": 1}, "chat": {"id": 1}},
                        checker, None)
    time.sleep(0.05)          # /update dispatches through a thread
    return list(bot.sent)


def confirm(bot, checker):
    """Press the confirm button on the question just asked.

    `/stop` asks first now, in both chats — so a routing test that only
    sends the command watches nothing happen. The token is re-derived on
    the press, which is exactly the routing this file cares about.
    """
    token = next((b["callback_data"]
                  for m in (bot.markups or []) if m
                  for row in m.get("inline_keyboard", [])
                  for b in row
                  if str(b.get("callback_data", "")).startswith("stop_go:")),
                 None)
    if token is None:
        return []
    bot.sent = []
    bot._handle_callback({"data": token, "id": "c", "from": {"id": 1},
                          "message": {"message_id": 1, "chat": {"id": 1}}},
                         checker)
    return list(bot.sent)


def multi_bot():
    """local(web, db) + nas(web, plex); `web` is outdated on both."""
    hosts = HostRegistry([
        make_host("local", ["web", "db"], outdated=["web"], is_local=True),
        make_host("nas", ["web", "plex"], outdated=["web", "plex"]),
    ])
    bot, checker, cfg = make_bot(hosts)
    return bot, checker, cfg, hosts


# ── 1. single host: identical to pre-#7, whether or not a registry exists ──
CMDS = ["/status", "/status web", "/status we*", "/status nope", "/status zz*",
        "/check", "/check web", "/check nope", "/check zz*",
        "/update", "/update web", "/update nope", "/update zz*",
        "/updates", "/restart web", "/stop web", "/restart we*",
        "/restart nope", "/restart zz*", "/help"]

no_reg_bot, no_reg_checker, _ = make_bot(None)
one_host = HostRegistry([make_host("local", ["web", "db"],
                                   outdated=["web"], is_local=True)])
one_reg_bot, one_reg_checker, _ = make_bot(one_host)

no_reg_out, one_reg_out = {}, {}
for cmd in CMDS:
    no_reg_out[cmd] = drive(no_reg_bot, cmd, no_reg_checker)
    one_reg_out[cmd] = drive(one_reg_bot, cmd, one_reg_checker)

checks["single host: hosts=None and a one-host registry reply identically"] = (
    no_reg_out == one_reg_out)
checks["single host: no `@host` marker anywhere"] = not any(
    " @" in t for texts in no_reg_out.values() for t in texts)
checks["single host: no local-only hint anywhere"] = not any(
    "Local host only" in t for texts in no_reg_out.values() for t in texts)
checks["single host: /status is the plain overview"] = (
    "🟢 `web`\n     ⏱ ? · 📦 `reg/web:1`" in no_reg_out["/status"][0]
    and "🟢 `db`\n     ⏱ ? · 📦 `reg/db:1`" in no_reg_out["/status"][0])
checks["single host: /update <name> unchanged"] = no_reg_out["/update web"] == [
    "🔍 Checking for updates...",
    "⏳ Updating 1 container(s) matching `web`…",
    "RUN_UPDATES@local:web"]
checks["single host: /help has no multi-host block"] = (
    "Several hosts" not in no_reg_out["/help"][0])
checks["single host: unknown container answers as before"] = (
    no_reg_out["/check nope"] == ["Container `nope` not found."])
checks["single host: a glob matching nothing answers as before"] = all(
    no_reg_out[c] == ["⚠️ No containers match `zz*`."]
    for c in ("/status zz*", "/check zz*", "/update zz*", "/restart zz*"))
checks["single host: `@` argument is not treated as a host"] = (
    drive(no_reg_bot, "/check @nas", no_reg_checker)
    == ["Container `@nas` not found."])

# ── 2. reads with no target → every host ──────────────────────────────
bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/check", checker)
checks["read /check: both hosts checked"] = (
    hosts.hosts[0].checker.check_calls == [None]
    and hosts.hosts[1].checker.check_calls == [None])
checks["read /check: remote results carry @nas"] = any(
    "`web` @nas" in t for t in out)
checks["read /check: local results carry no marker"] = any(
    "• `web` (reg/web:1)" in t for t in out)

bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/status", checker)
checks["read /status: containers from both hosts"] = (
    "`web`\n" in out[0] and "`plex` @nas" in out[0] and "`db`\n" in out[0])
checks["read /status: 4 containers total"] = "📊 *4*" in out[0]

bot, checker, cfg, hosts = multi_bot()
with open(cfg.pending_file, "w") as f:
    json.dump([{"name": "web", "image": "reg/web:1", "host": "local"},
               {"name": "plex", "image": "reg/plex:1", "host": "nas"}], f)
out = drive(bot, "/updates", checker)
checks["read /updates: both hosts listed"] = (
    "• `web`" in out[0] and "• `plex` @nas" in out[0])
out = drive(bot, "/updates @nas", checker)
checks["read /updates @nas: only that host"] = (
    "`plex` @nas" in out[0] and "`web`" not in out[0])

# ── 3. writes with no target → local only, and they say so ────────────
bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/update web", checker)
checks["write /update: only the local host checked"] = (
    hosts.hosts[0].checker.check_calls == [["web"]]
    and hosts.hosts[1].checker.check_calls == [])
checks["write /update: only the local host updated"] = (
    "RUN_UPDATES@local:web" in out and not any("@nas:" in t for t in out))
checks["write /update: reply carries the local-only hint"] = any(
    "Local host only" in t and "`@all`" in t for t in out)

bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/stop web", checker) + confirm(bot, checker)
checks["write /stop: acted on the local host only"] = (
    hosts.hosts[0].checker.stopped == ["web"]
    and hosts.hosts[1].checker.stopped == [])
checks["write /stop: reply carries the local-only hint"] = any(
    "Local host only" in t for t in out)

# ── 4. `@host` narrows ────────────────────────────────────────────────
bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/update web @nas", checker)
checks["@nas: only the named host was checked"] = (
    hosts.hosts[1].checker.check_calls == [["web"]]
    and hosts.hosts[0].checker.check_calls == [])
checks["@nas: the named host was updated"] = "RUN_UPDATES@nas:web" in out
checks["@nas: no local-only hint when aimed explicitly"] = not any(
    "Local host only" in t for t in out)

bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/stop plex @nas", checker) + confirm(bot, checker)
checks["@nas: lifecycle hit the remote host"] = (
    hosts.hosts[1].checker.stopped == ["plex"]
    and hosts.hosts[0].checker.stopped == [])
checks["@nas: lifecycle reply names the host"] = any("@nas" in t for t in out)

bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/check @nas", checker)
checks["@nas: bare check runs on that host only"] = (
    hosts.hosts[1].checker.check_calls == [None]
    and hosts.hosts[0].checker.check_calls == [])

bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/status @nas", checker)
checks["@nas: /status is that host's overview"] = (
    "📊 *2*" in out[0] and "`plex` @nas" in out[0] and "`db`" not in out[0])

# ── 4b. a container lives on one host: the others stay quiet ──────────
bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/status db", checker)
checks["one reply for a container only the local host has"] = (
    len(out) == 1 and out[0].startswith("🔎 *db*")
    and "not found" not in out[0])

bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/status plex", checker)
checks["one reply for a container only the remote host has"] = (
    len(out) == 1 and out[0].startswith("🔎 *plex* @nas"))
checks["remote /status detail carries no lifecycle buttons"] = (
    bot.markups == [None])

bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/status web", checker)
checks["a container on both hosts answers once per host"] = (
    len(out) == 2 and " @nas" in out[1] and " @nas" not in out[0])
checks["local /status detail keeps its buttons"] = (
    bot.markups[0] is not None and bot.markups[1] is None)

bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/check nope", checker)
checks["a name no host has answers once"] = (
    out == ["Container `nope` not found."])

# ── 5. `@all` widens a write ──────────────────────────────────────────
bot, checker, cfg, hosts = multi_bot()
out = drive(bot, "/update web @all", checker)
checks["@all: every host checked"] = (
    hosts.hosts[0].checker.check_calls == [["web"]]
    and hosts.hosts[1].checker.check_calls == [["web"]])
checks["@all: every host updated"] = (
    "RUN_UPDATES@local:web" in out and "RUN_UPDATES@nas:web" in out)
checks["@all: no local-only hint"] = not any("Local host only" in t for t in out)

# ── 6. unknown host → error, nothing executed ─────────────────────────
for cmd in ("/check @typo", "/update web @typo", "/stop web @typo",
            "/status @typo", "/updates @typo"):
    bot, checker, cfg, hosts = multi_bot()
    out = drive(bot, cmd, checker)
    ok = (len(out) == 1 and "typo" in out[0]
          and "`local`" in out[0] and "`nas`" in out[0]
          and not any(h.checker.check_calls for h in hosts)
          and not any(h.checker.stopped for h in hosts)
          and not any(h.backend.lifecycle_calls() for h in hosts))
    checks[f"unknown host: `{cmd}` errors and does nothing"] = ok

# ── 7. /help documents the rule when it applies ───────────────────────
bot, checker, cfg, hosts = multi_bot()
help_text = drive(bot, "/help", checker)[0]
checks["/help documents @host and @all"] = (
    "`@<host>`" in help_text and "`@all`" in help_text)
checks["/help documents the read/write split"] = (
    "local host only" in help_text.lower()
    and "/status" in help_text and "/update" in help_text)
checks["/help lists the managed hosts"] = (
    "`local`" in help_text and "`nas`" in help_text)


# ── 8. a genuinely DEAD host does not take /status down ───────────────
# The overview handled a host that RETURNS an error, but not one that
# RAISES (a timeout). A real dead host (srv20) proved it live: `/status`
# produced nothing and the exception surfaced as "Bot listener error".
from hosts import HostRegistry as _Reg
from container_store import LOCAL_HOST as _LH

_alive = ManagedHost("local", FakeBackend("local", ["web", "db"]),
                     FakeChecker("local", []), FakeStore(), is_local=True)
_dead = ManagedHost("srv20", DeadBackend("srv20"),
                    FakeChecker("srv20", []), FakeStore(),
                    endpoint="tcp://10.10.10.20:2375")
_hosts = _Reg([_alive, _dead])
_bot, _chk, _cfg = make_bot(_hosts)

_raised = False
try:
    _out = drive(_bot, "/status", _chk)
except Exception:
    _raised = True
checks["/status survives a host that times out (does not raise)"] = not _raised
checks["…and still shows the reachable host's containers"] = (
    not _raised and any("web" in m for m in _out))
checks["…and names the dead host as unreachable"] = (
    not _raised and any("srv20" in m for m in _out))
checks["…and actually tried the dead host (its run was called)"] = (
    bool(_dead.backend.calls))


def main():
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
