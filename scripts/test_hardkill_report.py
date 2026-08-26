#!/usr/bin/env python3
"""A force-killed container is reported, and compose gets the same grace (#62, @NotRetarded).

When Docksentry recreates a container it stops the old one. If the app
ignores SIGTERM, Docker SIGKILLs it after the grace and it exits 137 — but
`docker stop` returns success either way, so the old code reported a bare
"✅ updated" and the user only learned of the hard-kill from an external
monitor. Now `_stop_container` inspects the old container's exit code and
the recreate result says a force-kill happened; and the compose recreate
passes the same `--timeout` as the standalone path, so a compose container
gets ~60s to stop instead of Compose's 10s default.
"""
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
from update_checker import UpdateChecker            # noqa: E402


class _Backend:
    def __init__(self, stop_rc=0, exit_code=0):
        self._stop_rc, self._exit = stop_rc, exit_code
        self.stop_time = None
    def stop(self, name, *, time=None, timeout=None):
        self.stop_time = time
        return types.SimpleNamespace(returncode=self._stop_rc, stdout="", stderr="")
    def run(self, argv, timeout=None):
        if argv[:1] == ["inspect"] and "{{.State.ExitCode}}" in argv:
            return types.SimpleNamespace(returncode=0, stdout=str(self._exit), stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    def kill(self, name, timeout=None):
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def mk(stop_rc=0, exit_code=0, floor=60):
    u = UpdateChecker.__new__(UpdateChecker)
    u.config = types.SimpleNamespace(docker_stop_timeout=floor, debug=False)
    u._debug = lambda m: None
    u._lifecycle_timeout = lambda: 30
    u._backend = _Backend(stop_rc, exit_code)          # `backend` is a property
    return u


checks = {}

# ── _container_exit_code ──────────────────────────────────────────────────
checks["exit_code reads the int"] = mk(exit_code=137)._container_exit_code("x") == 137
checks["exit_code 143 reads 143"] = mk(exit_code=143)._container_exit_code("x") == 143


class _NoValue:
    def run(self, argv, timeout=None):
        return types.SimpleNamespace(returncode=0, stdout="<no value>", stderr="")
_nv = UpdateChecker.__new__(UpdateChecker); _nv._backend = _NoValue()
checks["exit_code '<no value>' → None"] = _nv._container_exit_code("x") is None

# ── _stop_container: 137 → force-killed, everything else → plain stopped ───
ok, detail = mk(exit_code=137)._stop_container("x", inspect_config={"Config": {}})
checks["stop 137 → ok"] = ok is True
checks["stop 137 → says force-killed with the code"] = (
    "force-killed" in detail and "137" in detail)

ok, detail = mk(exit_code=0)._stop_container("x", inspect_config={"Config": {}})
checks["stop 0 → plain 'stopped'"] = ok and detail == "stopped"

ok, detail = mk(exit_code=143)._stop_container("x", inspect_config={"Config": {}})
checks["stop 143 (clean SIGTERM) → plain 'stopped'"] = ok and detail == "stopped"

# ── the grace floor is honoured on the standalone stop ────────────────────
u = mk(exit_code=0, floor=60)
u._stop_container("x", inspect_config={"Config": {"StopTimeout": 5}})
checks["standalone stop uses max(StopTimeout, floor) = 60s"] = u.backend.stop_time == 60

# ── source guards: the result note and the compose --timeout ──────────────
src = open(os.path.join(APP, "update_checker.py"), encoding="utf-8").read()
checks["recreate result appends the force-kill note"] = (
    'force-killed' in src and 'old container force-killed' in src)
checks["compose recreate passes --timeout with the stop grace"] = (
    '"--timeout", str(stop_grace)' in src)
checks["compose stop_grace comes from docker_stop_timeout"] = (
    'stop_grace = int(getattr(self.config, "docker_stop_timeout"' in src)


for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
