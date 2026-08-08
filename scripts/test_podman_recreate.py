#!/usr/bin/env python3
"""Recreating a container on Podman — which never once worked.

`_build_run_args` reconstructs the `run` command line from a container's
inspect dump. It was written against Docker's inspect output, and Podman's
differs in three places. Each one on its own is fatal, and they stack, so
every fix only revealed the next:

**1. `Config.StopSignal` is a number.** Docker says `"SIGTERM"`, Podman says
`15` (measured, podman 4.9.3). That int went straight into the argument
list, and `subprocess.run` refuses:

    TypeError: expected str, bytes or os.PathLike object, not int

raised before the CLI is executed at all, from a frame that names no field.

**2. `HostConfig.Runtime` is `oci`.** Docker reports `runc`, which was
skipped as its default. Podman reports `oci` — not the name of a runtime
but its generic label — and handing it back gets:

    Error: default OCI runtime "oci" not found: invalid argument

**3. A pod member's `NetworkMode` is `container:<infra-id>`.** Identical in
shape to a Gluetun-style sidecar, so it was rebuilt as
`--network container:<id>`, and Podman refuses:

    Error: container dependency <id> is part of a pod, but container is
    not: invalid argument

Only the third is about pods. The first two hit **every** container on
Podman, which is to say: Podman users have never had a successful update
from this code, and the tests could not have told anyone, because
`test_podman_live.py` exercised the backend plumbing — `ps`, the `--url`
flag — and never once built a run command.

That is the lesson worth keeping more than the three fixes. What was tested
was the part that had been thought about.

Measured after the fixes, against real podman 4.9.3, on a pod member and on
an ordinary container:

    == ds-app ==  Netz: ['--pod f1a1182805eb63b374cc']
       run -> 0
    == ds-plain ==  Netz: ['--network slirp4netns']
       run -> 0

and `ds-app` was still a member of its pod afterwards.

No runtime needed here: these are the three inspect shapes, checked against
the builder directly. The live counterpart is in `test_podman_live.py`.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

POD_ID = "f1a1182805eb63b374cc20acaefbd83be5f4c7ae1fc8f546ebd2d56e9e47d241"
INFRA_ID = "37736e6f7f0ed085c853a2e3b02b7a3cf662d429e2a1c8c2224e6c890ffa6b43"


def podman_inspect(*, pod=True):
    """A Podman inspect dump, in the shapes that broke the builder."""
    return {
        "Id": "a" * 64,
        "Pod": POD_ID if pod else "",
        "Image": "sha256:" + "b" * 64,
        "Config": {
            "Image": "alpine:3.19",
            "StopSignal": 15,          # a NUMBER, where Docker has a string
            "Cmd": ["sleep", "600"],
            "Labels": {},
            "Env": [],
        },
        "HostConfig": {
            "NetworkMode": f"container:{INFRA_ID}" if pod else "slirp4netns",
            "Runtime": "oci",          # Podman's generic label
            "RestartPolicy": {"Name": ""},
        },
        "NetworkSettings": {"Networks": {}},
    }


def docker_inspect(runtime="runc"):
    return {
        "Id": "c" * 64,
        "Image": "sha256:" + "d" * 64,
        "Config": {
            "Image": "nginx:latest",
            "StopSignal": "SIGTERM",
            "Cmd": [],
            "Labels": {},
            "Env": [],
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Runtime": runtime,
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "NetworkSettings": {"Networks": {}},
    }


def build(cfg, name="c1"):
    uc = UpdateChecker.__new__(UpdateChecker)
    return uc._build_run_args(cfg, "alpine:3.19", name, None)


def flag_value(args, flag):
    """The value following `flag`, or None."""
    return args[args.index(flag) + 1] if flag in args else None


def main():
    checks = {}

    # ── 1. argv is strings ───────────────────────────────────────
    args = build(podman_inspect())
    checks["every argument is a string"] = all(
        isinstance(a, str) for a in args)
    checks["the numeric stop signal survives, as text"] = (
        flag_value(args, "--stop-signal") == "15")

    # ── 2. Podman's generic runtime label is not echoed back ─────
    checks["--runtime oci is not emitted"] = "--runtime" not in args
    checks["--runtime runc is not emitted either"] = (
        "--runtime" not in build(docker_inspect("runc")))
    # …but a runtime somebody actually chose still is. Dropping those
    # would be the opposite bug.
    chosen = build(docker_inspect("crun"))
    checks["a deliberately chosen runtime is kept"] = (
        flag_value(chosen, "--runtime") == "crun")

    # ── 3. a pod member rejoins its pod ──────────────────────────
    checks["a pod member is recreated with --pod"] = (
        flag_value(args, "--pod") == POD_ID)
    checks["…and not with --network container:<infra>"] = (
        f"container:{INFRA_ID}" not in args)
    # The per-container network knobs are forbidden inside a pod exactly
    # as they are for a shared namespace — publishing a port from a pod
    # member is the pod's business, not the container's.
    checks["…with no per-container network flags"] = not (
        {"-p", "--publish", "--hostname", "--mac-address"} & set(args))

    # A plain Podman container is untouched by any of it.
    plain = build(podman_inspect(pod=False))
    checks["a plain Podman container gets no --pod"] = "--pod" not in plain
    checks["…and keeps its own network mode"] = (
        flag_value(plain, "--network") == "slirp4netns")

    # ── and Docker is entirely unaffected ────────────────────────
    # `Pod` does not exist in Docker's inspect output, so the whole pod
    # branch is inert there — asserted rather than assumed, because this
    # builder is shared by both.
    d = build(docker_inspect())
    checks["Docker gets no --pod"] = "--pod" not in d
    checks["Docker keeps its stop signal by name"] = (
        flag_value(d, "--stop-signal") in (None, "SIGTERM"))

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
