#!/usr/bin/env python3
"""A timeout is ours, not the daemon's (#2, @famewolf).

He sent the log that turned "one dependent fails and we don't know why"
into a chain we can read end to end:

    Stop gluetun-nzbhydra2: effective_stop=60s, subprocess=90s
    Fixing dependent gluetun-nzbhydra2 crashed: Command 'docker rename
      gluetun-nzbhydra2 gluetun-nzbhydra2_old' timed out after 10 seconds
    Failed to restart dependent gluetun-nzbhydra2: No such container
    Failed to restart dependent gluetun-nzbhydra2: No such container

Five things went wrong in a row, and only the first is a timeout:

1. the stop was given 60/90 seconds, the rename right after it got a
   hard-coded 10 — generous where it did not matter, mean where it did;
2. **Docker completed the rename anyway.** Our timeout stops us waiting;
   it does not stop the daemon working. "The command timed out" and "the
   rename did not happen" are different statements and we conflated them.
   His own words: "The rm times out after 15 seconds but the delete
   actually works";
3. the exception escaped `recreate_dependent`, which has no `try` around
   that call, so the rebuild and the rollback were both skipped;
4. the container therefore existed only as `gluetun-nzbhydra2_old`;
5. and nothing ever looked again. Every later run found no such container,
   fell through to `restart`, and printed the same line. It could not
   self-heal, which is why it was consistent rather than intermittent.

`recovery.py` heals this exact shape for the main update path, but it runs
off an in-flight note that only that path writes — the dependent recreate
never wrote one, so it was never covered.
"""

import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402

checks = {}
R = lambda rc=0, out="", err="": types.SimpleNamespace(
    returncode=rc, stdout=out, stderr=err)


class Daemon:
    """A container daemon that is slow but not broken.

    `slow` names time out on the client side and then complete anyway,
    which is the behaviour the whole fix is about.
    """

    def __init__(self, present, slow=()):
        self.present = set(present)
        self.slow = set(slow)
        self.started = []

    def rename(self, src, dst, timeout=None):
        if src in self.slow:
            # Like Docker: we stop waiting, it carries on and finishes.
            self.present.discard(src)
            self.present.add(dst)
            raise subprocess.TimeoutExpired(
                cmd=["docker", "rename", src, dst], timeout=timeout or 10)
        if src not in self.present:
            return R(1, err=f"No such container: {src}")
        self.present.discard(src)
        self.present.add(dst)
        return R(0)

    def run(self, args, timeout=None):
        if args[:1] == ["start"]:
            self.started.append(args[1])
            return R(0)
        if args[:1] == ["inspect"]:
            # Answer truthfully from what the daemon actually holds — the
            # rename check reads this, so a fixture that says "yes" to
            # everything would hide the very thing being tested.
            return R(0 if args[-1] in self.present else 1)
        return R(0)


def checker(daemon, stop_timeout=60):
    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(docker_stop_timeout=stop_timeout, debug=False)
    c._backend = daemon   # `backend` is a read-only property
    c._debug = lambda *a, **k: None
    c._container_exists = lambda n: n in daemon.present
    return c


# ── a timed-out rename that actually happened is not a failure ───────
d = Daemon(present={"app"}, slow={"app"})
c = checker(d)
checks["a rename that times out but completes counts as done"] = (
    c._rename_container("app", "app_old") is True)
checks["…and the daemon really did it"] = (
    "app_old" in d.present and "app" not in d.present)

# A rename that times out and did NOT happen is still a failure.
class Stuck(Daemon):
    def rename(self, src, dst, timeout=None):
        raise subprocess.TimeoutExpired(cmd=["docker", "rename"], timeout=10)


d2 = Stuck(present={"app"})
checks["a rename that times out and did not happen is still a failure"] = (
    checker(d2)._rename_container("app", "app_old") is False)
# And an ordinary non-zero exit stays a failure.
d3 = Daemon(present=set())
checks["a rename of something absent is a failure"] = (
    checker(d3)._rename_container("ghost", "ghost_old") is False)

# ── the rename no longer runs on a hard-coded 10 seconds ────────────
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "update_checker.py"), encoding="utf-8").read()
checks["no rename is left on the old 10-second constant"] = (
    "rename(name, old_name, timeout=10)" not in src
    and "rename(old_name, name, timeout=10)" not in src)
checks["…they follow DOCKER_STOP_TIMEOUT like the rest"] = (
    src.count("rename(") <= src.count("_lifecycle_timeout()"))

# ── the exception no longer escapes the recreate ────────────────────
i = src.index("def recreate_dependent")
body = src[i:src.index("\n    def ", i + 10)]
checks["recreate_dependent renames through the safe helper"] = (
    "self._rename_container(name, old_name)" in body)
checks["…and returns a reason instead of throwing"] = (
    "could not rename to" in body)

# ── a dependent left as <name>_old heals itself ─────────────────────
d = Daemon(present={"gluetun-nzbhydra2_old"})
c = checker(d)
checks["a dependent left as <name>_old is recovered"] = (
    c.recover_dependent("gluetun-nzbhydra2") is True)
checks["…back under its own name"] = (
    "gluetun-nzbhydra2" in d.present
    and "gluetun-nzbhydra2_old" not in d.present)
checks["…and started again"] = d.started == ["gluetun-nzbhydra2"]

# It must not touch anything it was not asked about.
d = Daemon(present={"app", "app_old"})
checks["a live container is never replaced by its backup"] = (
    checker(d).recover_dependent("app") is False and "app_old" in d.present)
d = Daemon(present={"other_old"})
checks["nothing happens when there is no backup for that name"] = (
    checker(d).recover_dependent("app") is False)

# ── the whole chain, as it happened to him ──────────────────────────
# One fake daemon that answers everything the group path asks of it, so
# the test drives the real `_restart_group_dependents` rather than a
# hand-assembled stand-in.
class FullDaemon(Daemon):
    def inspect(self, name, fmt=None, timeout=None):
        if name not in self.present:
            return R(1, out="", err="No such container")
        return R(0, out="container:abc123")

    def restart(self, name, timeout=None):
        if name not in self.present:
            return R(1, err=f"No such container: {name}")
        return R(0)


d = FullDaemon(present={"gluetun-nzbhydra2_old"})   # the state he was left in
c = checker(d)
c._wait_healthy = lambda n, w: ("healthy", "running", "healthy")
c.recreate_dependent = lambda n, h: (True, "recreated")
msg = UpdateEngine.__new__(UpdateEngine)._restart_group_dependents(
    "gluetun", ["gluetun-nzbhydra2"], c, max_wait=1)
checks["his exact leftover state heals on the next run"] = (
    "gluetun-nzbhydra2" in d.present
    and "gluetun-nzbhydra2_old" not in d.present)
checks["…and it is started again"] = d.started == ["gluetun-nzbhydra2"]
checks["…and reported as fixed, not as a failure"] = "FAILED" not in msg

# A dependent that is genuinely gone, with no backup of ours, must still
# be reported as a failure rather than silently swallowed.
d = FullDaemon(present=set())
c = checker(d)
c._wait_healthy = lambda n, w: ("healthy", "running", "healthy")
c.recreate_dependent = lambda n, h: (True, "recreated")
msg = UpdateEngine.__new__(UpdateEngine)._restart_group_dependents(
    "gluetun", ["vanished"], c, max_wait=1)
checks["a dependent gone without a backup is still reported"] = (
    "FAILED" in msg and "vanished" in msg)


# ── "could not tell" must not be read as "did not happen" ───────────
# The probe after a timeout used to go through `_container_exists`, which
# answers "probably yes" when its own inspect fails. A loaded daemon is
# exactly the condition that got us into the timeout branch, so both
# probes could fail and `exists and not exists` read as "the rename did
# not happen" — reporting failure for a rename that worked. Found in the
# audit of this very fix.
class Unanswering(Daemon):
    """Renames slowly (and completes), then refuses to answer inspects."""

    def run(self, args, timeout=None):
        if args[:1] == ["inspect"]:
            raise subprocess.TimeoutExpired(cmd=["docker", "inspect"],
                                            timeout=timeout or 10)
        return super().run(args, timeout=timeout)


d = Unanswering(present={"app"}, slow={"app"})
c = UpdateChecker.__new__(UpdateChecker)
c.config = types.SimpleNamespace(docker_stop_timeout=60, debug=False)
c._backend = d
c._debug = lambda *a, **k: None
checks["an unanswerable probe is not mistaken for 'did not happen'"] = (
    c._renamed("app", "app_old") is None)
# And the caller stays conservative: unknown counts as failure, which the
# next run heals via recover_dependent rather than guessing here.
checks["…and the rename is then reported as not done"] = (
    c._rename_container("app", "app_old") is False)

# The three states are genuinely three.
d = Daemon(present={"there"})
c2 = UpdateChecker.__new__(UpdateChecker)
c2.config = types.SimpleNamespace(docker_stop_timeout=60, debug=False)
c2._backend = types.SimpleNamespace(
    run=lambda args, timeout=None: R(0 if args[-1] == "there" else 1))
checks["the probe says True for a container that is there"] = (
    c2._container_probe("there") is True)
checks["…False for one that is not"] = (
    c2._container_probe("absent") is False)

# ── the rollback path survives a timeout instead of escaping ────────
src2 = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "update_checker.py"), encoding="utf-8").read()
i = src2.index("def _rollback_to_old")
rb = src2[i:src2.index("\n    def ", i + 10)]
checks["the rollback guards its remove"] = (
    "except subprocess.SubprocessError" in rb)
checks["…renames through the timeout-tolerant helper"] = (
    "self._rename_container(old_name, name)" in rb)
checks["…and never raises out of the handler that called it"] = (
    rb.count("except subprocess.SubprocessError") >= 2)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
