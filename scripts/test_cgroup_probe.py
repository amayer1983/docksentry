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

import json
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


# Inside a try, because what is under test here is exactly whether the
# probe raises. Bare, a probe without its `except` took this procedure
# down with a traceback and no verdict line (audit, 18.11.2026), which
# `test_guard_mutations.py` scores [unrunnable], not caught.
try:
    _exploded = _probe(_Explodes())
except Exception as exc:                                      # noqa: BLE001
    _exploded = f"raised {type(exc).__name__}: {exc}"
checks["…and a backend that raises does not take the caller down"] = (
    _exploded == "1")
if _exploded != "1":
    print(f"      → a backend that raises: {_exploded}")

# ── a failed probe is not remembered ─────────────────────────────────
# The fallback "1" used to be cached for the host like a real answer, so
# one `info` that timed out on the first recreate marked a cgroup-v2
# podman host as v1 until Docksentry restarted, and every later recreate
# carried `--memory-swappiness` again (#50). Each call below stands for
# one recreate; the cache is cleared only once, at the start, because
# what is under test is what the cache keeps between recreates.
import subprocess  # noqa: E402


class _Flaky(_Engine):
    """Fails every `info` while `down` is set, otherwise answers like
    podman on v2. `_recreates` sets it for the first recreate only."""

    down = False

    def __init__(self, how):
        super().__init__("podman", {
            _DOCKER_FIELD: (125, _PODMAN_TEMPLATE_ERROR),
            _PODMAN_FIELD: (0, "v2\n"),
        })
        self.how = how

    def run(self, args, timeout=None, **kw):
        if args[:2] == ["info", "--format"] and self.down:
            self.asked.append(args[2])
            if self.how == "timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            if self.how == "oserror":
                raise OSError("ssh: connection reset")
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr="Cannot connect")
        return super().run(args, timeout=timeout, **kw)


def _recreates(engine, n):
    """What `n` recreates in a row read, and how many questions each asked."""
    UpdateChecker._cgroup_version_cache.clear()
    seen, asked = [], []
    try:
        for i in range(n):
            engine.down = i == 0
            before = len(engine.asked)
            # Same reason as above: a probe that raises is a red check
            # here, not the end of the procedure.
            try:
                seen.append(UpdateChecker._cgroup_version(engine))
            except Exception as exc:                          # noqa: BLE001
                seen.append(f"raised {type(exc).__name__}")
            asked.append(len(engine.asked) - before)
    finally:
        UpdateChecker._cgroup_version_cache.clear()
    return seen, asked


for how in ("timeout", "oserror", "refused"):
    seen, asked = _recreates(_Flaky(how), 3)
    checks[f"a first probe that failed ({how}) answers the conservative "
           f"v1 for that recreate"] = seen[:1] == ["1"]
    checks[f"…but the next recreate asks again and reads the v2 host as "
           f"v2 ({how})"] = seen[1:] == ["2", "2"]
    # The cost bound: one probe per recreate. A timeout or a dead
    # socket stops after the one question, a refusal tries the second
    # field and stops there, and once the host has answered it is not
    # asked again.
    checks[f"…at most one probe per recreate, none once answered "
           f"({how})"] = (
        asked[0] <= 2 and asked[1] <= 2 and asked[2] == 0)
    if seen != ["1", "2", "2"] or asked[2] != 0:
        print(f"      → {how}: read {seen}, questions per recreate {asked}")

# An engine that never answers is asked on every recreate, two
# questions each, and never more.
_never = _Engine("mystery", {})
seen, asked = _recreates(_never, 3)
checks["an engine that never answers is asked again on each recreate, "
       "two questions each"] = (seen == ["1", "1", "1"]
                                and asked == [2, 2, 2])

# Counter-direction: a definite answer IS kept. A real v1 host answers
# "1" once and is not asked again, and neither is a v2 docker host.
for label, engine, want in (
        ("a podman host that answered v1",
         _Engine("podman", {_DOCKER_FIELD: (125, _PODMAN_TEMPLATE_ERROR),
                            _PODMAN_FIELD: (0, "v1\n")}), "1"),
        ("a docker host that answered 2",
         _Engine("docker", {_DOCKER_FIELD: (0, "2\n")}), "2")):
    seen, asked = _recreates(engine, 3)
    checks[f"{label} is remembered: read {want} every time, asked once"] = (
        seen == [want] * 3 and asked[0] >= 1 and asked[1:] == [0, 0])

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

# ── …and the recreate paths actually hand the probe's answer in ──────
# The block above passes `cgroup_version` itself, so it says nothing
# about whether production does. Measured 18.11.2026: with all three
# `cgroup_version=self._cgroup_version(self.backend)` in
# update_checker.py replaced by `None`, this file stayed green. So each
# of the three paths is driven here, with the real probe: the daemon is
# a stand-in that answers `info` the way podman does and records the
# `run` it is handed. Nothing on the path from the probe to the `run`
# argv is replaced.
_INSPECT = json.dumps([{
    "Name": "/web", "Config": {"Image": "nginx:1.25", "Env": [],
                               "Labels": {}},
    "HostConfig": {"RestartPolicy": {"Name": "no"}, "Binds": [],
                   "PortBindings": {}, "MemorySwappiness": 0},
    "Mounts": []}])


class _Daemon(_Engine):
    """A podman that answers `info` like `_Engine`, `inspect` with one
    container, and records each `run`. Every other verb succeeds."""

    def __init__(self, name, answers):
        super().__init__(name, answers)
        self.runs = []

    def run(self, args, timeout=None, **kw):
        if args[:2] == ["info", "--format"]:
            return super().run(args, timeout=timeout, **kw)
        if args[:1] == ["run"]:
            self.runs.append(list(args))
        out = _INSPECT if args == ["inspect", "web"] else ""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    def __getattr__(self, verb):
        if verb.startswith("_"):
            raise AttributeError(verb)
        return lambda *a, **kw: types.SimpleNamespace(
            returncode=0, stdout="", stderr="")


def _recreate_on(path, daemon):
    """Drive one production recreate path; return the `run` argv seen."""
    UpdateChecker._cgroup_version_cache.clear()
    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(docker_stop_timeout=10,
                                     container_cli="podman", debug=False)
    c._backend = daemon
    exists = {"web": True}
    for _n in ("_debug", "_trace", "_save_history", "_mark_inflight",
               "_clear_inflight", "_attach_extra_networks",
               "_audit_inspect_coverage"):
        setattr(c, _n, lambda *a, **kw: None)
    c._lifecycle_timeout = lambda: 30
    c._container_exists = lambda n: exists.get(n, False)

    def _stop(n, inspect_config=None):
        # The vanished arm is the AutoRemove container that is gone the
        # moment it stops; that is the environment, not a decision.
        if path == "standalone, vanished after stop":
            exists[n] = False
        return True, ""
    c._stop_container = _stop
    c._rename_container = lambda src, dst: True
    c._get_image_defaults = lambda img: {}
    c._image_config = lambda img: {}
    c._layer_farewell = lambda n: ""
    c._format_version_arrow = lambda a, b: ""
    c._wait_healthy = lambda name, **kw: ("healthy", "running", "healthy")
    c._verify_running_image = lambda n, i: (True, "")
    c._rollback_to_old = lambda name, old: True
    c._tail_logs = lambda n, lines=10: ""
    try:
        if path == "the dependent recreate":
            c.recreate_dependent("web", "gluetun")
        else:
            c._update_standalone("web", "nginx:1.26")
    except Exception as exc:                                  # noqa: BLE001
        print(f"      → {path}: {type(exc).__name__}: {exc}")
    finally:
        UpdateChecker._cgroup_version_cache.clear()
    return daemon.runs


for _path in ("the dependent recreate", "standalone, container still there",
              "standalone, vanished after stop"):
    for _label, _answer, _want in (("v2", "v2\n", False), ("v1", "v1\n", True)):
        _d = _Daemon("podman", {_DOCKER_FIELD: (125, _PODMAN_TEMPLATE_ERROR),
                                _PODMAN_FIELD: (0, _answer)})
        _runs = _recreate_on(_path, _d)
        # Counter-direction first: without a `run` at all, "the flag is
        # not in it" would be true of nothing.
        checks[f"{_path} on a podman {_label} host runs the container"] = (
            len(_runs) == 1)
        checks[f"…and asks the daemon for its cgroup version "
               f"({_path}, {_label})"] = _PODMAN_FIELD in _d.asked
        _has = bool(_runs) and "--memory-swappiness" in _runs[0]
        checks[f"…and --memory-swappiness is "
               f"{'kept' if _want else 'left out'} ({_path}, {_label})"] = (
            bool(_runs) and _has is _want)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
