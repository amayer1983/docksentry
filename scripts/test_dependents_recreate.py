#!/usr/bin/env python3
"""End-to-end test for the recreate-dependents cascade (#8).

After a netns head (Gluetun) is recreated it gets a new container ID, so a
plain `docker restart` of its sidecars fails ("No such container"). The
cascade must RECREATE netns sidecars (rejoining the head by name) while a
non-netns group member is fine with a plain restart.

Asserts:
  1. recreate_dependent rebuilds a netns sidecar onto the new head (running).
  2. _restart_group_dependents recreates the netns sidecar AND restarts the
     non-netns member — both end up running, message lists them ok.

Requires Docker. Exits non-zero on any failure.
"""
import sys, os, types, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker
from telegram_bot import TelegramBot

HEAD, SIDE, PLAIN = "ds8_head", "ds8_side", "ds8_plain"


def _rm(*names):
    for n in names:
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)


def _running(name):
    return subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                          capture_output=True, text=True).stdout.strip() == "true"


def _netns(name):
    return subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", name],
                          capture_output=True, text=True).stdout.strip()


def main():
    chk = UpdateChecker(types.SimpleNamespace(debug=False))
    _rm(HEAD, SIDE, PLAIN)
    try:
        subprocess.run(["docker", "run", "-d", "--name", HEAD, "alpine", "sleep", "400"], capture_output=True)
        subprocess.run(["docker", "run", "-d", "--name", SIDE, "--network", f"container:{HEAD}",
                        "alpine", "sleep", "400"], capture_output=True)
        subprocess.run(["docker", "run", "-d", "--name", PLAIN, "alpine", "sleep", "400"], capture_output=True)

        # recreate the head → new ID; SIDE's stored netns id is now dead
        _rm(HEAD)
        subprocess.run(["docker", "run", "-d", "--name", HEAD, "alpine", "sleep", "400"], capture_output=True)

        # sanity: a plain restart of SIDE would fail now
        stale = subprocess.run(["docker", "restart", SIDE], capture_output=True, text=True)
        c0 = stale.returncode != 0  # confirms the bug exists pre-fix

        # 1. recreate_dependent fixes SIDE. Docker stores the resolved ID in
        # NetworkMode, so verify the owner by NAME via netns_target_name.
        ok, _ = chk.recreate_dependent(SIDE, HEAD)
        c1 = ok and _running(SIDE) and chk.netns_target_name(SIDE) == HEAD

        # break SIDE again (recreate head once more) to test the cascade path
        _rm(HEAD)
        subprocess.run(["docker", "run", "-d", "--name", HEAD, "alpine", "sleep", "400"], capture_output=True)

        # 2. cascade: SIDE (netns) recreated, PLAIN restarted
        bot = types.SimpleNamespace()
        bot._wait_healthy = lambda n, w: ("healthy", "running", "healthy")
        msg = TelegramBot._restart_group_dependents(bot, HEAD, [SIDE, PLAIN], chk, max_wait=5)
        c2 = _running(SIDE) and chk.netns_target_name(SIDE) == HEAD and _running(PLAIN)
        c3 = ("`%s`" % SIDE in msg) and ("failed" not in msg)
    finally:
        _rm(HEAD, SIDE, PLAIN)

    checks = {
        "plain restart of sidecar fails after head recreate (the bug)": c0,
        "recreate_dependent rejoins sidecar to new head": c1,
        "cascade recreates netns sidecar + restarts plain member": c2,
        "cascade message reports success (no failures)": c3,
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
