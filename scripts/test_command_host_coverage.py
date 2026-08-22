#!/usr/bin/env python3
"""Every container-touching Telegram command reaches every host (#2).

@famewolf, after the disk disaster: "take a pass across all the commands
and ensure they act on the appropriate host." We did — and the first
answer we gave ("only /checkimages was local-only") was WRONG: /logs and
/audit were local-only too, both lagging behind their already-host-aware
Discord twins. The lesson wasn't just the two commands; it was that a
completeness claim ("only X") was made from a heuristic instead of from
checking each command. This test IS that check, kept so the claim can
never rot again.

The rule it enforces: a command that resolves a container or reads its
runtime MUST route through the host family — `_resolve_targets` /
`_state_targets` for the `@host` token, and a per-host backend/checker/
store for the action — never the bot's own local `self.backend` /
`self.checker` for a container operation. New commands that forget this
fail here, at the exact point the audit would otherwise have to be
redone by hand.
"""

import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
src = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()

checks = {}

# Split the dispatcher into per-command blocks.
pat = re.compile(r'(?:if|elif) text(?:\.startswith\(|\s*==)\s*"(/[a-z_]+) ?"')
hits = [(m.group(1), m.start()) for m in pat.finditer(src)]
blocks = {}
for i, (cmd, pos) in enumerate(hits):
    end = hits[i + 1][1] if i + 1 < len(hits) else pos + 4000
    blocks.setdefault(cmd, "")
    blocks[cmd] += src[pos:end]

# Commands that operate on a specific container / a host's runtime, and
# therefore MUST be host-aware. (Instance-global commands — /settings,
# /backup, /selfupdate, /history's shared log, /help … — are excluded on
# purpose: they have no host dimension.)
# (/events and /history are excluded with the other instance-globals:
# both render a single shared log file — the monitor-events log, the
# update-history log — that has no per-host daemon dimension.)
HOST_COMMANDS = [
    "/status", "/check", "/update", "/updates", "/updateall",
    "/cleanup", "/checkimages", "/logs", "/audit",
    "/pin", "/unpin", "/autoupdate", "/cooldown", "/protect", "/note",
    "/trustrunning", "/askmajor", "/setlink", "/groups", "/stop",
]

# The markers that prove a block routes by host rather than assuming
# local. Any ONE of them present is enough — different commands use
# different members of the family.
ROUTERS = ("_resolve_targets", "_state_targets", "host_checkers",
           "_handle_callback", "_backend_for", "_checker_for",
           "_store_for", "self.hosts")

for cmd in HOST_COMMANDS:
    body = blocks.get(cmd, "")
    checks[f"{cmd} exists in the dispatcher"] = bool(body)
    routed = any(r in body for r in ROUTERS)
    checks[f"{cmd} routes by host, not local-only"] = routed

# The specific regression: /logs and /audit must act on the host's own
# backend, never the bot's. Stated as the intent rather than as one
# spelling of it — /logs hands `backend_for` to the shared core now, and
# a check that insisted on the old line would have failed the fix (#63).
for cmd in ("/logs", "/audit"):
    body = blocks.get(cmd, "")
    checks[f"{cmd} works through a per-host backend"] = (
        "_resolve_container(arg, backend=backend)" in body
        or "backend_for=self._backend_for" in body)
    checks[f"{cmd} reaches that host, not the bot's own"] = (
        "backend.run(" in body or "backend.logs(" in body
        or "container_flags." in body)
    # It must NOT fall back to the bot's own local backend for the action.
    checks[f"{cmd} does not act through self.backend"] = (
        "self.backend.run(" not in body and "self.backend.logs(" not in body)

# ── behavioural: /logs and /audit reach a remote container ───────────
sys.path.insert(0, APP)
import types
from telegram_bot import TelegramBot

def make_bot(registry):
    b = TelegramBot.__new__(TelegramBot)
    b.hosts = registry
    b.config = types.SimpleNamespace()
    b.backend = registry[0].backend   # the bot's own = the local host's
    sent = []
    b.send_message = lambda m: sent.append(m)
    b._sent = sent
    return b

class Backend:
    def __init__(self, names, label):
        self._names = names
        self.name = label
    def run(self, argv, timeout=None):
        if argv[:1] == ["ps"]:
            return types.SimpleNamespace(
                returncode=0, stdout="\n".join(self._names), stderr="")
        if argv[:1] == ["inspect"]:
            return types.SimpleNamespace(
                returncode=0, stdout='[{"HostConfig":{},"Config":{}}]',
                stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    def logs(self, name, tail=30, timeout=None):
        return types.SimpleNamespace(
            returncode=0, stdout=f"log line from {name}", stderr="")

class Host:
    def __init__(self, name, backend, local=False):
        self.name = name
        self.backend = backend
        self.checker = types.SimpleNamespace(
            netns_target_name=lambda n: None,
            image_version_label=lambda i: "")
        self.is_local = local

class Registry(list):
    is_multi = True
    @property
    def local(self):
        return self[0]
    @property
    def names(self):
        return [h.name for h in self]
    def get(self, name):
        for h in self:
            if h.name == name.strip().lower():
                return h
        return None

local_be = Backend(["web", "docksentry"], "local")
remote_be = Backend(["llama-server"], "dock8520")
reg = Registry([Host("local", local_be, local=True),
                Host("dock8520", remote_be)])

# /logs of a container that lives only on the remote host must find it.
bot = make_bot(reg)
from i18n import get_translator
bot.t = get_translator("en")
TelegramBot._handle_message  # ensure import side effects are fine
# Drive the resolver path the way the branch does:
arg, targets, err = bot._resolve_targets("llama-server", write=False)
found_on = []
for host in (targets or [None]):
    be = bot._backend_for(host)
    name, e = bot._resolve_container(arg, backend=be)
    if not e:
        found_on.append(host.name if host else "local")
checks["/logs-style sweep finds a remote-only container"] = (
    found_on == ["dock8520"])

# And a local-only container is found on local, not claimed on the remote.
arg, targets, err = bot._resolve_targets("web", write=False)
found_on = []
for host in (targets or [None]):
    be = bot._backend_for(host)
    name, e = bot._resolve_container(arg, backend=be)
    if not e:
        found_on.append(host.name if host else "local")
checks["…and a local-only container resolves on local only"] = (
    found_on == ["local"])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
