#!/usr/bin/env python3
"""DOCKER_HOSTS parsing and per-host backends (#7, multi-host groundwork).

Covers the config parser and that a checker can be pointed at another
host. Pure functions and argv inspection — no Docker, no network, no ssh.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import container_backend                      # noqa: E402
from config import parse_docker_hosts, RESERVED_HOST_NAMES   # noqa: E402
from update_checker import UpdateChecker      # noqa: E402

checks = {}
p = parse_docker_hosts

# ── the documented format from #7 ────────────────────────────────────
checks["parses name:endpoint pairs"] = p(
    "pve1:ssh://root@pve1, nas:tcp://nas:2375") == [
        {"name": "pve1", "endpoint": "ssh://root@pve1"},
        {"name": "nas", "endpoint": "tcp://nas:2375"}]
# Endpoints contain colons — only the FIRST one separates.
checks["splits on the first colon only"] = (
    p("nas:tcp://nas:2375")[0]["endpoint"] == "tcp://nas:2375")
checks["tolerates loose whitespace"] = p("  a:ssh://h  ,  b:ssh://i ") == [
    {"name": "a", "endpoint": "ssh://h"}, {"name": "b", "endpoint": "ssh://i"}]

# ── unset means single-host, exactly as before ───────────────────────
for empty in ("", None, "   ", ",,"):
    checks[f"empty input → no hosts: {empty!r}"] = p(empty) == []

# ── fails soft: one bad entry costs that host, not the instance ──────
checks["missing endpoint skipped"] = p("justaname") == []
checks["empty endpoint skipped"] = p("name:") == []
checks["bad name skipped"] = p("has space:ssh://h") == []
checks["name starting with dash skipped"] = p("-x:ssh://h") == []
checks["good entries survive a bad neighbour"] = p(
    "broken, ok:ssh://h") == [{"name": "ok", "endpoint": "ssh://h"}]

# ── normalisation ────────────────────────────────────────────────────
checks["names lowercased"] = p("PVE1:ssh://h")[0]["name"] == "pve1"
checks["duplicate names dropped"] = len(p("a:ssh://1, a:ssh://2")) == 1
checks["first duplicate wins"] = p("a:ssh://1, a:ssh://2")[0]["endpoint"] == "ssh://1"
for reserved in RESERVED_HOST_NAMES:
    checks[f"reserved name rejected: {reserved}"] = p(f"{reserved}:ssh://h") == []

# ── a checker can be pointed at another host ─────────────────────────
sent = []


class _CP:
    returncode = 0
    stdout = ""
    stderr = ""


real = container_backend.subprocess
container_backend.subprocess = types.SimpleNamespace(
    run=lambda argv, **kw: (sent.append(argv), _CP())[1],
    SubprocessError=real.SubprocessError,
    TimeoutExpired=real.TimeoutExpired,
    CalledProcessError=real.CalledProcessError,
    PIPE=real.PIPE, DEVNULL=real.DEVNULL,
)
try:
    cfg = types.SimpleNamespace(debug=False, container_cli="docker")
    remote = container_backend.RemoteBackend("ssh://root@pve1", name="pve1")
    chk = UpdateChecker(cfg, backend=remote)
    checks["checker keeps the backend it was given"] = chk.backend is remote
    chk._container_exists("nginx")
    checks["checker's commands carry the host endpoint"] = (
        sent and sent[-1][:3] == ["docker", "-H", "ssh://root@pve1"])

    # …and without one it stays local, i.e. today's behaviour.
    sent.clear()
    local = UpdateChecker(cfg)
    local._container_exists("nginx")
    checks["no backend given → local, no endpoint flag"] = (
        sent and sent[-1][0] == "docker" and "-H" not in sent[-1])
finally:
    container_backend.subprocess = real


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
