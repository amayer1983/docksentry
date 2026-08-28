#!/usr/bin/env python3
"""A relative `config_files` label resolves against `working_dir` (#65).

`com.docker.compose.project.config_files` is an absolute path on modern
Compose. It is not always: a label written as plain `compose.yml` turns
up in the wild, and @LeeNX's is exactly that — his log says

    Compose file not found: compose.yml — falling back to standalone

with no path in front of it, where this machine's own containers carry
`/home/amayer/docksentry/docker-compose.dev.yml`.

Relative to what? Not to our own working directory — Docksentry runs in
a container, where that is `/app` — but to
`com.docker.compose.project.working_dir`, which Docker records absolute
beside it. Without resolving against that first, `os.path.isfile()` on
such a label fails every single time, and the stack drops silently into
the standalone `docker run` recreate. That is the path that rebuilds the
container from its inspect data and loses the healthcheck he came to
report: the fallback, not the compose path, is what broke his tunnel.
"""
import os
import sys
import tempfile

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
from update_checker import UpdateChecker              # noqa: E402

checks = {}

# Against a build that predates this fix, `_compose_files` takes one
# argument and every call below would raise TypeError — an abort, not a
# result, and an abort tells a reader nothing about the behaviour. So
# the signature is the first check, and the rest only runs behind it.
import inspect                                        # noqa: E402

_params = list(inspect.signature(
    UpdateChecker._compose_files).parameters)
checks["_compose_files can be told the working directory"] = (
    "working_dir" in _params)
if not checks["_compose_files can be told the working directory"]:
    print("  ❌ _compose_files can be told the working directory")
    print("     (an older build: it cannot resolve a relative label at all)")
    print("FAIL")
    sys.exit(1)

f = UpdateChecker._compose_files

project = tempfile.mkdtemp()
single = os.path.join(project, "compose.yml")
override = os.path.join(project, "override.yml")
open(single, "w").close()
open(override, "w").close()

# ── the case that was broken ─────────────────────────────────────────
checks["a bare filename resolves against working_dir"] = (
    f("compose.yml", project) == [single])
checks["…and the file is then actually found"] = os.path.isfile(
    f("compose.yml", project)[0])

# Several files in one label, all relative — the same shape, comma-joined.
checks["comma-joined relative files resolve too"] = (
    f("compose.yml,override.yml", project) == [single, override])

# ── and nothing else changes ─────────────────────────────────────────
checks["an absolute path ignores working_dir"] = (
    f(single, project) == [single])
checks["absolute comma-joined paths are unchanged"] = (
    f(f"{single},{override}", project) == [single, override])
checks["no working_dir leaves a relative path alone"] = (
    f("compose.yml") == ["compose.yml"])
checks["an empty label is still nothing"] = f("", project) == []

# A path may legitimately contain a comma and the label format gives no
# way to tell that apart, so the split is only trusted when every piece
# is a real file. That rule predates this change and must survive it:
# resolving must not make a bad split look good.
comma_name = os.path.join(project, "we,ird.yml")
open(comma_name, "w").close()
checks["a comma inside a filename is not split blindly"] = (
    f(comma_name, project) == [comma_name])
checks["…and a split that does not resolve is left whole"] = (
    f("nope.yml,alsonope.yml", project) == [
        os.path.join(project, "nope.yml,alsonope.yml")])

# ── the call site passes it ──────────────────────────────────────────
src = open(os.path.join(APP, "update_checker.py"), encoding="utf-8").read()
checks["the compose update hands its working_dir over"] = (
    "self._compose_files(config_file, working_dir)" in src)

for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
