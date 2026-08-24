#!/usr/bin/env python3
"""Every Discord write command can be aimed at a host.

`/note`, `/trustrunning`, `/askmajor` and `/setlink` read
`opts.get("host")` and handed it to the core exactly like their
siblings — but none of them REGISTERED a `host` option, so the value was
always None and the command was permanently local. On a multi-host
install there was no way to put a note on the NAS from Discord, and
nothing said so.

That is Issue #2's shape, and it survived the whole core extraction
because the only host-coverage test in the suite
(`test_command_host_coverage.py`) reads `telegram_bot.py` and nothing
else. Half a parity check catches half the drift.

So: the option table is checked against the handlers, both directions.
A command whose handler reads `host` must offer it, and a command that
offers it must have a handler that reads it — an option nobody consults
is worse than none, because it looks like it works.
"""
import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import discord_bot as D  # noqa: E402

checks = {}
opts_of = {c["name"]: {o["name"] for o in c.get("options", ())}
           for c in D.COMMANDS}
checks["the command table was read"] = len(opts_of) >= 30

# Commands that change per-container or per-host state. Instance-global
# ones (/settings, /backup, /help …) have no host dimension and are not
# listed on purpose. /cleanup is deliberately absent too: it walks every
# host and takes no option at all.
WRITES = ["pin", "unpin", "autoupdate", "protect", "trustrunning",
          "askmajor", "cooldown", "note", "setlink",
          "stop", "start", "restart", "update", "updateall"]

for cmd in WRITES:
    checks[f"/{cmd} exists"] = cmd in opts_of
    checks[f"/{cmd} can be aimed at a host"] = "host" in opts_of.get(cmd, ())

# …and reads it. Map each command to the method the dispatcher sends it
# to, then check that method's body actually consults the option.
src = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
tree = ast.parse(src)
bodies = {}
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef) and node.name == "DiscordBot":
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                bodies[item.name] = ast.get_source_segment(src, item) or ""

HANDLER = {"pin": "_cmd_pin", "unpin": "_cmd_pin",
           "autoupdate": "_cmd_autoupdate", "protect": "_cmd_protect",
           "trustrunning": "_cmd_flag", "askmajor": "_cmd_flag",
           "cooldown": "_cmd_cooldown", "note": "_cmd_note",
           "setlink": "_cmd_setlink", "stop": "_cmd_lifecycle",
           "start": "_cmd_lifecycle", "restart": "_cmd_lifecycle",
           "update": "_cmd_update", "updateall": "_cmd_updateall"}
for cmd, meth in sorted(set(HANDLER.items())):
    body = bodies.get(meth, "")
    checks[f"{meth} exists"] = bool(body)
    checks[f"{meth} reads the host option"] = 'opts.get("host")' in body

# An option offered but never consulted is the worse half of the same
# bug — it looks like it works. Checked over every command, not just the
# writes.
unread = []
for cmd, o in sorted(opts_of.items()):
    if "host" not in o:
        continue
    meth = HANDLER.get(cmd)
    if meth is None:                      # a read command; find it by name
        meth = f"_cmd_{cmd}"
    body = bodies.get(meth, "")
    if body and 'opts.get("host")' not in body and '"host"' not in body:
        unread.append(f"/{cmd} → {meth}")
checks["no command offers a host option it ignores"] = unread == []
if unread:
    print("  ignoriert:", "; ".join(unread))

# The same number, the same words. Discord divided by 1024 and Telegram
# by 1000, so one measurement read as "214 MB" in one chat and "224 MB"
# in the other.
from update_checker import UpdateChecker  # noqa: E402
sizes = [0, 1, 999, 1_000, 8_534_000, 224_000_000, 1_500_000_000, 9 << 40]
checks["both chats format a size identically"] = all(
    D.DiscordBot._human_size(n) == UpdateChecker._human_bytes(n)
    for n in sizes)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
