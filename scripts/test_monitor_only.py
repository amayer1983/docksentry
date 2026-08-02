#!/usr/bin/env python3
"""Monitor-only containers, and wildcard name matching (#55, @LeeNX).

The case: podman quadlets. systemd owns those containers, so recreating one
behind its back leaves two things with an opinion about what should be
running. Every pre-existing exit meant "stop looking" — `pin`,
`enable=false` and `exclude` drop the container from the scan entirely, so
you lose the version and update information that is the reason for watching
it. This one means "look, report, never touch".
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker, name_matches


def main():
    checks = {}

    # ── wildcard matching ─────────────────────────────────────────
    # Wildcards rather than regex on purpose: the thing people reach for is
    # `systemd-*`, and a regex matching more than intended is a bad failure
    # mode for a setting whose job is to stop Docksentry touching things.
    checks["prefix pattern matches"] = name_matches("systemd-nginx", ["systemd-*"])
    checks["prefix pattern does not over-match"] = not name_matches("nginx", ["systemd-*"])
    checks["several patterns"] = name_matches("gitea-runner", ["systemd-*", "gitea-*"])
    checks["a plain name still works"] = name_matches("nginx", ["nginx"])
    checks["a plain name is not a prefix"] = not name_matches("nginx-two", ["nginx"])
    checks["empty list matches nothing"] = not name_matches("x", [])
    checks["blank entries are ignored"] = name_matches("a-b", ["a-*", "", "  "])
    # Container names are case-sensitive to Docker, so matching must be too.
    checks["matching is case-sensitive"] = not name_matches("SYSTEMD-x", ["systemd-*"])
    checks["? and [] work"] = (name_matches("web1", ["web?"])
                               and name_matches("web1", ["web[0-9]"]))

    # ── the monitor-only decision ─────────────────────────────────
    o = UpdateChecker.__new__(UpdateChecker)
    o.config = types.SimpleNamespace(monitor_only_containers=["systemd-*"])
    mo = lambda n, lab=None: UpdateChecker.is_monitor_only(o, n, lab)

    checks["pattern makes a container monitor-only"] = mo("systemd-nginx")
    checks["an unmatched container is not"] = not mo("nginx")
    # The label wins where set, matching every other docksentry.* label.
    checks["label opts a container in"] = mo("nginx", {"docksentry.monitor-only": "true"})
    checks["label opts a matched container OUT"] = not mo(
        "systemd-x", {"docksentry.monitor-only": "false"})
    checks["an unrelated label changes nothing"] = mo(
        "systemd-x", {"docksentry.auto": "true"})

    # ── the refusal, at the one gate every caller passes ──────────
    # Placed beside the existing self-kill backstop for the same reason:
    # the scheduler, the Web UI button, both bots and anything added later
    # all route through update_container. "Never automatically" would be a
    # half-exception — the update is wrong no matter who asks.
    calls = []
    o2 = UpdateChecker.__new__(UpdateChecker)
    o2.config = types.SimpleNamespace(monitor_only_containers=["systemd-*"])
    o2.debug = False
    o2._debug = lambda *a, **k: None
    o2._save_history = lambda *a, **k: calls.append(a)
    o2._would_kill_self = lambda n: False
    o2.get_container_labels = lambda n: {}
    ok, msg = UpdateChecker.update_container(o2, "systemd-nginx", "nginx:alpine")
    checks["update is refused"] = ok is False
    checks["the refusal says why"] = "monitor-only" in msg
    checks["the refusal is recorded in history"] = len(calls) == 1

    # A failure to read labels must not silently ALLOW an update the
    # operator asked to be impossible.
    o3 = UpdateChecker.__new__(UpdateChecker)
    o3.config = types.SimpleNamespace(monitor_only_containers=["systemd-*"])
    o3.debug = False
    o3._debug = lambda *a, **k: None
    o3._save_history = lambda *a, **k: None
    o3._would_kill_self = lambda n: False

    def boom(n):
        raise RuntimeError("daemon unreachable")

    o3.get_container_labels = boom
    ok3, msg3 = UpdateChecker.update_container(o3, "systemd-nginx", "nginx:alpine")
    checks["a label-read failure still refuses"] = ok3 is False

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
