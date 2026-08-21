#!/usr/bin/env python3
"""Changelog logic lives in the neutral core, not in a front end (#63).

First step of the core extraction: `changelog.py` reads CHANGELOG.md and
compares versions, and both bots call it as equals. Discord used to reach
into the Telegram bot instance (`bot._parse_changelog_entries`,
`bot._fetch_changelog`) — that coupling is what this pins gone. It also
covers a bug found in passing: Discord's `/changelog` did
`"\n\n".join(entries)` over a list of (version, date, body) TUPLES, which
raised TypeError; the adapter renders the entries now.
"""
import os
import re
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}

# ── the neutral module exists and carries the logic ──────────────────
import changelog  # noqa: E402
for fn in ("fetch", "version_key", "parse_entries", "entry_for"):
    checks[f"changelog.{fn} exists"] = callable(getattr(changelog, fn, None))

# ── the Telegram bot no longer owns any of it ────────────────────────
tsrc = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
for gone in ("_parse_changelog_entries", "_version_key",
             "_changelog_entry_for", "_fetch_changelog"):
    checks[f"telegram_bot no longer defines {gone}"] = (
        f"def {gone}" not in tsrc)
checks["…and calls the core instead"] = (
    "changelog.fetch()" in tsrc and "changelog.parse_entries(" in tsrc
    and "changelog.entry_for(" in tsrc)

# ── Discord calls the core directly, not the Telegram bot ────────────
dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
i = dsrc.index("def _cmd_changelog")
body = dsrc[i:dsrc.index("\n    def ", i + 10)]
checks["Discord's /changelog calls the core"] = (
    "changelog.fetch()" in body and "changelog.parse_entries(" in body)
checks["…and stops borrowing it from the Telegram bot"] = (
    "bot._fetch_changelog" not in body
    and "bot._parse_changelog_entries" not in body)
checks["…and renders the tuples instead of join-crashing on them"] = (
    'join(blocks)' in body and 'join(entries)' not in body)

# ── behavioural: the rendered Discord reply is a real string ─────────
SAMPLE = ("# Changelog\n\n"
          "## [2.18.0-beta.13] - 2026-08-20\nthirteenth\n\n"
          "## [2.17.0] - 2026-08-18\nstable\n")
entries = changelog.parse_entries(SAMPLE, "2.17.0")
blocks = [f"**{v}** — {d}\n{body}" for v, d, body in entries]
rendered = "\n\n".join(blocks)
checks["the render produces a string, not a TypeError"] = (
    isinstance(rendered, str) and "2.18.0-beta.13" in rendered
    and "thirteenth" in rendered)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
