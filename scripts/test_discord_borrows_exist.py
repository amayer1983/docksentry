#!/usr/bin/env python3
"""Every Telegram-bot method Discord borrows actually exists (#63).

Discord's front end reaches into the Telegram bot instance for the bits
of shared machinery that have not been pulled into a neutral core yet
(self-update, restart). Nothing checked that the borrowed names were real
— so `/selfupdate` called `bot.check_selfupdate`, which never existed,
and the command just AttributeError'd. Exactly like `/changelog` did on a
different call. The real fix is the extraction that removes the borrowing
entirely; until then, this keeps the borrows honest.

Scanned over the AST, not with a regex over the text. The first version
did use a regex, and it matched only a CALL — `bot.NAME(`, with its
parenthesis — which missed two forms standing in the file at the time:

  * the bare reference: `threading.Thread(target=bot._handle_selfupdate)`
    hands the method over without ever writing a `(` after its name;
  * the lookup by string: `getattr(self.telegram, "_run_queued_selfupdate",
    None)` and `hasattr(bot, "_handle_selfupdate")`.

Both fail the same way the two real bugs did — the getattr one silently,
since it has a default. The AST also settles the other half the regex got
wrong: it read `bot.check_selfupdate` out of a COMMENT (this docstring's
sibling, describing the bug) and reported a phantom.
"""
import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
from telegram_bot import TelegramBot  # noqa: E402

dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
tree = ast.parse(dsrc)


def _is_the_bot(node):
    """True for `bot` and for `self.telegram` — the two spellings the
    borrowed Telegram bot goes by in this file."""
    if isinstance(node, ast.Name) and node.id == "bot":
        return True
    return (isinstance(node, ast.Attribute) and node.attr == "telegram"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


borrowed = {}          # name → line, first sighting
written = set()        # names ASSIGNED on the bot, which are not borrows

for node in ast.walk(tree):
    # `bot.t = ...` replaces the Telegram bot's translator when /lang
    # switches the language. That is a write, not a borrow; requiring it
    # to be a callable method would fail for the wrong reason.
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and _is_the_bot(t.value):
                written.add(t.attr)
    # `bot.NAME` — call, bare reference, thread target, anything.
    if isinstance(node, ast.Attribute) and _is_the_bot(node.value):
        borrowed.setdefault(node.attr, node.lineno)
    # `getattr(bot, "NAME", …)` / `hasattr(self.telegram, "NAME")`
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr", "delattr")
            and len(node.args) >= 2 and _is_the_bot(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)):
        borrowed.setdefault(node.args[1].value, node.lineno)

for name in written:
    borrowed.pop(name, None)

checks = {}
checks["something is actually borrowed (the scan found names)"] = bool(borrowed)

missing = sorted(f"{n} (line {ln})" for n, ln in borrowed.items()
                 if getattr(TelegramBot, n, None) is None)
checks["every borrowed name exists on TelegramBot"] = not missing
if missing:
    print("  → not on TelegramBot: " + ", ".join(missing))

not_callable = sorted(f"{n} (line {ln})" for n, ln in borrowed.items()
                      if getattr(TelegramBot, n, None) is not None
                      and not callable(getattr(TelegramBot, n)))
checks["…and every one of them is callable"] = not not_callable
if not_callable:
    print("  → not callable: " + ", ".join(not_callable))

# The specific one that was broken, still wired to the real method.
i = dsrc.index("def _cmd_selfupdate")
body = dsrc[i:dsrc.index("\n    def ", i + 10)]
checks["/selfupdate calls the real _handle_selfupdate"] = (
    "_handle_selfupdate" in body)
checks["…and no longer calls the phantom check_selfupdate"] = (
    "check_selfupdate(" not in body)

# The scan must see the forms the regex could not. Both are real call
# sites in the file; if the extraction removes them, this check is what
# says so out loud rather than the scan quietly going blind.
checks["the scan sees bare references and getattr lookups"] = (
    "_handle_selfupdate" in borrowed and "_run_queued_selfupdate" in borrowed)

print(f"  borrowed: {', '.join(sorted(borrowed))}")
failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
