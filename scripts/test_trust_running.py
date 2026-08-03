#!/usr/bin/env python3
"""Regression test for the per-container "trust running state over
healthcheck" flag (#9).

Asserts three things against real containers:
  1. running + unhealthy, flag OFF  → "unhealthy" (would roll back)
  2. running + unhealthy, flag ON   → "healthy"   (accepted)
  3. crash-loop, flag ON            → "crashloop" (flag must NOT mask this)

Requires a working Docker daemon. Exits non-zero on any failure.
"""
import sys, os, time, json, subprocess, tempfile, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker

# A missing container runtime is not a regression. Without this the
# procedure goes red on a machine that simply has no Docker, which is
# indistinguishable from a real failure — measured: five procedures did
# exactly that before the guard, while test_multihost_live skipped
# cleanly. CI must be able to tell the two apart.
if __import__("subprocess").run(["docker", "info"],
                                capture_output=True).returncode != 0:
    print("SKIP: no Docker daemon available")
    __import__("sys").exit(0)


def _make_checker(trust_file, stable=2):
    cfg = types.SimpleNamespace(
        debug=False, healthcheck_max_starting=60,
        crashloop_stable_seconds=stable, trust_running_file=trust_file,
    )
    return UpdateChecker(cfg)


def _rm(name):
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_unhealthy_running():
    tmp = tempfile.mkdtemp()
    trust_file = os.path.join(tmp, "trust.json")
    chk = _make_checker(trust_file)
    name = "ds_trust_test"
    _rm(name)
    subprocess.run(
        ["docker", "run", "-d", "--name", name,
         "--health-cmd", "exit 1", "--health-interval=1s",
         "--health-retries=1", "--health-start-period=0s",
         "alpine", "sleep", "300"], capture_output=True)
    try:
        for _ in range(20):
            h = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{.State.Health.Status}}", name],
                capture_output=True, text=True).stdout.strip()
            if h == "unhealthy":
                break
            time.sleep(1)
        # flag OFF
        out_off, _, _ = chk._wait_healthy(name, interval=1)
        # flag ON
        json.dump([name], open(trust_file, "w"))
        out_on, _, _ = chk._wait_healthy(name, interval=1)
    finally:
        _rm(name)
    assert out_off == "unhealthy", f"flag OFF: expected unhealthy, got {out_off}"
    assert out_on == "healthy", f"flag ON: expected healthy, got {out_on}"
    print("  ✅ running+unhealthy: OFF→unhealthy, ON→healthy")


def test_crashloop_not_masked():
    tmp = tempfile.mkdtemp()
    trust_file = os.path.join(tmp, "trust.json")
    name = "ds_trust_crash"
    json.dump([name], open(trust_file, "w"))  # flag ON
    chk = _make_checker(trust_file, stable=5)
    _rm(name)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "--restart", "always",
         "alpine", "sh", "-c", "exit 1"], capture_output=True)
    time.sleep(2)
    try:
        out, _, _ = chk._wait_healthy(name, interval=2)
    finally:
        _rm(name)
    assert out == "crashloop", f"crash-loop with flag ON: expected crashloop, got {out}"
    print("  ✅ crash-loop with flag ON still reported as crashloop")


if __name__ == "__main__":
    print("trust_running (#9) regression test")
    try:
        test_unhealthy_running()
        test_crashloop_not_masked()
    except AssertionError as e:
        print(f"  ❌ {e}")
        sys.exit(1)
    print("PASS")
