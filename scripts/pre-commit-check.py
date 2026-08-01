#!/usr/bin/env python3
"""Pre-commit check: verify all languages are in sync and README is complete."""

import json
import glob
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

# 4b. Emoji doubled next to a styled icon (#46, @LeeNX).
#     A label like "🔍 Check Updates" is right in Telegram, where the
#     emoji IS the button's icon. In the Web UI the styled SVG is the
#     icon, so the emoji draws a second one beside it. v1.47.4 fixed
#     that for the legend, but the global "Check Updates" button kept
#     doing it for months, in all 16 languages, top-right on the main
#     page — nobody spots this in a diff, only on screen. So: catch it
#     mechanically. Wrap the label in _strip_emoji() to fix a hit.
def _icon_text_keys(src):
    return set(_re.findall(
        r'\{_ICONS\[[^\]]+\]\}<span>\{t\("([a-z0-9_]+)"\)\}', src))


_icon_keys = _icon_text_keys(web_src)
_doubled = {}
for _lf in sorted(glob.glob(os.path.join(LANG_DIR, "*.json"))):
    with open(_lf, encoding="utf-8") as _f:
        _d = json.load(_f)
    for _k in _icon_keys:
        _val = _d.get(_k, "")
        if isinstance(_val, str) and _val and _re.match(r"[^\w(]", _val):
            _doubled.setdefault(_k, []).append(
                os.path.basename(_lf)[:-5])
for _k, _langs in sorted(_doubled.items()):
    print(f"     {_k}: leading emoji next to a styled icon in "
          f"{len(_langs)} language(s) — wrap it in _strip_emoji()")
check(not _doubled, "no emoji doubled next to a styled icon")

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

# 3b. Release safety: a pre-release must never move the `latest` tag.
#     Everyone running `docksentry:latest` with AUTO_SELFUPDATE on would be
#     pulled onto an rc/beta overnight, without a click and without being
#     asked. This is a crude presence check on the workflow rather than a
#     YAML parse — it exists so the guard can't be deleted unnoticed, which
#     is the realistic failure, not someone rewriting it cleverly.
print("\n=== RELEASE SAFETY ===")
_wf = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows",
                   "docker-publish.yml")
try:
    with open(_wf) as _f:
        _wf_text = _f.read()
    _has_guard = ('*-*' in _wf_text and 'PRERELEASE' in _wf_text)
    check(_has_guard,
          "pre-releases (x.y.z-rc.N) do not get tagged :latest")
except OSError as _e:
    check(False, f"publish workflow unreadable: {_e}")

# 4. Contract lint — return-arity drift + cross-class doppelgangers
#    (the v1.37.1 _wait_healthy crash class). Pure stdlib AST.
print("\n=== CONTRACT LINT ===")
import subprocess as _sp
_lc = _sp.run([sys.executable, os.path.join(os.path.dirname(__file__), "lint_contracts.py")],
              capture_output=True, text=True)
print(_lc.stdout.strip())
check(_lc.returncode == 0, "contract lint (return shapes)")

print()
if errors:
    print(f"\u274c {errors} issue(s) found. Fix before committing!")
    sys.exit(1)
else:
    print("\u2705 All checks passed!")
    sys.exit(0)
