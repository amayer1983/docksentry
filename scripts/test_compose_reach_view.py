#!/usr/bin/env python3
"""The detail page says where the compose file is, and whether we can open it.

@NotRetarded mounted his stacks exactly where we told him to and still got
"not reachable", with no way to see which path Docksentry was actually
opening (#2). The page now shows it. What matters is that it answers with
the SAME resolver the update path uses — a page that says "reachable"
while `docker compose up` disagrees is worse than a page that says nothing.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402
from update_checker import UpdateChecker  # noqa: E402

checks = {}

_CFG = type("C", (), {"language": "en", "ui_mode": "advanced", "debug": False})()
_HANDLER = web_ui.create_handler(_CFG, None, bot=None, store=None)


def _reach(info):
    # Reached through the class the page uses, not a copy of the logic.
    return _HANDLER._compose_reach(info)


with tempfile.TemporaryDirectory() as d:
    here = os.path.join(d, "compose.yml")
    open(here, "w").write("services: {}\n")
    other = os.path.join(d, "override.yml")
    open(other, "w").write("services: {}\n")

    paths, ok = _reach({"compose_file": here})
    checks["an absolute path that exists reads as reachable"] = (paths == [here] and ok)

    paths, ok = _reach({"compose_file": os.path.join(d, "gone.yml")})
    checks["a missing file reads as not reachable"] = (not ok and len(paths) == 1)

    checks["the path is shown even when it cannot be opened"] = (
        paths[0].endswith("gone.yml"))

    paths, ok = _reach({"compose_file": "compose.yml", "compose_working_dir": d})
    checks["a relative label is resolved, not given up on"] = (paths == [here] and ok)

    paths, ok = _reach({"compose_file": f"{here},{other}"})
    checks["a two-file label is split, and both count"] = (len(paths) == 2 and ok)

    paths, ok = _reach({"compose_file": f"{here},{os.path.join(d, 'gone.yml')}"})
    checks["one missing file is enough to be unreachable"] = (not ok)

    checks["no label at all says nothing rather than guessing"] = (
        _reach({"compose_file": ""}) == ([], False))

    # The point of the whole row: same answer as the updater.
    same = UpdateChecker._compose_files(f"{here},{other}", None)
    checks["it asks the update path's own resolver"] = (
        _reach({"compose_file": f"{here},{other}"})[0] == same)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
