#!/usr/bin/env python3
"""Pre-commit check: verify all languages are in sync and README is complete."""

import json
import os
import sys

LANG_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "lang")
README = os.path.join(os.path.dirname(__file__), "..", "README.md")
WEB_UI = os.path.join(os.path.dirname(__file__), "..", "app", "web_ui.py")

errors = 0


def _scan_js_string_breaks(js):
    """Return a list of (approx_line, quote) for raw newlines/CRs that
    appear INSIDE a single- or double-quoted JS string literal. Such a
    break is a hard SyntaxError that aborts the entire <script> block,
    silently killing every function defined in it.

    This is exactly the bug that broke the whole Web UI JS from v1.22.0
    to v1.23.1: a `\\n` written in the _BASE_JS triple-quoted Python
    string became a REAL newline in the rendered JS, inside a
    `confirm('...')` literal. Backend tests and "is the function text
    present" checks both passed — only an actual browser (or this
    scanner) catches it. Skips // and /* */ comments and `/regex/`
    literals (which can legitimately contain quotes). Backtick template
    literals may span lines legitimately, so they're not flagged.
    """
    problems = []
    i, n, line = 0, len(js), 1
    in_str = None       # "'" or '"' or '`'
    esc = False
    line_comment = block_comment = False
    prev_significant = ""   # last non-space token char, to spot regex context
    while i < n:
        ch = js[i]
        nxt = js[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if esc:
                esc = False
                i += 1
                continue
            if ch == "\\":
                esc = True
                i += 1
                continue
            if ch in "\n\r" and in_str in ("'", '"'):
                problems.append((line, in_str))
                in_str = None
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        # not in string/comment
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch == "/" and prev_significant in ("", "(", ",", "=", ":", "[",
                                              "!", "&", "|", "?", "{", ";",
                                              "return"):
            # Regex literal — skip to the closing unescaped '/'.
            i += 1
            r_esc = r_class = False
            while i < n:
                c = js[i]
                if r_esc:
                    r_esc = False
                elif c == "\\":
                    r_esc = True
                elif c == "[":
                    r_class = True
                elif c == "]":
                    r_class = False
                elif c == "/" and not r_class:
                    i += 1
                    break
                elif c == "\n":
                    break
                i += 1
            prev_significant = "/"
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
        if not ch.isspace():
            prev_significant = ch
        i += 1
    return problems


def check(ok, msg):
    global errors
    if ok:
        print(f"  \u2705 {msg}")
    else:
        print(f"  \u274c {msg}")
        errors += 1


# 1. Language sync check
files = sorted([f for f in os.listdir(LANG_DIR) if f.endswith(".json")])
langs = {}
for f in files:
    with open(os.path.join(LANG_DIR, f), encoding="utf-8") as fh:
        try:
            langs[f[:-5]] = set(json.load(fh).keys())
        except json.JSONDecodeError as e:
            check(False, f"{f}: Invalid JSON - {e}")

ref = langs.get("en", set())

print(f"=== LANGUAGE CHECK: {len(files)} files, {len(ref)} keys ===")
for code in sorted(langs):
    missing = ref - langs[code]
    extra = langs[code] - ref
    if missing:
        check(False, f"{code}: missing keys: {', '.join(sorted(missing))}")
    if extra:
        check(False, f"{code}: extra keys: {', '.join(sorted(extra))}")
if not any(ref - langs[c] or langs[c] - ref for c in langs):
    check(True, f"All {len(files)} languages in sync ({len(ref)} keys each)")

# 2. README coverage
with open(README, encoding="utf-8") as f:
    readme = f.read()

print("\n=== README: ENV VARS ===")
for var in ["BOT_TOKEN", "CHAT_ID", "CRON_SCHEDULE", "EXCLUDE_CONTAINERS",
            "AUTO_SELFUPDATE", "LANGUAGE", "WEB_UI", "WEB_PORT", "WEB_PASSWORD",
            "DISCORD_WEBHOOK", "WEBHOOK_URL", "TZ", "DOCKER_HOST"]:
    check(var in readme, var)

print("\n=== README: COMMANDS ===")
for cmd in ["/status", "/check", "/updates", "/cleanup", "/history",
            "/pin", "/unpin", "/autoupdate", "/selfupdate", "/debug", "/logs", "/lang", "/settings", "/help"]:
    check(cmd in readme, cmd)

print("\n=== README: FEATURES ===")
for feat in ["Web UI", "Multi-language", "Auto-rollback", "Self-update", "Socket Proxy"]:
    check(feat.lower() in readme.lower(), feat)

# 3. Web UI inline-JS string-literal sanity (regression guard for the
#    v1.22.0\u2013v1.23.1 break: a raw newline inside a JS string literal in
#    _BASE_JS aborts the whole <script> block in every browser).
print("\n=== WEB UI: inline JS string literals ===")
import re as _re
with open(WEB_UI, encoding="utf-8") as f:
    web_src = f.read()
# Pull the _BASE_JS triple-quoted block (the big shared script) and any
# other triple-quoted chunk that contains JS function defs.
_js_blocks = _re.findall(r'_BASE_JS\s*=\s*"""(.*?)"""', web_src, _re.DOTALL)
js_problem_total = 0
for blk in _js_blocks:
    probs = _scan_js_string_breaks(blk)
    js_problem_total += len(probs)
    for ln, q in probs[:5]:
        check(False, f"_BASE_JS line ~{ln}: raw newline inside {q}-quoted JS string")
if js_problem_total == 0:
    check(True, "no raw control chars inside _BASE_JS string literals")

print()
if errors:
    print(f"\u274c {errors} issue(s) found. Fix before committing!")
    sys.exit(1)
else:
    print("\u2705 All checks passed!")
    sys.exit(0)
