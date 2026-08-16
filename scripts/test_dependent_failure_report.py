#!/usr/bin/env python3
"""A failed dependent has to say why, and has to be visible (#2, @famewolf).

He ran a Gluetun stack with ten containers behind it. One of them,
`gluetun-nzbhydra2`, failed to be recreated on 6 August and every time
after. His whole arr stack depends on it for searches, so it sat broken
for **ten days** while Docksentry told him, in the middle of a line of
good news:

    🔁 gluetun dependents: 9 ok (booksarr, easynewsindexer, flaresolverr,
    lidarr, prowlarr, qbittorrent, radarr, readarr, sonarr), failed 1
    (gluetun-nzbhydra2)

Two separate failures of ours in that one line.

**The reason was thrown away.** `recreate_dependent` returns `(ok,
detail)`; the caller printed `detail` to the container's own log and put
only the container's name in the notification. So the message could tell
him that something was wrong and never what, and the one line that would
have let him act sat in a log he had no reason to open. That is why ten
days passed rather than ten minutes.

**And the failure was a suffix on a success line.** Nine ticks, then the
bad news at the end of the same sentence. He missed it repeatedly and
asked for it "in bold and in red". He is right: a message where failure
looks like the tail of good news is a message that will be skimmed.

Alongside that he hit hard-coded 15-second timeouts — `docker kill ollama`
and `docker rm -f` on byparr and metube — which is not enough for a
container that is slow to die. Those now follow `DOCKER_STOP_TIMEOUT`
instead of being a constant.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_engine import UpdateEngine  # noqa: E402
from update_checker import UpdateChecker  # noqa: E402

checks = {}


class FakeBackend:
    """Every dependent is a netns sidecar unless named otherwise."""

    def __init__(self, netns=True):
        self.netns = netns

    def inspect(self, name, fmt=None, timeout=None):
        mode = "container:abc123" if self.netns else "bridge"
        return types.SimpleNamespace(returncode=0, stdout=mode, stderr="")

    def restart(self, name, timeout=None):
        ok = "bad" not in name
        return types.SimpleNamespace(
            returncode=0 if ok else 1, stdout="",
            stderr="" if ok else "no such container")


class FakeChecker:
    def __init__(self, backend, failing=(), reason="disk full"):
        self.backend = backend
        self.failing = set(failing)
        self.reason = reason

    def _wait_healthy(self, name, wait):
        return ("healthy", "running", "healthy")

    def recreate_dependent(self, name, head):
        if name in self.failing:
            return False, self.reason
        return True, "recreated"


def run(dependents, failing=(), reason="disk full", netns=True):
    eng = UpdateEngine.__new__(UpdateEngine)
    backend = FakeBackend(netns=netns)
    checker = FakeChecker(backend, failing=failing, reason=reason)
    return eng._restart_group_dependents("gluetun", dependents, checker,
                                         max_wait=1)


# ── the reason reaches the message ───────────────────────────────────
NINE = [f"gluetun-{n}" for n in
        ("booksarr", "easynewsindexer", "flaresolverr", "lidarr", "prowlarr",
         "qbittorrent", "radarr", "readarr", "sonarr")]
msg = run(NINE + ["gluetun-nzbhydra2"], failing=["gluetun-nzbhydra2"],
          reason="name in use by container 0e1f2a")

checks["the failing container is named"] = "gluetun-nzbhydra2" in msg
checks["…and the REASON is in the message, not only the log"] = (
    "name in use by container 0e1f2a" in msg)
# The whole point: he could not act because he could not see why.
checks["…so the message is actionable on its own"] = (
    len([l for l in msg.split("\n") if "nzbhydra2" in l and ":" in l]) >= 1)

# ── the failure is not a suffix on good news ─────────────────────────
first = msg.split("\n")[0]
checks["the first line reports the failure, not the successes"] = (
    "FAILED" in first and "nzbhydra2" not in first.split("FAILED")[0])
checks["…marked so it cannot be skimmed past"] = first.startswith("❌")
checks["…and the successes come after, not before"] = (
    msg.index("FAILED") < msg.index("9 ok"))
checks["…on their own line"] = msg.count("\n") >= 2
# The old shape, which read as good news at a glance, must be gone.
checks["the old 'N ok (…), failed N (…)' shape is gone"] = (
    "ok (" in msg and not msg.split("\n")[0].startswith("🔁"))

# ── nothing changes when everything works ───────────────────────────
good = run(NINE)
checks["an all-good run still reads as one cheerful line"] = (
    good.startswith("🔁") and "FAILED" not in good and "\n" not in good)

# ── several failures each carry their own reason ────────────────────
multi = run(["a", "b", "c"], failing=["a", "c"], reason="daemon says no")
checks["every failure gets its own line"] = multi.count("daemon says no") == 2
checks["…and the survivors are still listed"] = "`b`" in multi

# ── a non-netns member that fails to restart reports its stderr ─────
plain = run(["bad-one"], netns=False)
checks["a plain restart failure also carries its reason"] = (
    "no such container" in plain)

# ── the 15-second constant is gone ──────────────────────────────────
c = UpdateChecker.__new__(UpdateChecker)
c.config = types.SimpleNamespace(docker_stop_timeout=60)
checks["kill/rm no longer time out at a hard-coded 15s"] = (
    c._lifecycle_timeout() >= 30)
checks["…it follows DOCKER_STOP_TIMEOUT"] = (
    (setattr(c.config, "docker_stop_timeout", 300) or c._lifecycle_timeout())
    == 300)
checks["…with a floor, so a tiny setting cannot make it worse"] = (
    (setattr(c.config, "docker_stop_timeout", 5) or c._lifecycle_timeout())
    == 30)
checks["…and survives a missing or zero setting"] = (
    (setattr(c.config, "docker_stop_timeout", None) or c._lifecycle_timeout())
    == 60)
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "update_checker.py"), encoding="utf-8").read()
checks["no lifecycle call is left on the old constant"] = (
    "force=True, timeout=15" not in src and "kill(name, timeout=15)" not in src)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
