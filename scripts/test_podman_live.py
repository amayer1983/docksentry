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


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
