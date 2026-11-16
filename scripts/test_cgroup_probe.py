#!/usr/bin/env python3
"""Which cgroup version the daemon runs — asked of Docker AND of Podman.

`_build_run_args` emits `--memory-swappiness` only when it is told the
host is not cgroup v2, because that knob is a v1-only control and crun
rejects it outright on a v2 host (#50). So the recreate path is only as
right as this probe is.

The probe asked `info --format "{{.CgroupVersion}}"`. That is Docker's
field name. Podman has no such field and answers with a template error:

    Error: template: info:1:2: executing "info" at <.CgroupVersion>:
    can't evaluate field CgroupVersion in type *define.Info

…so the probe fell through to its "1" default on EVERY podman host, and
every recreate there carried a flag the runtime can refuse. Measured
17.11.2026 on a host where `docker info` says `2` and `podman info
--format '{{.Host.CgroupsVersion}}'` says `v2`: the probe said "1" for
podman and "2" for docker, on the same kernel, with
`/sys/fs/cgroup` a cgroup2fs.

Driven with stand-ins that answer the way each engine really answers —
including Podman's leading `v`, and including the failure shapes, where
the safe answer is the conservative "1" rather than a guess.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

checks = {}


class _Engine:
    """Answers `info --format <fmt>` from a table; everything else fails."""

    def __init__(self, name, answers):
        self.name = name
        self.answers = answers
        self.asked = []

    def run(self, args, timeout=None, **kw):
        if args[:2] == ["info", "--format"]:
            fmt = args[2]
            self.asked.append(fmt)
            if fmt in self.answers:
                rc, out = self.answers[fmt]
                return types.SimpleNamespace(returncode=rc, stdout=out,
                                             stderr="")
        return types.SimpleNamespace(returncode=125, stdout="", stderr="")


_DOCKER_FIELD = "{{.CgroupVersion}}"
_PODMAN_FIELD = "{{.Host.CgroupsVersion}}"

_PODMAN_TEMPLATE_ERROR = (
    'Error: template: info:1:2: executing "info" at <.CgroupVersion>: '
    "can't evaluate field CgroupVersion in type *define.Info")


def _probe(engine):
    UpdateChecker._cgroup_version_cache.clear()
    try:
        return UpdateChecker._cgroup_version(engine)
    finally:
        UpdateChecker._cgroup_version_cache.clear()


# ── Podman, as it really answers ─────────────────────────────────────
podman = _Engine("podman", {
    _DOCKER_FIELD: (125, _PODMAN_TEMPLATE_ERROR),
    _PODMAN_FIELD: (0, "v2\n"),
})
checks["podman on cgroup v2 is read as v2"] = _probe(podman) == "2"
checks["…and the podman field was actually consulted"] = (
    _PODMAN_FIELD in podman.asked)

podman_v1 = _Engine("podman", {
    _DOCKER_FIELD: (125, _PODMAN_TEMPLATE_ERROR),
    _PODMAN_FIELD: (0, "v1\n"),
})
checks["…while podman on cgroup v1 is still read as v1"] = (
    _probe(podman_v1) == "1")

# ── Docker, unchanged ────────────────────────────────────────────────
docker = _Engine("docker", {_DOCKER_FIELD: (0, "2\n")})
checks["docker on cgroup v2 is read as v2"] = _probe(docker) == "2"
checks["…and docker is answered on the first question"] = (
    docker.asked == [_DOCKER_FIELD])
docker_v1 = _Engine("docker", {_DOCKER_FIELD: (0, "1\n")})
checks["…and docker on cgroup v1 as v1"] = _probe(docker_v1) == "1"

# ── the failure shapes: conservative, never a guess ──────────────────
# "1" is the safe answer: it only ever ADDS a flag that a v1 host
# accepts. Guessing "2" on a host we cannot read would silently drop a
# memory setting the container was running with.
mute = _Engine("mystery", {})
checks["an engine that answers neither question falls back to v1"] = (
    _probe(mute) == "1")
empty = _Engine("docker", {_DOCKER_FIELD: (0, ""), _PODMAN_FIELD: (0, "")})
checks["…and so does an empty answer"] = _probe(empty) == "1"
junk = _Engine("docker", {_DOCKER_FIELD: (0, "banana"),
                          _PODMAN_FIELD: (0, "also banana")})
checks["…and so does a nonsense answer"] = _probe(junk) == "1"


class _Explodes:
    name = "broken"

    def run(self, *a, **kw):
        raise OSError("socket is gone")


checks["…and a backend that raises does not take the caller down"] = (
    _probe(_Explodes()) == "1")

# ── the consequence, driven ──────────────────────────────────────────
# The point of the probe is this one flag. A v2 answer must keep it out
# of the rebuilt command, and a v1 answer must keep it in — otherwise
# the probe could be right and the recreate still wrong.
_cfg = {"HostConfig": {"MemorySwappiness": 0}, "Config": {"Image": "x"},
        "Name": "/c"}
_uc = UpdateChecker.__new__(UpdateChecker)
_uc._backend = docker
for version, want_flag in (("1", True), ("2", False)):
    _cmd = _uc._build_run_args(_cfg, "x", "c", None, cgroup_version=version)
    checks[f"with cgroup {version}, --memory-swappiness is "
           f"{'kept' if want_flag else 'left out'}"] = (
        ("--memory-swappiness" in _cmd) is want_flag)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
