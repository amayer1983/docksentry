#!/usr/bin/env python3
"""Pre-commit check: verify all languages are in sync and README is complete."""

import json
import os
import sys

LANG_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "lang")
README = os.path.join(os.path.dirname(__file__), "..", "README.md")

errors = 0


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
            "AUTO_SELFUPDATE", "LANGUAGE", "WEB_UI", "WEB_PORT", "WEB_PASSWORD", "TZ"]:
    check(var in readme, var)

print("\n=== README: COMMANDS ===")
for cmd in ["/status", "/check", "/updates", "/cleanup", "/history",
            "/pin", "/unpin", "/autoupdate", "/selfupdate", "/debug", "/lang", "/settings", "/help"]:
    check(cmd in readme, cmd)

print("\n=== README: FEATURES ===")
for feat in ["Web UI", "Multi-language", "Auto-rollback", "Self-update"]:
    check(feat.lower() in readme.lower(), feat)

print()
if errors:
    print(f"\u274c {errors} issue(s) found. Fix before committing!")
    sys.exit(1)
else:
    print("\u2705 All checks passed!")
    sys.exit(0)
