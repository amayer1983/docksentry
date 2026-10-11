#!/usr/bin/env python3
"""Regression test for the per-container "trust running state over
healthcheck" flag (#9).

Three things, against real containers:
  1. running + unhealthy, flag OFF  → "unhealthy" (would roll back)
  2. running + unhealthy, flag ON   → "healthy"   (accepted)
  3. crash-loop, flag ON            → "crashloop" (flag must NOT mask this)

The third is the one worth having. The flag says "trust that it is
running even though its healthcheck is unhappy", and a container that
keeps dying and being restarted is also, briefly, running — so the flag
must not swallow a crash loop. That is the difference between accepting
a container whose healthcheck is wrong and accepting one that is broken.

Requires a working Docker daemon; says SKIP and exits 0 without one,
because a missing runtime is not a regression.

Collected in a `checks` dict rather than asserted inline. Bare `assert`
stopped at the first failure, so a broken case one hid cases two and
three — and `verify.py` counted the file's two print lines as its two
checks, which is how fourteen seconds of real Docker time appeared in
`VERIFICATION.md` as less coverage than a two-line grep.
"""
import sys, os, time, json, subprocess, tempfile, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livefixture as lf  # noqa: E402
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


def _make_checker(trust_file, stable=2, max_starting=6):
    # The verdict is what this test is about, not how long we sit out
    # waiting for it. `healthcheck_max_starting` was 60 and the file spent
    # 41 of the suite's 362 seconds proving two strings.
    cfg = types.SimpleNamespace(
        debug=False, healthcheck_max_starting=max_starting,
        crashloop_stable_seconds=stable, trust_running_file=trust_file,
    )
    return UpdateChecker(cfg)


def _rm(name):
    lf.rm(name)


checks = {}


def test_unhealthy_running():
    tmp = tempfile.mkdtemp()
    trust_file = os.path.join(tmp, "trust.json")
    chk = _make_checker(trust_file)
    # Per-run: the name was fixed and the fixture force-removes it
    # first, so two suites at once destroyed each other's container
    # and the loser reported the trust flag as broken.
    name = lf.name("ds_trust_test")
    _rm(name)
    # And the return code is read: with no container, the 20-second
    # wait never sees `unhealthy` and both checks go red for a reason
    # that has nothing to do with the flag.
    lf.start(["run", "-d", "--name", name,
              "--health-cmd", "exit 1", "--health-interval=1s",
              "--health-retries=1", "--health-start-period=0s",
              "alpine", "sleep", "300"], checks=checks)
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
    checks["running but unhealthy, flag OFF: reported unhealthy"] = (
        out_off == "unhealthy")
    checks["…and with the flag ON: accepted as healthy"] = (
        out_on == "healthy")
    if out_off != "unhealthy":
        print(f"      → flag OFF gave {out_off!r}")
    if out_on != "healthy":
        print(f"      → flag ON gave {out_on!r}")


def test_crashloop_not_masked():
    tmp = tempfile.mkdtemp()
    trust_file = os.path.join(tmp, "trust.json")
    name = lf.name("ds_trust_crash")
    json.dump([name], open(trust_file, "w"))  # flag ON
    chk = _make_checker(trust_file, stable=5, max_starting=60)
    _rm(name)
    lf.start(["run", "-d", "--name", name, "--restart", "always",
              "alpine", "sh", "-c", "exit 1"], checks=checks)
    time.sleep(2)
    try:
        out, _, _ = chk._wait_healthy(name, interval=2)
    finally:
        _rm(name)
    # The flag must not mask this: a container that keeps dying is
    # briefly running too, and "trust the running state" was never meant
    # to mean "accept anything that is up at the moment I looked".
    checks["a crash loop is still a crash loop with the flag ON"] = (
        out == "crashloop")
    if out != "crashloop":
        print(f"      → got {out!r}")


if __name__ == "__main__":
    # Both cases run even if the first one fails: a bare `assert` stopped
    # here and hid the crash-loop case, which is the one that matters.
    for case in (test_unhealthy_running, test_crashloop_not_masked):
        try:
            case()
        except Exception as e:                              # noqa: BLE001
            checks[f"{case.__name__} ran at all"] = False
            print(f"      → {type(e).__name__}: {e}")
    bad = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print("FAIL" if bad else "PASS")
    sys.exit(1 if bad else 0)
