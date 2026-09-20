#!/usr/bin/env python3
"""A table with no card version must not vanish on a phone.

The status page shows cards below 700px instead of a five-column table
you would otherwise swipe sideways through. The rule that does it hid
**every** `.table-scroll` on the site, and exactly one of the ten has
cards behind it. So on a phone nine tables were simply blank: the
container detail page and its history tab, the history page, events,
audit, API tokens, the info card, update windows and the pending-major
list.

Ten, not the six I first counted — four of them do not start their line
with `<div class=`, which is what my grep matched. The scan gave me
candidates and I read them as the answer.

@NotRetarded reported it twice — "I don't see anything under the
overview tab" — and the first time I decided his screenshot was cropped
and told him to scroll. It was not cropped. The rule was too wide.

So the rule names the one table that steps aside, and this test fails if
a second table ever gets that class without cards to replace it.
"""
import os
import re
import sys

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")
css = open(os.path.join(ROOT, "app", "static", "app.css"), encoding="utf-8").read()
web = open(os.path.join(ROOT, "app", "web_ui.py"), encoding="utf-8").read()

# ── the rule only hides what has a replacement ───────────────────────
# Cut at the closing brace of the media block, not at the first `}\n` —
# a comment inside it contains one, and the slice then stopped early.
_blk = css.split("@media (max-width: 700px)")[-1]
narrow = _blk.split("\n}")[0]
checks["the narrow rule hides only the marked table"] = (
    ".table-scroll.has-tiles { display: none; }" in css)
checks["…and no longer every table on the site"] = (
    not re.search(r"^\s*\.table-scroll\s*\{\s*display:\s*none", css, re.M))
checks["the cards still appear in its place"] = (
    ".tile-list { display: grid;" in narrow)

# ── exactly one table claims it, and that one has the cards ──────────
marked = re.findall(r'class="table-scroll has-tiles"', web)
checks["exactly one table steps aside on a phone"] = len(marked) == 1
_after = web.split('class="table-scroll has-tiles"')[1][:3000]
checks["…and its card list follows it"] = 'class="tile-list"' in _after

# ── every other table stays visible ──────────────────────────────────
# Counted, not sampled: six tables, five of which have no card version
# and must therefore keep rendering.
total = len(re.findall(r'class="table-scroll', web))
plain = len(re.findall(r'class="table-scroll"', web))
checks["all ten tables are accounted for"] = total == 10
checks["…and the other nine keep rendering"] = plain == 9

# ── and they are usable, not merely present ──────────────────────────
base = css.split(".table-scroll {")[1].split("}")[0]
checks["a wide table scrolls sideways instead of being cut off"] = (
    "overflow-x: auto" in base and "max-width: 100%" in base)

# ── and the row points at the page that has the answer ───────────────
# The container page has carried the exact path and the missing mount
# line since 01.09. @NotRetarded spent three weeks on that problem
# without knowing the page had it — an answer nobody can find is not an
# answer. A mark in the row says so from where the container is.
row = web.split("def _status_row")[1].split("\n        def ")[0]
checks["a row whose Compose file is out of reach says so"] = (
    "web_badge_compose_unreachable" in row)
checks["…worked out from labels already in hand"] = (
    'c.get("labels")' in row and "self._compose_reach(" in row)
checks["…and not from a fresh daemon round trip per row"] = (
    "inspect" not in row.split('c.get("labels")')[1][:600])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
