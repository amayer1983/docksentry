#!/usr/bin/env python3
"""Two ways to draw the status page, and a switch between them.

The status page was measurably the dense one: 25 containers produced 236
forms, 380 fields and 289 buttons, about nine forms per row, and 426 of
its explanations sat in `title` tooltips a phone cannot show. Worse, in a
tool whose job is telling you when a container needs updating, the row
never said whether it needed updating.

The first attempt at that was a whole second interface behind an env-only
`WEB_UI_V2`, for testers. The owner pushed back, and he was right: the
same measurements showed Settings — four tabs, 45 fields — was the
tidiest page in the product. A second interface would have been solving a
problem the numbers said did not exist on five of the six pages.

So it is one page with two layouts, and an ordinary setting to pick:

  table — every action on every row, wide layout (the original)
  list  — update state first, everything else in a detail panel

Which makes the defaults matter. `table` wins, because an upgrade must
not rearrange a page somebody already knows how to use. And it is a
normal persisted setting rather than an env-only switch: a preference is
supposed to survive a restart, which is exactly the opposite of what a
test flag wants.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from config import (Config, PERSISTENT_ENV_DEFAULTS,  # noqa: E402
                    PERSISTENT_ENV_VARS, PERSISTENT_KEYS)

checks = {}


def with_env(**env):
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return Config.from_env()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── the setting ──────────────────────────────────────────────────────
checks["an upgrade keeps the page it already had"] = (
    with_env(STATUS_VIEW=None).status_view == "table")
checks["the compact list can be asked for"] = (
    with_env(STATUS_VIEW="list").status_view == "list")
checks["…case and whitespace do not matter"] = (
    with_env(STATUS_VIEW=" LIST ").status_view == "list")
for junk in ("v2", "compact", "", "true"):
    checks[f"{junk!r} falls back to the table rather than guessing"] = (
        with_env(STATUS_VIEW=junk).status_view == "table")

# ── it is a preference, so it persists ───────────────────────────────
checks["persisted like every other preference"] = (
    "status_view" in PERSISTENT_KEYS)
checks["…seeded from the environment"] = (
    PERSISTENT_ENV_VARS.get("status_view") == "STATUS_VIEW")
checks["…with a default the override check can compare against"] = (
    PERSISTENT_ENV_DEFAULTS.get("status_view") == "table")

with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "settings.json"), "w") as f:
        json.dump({"status_view": "list"}, f)
    checks["a saved choice survives a restart"] = (
        with_env(DATA_DIR=tmp, STATUS_VIEW=None).status_view == "list")

# ── and it is findable, which is the whole lesson of #2 ──────────────
web_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "web_ui.py"), encoding="utf-8").read()
checks["the chooser is on the settings page"] = (
    'name="status_view" form="settings-form"' in web_src)
checks["…offering both, and only both"] = (
    web_src.count('<option value="table"') == 1
    and web_src.count('<option value="list"') == 1)
checks["…and the form that saves settings accepts it"] = (
    'if _sv in ("table", "list"):' in web_src)
checks["it is not hidden behind the advanced mode"] = (
    "adv-only" not in web_src[web_src.index('name="status_view"') - 400:
                              web_src.index('name="status_view"')])
checks["the page says which layout it drew"] = 'data-ui="{ui_gen}"' in web_src
checks["…and the list view is what asks for its stylesheet"] = (
    'v2 = getattr(config, "status_view", "table") == "list"' in web_src)

docs = open(os.path.join(os.path.dirname(__file__), "..", "docs",
                         "configuration.md"), encoding="utf-8").read()
row = [l for l in docs.splitlines() if "`STATUS_VIEW`" in l]
checks["documented, like every other variable"] = len(row) == 1
checks["…saying which one is the default"] = "`table`" in row[0]
checks["…and that the interface can change it too"] = "Settings" in row[0]
# The env-only test flag is gone: a preference that cannot be changed
# from the interface is the trap this project spent a whole day on.
checks["the old WEB_UI_V2 switch is gone"] = "WEB_UI_V2" not in docs and (
    "web_ui_v2" not in web_src)

# ── the mobile card list must be a SIBLING of the table, not a child
# (#63, @NotRetarded). The table view renders both a `<table>` (wrapped
# in `.table-scroll`) and a `.tile-list` of cards; CSS shows the table on
# desktop and the cards on a phone by setting `.table-scroll{display:none}`
# below 700px. The tile-list had been placed INSIDE `.table-scroll`, so on
# a phone it inherited that display:none and vanished — 16 containers, an
# empty list, and no table either. Measured with a headless browser at
# 390px: `.tile-list` computed to `display:grid` but height 0 and
# offsetParent null, because its parent was hidden. The fix is structural:
# `.table-scroll` wraps the table ALONE. This pins that the scroll wrapper
# closes before the tile-list opens, so the two are siblings.
i = web_src.index('<div class="table-scroll"><table id="ctbl">')
j = web_src.index('<div class="tile-list" id="ctblTiles">', i)
between = web_src[i:j]
# `.table-scroll` (which is display:none on a phone) must wrap the table
# ALONE, closing before the tile-list opens — otherwise the mobile card
# list is a child of a hidden element and vanishes. The `</table></div>`
# is that early close; without it the tile-list is nested and the phone
# view shows nothing.
checks["the scroll wrapper closes right after the table, not around the cards"] = (
    "</table></div>" in between)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
