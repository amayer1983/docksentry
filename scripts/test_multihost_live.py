#!/usr/bin/env python3
"""Multi-host end to end against a REAL container daemon (#7).

Points a "remote" host at the local socket (`unix:///var/run/docker.sock`)
— a genuine `-H` endpoint that actually answers — so the whole chain gets
exercised for real: registry → per-host backend → checker → host-keyed
state. Proves the remote path works without needing a second machine.

Skips cleanly when Docker isn't available.
"""
import os
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import livefixture as lf                      # noqa: E402
from config import parse_docker_hosts          # noqa: E402
from container_store import ContainerStore     # noqa: E402
from hosts import build_hosts                  # noqa: E402

SOCKET = "unix:///var/run/docker.sock"

if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP: no Docker daemon available")
    sys.exit(0)

checks = {}


def _cfg(d, hosts):
    names = ["pinned", "autoupdate", "update_windows", "ask_before_major",
             "trust_running", "cooldown", "protect_stop", "major_pending",
             "groups", "notes", "links"]
    cfg = types.SimpleNamespace(
        debug=False, container_cli="docker", exclude_containers=[],
        data_dir=d, pending_file=os.path.join(d, "pending_updates.json"),
        docker_hosts=parse_docker_hosts(hosts))
    for n in names:
        setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
    return cfg


# This procedure used to read whatever happened to be running on the
# machine, and `sorted(remote_names)[0]` then raised IndexError against
# a daemon that is up but empty — a crash, which is not a failed check
# and not a skip either. It brings its own container now, so the
# measurement is of the host registry rather than of the machine.
OWN = lf.name("ds_mhlive")
lf.rm(OWN)
lf.start(["run", "-d", "--name", OWN, "alpine", "sh", "-c",
          "trap 'exit 0' TERM; sleep 300 & wait"])

with tempfile.TemporaryDirectory() as d:
    cfg = _cfg(d, f"selftest:{SOCKET}")
    reg = build_hosts(cfg, ContainerStore(cfg))

    checks["registry has local + the remote"] = reg.names == ["local", "selftest"]
    remote = reg.get("selftest")

    # The remote checker really talks to a daemon over its endpoint.
    remote_names = {c["name"] for c in remote.checker.get_running_containers()}
    local_names = {c["name"] for c in reg.local.checker.get_running_containers()}
    checks["remote host lists containers over its endpoint"] = (
        OWN in remote_names)
    # Same daemon behind both, so the two views must agree — which is
    # exactly what makes this a valid stand-in for a real second host.
    checks["remote view matches the local one"] = remote_names == local_names

    # State written through the remote host is keyed to it, and does not
    # leak into the local host's keys.
    # Our own container, not "whatever is first alphabetically" — the
    # pin below writes state, and writing it to a stranger's container
    # name is not something a test may do on somebody's machine.
    sample = OWN
    remote.store.pin(sample)
    checks["pin via remote is visible on remote"] = remote.store.is_pinned(sample)
    checks["pin via remote is NOT on local"] = not reg.local.store.is_pinned(sample)
    raw_keys = ContainerStore(cfg).get_pinned()
    checks["stored under the host-prefixed key"] = raw_keys == [f"selftest/{sample}"]

lf.rm(OWN)


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
