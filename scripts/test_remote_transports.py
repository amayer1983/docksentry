#!/usr/bin/env python3
"""Multi-host over a real network endpoint, not a socket that looks like one.

Everything multi-host had been tested against `unix://` — the local socket
wearing an `-H` flag — plus argv assertions with mocked backends. Both are
worth having and neither touches a network. The transports the README
actually tells people to use, `tcp://` and `ssh://`, had never carried a
byte.

Closing that gap turned up a defect rather than just missing evidence: the
image had no `ssh` binary. Every `ssh://` endpoint failed with

    exec: "ssh": executable file not found in $PATH

while README said "SSH endpoints also work". Nothing caught it because the
suite only ever asserted the argv (`docker -H ssh://root@pve1 …`) and never
ran it. So the cheap half of this file guards that specific hole, and the
expensive half drives a genuine TCP endpoint end to end.

What was proven by hand before this was written, against a real sshd
container with the socket mounted:

    ssh://root@sshhost        19 containers through hosts -> checker
    tcp://demo-dind:2375      10 containers + a real registry digest

The SSH half needs key material and a second image, so it stays a manual
procedure; what runs here is the part that can be self-contained.
"""

import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

ROOT = os.path.join(os.path.dirname(__file__), "..")
PORT = 12375
NAME = "ds-tcp-transport-test"


def docker_ok():
    return (shutil.which("docker")
            and subprocess.run(["docker", "info"], capture_output=True).returncode == 0)


def main():
    checks = {}

    # ── the regression that shipped ──────────────────────────────
    # A documented transport whose binary is absent is worse than an
    # undocumented one: the user follows the README, gets an error that
    # names `ssh` rather than us, and blames their own key setup.
    dockerfile = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
    checks["the image installs an ssh client"] = "openssh-client" in dockerfile
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    documents_ssh = "ssh://" in readme
    checks["ssh is documented and therefore must be installed"] = (
        not documents_ssh or "openssh-client" in dockerfile)

    if not docker_ok():
        print("SKIP: no usable Docker, cannot start a TCP endpoint")
        for k, v in checks.items():
            print(f"  {'PASS' if v else 'FAIL'} {k}")
        print("FAIL" if [k for k, v in checks.items() if not v] else "PASS")
        return 1 if [k for k, v in checks.items() if not v] else 0

    # ── a genuine TCP endpoint ───────────────────────────────────
    # docker:dind published on loopback: the requests leave through the
    # network stack, which `unix://` never does however it is spelled.
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    up = subprocess.run(
        ["docker", "run", "-d", "--name", NAME, "--privileged",
         "-e", "DOCKER_TLS_CERTDIR=", "-p", f"127.0.0.1:{PORT}:2375",
         "docker:dind"], capture_output=True, text=True)
    if up.returncode != 0:
        print(f"SKIP: could not start dind ({up.stderr.strip()[:80]})")
        return 0
    try:
        endpoint = f"tcp://127.0.0.1:{PORT}"
        ready = False
        for _ in range(40):
            time.sleep(2)
            r = subprocess.run(["docker", "-H", endpoint, "info"],
                               capture_output=True)
            if r.returncode == 0:
                ready = True
                break
        checks["the TCP endpoint answers"] = ready
        if not ready:
            raise SystemExit(_report(checks))

        subprocess.run(["docker", "-H", endpoint, "run", "-d",
                        "--name", "probe", "alpine:3.19", "sleep", "600"],
                       capture_output=True)

        # Same shape the other host procedures use — a namespace with the
        # state-file paths, not a full Config, so the test owns its data
        # directory and cannot touch a real one.
        import tempfile
        import types
        from config import parse_docker_hosts
        from container_store import ContainerStore
        from hosts import build_hosts
        d = tempfile.mkdtemp()
        cfg = types.SimpleNamespace(
            debug=False, container_cli="docker", exclude_containers=[],
            data_dir=d, pending_file=os.path.join(d, "pending_updates.json"),
            docker_hosts=parse_docker_hosts(f"tcpbox:{endpoint}"))
        for n in ("pinned", "autoupdate", "update_windows", "ask_before_major",
                  "trust_running", "cooldown", "protect_stop", "major_pending",
                  "groups", "notes", "links"):
            setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
        hosts = build_hosts(cfg, ContainerStore(cfg))

        checks["the host registry carries it"] = "tcpbox" in hosts.names
        checks["multi-host is active"] = hosts.is_multi
        host = hosts.get("tcpbox")
        checks["the endpoint survives to the host"] = host.endpoint == endpoint
        # The whole chain, not just a `docker version`: the host's own
        # backend, its own checker, its own store.
        rows = host.checker.get_running_containers()
        names = [r["name"] for r in rows]
        checks["containers are listed over TCP"] = "probe" in names
        # And the local host must not have been consulted instead — the
        # multi-host trap this project has fallen into four times.
        checks["it is NOT reading the local daemon"] = not any(
            n in names for n in ("docksentry", NAME))

        argv = host.backend.build(["ps"])
        checks["the endpoint reaches argv"] = argv[:3] == ["docker", "-H", endpoint]
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)

    return _report(checks)


def _report(checks):
    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
