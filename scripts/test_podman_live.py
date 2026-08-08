#!/usr/bin/env python3
"""Podman against a REAL podman, including a remote endpoint (#7, #23).

Every Podman fix so far (#43, #48, #49, #50) was made from bug reports
without a Podman to try them on. This is the test bed that was missing.

Two things it pins down:

  * `PodmanBackend` really drives the `podman` binary, and
  * `RemoteBackend(cli_binary="podman")` reaches a remote Podman service
    over the flag we emit.

That second one matters more than it looks: `podman --help` documents
`--url` and `-c/--connection` but NOT `-H`. Podman 4.9.3 happens to accept
`-H` as an undocumented alias, which is exactly the kind of thing that
disappears in a later release — so we emit `--url` and this test is what
tells us if that ever stops working.

Since v2.4.0 it also drives a real **recreate**, because everything above
is plumbing and plumbing was never the problem. Three differences between
Podman's inspect output and Docker's each broke `_build_run_args` outright
— a numeric `StopSignal`, `Runtime: "oci"`, and a pod member whose
`NetworkMode` reads `container:<infra-id>` — which means no Podman user
ever had a successful update from this code, and this file could not have
said so, because it only ever checked that `ps` worked.

Skips cleanly when podman isn't installed.
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from container_backend import PodmanBackend, RemoteBackend   # noqa: E402

if not shutil.which("podman"):
    print("SKIP: podman not installed")
    sys.exit(0)

checks = {}
PORT = 8791
ENDPOINT = f"tcp://127.0.0.1:{PORT}"

# ── local podman ─────────────────────────────────────────────────────
pb = PodmanBackend()
r = pb.version(fmt="{{.Client.Version}}", timeout=30)
checks["local podman answers through the backend"] = (
    r.returncode == 0 and r.stdout.strip() != "")
checks["ps works locally"] = pb.ps(fmt="{{.Names}}", timeout=30).returncode == 0

# ── the endpoint flag we emit is the documented one ──────────────────
rb = RemoteBackend(ENDPOINT, name="podbox", cli_binary="podman")
checks["podman remote uses --url, not -H"] = rb.global_args == ("--url", ENDPOINT)
checks["docker remote still uses -H"] = (
    RemoteBackend(ENDPOINT, cli_binary="docker").global_args == ("-H", ENDPOINT))

# ── …and a real remote podman actually accepts it ────────────────────
svc = subprocess.Popen(
    ["podman", "system", "service", "--time=0", ENDPOINT],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    # Give the service a moment to bind before the first call.
    deadline = time.time() + 20
    ok = False
    while time.time() < deadline:
        probe = rb.version(fmt="{{.Server.Version}}", timeout=10)
        if probe.returncode == 0 and probe.stdout.strip():
            ok = True
            break
        time.sleep(1)
    checks["remote podman answers over the emitted flag"] = ok
    checks["remote ps succeeds"] = rb.ps(fmt="{{.Names}}", timeout=20).returncode == 0
    # A read that goes through the same generic run() path the update core
    # uses, so this isn't only exercising the typed helpers.
    gen = rb.run(["info", "--format", "{{.Host.OS}}"], timeout=20)
    checks["generic run() reaches the remote too"] = (
        gen.returncode == 0 and gen.stdout.strip() != "")
finally:
    svc.terminate()
    try:
        svc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        svc.kill()



# ── a real recreate, in a real pod ───────────────────────────────────
# The shapes are pinned in test_podman_recreate.py without needing a
# runtime; this is the half that only a real Podman can answer. A pod,
# because a pod member exercises all three differences at once.
POD, CTR = "ds-selftest-pod", "ds-selftest-ctr"
_have_image = pb.run(["image", "exists", "alpine:3.19"], timeout=30).returncode == 0
if not _have_image:
    _have_image = pb.run(["pull", "-q", "alpine:3.19"], timeout=300).returncode == 0

if _have_image:
    import json as _json
    from update_checker import UpdateChecker   # noqa: E402
    pb.run(["rm", "-f", CTR], timeout=30)
    pb.run(["pod", "rm", "-f", POD], timeout=30)
    try:
        made = pb.run(["pod", "create", "--name", POD], timeout=60).returncode == 0
        made = made and pb.run(
            ["run", "-d", "--pod", POD, "--name", CTR,
             "alpine:3.19", "sleep", "600"], timeout=120).returncode == 0
        checks["a pod and a member can be created"] = made
        if made:
            uc = UpdateChecker.__new__(UpdateChecker)
            uc._backend = pb
            raw = pb.run(["inspect", CTR], timeout=30).stdout
            cfg = _json.loads(raw)[0]
            pod_id = cfg.get("Pod") or ""
            cmd = uc._build_run_args(cfg, "alpine:3.19", CTR, None)

            checks["the rebuilt command is argv, all strings"] = all(
                isinstance(a, str) for a in cmd)
            checks["it rejoins the pod rather than the infra namespace"] = (
                "--pod" in cmd and f"container:{pod_id}" not in cmd)

            # The sequence a real update performs: stop, rename aside, run.
            pb.run(["rm", "-f", CTR + "_old"], timeout=30)
            pb.run(["stop", "-t", "3", CTR], timeout=60)
            pb.rename(CTR, CTR + "_old", timeout=30)
            run = pb.run(cmd[1:], timeout=120)
            checks["podman accepts the rebuilt run command"] = (
                run.returncode == 0)
            if run.returncode != 0:
                print("    podman said: " + (run.stderr or "").strip()[:180])
                uc._rollback_to_old(CTR, CTR + "_old")
            else:
                after = pb.run(["inspect", CTR, "--format", "{{.Pod}}"],
                               timeout=30).stdout.strip()
                checks["and the container is still in its pod afterwards"] = (
                    after == pod_id)
    finally:
        pb.run(["rm", "-f", CTR, CTR + "_old"], timeout=30)
        pb.run(["pod", "rm", "-f", POD], timeout=60)
else:
    print("  (skipping the recreate: alpine:3.19 unavailable)")


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
