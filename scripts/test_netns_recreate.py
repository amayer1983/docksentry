#!/usr/bin/env python3
"""End-to-end test for the Gluetun-style netns-sidecar recreate fix (#2).

Reproduces the failure: a sidecar joins a head's network namespace
(`--network container:<head>`). When the head is recreated (new ID), the
sidecar's stored `NetworkMode=container:<old-id>` is dead, so recreating the
sidecar with that stale ID fails with "No such container". The fix resolves
the head to a stable NAME (snapshot before recreate) and recreates the
sidecar against `container:<name>`, which joins the NEW head.

Asserts:
  1. netns_target_name() resolves the sidecar's head to the head's name.
  2. _build_run_args(..., netns_name=head) emits `--network container:<name>`.
  3. Recreating the sidecar with the stale ID FAILS (the bug);
     recreating it with the name override SUCCEEDS and joins the new head.

Requires a working Docker daemon. Exits non-zero on any failure.
"""
import sys, os, json, subprocess, types

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

HEAD = "ds_netns_head"
SIDE = "ds_netns_side"


def _rm(*names):
    for n in names:
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)


def _run(args):
    return subprocess.run(args, capture_output=True, text=True)


def main():
    chk = UpdateChecker(types.SimpleNamespace(debug=False))
    _rm(HEAD, SIDE)
    try:
        # head v1 + sidecar sharing its netns
        _run(["docker", "run", "-d", "--name", HEAD, "alpine", "sleep", "300"])
        _run(["docker", "run", "-d", "--name", SIDE,
              "--network", f"container:{HEAD}", "alpine", "sleep", "300"])

        # 1. resolve the netns owner name
        resolved = chk.netns_target_name(SIDE)
        c1 = resolved == HEAD

        # capture the sidecar's config (holds NetworkMode=container:<old-id>)
        side_cfg = json.loads(_run(["docker", "inspect", SIDE]).stdout)[0]
        old_netmode = (side_cfg.get("HostConfig") or {}).get("NetworkMode", "")

        # 2. build args with the name override → must use container:<name>
        args_fixed = UpdateChecker._build_run_args(
            side_cfg, "alpine", SIDE, None, netns_name=HEAD)
        c2 = f"container:{HEAD}" in args_fixed and old_netmode not in args_fixed

        # recreate the HEAD → new ID; the old ID in old_netmode is now dead
        _rm(HEAD)
        _run(["docker", "run", "-d", "--name", HEAD, "alpine", "sleep", "300"])

        # 3a. stale-ID recreate (the bug) must FAIL
        _rm(SIDE)
        args_stale = ["docker", "run", "-d", "--name", SIDE,
                      "--network", old_netmode, "alpine", "sleep", "300"]
        stale = _run(args_stale)
        c3a = stale.returncode != 0 and "No such container" in (stale.stderr or "")

        # 3b. name-override recreate must SUCCEED and join the new head
        _rm(SIDE)
        fixed = _run(args_fixed)
        running = _run(["docker", "inspect", "--format", "{{.State.Running}}", SIDE]).stdout.strip()
        c3b = fixed.returncode == 0 and running == "true"
    finally:
        _rm(HEAD, SIDE)

    checks = {
        "netns_target_name resolves head name": c1,
        "build_run_args uses container:<name> (not stale id)": c2,
        "stale-id recreate fails (reproduces the bug)": c3a,
        "name-override recreate succeeds + joins new head": c3b,
    }
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
