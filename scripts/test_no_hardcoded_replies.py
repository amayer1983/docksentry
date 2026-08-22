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
  * the `!` prefix on `_match_in`'s answer, a sentinel the caller strips;
  * the documentation URL under `/help`, which is an address, not a
    sentence — translating it would be inventing a link that does not
    exist.

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

#: …and it has to contain real words. A format skeleton like
#: `• {name} ({image})\n  📦 {size} | 🗓️ {date}` is long and has spaces,
#: but every word in it is a placeholder the core fills — that is layout,
#: which is the connection's own business. Three plain words is the line.
WORDS = 3


def _is_sentence(text):
    import re as _re
    body = _re.sub(r"`[^`]*`", " ", text)          # code spans are data
    words = _re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", body)
    return len(words) >= WORDS



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
    """Literal sentences a method hands to a user — by any route.

    Three routes, all of them real, all of them found the hard way:

      * `return "…"` — the obvious one;
      * `return A if cond else B` — a ternary, which the first version of
        this check walked straight past while two Usage lines sat in it;
      * `lines.append("…")` and its siblings, where a reply is assembled
        piece by piece and returned as one joined string at the end. That
        route alone was hiding twenty-one English sentences AFTER this
        check reported none.

    A guard that only sees one shape is worse than no guard, because
    people believe it. So it looks at all three.
    """
    src = open(path, encoding="utf-8").read()
    out = []
    #: An address, not a sentence — see the docstring.
    ALLOWED = ("github.com/amayer1983/docksentry",)
    for node in ast.walk(ast.parse(src)):
        # Sentences appended into a reply that is joined and returned.
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend", "insert")):
            for arg in node.args:
                for v in _branches(arg):
                    text = None
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        text = v.value
                    elif isinstance(v, ast.JoinedStr):
                        text = "".join(p.value for p in v.values
                                       if isinstance(p, ast.Constant))
                    if (text and len(text.strip()) >= SENTENCE
                            and _is_sentence(text)
                            and not any(a in text for a in ALLOWED)):
                        out.append((node.lineno, text.strip()))
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
            if (text and len(text.strip()) >= SENTENCE
                    and _is_sentence(text)
                    and not any(a in text for a in ALLOWED)):
                out.append((node.lineno, text.strip()))
    return out


#: Every connection, not just the one that was worst. Telegram was the
#: original home of the shared wording, so it reads from the keys by
#: construction — but nothing was checking that it stayed that way, and
#: the six notification channels were each writing their own English
#: until the day this list grew to include them.
CONNECTIONS = ["discord_bot.py", "telegram_bot.py",
               os.path.join("notifiers", "ntfy.py"),
               os.path.join("notifiers", "smtp.py"),
               os.path.join("notifiers", "gotify.py"),
               os.path.join("notifiers", "matrix.py"),
               os.path.join("notifiers", "apprise.py"),
               os.path.join("notifiers", "discord.py")]

found = []
for rel in CONNECTIONS:
    for ln, text in sentences_returned(os.path.join(APP, rel)):
        found.append((rel, ln, text))
checks["no connection writes sentences of its own"] = not found
if found:
    for rel, ln, t in found[:12]:
        print(f"  → {rel}:{ln}: {t[:70]!r}")

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
