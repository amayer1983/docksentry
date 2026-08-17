#!/usr/bin/env python3
"""The rebuilt interface, only for people who asked for it.

The owner wants a second generation of the Web UI — the current one is
too dense in places, measurably so: 25 containers on the status page
produce 236 forms, 380 fields and 289 buttons, about nine forms per row,
and 426 of its explanations sit in `title` tooltips that a phone cannot
show at all.

That is a rebuild, not a tidy-up, and a rebuild has to be visible to the
handful of people testing it and to nobody else — "nur die, die wissen
wie". Which makes the gate a design decision worth pinning:

**An environment variable, not a saved setting.** A saved value outranks
the environment, so a test switch that reached settings.json would be one
you could not turn off again without editing a file inside the volume.
That is exactly the trap @famewolf fell into three times in one night
with DISK_WARN_AUTO_CLEANUP, WEB_PASSWORD and BOT_LABEL (#2). So
`web_ui_v2` is absent from PERSISTENT_KEYS and from PERSISTENT_ENV_VARS,
and reading it never touches the file.

**And no toggle in the interface.** A switch in the UI is a switch
everybody finds, which is the opposite of the point.

**One image, not a second one.** `:beta` gates a whole version; this has
to be something a tester can turn on against their real instance and
turn off again, without a parallel container.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from config import (Config, PERSISTENT_ENV_VARS,  # noqa: E402
                    PERSISTENT_KEYS)

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


# ── the switch itself ────────────────────────────────────────────────
checks["off unless asked for"] = with_env(WEB_UI_V2=None).web_ui_v2 is False
checks["WEB_UI_V2=true turns it on"] = (
    with_env(WEB_UI_V2="true").web_ui_v2 is True)
for spelling in ("1", "yes", "TRUE", "True"):
    checks[f"…and so does {spelling!r}"] = (
        with_env(WEB_UI_V2=spelling).web_ui_v2 is True)
for off in ("false", "0", "no", "", "maybe"):
    checks[f"{off!r} leaves it off"] = (
        with_env(WEB_UI_V2=off).web_ui_v2 is False)

# ── it must never become a saved setting ─────────────────────────────
checks["not a persistent key"] = "web_ui_v2" not in PERSISTENT_KEYS
checks["…and not seeded through the persistent env map"] = (
    "web_ui_v2" not in PERSISTENT_ENV_VARS)
_cfg_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                             "config.py"), encoding="utf-8").read()
checks["…so save_persistent cannot freeze it"] = (
    "web_ui_v2" not in _cfg_src.split("PERSISTENT_KEYS = [")[1].split("]")[0])

# The real test of that: a value smuggled into settings.json must not
# switch the interface on. Verified against a live container too — this
# pins the mechanism that made it true.
import json  # noqa: E402
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "settings.json"), "w") as f:
        json.dump({"web_ui_v2": True, "language": "en"}, f)
    c = with_env(DATA_DIR=tmp, WEB_UI_V2=None)
    checks["a saved value cannot switch it on"] = c.web_ui_v2 is False
    # …and the environment still wins when it is the one asking.
    c = with_env(DATA_DIR=tmp, WEB_UI_V2="true")
    checks["…while the environment still can"] = c.web_ui_v2 is True

# ── nothing announces it ─────────────────────────────────────────────
web_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "web_ui.py"), encoding="utf-8").read()
checks["the page says which generation it is"] = (
    'data-ui="{ui_gen}"' in web_src)
checks["…and carries a class the stylesheet can hang off"] = (
    'v2_class = " ui-v2" if v2 else ""' in web_src)
checks["there is no toggle for it in the interface"] = (
    "web_ui_v2" not in web_src.replace('getattr(config, "web_ui_v2", False)', "")
)

# My first instinct was to leave it out of the reference so only the
# people who were told would find it. `pre-commit-check.py` refused —
# every env var must be documented — and it is right: an undocumented
# variable is precisely the shape this project has been bitten by. So it
# is listed, and listed honestly. Discretion comes from nothing linking
# to it and from it being off by default, not from hiding it in our own
# documentation.
docs = open(os.path.join(os.path.dirname(__file__), "..", "docs",
                         "configuration.md"), encoding="utf-8").read()
row = [l for l in docs.splitlines() if "`WEB_UI_V2`" in l]
checks["documented, like every other variable"] = len(row) == 1
checks["…defaulting to off"] = "`false`" in row[0]
checks["…and saying plainly that it is unfinished"] = (
    "Unfinished" in row[0] and "not supported" in row[0])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
