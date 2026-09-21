#!/usr/bin/env python3
"""A compose path that belongs to a stack manager says so (#2, #65).

`com.docker.compose.project.config_files` records the path the thing
that CREATED the stack saw, and that thing is usually itself a
container. Portainer keeps stacks at `/data/compose/<id>/` inside its
own container; Dockge and Dockhand at `/app/data/stacks/`. None of those
exist on the host.

The old advice — "mount that directory into the Docksentry container" —
therefore sent people looking for a directory that is not there. Three
of them hit it in one week, each with a different manager, and each
concluded their own mount was wrong. It was not. The advice was.
"""
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import compose_paths as cp                            # noqa: E402
from i18n import get_translator                       # noqa: E402

checks = {}

cases = {
    "/data/compose/83/docker-compose.yml": ("Portainer", "/data/compose"),
    "/data/compose/119/docker-compose.yml": ("Portainer", "/data/compose"),
    "/app/data/stacks/QNAP/dozzle/compose.yaml":
        ("Dockge or Dockhand", "/app/data/stacks"),
}
for path, (owner, mount) in cases.items():
    checks[f"recognises {owner} in {path.split('/')[2]}"] = (
        cp.owner(path) == owner)
    checks[f"…and points at {mount}"] = cp.mount_root(path) == mount

# A path we do not recognise gets no guess. A confident wrong name is
# worse than no name: the generic advice is imperfect but true.
# `/opt/stacks` is Dockge's, and deliberately NOT recognised: Dockge
# mounts it at the identical path, so the label is a valid host path and
# there is nothing to map. Claiming otherwise would tell somebody they
# have a problem they do not have.
for path in ("/home/leet/stacks/app/compose.yml", "compose.yml",
             "/opt/stacks/media/compose.yml",
             "/opt/dockers/cft-base/x/compose.yml", "", None):
    checks[f"no guess for {path!r}"] = (
        cp.owner(path) is None and cp.mount_root(path) is None)

# The mount root is the manager's data directory, not the stack's own
# folder: one mount covers every stack it holds.
checks["one mount covers every stack"] = (
    cp.mount_root("/data/compose/1/docker-compose.yml")
    == cp.mount_root("/data/compose/999/docker-compose.yml"))

# ── who holds the file, read off the daemon ──────────────────────────
# The label says where the file was when it was written; only the
# daemon knows which container that path is inside now. Answering
# "your source is wrong" without this is a sentence that stops one
# word before the answer (#63).
OURS = {"name": "DockSentry", "image": "amayer1983/docksentry:latest",
        "type": "bind", "vol": "", "src": "/share/Container/stacks",
        "dest": "/app/data/stacks"}
DOCKGE = {"name": "dockge", "image": "louislam/dockge:1", "type": "bind",
          "vol": "", "src": "/share/CACHEDEV1_DATA/stacks",
          "dest": "/app/data/stacks"}
P = "/app/data/stacks/QNAP/dockmon/compose.yaml"

checks["the manager wins over the bystander mounted just as deep"] = (
    cp.holder(P, [OURS, DOCKGE]) == ("/share/CACHEDEV1_DATA/stacks",
                                     "/app/data/stacks", "dockge"))
checks["…whichever order the daemon lists them in"] = (
    cp.holder(P, [DOCKGE, OURS]) == cp.holder(P, [OURS, DOCKGE]))
checks["the deeper mount wins over the shallower one"] = (
    cp.holder(P, [DOCKGE, {**DOCKGE, "name": "deep",
                           "src": "/srv/qnap",
                           "dest": "/app/data/stacks/QNAP"}])[0] == "/srv/qnap")
checks["a named volume answers with the volume, not a path"] = (
    cp.holder("/data/compose/83/docker-compose.yml",
              [{"name": "portainer", "image": "portainer/portainer-ce",
                "type": "volume", "vol": "portainer_data", "src": "/var/lib/x",
                "dest": "/data"}])[0] == "portainer_data")
checks["nothing covering the path is not a holder"] = (
    cp.holder(P, [{**OURS, "dest": "/somewhere/else"}]) is None)
checks["…and neither is an empty daemon"] = cp.holder(P, []) is None
# Two managers at the same depth: there is no honest answer, and a
# confidently wrong mount is what #2 and #65 were about.
checks["two managers at the same depth is refused, not guessed"] = (
    cp.holder(P, [DOCKGE, {**DOCKGE, "name": "dockge2",
                           "src": "/srv/other"}]) is cp.AMBIGUOUS)
checks["…and that refusal is not mistaken for absence"] = (
    cp.AMBIGUOUS is not None)

# ── the message, in every language ───────────────────────────────────
for lang in ("en", "de", "ja", "ar"):
    t = get_translator(lang)
    out = t("compose_fallback_managed", file="/data/compose/83/x.yml",
            manager="Portainer", mount="/data/compose")
    checks[f"[{lang}] the message names the manager"] = "Portainer" in out
    checks[f"[{lang}] …and the path to mount at"] = "/data/compose" in out
    checks[f"[{lang}] …and is not the untranslated key"] = (
        out != "compose_fallback_managed")

# ── and the update path chooses between the two ──────────────────────
src = open(os.path.join(APP, "update_checker.py"), encoding="utf-8").read()
checks["a recognised path gets the specific message"] = (
    'self._t("compose_fallback_managed"' in src)
checks["…and an unrecognised one keeps the general one"] = (
    'self._t("compose_fallback", file=config_file or "?")' in src)

for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
