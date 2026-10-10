#!/usr/bin/env python3
"""The decision to roll back, not the rolling back.

README:37 and :65 — a container that fails its healthcheck is put back
the way it was. Last week I covered `_rollback_to_old` thoroughly and
believed the promise was held. It was not: I had covered the function,
not the decision to call it.

Measured on 10.10.2026 — all three sites that read a health outcome and
act on it could be given `if False:` and **not one of 194 procedures
went red**:

    app/update_checker.py:3840   the compose recreate
    app/update_checker.py:5099   the standalone recreate, first gate
    app/update_checker.py:5202   the standalone recreate, health gate

A container that comes up unhealthy or crash-looping would then simply
stay, broken, in place of the one that worked — unattended, at three in
the morning, which is the hour the whole feature exists for.

What is driven here is the wiring: `_wait_healthy` is told what it saw,
and the question is whether the rollback follows. The health check
itself has its own coverage (`test_trust_running.py`, against real
containers) and the rollback has its own (`test_rollback.py`); this is
the sentence between them.

The other direction is asserted too, and it is not a formality: a check
that only says "unhealthy rolls back" also passes on a program that
rolls back always, which would undo every successful update.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

checks = {}


def _run(outcome, compose=False):
    """Recreate a container whose health check saw `outcome`.

    Returns (rolled_back, reported_ok, detail).
    """
    rolled = []

    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(docker_stop_timeout=10,
                                     container_cli="docker", debug=False)

    class _Backend:
        def run(self, args, timeout=None):
            out = ""
            if args[:1] == ["inspect"] and "--format" not in args:
                out = ('[{"Name": "/web", "Config": {"Image": "nginx:1.25",'
                       ' "Env": [], "Labels": {}}, "HostConfig":'
                       ' {"RestartPolicy": {"Name": "no"}, "Binds": [],'
                       ' "PortBindings": {}}, "Mounts": []}]')
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

        def __getattr__(self, verb):
            if verb.startswith("_"):
                raise AttributeError(verb)
            return lambda *a, **kw: types.SimpleNamespace(
                returncode=0, stdout="", stderr="")

    c._backend = _Backend()
    for name in ("_debug", "_trace", "_save_history"):
        setattr(c, name, lambda *a, **kw: None)
    c._lifecycle_timeout = lambda: 30
    c._container_exists = lambda n: True
    c._rename_container = lambda src, dst: True
    # Signature matters: the real one takes `inspect_config=`, and a
    # stub that does not swallows the call into the method's own
    # `except` — where the catch-all rolls back, so a HEALTHY container
    # looked like it was being rolled back and the fixture, not the
    # code, was what the check measured.
    c._stop_container = lambda n, inspect_config=None: (True, "")
    c._get_image_defaults = lambda img: {}
    c._image_config = lambda img: {}
    c._cgroup_version = lambda backend: 2
    c._get_image_created = lambda img: ""
    c._get_image_size = lambda img: 0
    c._get_image_version_label = lambda img: ""
    c._format_version_arrow = lambda a, b: ""
    c._layer_farewell = lambda n: ""
    c._tail_logs = lambda n, lines=10: ""
    c._health_output = lambda n: ""
    c._verify_running_image = lambda n, i: (True, "")
    c._compose_files = lambda cf, wd: []
    # The two things under test, and only these two are faked: what the
    # health check saw, and whether a rollback happened.
    c._wait_healthy = lambda name, **kw: (outcome, "running", outcome)
    c._rollback_to_old = lambda name, old: rolled.append((name, old)) or True

    try:
        ok, detail = c._update_standalone("web", "nginx:1.26")
    except Exception as e:                                  # noqa: BLE001
        return (bool(rolled), None, f"{type(e).__name__}: {e}")
    return (bool(rolled), ok, detail)


# ── the failures that must undo the update ───────────────────────────
for _bad in ("unhealthy", "crashloop"):
    _rolled, _ok, _detail = _run(_bad)
    checks[f"a container that came up {_bad} is rolled back"] = _rolled
    checks[f"…and the update is not reported as successful ({_bad})"] = (
        _ok is not True)
    if not _rolled:
        print(f"      → {_bad}: no rollback, update reported {_ok!r} "
              f"({str(_detail)[:60]})")

# ── and the direction that stops the above passing on "always" ───────
# A program that rolls back after every update would satisfy the checks
# above and destroy every working one.
_rolled, _ok, _detail = _run("healthy")
checks["a healthy container is NOT rolled back"] = not _rolled
checks["…and its update is reported as successful"] = _ok is True
if _rolled:
    print(f"      → healthy: rolled back anyway ({str(_detail)[:60]})")

# ── an outcome nobody planned for ────────────────────────────────────
# `_wait_healthy` can also answer "starting" or "" — a container with no
# healthcheck at all. Neither is a failure, and treating an unknown
# answer as one would roll back every container that has no healthcheck
# configured, which is most of them.
for _unknown in ("", "starting", "none"):
    _rolled, _ok, _ = _run(_unknown)
    checks[f"an outcome of {_unknown!r} is not treated as a failure"] = (
        not _rolled)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
