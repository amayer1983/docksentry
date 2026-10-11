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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livefixture as lf  # noqa: E402
from update_checker import UpdateChecker
from telegram_bot import TelegramBot
from update_engine import UpdateEngine

# A missing container runtime is not a regression. Without this the
# procedure goes red on a machine that simply has no Docker, which is
# indistinguishable from a real failure — measured: five procedures did
# exactly that before the guard, while test_multihost_live skipped
# cleanly. CI must be able to tell the two apart.
if __import__("subprocess").run(["docker", "info"],
                                capture_output=True).returncode != 0:
    print("SKIP: no Docker daemon available")
    __import__("sys").exit(0)

# Per-run names. They were fixed, and every run begins by force-removing
# them — so two suites at once pulled each other's containers out from
# underneath and the loser reported dependent recreation as broken.
# Measured on `test_glob.py`, which had the same shape: with a foreign
# `ds_ctf-a-even` running, the old fixture removed it.
HEAD, SIDE, PLAIN = (lf.name("ds8_head"), lf.name("ds8_side"),
                     lf.name("ds8_plain"))


def _rm(*names):
    lf.rm(*names)


def _running(name):
    return subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                          capture_output=True, text=True).stdout.strip() == "true"


def _alpine(name, *extra):
    """The argv for one throwaway container that waits to be stopped."""
    return ["run", "-d", "--name", name, *extra, "alpine", "sh", "-c",
            "trap 'exit 0' TERM; sleep 400 & wait"]


def _netns(name):
    return subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", name],
                          capture_output=True, text=True).stdout.strip()


def main():
    chk = UpdateChecker(types.SimpleNamespace(debug=False))
    _rm(HEAD, SIDE, PLAIN)
    try:
        # `lf.start`, not a discarded return code: a registry that will
        # not serve `alpine` leaves no container, and then every check
        # below reports dependent recreation as broken — a red that
        # points at code which is fine.
        lf.start(_alpine(HEAD))
        lf.start(_alpine(SIDE, "--network", f"container:{HEAD}"))
        lf.start(_alpine(PLAIN))

        # recreate the head → new ID; SIDE's stored netns id is now dead
        _rm(HEAD)
        lf.start(_alpine(HEAD))

        # sanity: a plain restart of SIDE would fail now
        stale = subprocess.run(["docker", "restart", SIDE], capture_output=True, text=True)
        c0 = stale.returncode != 0  # confirms the bug exists pre-fix

        # 1. recreate_dependent fixes SIDE. Docker stores the resolved ID in
        # NetworkMode, so verify the owner by NAME via netns_target_name.
        ok, _ = chk.recreate_dependent(SIDE, HEAD)
        c1 = ok and _running(SIDE) and chk.netns_target_name(SIDE) == HEAD

        # break SIDE again (recreate head once more) to test the cascade path
        _rm(HEAD)
        lf.start(_alpine(HEAD))

        # 2. cascade: SIDE (netns) recreated, PLAIN restarted.
        # The cascade waits on the head via checker._wait_healthy (3-tuple) —
        # stub it on the real checker (not the bot) so we don't actually wait,
        # and so this exercises the real call path (#2 @famewolf: a bot-local
        # bool _wait_healthy duplicate used to crash the cascade here).
        bot = types.SimpleNamespace()
        chk._wait_healthy = lambda name, max_starting=None, interval=10: ("healthy", "running", "healthy")
        # _restart_group_dependents now lives on UpdateEngine; call it there
        # directly (it only needs the passed `checker` + subprocess).
        msg = UpdateEngine._restart_group_dependents(bot, HEAD, [SIDE, PLAIN], chk, max_wait=5)
        c2 = _running(SIDE) and chk.netns_target_name(SIDE) == HEAD and _running(PLAIN)
        # `"failed"` lower-case, while `update_engine.py:532` writes
        # `"{n} FAILED"` — so this could not fall. Both spellings now,
        # and the failure marker with them.
        c3 = (("`%s`" % SIDE in msg)
              and not any(w in msg for w in ("failed", "FAILED", "❌")))
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
