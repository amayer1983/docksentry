#!/usr/bin/env python3
"""A connection carries no sentences of its own (#63).

The owner drew the line: whatever goes in or out must be identical across
every connection — only presentation may differ. Discord broke it without
anyone noticing, because it carried seventy-two hardcoded English replies
while Telegram read the shared translations. One instance then answered
German in Telegram and English in Discord to the same question. That is
content, not presentation.

So this scans the Discord front end for user-facing sentences returned as
literal text. A reply must come from `self.t(...)` — the same keys
Telegram reads — and the connection may only choose its markup (bold, the
length cap) on the way out.

What is deliberately NOT flagged:

  * command NAMES and descriptions in the COMMANDS table — Discord
    registers those with its API, which has its own rules about them;
  * short markers and separators, log lines, and anything under a few
    words, which carry no sentence;
  * the `!` prefix on `_match_in`'s answer, a sentinel the caller strips.

A new connection (Matrix, whatever comes next) should be held to this
too the day it can answer a command.
"""
import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")

checks = {}

#: A returned string this long, with a space in it, is a sentence someone
#: reads — not a marker, a separator or a format fragment.
SENTENCE = 12



def _branches(value):
    """Every expression a `return` can hand back, flattened.

    A ternary returns two, a boolean chain returns each side; anything
    else returns itself. Written out because the missed case was exactly
    the one nobody thinks to write a test for.
    """
    if isinstance(value, ast.IfExp):
        return _branches(value.body) + _branches(value.orelse)
    if isinstance(value, ast.BoolOp):
        out = []
        for v in value.values:
            out += _branches(v)
        return out
    return [value]


def sentences_returned(path):
    """Literal sentences a method hands back to a user."""
    src = open(path, encoding="utf-8").read()
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        # Every shape a returned sentence can take. The first version
        # looked at plain constants and f-strings only, and missed
        # `return A if cond else B` — which is how two `Usage:` lines sat
        # in the file while the check reported none. A guard with a hole
        # is worse than no guard: it is a guard people trust.
        for v in _branches(node.value):
            text = None
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                text = v.value
            elif isinstance(v, ast.JoinedStr):
                text = "".join(p.value for p in v.values
                               if isinstance(p, ast.Constant))
            if text and len(text.strip()) >= SENTENCE and " " in text.strip():
                out.append((node.lineno, text.strip()))
    return out


found = sentences_returned(os.path.join(APP, "discord_bot.py"))
checks["the Discord connection returns no sentences of its own"] = not found
if found:
    for ln, t in found[:10]:
        print(f"  → line {ln}: {t[:70]!r}")

# It must actually be reading the shared translations, not just be quiet.
dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
checks["…because it reads the shared translations instead"] = (
    dsrc.count("self.t(") >= 40)
checks["…resolved per call, so /lang applies at once"] = (
    "def t(self)" in dsrc and "get_translator(getattr(self.config" in dsrc)
# Presentation is the connection's own business, and stays here.
checks["…while the markup conversion stays in the connection"] = (
    "_tg_bold_to_discord" in dsrc)

# Every key it asks for has to exist, in English at least — a typo'd key
# renders as the key itself, which is worse than the hardcoded sentence.
import json  # noqa: E402
import re  # noqa: E402
en = json.load(open(os.path.join(APP, "lang", "en.json"), encoding="utf-8"))
asked = set(re.findall(r'self\.t\(\s*"([a-z0-9_]+)"', dsrc))
missing = sorted(k for k in asked if k not in en)
checks["every key it asks for is a real one"] = not missing
if missing:
    print("  → not in en.json: " + ", ".join(missing))

failed = [k for k, v in checks.items() if not v]
print(f"  ({len(asked)} keys asked for, {len(found)} loose sentences)")
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
