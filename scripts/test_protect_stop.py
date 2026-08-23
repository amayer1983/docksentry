#!/usr/bin/env python3
"""Test for the per-container stop-protection flag (#38, @LeeNX).

  - store toggle/is roundtrip
  - lifecycle.act refuses STOP for a protected container (before touching
    Docker), but still allows RESTART

Uses a real throwaway container + real store/checker. Requires Docker.
Exits non-zero on any failure.
"""
import sys, os, types, tempfile, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from container_store import ContainerStore
from update_checker import UpdateChecker
from container_backend import get_backend
from i18n import get_translator

# A missing container runtime is not a regression. Without this the
# procedure goes red on a machine that simply has no Docker, which is
# indistinguishable from a real failure — measured: five procedures did
# exactly that before the guard, while test_multihost_live skipped
# cleanly. CI must be able to tell the two apart.
if __import__("subprocess").run(["docker", "info"],
                                capture_output=True).returncode != 0:
    print("SKIP: no Docker daemon available")
    __import__("sys").exit(0)

NAME = "ds_protect_test"
FILE_ATTRS = ["pinned_file", "autoupdate_file", "update_windows_file",
              "ask_before_major_file", "trust_running_file", "cooldown_file",
              "protect_stop_file", "major_pending_file", "groups_file",
              "notes_file", "links_file"]


def _running(name):
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                       capture_output=True, text=True)
    return r.stdout.strip() == "true"


def main():
    d = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(debug=False,
                                **{a: os.path.join(d, a + ".json") for a in FILE_ATTRS})
    store = ContainerStore(cfg)
    checker = UpdateChecker(cfg)

    # The action itself is `lifecycle.act` now — shared with Discord, so
    # this exercises what BOTH chats do rather than one of them (#63).
    # Against a real container, because the point of this test is that
    # the refusal happens before docker is touched, and a stub backend
    # cannot show that.
    import lifecycle
    backend = get_backend(None)
    t = get_translator("en")

    def act(action):
        """`(ok, message)` — the old shape, so the checks below still read
        as "was it stopped and what did it say"."""
        o = lifecycle.act(action, [None],
                          backend_for=lambda h: backend,
                          checker_for=lambda h: checker,
                          store_for=lambda h: store,
                          partial=NAME, update_running=False)
        if o.fatal is not None:
            return False, t(o.fatal.key, **o.fatal.params)
        r = o.replies[0]
        return r.ok, t(r.key, **r.params)

    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", NAME, "alpine", "sleep", "300"],
                   capture_output=True)
    try:
        c1 = store.toggle_protect_stop(NAME) is True and store.is_protect_stop(NAME)

        ok_stop, msg_stop = act("stop")
        c2 = (ok_stop is False) and ("protected" in msg_stop) and _running(NAME)

        ok_re, _ = act("restart")
        c3 = (ok_re is True) and _running(NAME)

        store.toggle_protect_stop(NAME)  # unprotect
        ok_stop2, _ = act("stop")
        c4 = (ok_stop2 is True) and (not _running(NAME))
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)

    checks = {
        "toggle/is roundtrip": c1,
        "stop refused while protected (container still running)": c2,
        "restart allowed while protected": c3,
        "stop works again after unprotect": c4,
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
