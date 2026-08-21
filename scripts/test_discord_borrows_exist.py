#!/usr/bin/env python3
"""Discord borrows no behaviour from the Telegram bot (#63).

This started as a guard: the Discord front end reached into the Telegram
bot instance for machinery that had not been pulled into a neutral core
yet, and nothing checked that the borrowed names were real — so
`/selfupdate` called `bot.check_selfupdate`, which never existed, and the
command just AttributeError'd. Exactly like `/changelog` did on a
different call, and `/restart` on a third.

The extraction is finished, so the guard states the finished thing: the
list of borrowed methods is EMPTY. What is left is one attribute WRITE —
`bot.t`, when /lang switches the language — and that is named here rather
than waved through, so it cannot quietly grow company.

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
checks["Discord borrows no method from the Telegram bot"] = not borrowed
if borrowed:
    print("  → geborgt: " + ", ".join(
        f"{n} (line {ln})" for n, ln in sorted(borrowed.items())))

# Should one come back, it must at least be real — the check that caught
# the three broken commands, kept for the borrow that has not happened.
missing = sorted(f"{n} (line {ln})" for n, ln in borrowed.items()
                 if not callable(getattr(TelegramBot, n, None)))
checks["…and were one to come back, it would have to exist"] = not missing
if missing:
    print("  → not on TelegramBot: " + ", ".join(missing))

# The one write, stated by name. `t` is replaced on the Telegram bot when
# Discord's /lang switches the language, because the bot holds its
# translator rather than resolving it from `config.language`. Removing it
# means making `t` resolve itself — a change across every call site in
# the project, which is not something to do in the last step of a large
# extraction. It is the last thread, and this is where it is recorded.
checks["the only thing written on the bot is the translator"] = (
    written == {"t"})
if written != {"t"}:
    print(f"  → geschrieben: {sorted(written)}")

# The three commands that were broken, each now on the core.
i = dsrc.index("def _cmd_selfupdate")
body = dsrc[i:dsrc.index("\n    def ", i + 10)]
checks["/selfupdate drives the shared self-update"] = (
    "selfupdate.start" in body and "check_selfupdate(" not in body)
i = dsrc.index("def _cmd_changelog")
body = dsrc[i:dsrc.index("\n    def ", i + 10)]
checks["/changelog reads the shared changelog"] = "changelog." in body
i = dsrc.index("def _cmd_restart_self")
body = dsrc[i:dsrc.index("\n    def ", i + 10)]
checks["/restart asks the shared restart guard"] = "selfrestart.policy" in body

# The scan must see the forms the regex could not. Both are real call
# sites in the file; if the extraction removes them, this check is what
# says so out loud rather than the scan quietly going blind.
# The scan must not go blind while reporting "nothing borrowed". Feed it
# both forms the regex version missed and check it still sees them.
PROBE = '''
class X:
    def a(self):
        threading.Thread(target=bot._bare_reference)
        runner = getattr(self.telegram, "_by_string", None)
        bot.plain_call()
'''
probe_tree = ast.parse(PROBE)
probe = set()
for node in ast.walk(probe_tree):
    if isinstance(node, ast.Attribute) and _is_the_bot(node.value):
        probe.add(node.attr)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr", "delattr")
            and len(node.args) >= 2 and _is_the_bot(node.args[0])
            and isinstance(node.args[1], ast.Constant)):
        probe.add(node.args[1].value)
checks["the scan still sees all three shapes it is meant to catch"] = (
    probe == {"_bare_reference", "_by_string", "plain_call"})

print(f"  geborgt: {', '.join(sorted(borrowed)) or '— nichts'}"
      f"   | geschrieben: {', '.join(sorted(written)) or '—'}")
failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
