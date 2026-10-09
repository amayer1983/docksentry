#!/usr/bin/env python3
"""The promise people lean on hardest, driven for the first time.

README:37 and :65: Docksentry updates a container and rolls it back
automatically when the new one fails its healthcheck. That is the reason
somebody lets a tool work unattended at three in the morning, and on
06.10.2026 a mutation sweep found it was kept by nothing the suite could
see — `_rollback_to_old` could be given a bare `return` as its first
line and all 181 procedures stayed green. The only thing standing near
it was a source grep for the name of the call site.

What makes this function worth real care is its first rule: **it must
not make things worse.** The container being restored may be the only
copy the user has. So the order is deliberate —

1. no backup under `<name>_old` → touch nothing at all, and say so.
   Destroying `name` here would be strictly worse than the failed
   update;
2. otherwise force-remove whatever sits at `name` (the broken new
   container, possibly still running), rename the backup into place,
   start it.

And it must never raise. `_update_standalone` calls it from its own
`except` handler, so an exception escaping here skips the history write
and the in-flight clear — which is #2 all over again, arriving through
the code written to prevent it.

Every case below is the real method against a stand-in backend. Nothing
needs a daemon: `backend` is the seam every container command goes
through.
"""
import ast
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")


class _Backend:
    """A host that remembers what it was asked to do."""

    def __init__(self, existing=(), fail=(), raises=()):
        self.existing = set(existing)
        self.fail = set(fail)        # verbs that return non-zero
        self.raises = set(raises)    # verbs that blow up
        self.did = []                # (verb, args…)

    def _answer(self, verb, *args):
        self.did.append((verb,) + args)
        if verb in self.raises:
            raise subprocess.SubprocessError(f"{verb} timed out")
        rc = 1 if verb in self.fail else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="no")

    def rm(self, name, force=False, timeout=None):
        if not self.fail & {"rm"}:
            self.existing.discard(name)
        return self._answer("rm", name, force)

    def start(self, name, timeout=None):
        return self._answer("start", name)

    def rename(self, src, dst, timeout=None):
        r = self._answer("rename", src, dst)
        # A rename only works if the source is there. The first version
        # added `dst` regardless, so with the no-backup guard removed
        # the container reappeared and "the container the user still
        # has is still there" read green on a container that had just
        # been removed. Real Docker fails the rename.
        if r.returncode == 0 and src in self.existing:
            self.existing.discard(src)
            self.existing.add(dst)
        elif r.returncode == 0:
            return types.SimpleNamespace(returncode=1, stdout="",
                                         stderr=f"No such container: {src}")
        return r

    def run(self, args, timeout=None):
        return self._answer("run", *args)


def _checker(backend):
    c = UpdateChecker.__new__(UpdateChecker)
    c._backend = backend
    c.config = types.SimpleNamespace(docker_stop_timeout=10)
    c._debug = lambda *_a, **_k: None
    c._lifecycle_timeout = lambda: 30
    c._container_exists = lambda n: n in backend.existing
    c._rename_container = lambda src, dst: (
        backend.rename(src, dst).returncode == 0)
    return c


def _verbs(b):
    return [d[0] for d in b.did]


# ── 1. no backup: do not make it worse ───────────────────────────────
# The one case where the wrong move is unrecoverable. `web` may be the
# only container the user has; the update failed, and removing it would
# turn a broken service into no service.
b = _Backend(existing={"web"})
restored = _checker(b)._rollback_to_old("web", "web_old")
checks["with no backup, nothing is touched"] = b.did == []
checks["…and it says the rollback did not happen"] = restored is False
checks["…and the container the user still has is still there"] = (
    "web" in b.existing)

# ── 2. the ordinary case ─────────────────────────────────────────────
b = _Backend(existing={"web", "web_old"})
restored = _checker(b)._rollback_to_old("web", "web_old")
checks["with a backup, the broken container is removed"] = (
    ("rm", "web", True) in b.did)
checks["…forcibly, because a wedged container used to block the rename"] = (
    any(d[0] == "rm" and d[2] is True for d in b.did))
checks["…then the backup is renamed into its place"] = (
    ("rename", "web_old", "web") in b.did)
checks["…and started"] = ("start", "web") in b.did
checks["…in that order"] = _verbs(b) == ["rm", "rename", "start"]
checks["…and the rollback reports success"] = restored is True
checks["…leaving the service under its own name again"] = (
    "web" in b.existing and "web_old" not in b.existing)

# ── 3. the remove fails — carry on, the rename is what matters ───────
# There may be nothing at `name` at all, which is not a problem: the
# rename below is the step that decides.
b = _Backend(existing={"web_old"}, fail={"rm"})
restored = _checker(b)._rollback_to_old("web", "web_old")
checks["a failed remove does not abandon the rollback"] = (
    ("rename", "web_old", "web") in b.did and restored is True)

# ── 4. the rename fails — stop, and admit it ─────────────────────────
b = _Backend(existing={"web", "web_old"}, fail={"rename"})
restored = _checker(b)._rollback_to_old("web", "web_old")
checks["a failed rename ends the rollback"] = restored is False
checks["…and nothing is started on top of the wreckage"] = (
    "start" not in _verbs(b))

# ── 5. restored but not started ──────────────────────────────────────
# Honesty matters more than the return value here: claiming a rollback
# that left the service down is worse than reporting a half-success.
b = _Backend(existing={"web", "web_old"}, fail={"start"})
restored = _checker(b)._rollback_to_old("web", "web_old")
checks["a container restored but not started is not called a success"] = (
    restored is False)
checks["…though it was restored, so the backup is in place"] = (
    ("rename", "web_old", "web") in b.did)

# ── 6. it must never raise ───────────────────────────────────────────
# `_update_standalone` calls this from its own `except` handler. An
# exception escaping here skips the history write and the in-flight
# clear and leaves the container as `<name>_old` with nothing under its
# own name — the same shape as #2, arriving through the code meant to
# prevent it.
for verb in ("rm", "start"):
    b = _Backend(existing={"web", "web_old"}, raises={verb})
    try:
        _checker(b)._rollback_to_old("web", "web_old")
        raised = False
    except Exception:
        raised = True
    checks[f"a backend that raises on {verb} does not escape the rollback"] = (
        not raised)
# …and a raising `rm` still lets the rename happen, because the backup
# is the thing that matters.
b = _Backend(existing={"web", "web_old"}, raises={"rm"})
_checker(b)._rollback_to_old("web", "web_old")
checks["…and a raising remove still tries the rename"] = (
    ("rename", "web_old", "web") in b.did)

# ── 7. every failure path in the update really calls it ──────────────
# Read as a tree, not as text: a grep for the name is satisfied by a
# comment, and four of the five call sites here sit next to a comment
# that mentions the function by name. What is counted is actual calls.
_src = open(os.path.join(ROOT, "app", "update_checker.py"),
            encoding="utf-8").read()
_tree = ast.parse(_src)
_calls = [n for n in ast.walk(_tree)
          if isinstance(n, ast.Call)
          and isinstance(n.func, ast.Attribute)
          and n.func.attr == "_rollback_to_old"]
checks["every failure path in the recreate still rolls back"] = len(_calls) >= 5
# The catch-all handler is the one that must not depend on a local that
# may never have been assigned, so it passes the literal name.
checks["…and the catch-all uses the literal backup name"] = any(
    len(c.args) == 2 and isinstance(c.args[1], ast.JoinedStr)
    for c in _calls)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
