#!/usr/bin/env python3
"""Every file Docksentry reads or writes as text names its encoding.

Without `encoding=`, `open()` follows the process locale. Inside the
official image that is harmless today — `python:3.12-alpine` sets no
`LANG`, so CPython turns on the UTF-8 mode (PEP 540) by itself — but a
user who sets `LANG` in their compose file takes that away, and the same
file then loads on one host and raises on another. Nothing about that is
visible in a diff.

The failure it produced is specific, and three instances of it were
measured on 07.11.2026: a `UnicodeDecodeError` is a `ValueError`, not a
`JSONDecodeError` and not an `OSError`, so every
`except (json.JSONDecodeError, OSError)` in the project let it straight
through. One Latin-1 byte in `settings.json` escaped the boot guard; one
in `maintenance.json` killed the scheduler tick; one in
`pinned_containers.json` escaped `_get_pinned` entirely.

Reading is only half of it. `atomic_write_json` is the single writer
behind sixty call sites, and it used `os.fdopen(fd, "w")`. What saved it
was `json.dump`'s default `ensure_ascii=True` — pure ASCII in every
locale. The day somebody sets `ensure_ascii=False` to get readable
umlauts in a note, that one line starts producing files in whatever
locale the process happens to have, and those are exactly the files the
readers above cannot read. So the writer is checked too.

A lint, deliberately: the promise is about every call site, present and
future, which is not something a driven procedure can cover. It is
checked against the real AST rather than a grep, so a call spread over
several lines counts like any other.
"""

import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")

checks = {}
text_calls = []
binary_calls = []

for dirpath, _dirs, files in os.walk(APP):
    if "__pycache__" in dirpath:
        continue
    for fn in sorted(files):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(dirpath, fn)
        rel = os.path.relpath(path, os.path.join(APP, ".."))
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (getattr(node.func, "id", None)
                    or getattr(node.func, "attr", None))
            if name not in ("open", "fdopen"):
                continue
            mode = ""
            if (len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value or ""
            has_enc = any(kw.arg == "encoding" for kw in node.keywords)
            entry = (f"{rel}:{node.lineno}", has_enc)
            (binary_calls if "b" in mode else text_calls).append(entry)

# The counter-direction first: these are statements about a set, and an
# empty set satisfies every one of them. If the walk ever stops finding
# files, this says so instead of going quiet.
checks["the scan finds the text-mode file calls"] = len(text_calls) >= 40
checks["…and the binary ones too"] = len(binary_calls) >= 1

missing = [where for where, has_enc in text_calls if not has_enc]
checks["every text-mode open names its encoding"] = not missing
for where in missing[:12]:
    print(f"      → {where}: open() in text mode without encoding=")

# …and the other way round, because `encoding=` on a binary handle is a
# TypeError at runtime — a mistake this very rule invites.
wrong = [where for where, has_enc in binary_calls if has_enc]
checks["…and no binary open carries one"] = not wrong
for where in wrong[:12]:
    print(f"      → {where}: binary open() with encoding=")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print(f"  ({len(text_calls)} text-mode, {len(binary_calls)} binary)")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
