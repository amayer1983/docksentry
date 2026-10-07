#!/usr/bin/env python3
"""What Docksentry is allowed to touch — driven, not mirrored.

Four promises live in `get_running_containers`, and all four are
switches a user sets on purpose:

* `EXCLUDE_CONTAINERS=name,glob*` — keep these out of every run
* `/pin <name>` — the stored pin, "never update this one"
* `docksentry.pin=true` on the container — the same thing from a
  compose file (#42, @LeeNX)
* `docksentry.enable=false` / `docksentry.exclude=true` — take a
  container out of Docksentry's scope entirely

On 06.10.2026 a mutation sweep showed each of the four `continue`s could
be removed one at a time and not one of 181 procedures went red. The
reason is in the old test's own words — `test_labels.py` has a section
headed "exclude decision (mirrors get_running_containers)" and then
writes the production expression out again. Four checks on a copy. The
copy agreed with itself while the original could say anything.

So this drives the real method against a fake backend and asks what came
back. A pinned database that starts appearing in the update list is the
kind of thing that is noticed after the update, which is too late — it
is the promise people lean on hardest when they let a tool do unattended
work.

Nothing here needs a daemon: `backend` is the seam every container
command in `update_checker` goes through, so a stand-in is the whole
host.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

checks = {}


class _Backend:
    """A host with containers, their labels, and nothing else."""

    def __init__(self, rows, labels=None):
        self._rows = rows                       # [(name, image)]
        self._labels = labels or {}
        self.asked = []

    def run(self, args, timeout=None):
        self.asked.append(" ".join(args[:2]))
        if args[:1] == ["ps"]:
            out = "\n".join(f"{n}|{i}" for n, i in self._rows)
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        if args[:1] == ["inspect"]:
            # `_labels_for` asks for labels per container; anything else
            # inspecting is the image-id resolution, which these
            # fixtures avoid by using real image names.
            name = args[-1]
            labels = self._labels.get(name) or {}
            out = ",".join(f"{k}={v}" for k, v in labels.items())
            return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _checker(rows, labels=None, exclude=(), pinned=()):
    c = UpdateChecker.__new__(UpdateChecker)
    # A LIST, which is what `config.py` gives it (default `[]`) — a
    # string here iterates to single characters and matches nothing,
    # which is how this fixture was wrong the first time round.
    c.config = types.SimpleNamespace(exclude_containers=list(exclude),
                                     container_cli="docker")
    c._backend = _Backend(rows, labels)
    c._trace = lambda *_a, **_k: None
    c._own_container_name = lambda: "docksentry"
    c._get_pinned = lambda: set(pinned)
    c._get_compose_info = lambda name: {}
    c._labels_for = lambda names: {n: (labels or {}).get(n) or {}
                                   for n in names}
    return c


def _names(c, **kw):
    return [x["name"] for x in c.get_running_containers(**kw)]


ROWS = [("web", "nginx:1.25"), ("db", "postgres:16"),
        ("cache", "redis:7"), ("docksentry", "amayer1983/docksentry:latest")]

# ── the baseline: without a switch set, everything but us is in scope ─
# Stated first, because every check below is "…and this one is NOT in
# that list". If the baseline were empty they would all pass for the
# wrong reason.
plain = _checker(ROWS)
checks["without a switch set, every container is in scope"] = (
    _names(plain) == ["web", "db", "cache"])
checks["…except Docksentry itself, which the update path must not touch"] = (
    "docksentry" not in _names(plain))
# …but a reader sees it: `/status` hiding the container that answers it
# confused @NotRetarded twice over (#2).
checks["…though a reader asking for it does see it"] = (
    "docksentry" in _names(plain, include_self=True))

# ── EXCLUDE_CONTAINERS ───────────────────────────────────────────────
ex = _checker(ROWS, exclude=["db"])
checks["EXCLUDE_CONTAINERS keeps that container out"] = (
    _names(ex) == ["web", "cache"])
checks["…and leaves the others alone"] = "web" in _names(ex)
glob = _checker([("web-1", "nginx:1.25"), ("web-2", "nginx:1.25"),
                 ("db", "postgres:16")], exclude=["web-*"])
checks["…and a glob excludes the whole family"] = _names(glob) == ["db"]
several = _checker(ROWS, exclude=["db", "cache"])
checks["…and a list excludes each of them"] = _names(several) == ["web"]

# ── the stored pin ───────────────────────────────────────────────────
pin = _checker(ROWS, pinned=("db",))
checks["a pinned container is not in scope at all"] = _names(pin) == ["web", "cache"]
checks["…and unpinning it brings it back"] = "db" in _names(_checker(ROWS))

# ── docksentry.pin on the container (#42, @LeeNX) ────────────────────
label_pin = _checker(ROWS, labels={"db": {"docksentry.pin": "true"}})
checks["docksentry.pin=true does the same from a compose file"] = (
    _names(label_pin) == ["web", "cache"])
checks["…and docksentry.pin=false does not pin anything"] = (
    "db" in _names(_checker(ROWS, labels={"db": {"docksentry.pin": "false"}})))

# ── docksentry.enable / docksentry.exclude ───────────────────────────
off = _checker(ROWS, labels={"cache": {"docksentry.enable": "false"}})
checks["docksentry.enable=false takes a container out of scope"] = (
    _names(off) == ["web", "db"])
excl = _checker(ROWS, labels={"cache": {"docksentry.exclude": "true"}})
checks["docksentry.exclude=true does too"] = _names(excl) == ["web", "db"]
checks["…and enable=true leaves it in"] = (
    "cache" in _names(_checker(ROWS,
                               labels={"cache": {"docksentry.enable": "true"}})))

# ── the switches do not interfere with each other ────────────────────
both = _checker(ROWS, exclude=["web"], pinned=("db",),
                labels={"cache": {"docksentry.exclude": "true"}})
checks["three different switches together leave nothing in scope"] = (
    _names(both) == [])

# ── a failing `ps` is not an empty host ──────────────────────────────
# Measured behaviour worth pinning while we are in here: an empty list
# from a dead daemon is indistinguishable from a host that runs nothing,
# and the update path used to treat it as the latter.
class _Dead:
    def run(self, args, timeout=None):
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="Cannot connect to the daemon")


dead = _checker(ROWS)
dead._backend = _Dead()
try:
    dead.get_running_containers()
    checks["a host whose `ps` failed is not reported as empty"] = False
except Exception as e:
    checks["a host whose `ps` failed is not reported as empty"] = (
        type(e).__name__ == "ContainerListUnavailable")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
