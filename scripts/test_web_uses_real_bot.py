#!/usr/bin/env python3
"""The Web UI may only call methods the bot actually has.

This exists because the Web UI's Start/Stop/Restart buttons were dead and
the whole suite stayed green. Moving stop/start/restart into
`lifecycle.act` removed `TelegramBot._lifecycle_action`; `web_ui.py` still
called it, the AttributeError was swallowed by a bare `except` and
printed, and the handler redirected as if it had worked. Press Stop, page
reloads, container keeps running, no error anywhere a user would see.

Two tests made that invisible and neither was wrong on its own:
`test_lifecycle_core.py` asserts the method is GONE (it should be), and
`test_web_multihost.py` defines a stub bot that HAS it (so the Web UI
tests pass against a bot the real one no longer resembles). Together they
guaranteed production was broken and the suite was quiet.

So this checks the Web UI against the real class, not against a stub.
It is the same failure Discord had with `bot.check_selfupdate` — a call
to a method that does not exist, in the one connection nothing exercised.
"""
import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
from telegram_bot import TelegramBot          # noqa: E402

checks = {}
src = open(os.path.join(APP, "web_ui.py"), encoding="utf-8").read()
tree = ast.parse(src)

# Every `bot.<name>` the Web UI touches, with the line it is on.
wanted = {}
for node in ast.walk(tree):
    if (isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "bot"):
        wanted.setdefault(node.attr, node.lineno)

checks["the Web UI does talk to the bot at all"] = len(wanted) >= 5

# Class members plus everything the bot assigns to `self` anywhere in
# its own source — `bot.engine` and `bot.t` are instance attributes, and
# a check that only knew `dir()` would call them missing.
real = set(dir(TelegramBot))
bot_src = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
for node in ast.walk(ast.parse(bot_src)):
    if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name) and node.value.id == "self"):
        real.add(node.attr)
missing = sorted(f"bot.{n} (web_ui.py:{ln})"
                 for n, ln in wanted.items() if n not in real)
checks["every bot member the Web UI calls exists"] = missing == []
if missing:
    print("  fehlend:", "; ".join(missing))

# And the stubs the Web UI tests drive must not invent members the real
# bot lacks — that is exactly how the break stayed hidden.
stub_src = open(os.path.join(os.path.dirname(__file__),
                             "test_web_multihost.py"), encoding="utf-8").read()
stub_tree = ast.parse(stub_src)
invented = []
for node in ast.walk(stub_tree):
    if not isinstance(node, ast.ClassDef):
        continue
    if "bot" not in node.name.lower():
        continue
    for item in node.body:
        if (isinstance(item, ast.FunctionDef)
                and not item.name.startswith("__")
                and item.name not in real):
            invented.append(f"{node.name}.{item.name}")
checks["the Web UI test's stub bot invents no methods"] = invented == []
if invented:
    print("  erfunden:", "; ".join(invented))

# The lifecycle path specifically: through the core, with the guards.
i = src.index('action in ("start", "stop", "restart")')
block = src[i:i + 2500]
checks["Start/Stop/Restart goes through lifecycle.act"] = (
    "lifecycle.act(" in block)
checks["…and hands it the update-running flag"] = (
    "update_running=" in block)
# The two guards the Web UI never had: it only checked "would this stop
# me?", so the button could stop a container both chats refuse to touch.
# Anchored on the CALL, not on the word: the replacement comment
# mentions `_would_kill_self` to explain why the core's version is safe
# across hosts, and a check that matched the word failed on its own
# explanation.
checks["…so stop-protection now applies here too"] = (
    "_would_kill_self(" not in block)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
