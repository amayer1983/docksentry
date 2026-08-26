#!/usr/bin/env python3
"""Bare `/restart` restarts Docksentry, and stays reachable.

It stopped being reachable in 2.18.0-beta.20. The lifecycle branch
matches `text.startswith("/restart")` — no space — so once bare `/stop`
and `/restart` were routed there to answer with a usage line instead of
silence (2f73b59), it swallowed the bare form as well and the
self-restart branch below it could never run. `/restart` answered
"usage: /restart <name>" and restarted nothing. 2.17.1 is unaffected.

The branch that means "restart the bot" therefore has to be tested for
BEFORE the lifecycle branch, and on an exact match so that `/restart
web` and `/restartx` still go where they always went.

This is checked by walking the dispatcher's conditions in source order
and evaluating them against real inputs — the same thing Python does at
run time — rather than by looking for a string that happens to be
present somewhere.
"""
import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}
src = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()

# Every `if`/`elif` of the dispatcher chain, in order, with its full
# condition (they can be continued over two lines with a backslash).
chain = []
lines = src.split("\n")
for n, line in enumerate(lines):
    m = re.match(r'^        (?:el)?if (text\b.*?):\s*$', line)
    if m:
        chain.append((n + 1, m.group(1)))
        continue
    m = re.match(r'^        (?:el)?if (text\b.*?)\s*\\$', line)
    if m:
        cont = lines[n + 1].strip().rstrip(":")
        chain.append((n + 1, f"{m.group(1)} {cont}"))

# Three guards run before the command chain and match anything starting
# with a slash — the `-?` help alias, the auth gate, the `@botname`
# strip. They are not command branches and must not be mistaken for the
# winner, or every input would "land" on the first of them.
chain = [(n, c) for n, c in chain if re.search(r'"/[a-z]', c)]

checks["the dispatcher chain was found"] = len(chain) > 30


def winner(text_value):
    """Which branch handles `text_value` — the first condition that is
    true, exactly as the chain decides at run time."""
    for lineno, cond in chain:
        # `self._multi()` appears in one condition. Rather than guess it,
        # evaluate the branch BOTH ways: if the answer is the same either
        # way, the branch is decided for this input and we can move on.
        # If it differs, this test cannot say which branch wins and must
        # say so instead of quietly picking one.
        expr = cond.replace("self._multi()", "_multi")
        results = set()
        for _multi in (True, False):
            results.add(bool(eval(expr, {"__builtins__": {}},          # noqa: S307
                                  {"text": text_value, "_multi": _multi})))
        if len(results) != 1:
            raise AssertionError(
                f"line {lineno} depends on instance state for "
                f"{text_value!r}: {cond}")
        if results.pop():
            return lineno, cond
    return None, None


_restart_line = next((n for n, c in chain if c == 'text == "/restart"'), 10**9)
checks["the self-restart branch matches exactly, not by prefix"] = (
    _restart_line < 10**9)

_lifecycle = next((n for n, c in chain if 'startswith("/stop")' in c), None)
checks["the lifecycle branch was found"] = _lifecycle is not None
checks["self-restart is tested BEFORE the lifecycle branch"] = (
    _restart_line < (_lifecycle or 0))

ln, _ = winner("/restart")
checks["bare /restart lands on the self-restart branch"] = ln == _restart_line

body = src.split("\n")[_restart_line:_restart_line + 8]
checks["…which is the one that restarts the bot"] = any(
    "restart_self" in b for b in body)

ln, _ = winner("/restart web")
checks["/restart <name> still lands on the lifecycle branch"] = ln == _lifecycle
ln, _ = winner("/restartx")
checks["/restartx is unchanged — still the lifecycle branch"] = ln == _lifecycle
ln, _ = winner("/stop")
checks["bare /stop keeps its usage line from the lifecycle branch"] = ln == _lifecycle

for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
