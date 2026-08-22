#!/usr/bin/env python3
"""A connection carries no sentences of its own (#63).

The owner drew the line: whatever goes in or out must be identical across
every connection — only presentation may differ. Discord broke it without
anyone noticing, because it carried seventy-two hardcoded English replies
while Telegram read the shared translations. One instance then answered
German in Telegram and English in Discord to the same question. That is
content, not presentation.

This is the fourth version of this check, and the first three were each
wrong in the same way: they enumerated the SHAPES a sentence could take —
a bare `return`, then also a ternary, then also `.append` — and each time
the sentences that were actually in the file used a shape not on the
list. `self._clip(f"…")` wraps it in a call. `"…" % (…)` is a BinOp.
`return None, "…"` is a tuple. Every version passed while the file it
guarded was full of English.

So it stopped enumerating. It looks at EVERY string literal in the file
and subtracts what is provably not a chat reply:

  * docstrings, identified by position rather than by content;
  * anything inside a `log()` or `print()` call — diagnostics for whoever
    reads the container's output, English throughout this project;
  * command names and descriptions, which are an API contract with
    Telegram and Discord, not something a user reads in an answer;
  * strings without a space, which are keys and identifiers;
  * URLs, which are addresses.

What survives that is a sentence a person reads, and it has to come from
`self.t(...)` — the same keys every other connection uses.
"""
import ast
import json
import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}

#: Long enough, and with enough real words, to be a sentence rather than a
#: format skeleton. `• {name} ({image}) 📦 {size}` is long and full of
#: spaces, but every word in it is a placeholder the core fills — that is
#: layout, which is the connection's own business.
MIN_LEN = 12
MIN_WORDS = 3

#: An address is not a sentence; translating it would invent a link.
ALLOWED_SUBSTRINGS = ("github.com/amayer1983/docksentry",)


def _is_sentence(text):
    if " " not in text.strip():
        return False
    body = re.sub(r"`[^`]*`", " ", text)          # code spans are data
    return len(re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", body)) >= MIN_WORDS


def _skip_ids(tree):
    """Nodes that are provably not a chat reply."""
    skip = set()
    for node in ast.walk(tree):
        # Docstrings — by position, because comparing the text fails on
        # indentation and that is how one earlier version let them all in.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                skip.add(id(body[0].value))
        # Diagnostics.
        if isinstance(node, ast.Call):
            f = node.func
            name = (f.attr if isinstance(f, ast.Attribute)
                    else f.id if isinstance(f, ast.Name) else "")
            # `_warn_rejected_once` is log-only by design — its own
            # docstring says so, and answering an unauthorised chat would
            # confirm the bot is there, which is the point of refusing.
            if name in ("log", "print", "_warn_rejected_once"):
                for child in ast.walk(node):
                    skip.add(id(child))
        # The command tables: `{"name": …, "description": …}` on Discord
        # and the `("status", "Container overview…", …)` tuples Telegram
        # hands to setMyCommands. Both are registered with the platform,
        # which has its own rules about them; neither is an answer.
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in ("name",
                                                               "description"):
                    skip.add(id(v))
    return skip


def _command_table_ids(tree, src):
    """Every literal inside a `_BOT_COMMANDS`-style list of tuples."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.endswith("COMMANDS"):
                    for child in ast.walk(node):
                        skip.add(id(child))
    return skip


def loose_sentences(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    skip = _skip_ids(tree) | _command_table_ids(tree, src)
    out = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(p.value for p in node.values
                           if isinstance(p, ast.Constant))
        if not text or len(text.strip()) < MIN_LEN:
            continue
        if not _is_sentence(text):
            continue
        if text.strip().startswith(("http", "application/")):
            continue
        # Wire format, not language: the multipart envelope Telegram's
        # sendDocument needs is a protocol detail with words in it.
        if any(m in text for m in ("Content-Disposition", "form-data",
                                   "boundary=", "multipart/", "Content-Type")):
            continue
        if any(a in text for a in ALLOWED_SUBSTRINGS):
            continue
        out.append((node.lineno, text.strip()))
    return out


#: Every connection, not just the one that was worst.
CONNECTIONS = ["discord_bot.py", "telegram_bot.py"] + [
    os.path.join("notifiers", f)
    for f in sorted(os.listdir(os.path.join(APP, "notifiers")))
    if f.endswith(".py") and f != "__init__.py"]

found = []
for rel in CONNECTIONS:
    for ln, text in loose_sentences(os.path.join(APP, rel)):
        found.append((rel, ln, text))
checks["no connection writes sentences of its own"] = not found
for rel, ln, t in found[:15]:
    print(f"  → {rel}:{ln}: {t[:72]!r}")

# Being quiet is not enough — it has to be reading the shared strings.
dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
checks["Discord reads the shared translations"] = dsrc.count("self.t(") >= 40
checks["…resolved per call, so /lang applies at once"] = (
    "def t(self)" in dsrc and "get_translator(getattr(self.config" in dsrc)
checks["…while the markup conversion stays in the connection"] = (
    "_tg_bold_to_discord" in dsrc)

# A key that does not exist renders as the key itself, which is worse than
# the hardcoded sentence it replaced.
en = json.load(open(os.path.join(APP, "lang", "en.json"), encoding="utf-8"))
asked = set(re.findall(r'self\.t\(\s*"([a-z0-9_]+)"', dsrc))
asked |= set(re.findall(r'\bt\(\s*"([a-z0-9_]+)"', dsrc))
missing = sorted(k for k in asked if k not in en)
checks["every key asked for is a real one"] = not missing
if missing:
    print("  → not in en.json: " + ", ".join(missing))

print(f"  ({len(asked)} keys asked for, {len(found)} loose sentences, "
      f"{len(CONNECTIONS)} connections scanned)")
failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
