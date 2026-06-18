#!/usr/bin/env python3
"""Local simulation for the post-update crash-loop guard (2026-06-18 GitLab incident).

Each case spins up a real Docker container reproducing one outcome and asserts
UpdateChecker._wait_healthy classifies it correctly:

  crash      : exits every few seconds, restart=always, healthcheck stuck
               "starting" the whole time          → "crashloop" (GitLab mode)
  ok         : plain running, no healthcheck, no restarts → "healthy"
  slow       : running, healthcheck perpetually "starting", NO restarts
                                                   → "starting" (legit slow boot)
  delayed    : boots fine, looks healthy, THEN crashes a few seconds later
                                                   → "crashloop" (slow-loop gap)
  delayed_nostable : SAME container, crashloop_stable_seconds=0 → "healthy"
               (proves the gap existed before the stabilization window)

_get_start_period_seconds is monkeypatched to 0 so timeouts stay short
regardless of the container's real (deliberately huge) start-period.
"""
import os, subprocess, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker


class Cfg:
    debug = True
    healthcheck_max_starting = 14
    crashloop_stable_seconds = 6


HC = ["--health-cmd", "exit 1", "--health-interval=2s",
      "--health-start-period=600s", "--health-retries=30"]
SPECS = {
    "dsim_crash":   (["--restart=always", *HC], ["sleep 3; exit 1"], 6, "crashloop"),
    "dsim_ok":      (["--restart=always"],      ["sleep 600"],       6, "healthy"),
    "dsim_slow":    (["--restart=always", *HC], ["sleep 600"],       6, "starting"),
    "dsim_delayed": (["--restart=always"],      ["sleep 6; exit 1"], 6, "crashloop"),
    # same as delayed, but stabilization disabled → old (buggy) behaviour
    "dsim_nostab":  (["--restart=always"],      ["sleep 6; exit 1"], 0, "healthy"),
}


def rmf(n):
    subprocess.run(["docker", "rm", "-f", n], capture_output=True)


def main():
    chk = UpdateChecker(Cfg())
    chk._get_start_period_seconds = lambda name: 0.0

    results = {}
    for name, (args, cmd, stable, expected) in SPECS.items():
        rmf(name)
        subprocess.run(["docker", "run", "-d", "--name", name, *args,
                        "alpine", "sh", "-c", *cmd], capture_output=True)
        chk.config.crashloop_stable_seconds = stable
        print(f"\n--- {name} (stable={stable}s, expect={expected}) ---")
        outcome, state, health = chk._wait_healthy(name, max_starting=14, interval=2)
        results[name] = (outcome, expected)
        print(f"  => outcome={outcome} state={state} health={health}")
        rmf(name)

    print("\n=== RESULT ===")
    ok = True
    for name, (got, exp) in results.items():
        mark = "PASS" if got == exp else "FAIL"
        if got != exp:
            ok = False
        print(f"  [{mark}] {name}: expected={exp} got={got}")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    finally:
        for n in SPECS:
            rmf(n)
