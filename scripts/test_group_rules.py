#!/usr/bin/env python3
"""The three group rules nothing was holding.

`docs/updates.md:41` — a container belongs to one group.
`docs/updates.md:44` — container 1 updates, then a wait, then container 2.
`docs/updates.md:120-123` — the dependent restart never waits less than
thirty seconds.

A sweep on 10.10.2026 reverted each of these and no procedure noticed.
The group machinery itself is well covered — the ordering, the abort on
failure, the head being the first member, a sick head not blocking the
repair — so what was missing is narrow and specific: the pauses between
the steps, and the rule that keeps a container out of two groups.

The waits are what make a group a group. A stack whose database and
application are recreated in the same second is a stack where the
application starts against a database that is not listening yet, which
is the whole reason somebody grouped them.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import update_engine  # noqa: E402
from container_store import ContainerStore  # noqa: E402

checks = {}


def _store():
    d = tempfile.mkdtemp(prefix="groups-")
    cfg = types.SimpleNamespace(data_dir=d)
    for n in ("pinned", "autoupdate", "update_windows", "ask_before_major",
              "trust_running", "cooldown", "protect_stop", "major_pending",
              "groups", "notes", "links", "pending_updates"):
        setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
    return ContainerStore(cfg)


# ── a container belongs to one group ─────────────────────────────────
# Two groups claiming the same container means the update order is
# whichever group the loop reached first, which is no order at all.
s = _store()
s.save_group("media", "media", ["gluetun", "sonarr", "radarr"], wait_seconds=30)
s.save_group("downloads", "downloads", ["gluetun", "qbittorrent"], wait_seconds=30)
_groups = s.get_groups()
checks["moving a container to another group takes it out of the first"] = (
    "gluetun" not in (_groups.get("media") or {}).get("containers", []))
checks["…and it is in the new one"] = (
    "gluetun" in (_groups.get("downloads") or {}).get("containers", []))
checks["…while the other members stay where they were"] = (
    sorted((_groups.get("media") or {}).get("containers", []))
    == ["radarr", "sonarr"])

# A group left with nothing in it is removed rather than kept as an
# empty shell that shows up in the interface.
s2 = _store()
s2.save_group("solo", "solo", ["only"], wait_seconds=30)
s2.save_group("other", "other", ["only"], wait_seconds=30)
checks["a group emptied by the move disappears"] = "solo" not in s2.get_groups()

# ── the wait is bounded and has a sane default ───────────────────────
s3 = _store()
s3.save_group("g", "g", ["a", "b"], wait_seconds=45)
checks["a wait inside the range is kept"] = (
    s3.get_groups()["g"]["wait_seconds"] == 45)
s3.save_group("g", "g", ["a", "b"], wait_seconds=9999)
checks["…one that is absurd is capped"] = (
    s3.get_groups()["g"]["wait_seconds"] == 600)
s3.save_group("g", "g", ["a", "b"], wait_seconds=-5)
checks["…a negative one becomes zero, not a hang"] = (
    s3.get_groups()["g"]["wait_seconds"] == 0)
s3.save_group("g", "g", ["a", "b"], wait_seconds="nonsense")
checks["…and something unreadable falls back to thirty seconds"] = (
    s3.get_groups()["g"]["wait_seconds"] == 30)

# ── the two waits inside the batch ───────────────────────────────────
# Read as a tree, and that is a compromise I would rather state than
# bury. `_process_update_batch` needs a dozen collaborators before it
# reaches either line, and a fixture propped up that far measures its
# own scaffold — I tried, and stopped when the sixth stub went in.
#
# What a tree still buys over the grep these replace: a comment cannot
# satisfy it, and the SHAPE is asserted rather than the words. The
# first version of this block was worse than either — it copied the
# engine's own loop condition into the test and checked the copy,
# which is the mistake CLAUDE.md has a rule about.
import ast  # noqa: E402

_SRC = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "update_engine.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)
_BATCH = next((n for n in ast.walk(_TREE)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_process_update_batch"), None)
checks["the batch is still where the waits live"] = _BATCH is not None
_BATCH = _BATCH or ast.parse("def x(): pass").body[0]


def _seg(node):
    return ast.get_source_segment(_SRC, node) or ""


# The inter-container wait: a call to `_wait` inside the batch, guarded
# by the group's own `wait_seconds` being above zero.
_waits = [n for n in ast.walk(_BATCH)
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
          and n.func.id == "_wait"]
checks["the batch pauses between containers of one group"] = bool(_waits)
_guards = [_seg(n) for n in ast.walk(_BATCH)
           if isinstance(n, ast.If) and "wait_s" in _seg(n)]
checks["…only when the group asks for a pause"] = any(
    "> 0" in g for g in _guards)
checks["…and only while staying inside the same group"] = any(
    "prev_group" in _seg(n) for n in ast.walk(_BATCH)
    if isinstance(n, ast.If))

# The dependent restart's floor. `docs/updates.md:120-123` — a sidecar
# rejoining a namespace needs the head to be listening, and a zero
# here is a race the user cannot see.
# EVERY call site, not any of them. There are two — the ordinary
# cascade and the one after a head rollback — and asking whether a
# floor exists somewhere passed with one of the two stripped.
_cascades = [n for n in ast.walk(_BATCH)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_restart_group_dependents"]
_with_max_wait = [n for n in _cascades
                  if any(kw.arg == "max_wait" for kw in n.keywords)]
checks["every dependent restart is given an explicit wait"] = (
    len(_cascades) >= 2 and len(_with_max_wait) == len(_cascades))

_floored = [n for n in ast.walk(_BATCH)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "max" and "wait_seconds" in _seg(n)
            and any(isinstance(a, ast.Constant) and a.value == 30
                    for a in n.args)]
checks["…and each of them floors it at the documented thirty seconds"] = (
    len(_floored) == len(_cascades))
if len(_floored) != len(_cascades):
    print(f"      → {len(_cascades)} cascades, {len(_floored)} floored")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
