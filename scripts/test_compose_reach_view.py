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

# The mount line the page offers underneath an unreachable path.
_targets = _HANDLER._compose_mount_targets

checks["a recognised manager gets its root, not one mount per stack"] = (
    _targets(["/data/compose/83/docker-compose.yml"], "foo") == ["/data/compose"])
checks["anything else gets the file's own directory"] = (
    _targets(["/opt/stacks/plex/compose.yaml"], "plex") == ["/opt/stacks/plex"])
checks["a plain project is mounted onto itself, not its parent"] = (
    _targets(["/home/you/vereinskasse/docker-compose.yml"], "vereinskasse")
    == ["/home/you/vereinskasse"])
checks["two files in one directory make one mount, not two"] = (
    _targets(["/a/b/compose.yml", "/a/b/override.yml"], "b") == ["/a/b"])
checks["two directories make two mounts"] = (
    len(_targets(["/a/b/compose.yml", "/c/d/compose.yml"], "x")) == 2)

# The exact line, read off whichever container already holds the files.
# @NotRetarded's Portainer keeps its stacks in a named volume, so "mount
# that directory" was never an instruction anyone could follow (#2).
_M = [
    {"image": "portainer/portainer-ce:latest", "type": "volume",
     "vol": "portainer_data", "src": "/var/lib/docker/volumes/x/_data", "dest": "/data"},
    {"image": "redis:7", "type": "bind",
     "vol": "", "src": "/srv/redis", "dest": "/data"},
    {"image": "dockhand/dockhand:latest", "type": "bind",
     "vol": "", "src": "/share/Container/Dockhand/stacks", "dest": "/opt/stacks"},
]
_orig_all = _HANDLER._all_mounts


def _with_mounts(rows, paths):
    _HANDLER._all_mounts = staticmethod(lambda: rows)
    try:
        return _HANDLER._compose_mount_exact(paths)
    finally:
        _HANDLER._all_mounts = staticmethod(_orig_all)


checks["a named volume is named, not turned into a directory"] = (
    _with_mounts(_M, ["/data/compose/13/docker-compose.yml"])
    == [("portainer_data", "/data")])
checks["a manager we have never heard of still resolves"] = (
    _with_mounts(_M, ["/opt/stacks/plex/compose.yaml"])
    == [("/share/Container/Dockhand/stacks", "/opt/stacks")])
checks["a host path resolves to nothing, so the self-mount stands"] = (
    _with_mounts(_M, ["/home/you/stacks/docker-compose.yml"]) is None)
checks["two equally deep mounts and no manager: say nothing"] = (
    _with_mounts([dict(_M[1]), dict(_M[1], src="/srv/other")],
                 ["/data/compose/13/docker-compose.yml"]) is None)
checks["the manager breaks the tie against a plain container"] = (
    _with_mounts(_M, ["/data/compose/2/docker-compose.yml"])
    == [("portainer_data", "/data")])
checks["no mount information at all is not a guess"] = (
    _with_mounts([], ["/data/compose/2/docker-compose.yml"]) is None)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
