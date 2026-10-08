#!/usr/bin/env python3
"""A compose recreate waits long enough for the grace it grants (#2).

beta.21 gave the compose recreate `--timeout <docker_stop_timeout>` so a
slow container would stop on its own terms instead of being SIGKILLed
after Compose's 10s default. The grace was right. What it forgot is that
the subprocess wait around it stayed a flat 120 seconds — so the budget
left for pull, create and start fell from 110s to 60s, and a stack whose
container has to rejoin a VPN network namespace ran out of it:

    Command 'docker compose -f … -f … -p gluetun up -d --no-deps
    --force-recreate sabnzbd' timed out after 120 seconds

Measured against real Docker while fixing this: a container that ignores
SIGTERM spends the full 60s stopping under `--timeout 60`, and 10s
without it. The grace does not come out of thin air — it comes out of
the same 120 seconds.

The standalone path never had this problem because it waits
`effective_stop + 30` rather than a fixed number. This is the same idea:
whatever grace we grant is added to the wait, never subtracted from the
work.
"""
import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}

# ── read as a tree, not as text ──────────────────────────────────────
# Every check here used to be a grep anchored with
# `src.index("# Recreate service via compose.")` — a COMMENT. That went
# wrong in both directions: rewording the comment made the procedure
# crash with `ValueError: substring not found` instead of reporting
# anything, and the words it searched for appear in that same comment
# block, so a comment could satisfy a claim about the code.
#
# An AST cannot be satisfied by a comment, which is the defect that
# mattered. Driving `_update_compose` end to end would be better still,
# and I tried: it needs a real compose file on disk, image metadata, an
# inspect payload and a dozen backend verbs before it reaches the
# recreate, and a fixture propped up that far measures its own scaffold
# as much as the code. That is the trade, stated rather than hidden.
_SRC = open(os.path.join(APP, "update_checker.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)
_FN = next((n for n in ast.walk(_TREE)
            if isinstance(n, ast.FunctionDef) and n.name == "_update_compose"),
           None)
checks["the compose path is still where it was"] = _FN is not None
_FN = _FN or ast.FunctionDef(body=[], decorator_list=[], args=None, name="x")


def _seg(node):
    return ast.get_source_segment(_SRC, node) or ""


# ── the grace it grants ──────────────────────────────────────────────
# The `up` argv is built as a list; find it and read the element after
# `--timeout`.
_up_lists = [n for n in ast.walk(_FN)
             if isinstance(n, ast.List)
             and any(isinstance(e, ast.Constant) and e.value == "up"
                     for e in n.elts)]
_up = _up_lists[0] if _up_lists else None
_up_items = [_seg(e) for e in (_up.elts if _up else [])]
checks["the compose recreate grants the stop grace"] = (
    '"--timeout"' in _up_items
    and _up_items[_up_items.index('"--timeout"') + 1] == "str(stop_grace)")
checks["…and forces the container to be replaced"] = (
    '"--force-recreate"' in _up_items)
checks["…without dragging its dependencies along"] = '"--no-deps"' in _up_items

# …and the grace is the configured stop timeout, not a number of its own.
_grace = [n for n in ast.walk(_FN)
          if isinstance(n, ast.Assign)
          and any(isinstance(t, ast.Name) and t.id == "stop_grace"
                  for t in n.targets)]
checks["…and the grace is docker_stop_timeout"] = (
    bool(_grace) and "docker_stop_timeout" in _seg(_grace[0]))
checks["…with a default rather than a crash when it is unset"] = (
    bool(_grace) and "or 60" in _seg(_grace[0]))

# ── the wait has to CONTAIN that grace ───────────────────────────────
# beta.21 granted `--timeout 60` and left the wait at a flat 120s, so
# the budget for pull, create and start fell from 110s to 60s and a
# stack rejoining a VPN namespace ran out of it (#2, @famewolf).
_waits = [kw.value for n in ast.walk(_FN)
          if isinstance(n, ast.Call)
          for kw in n.keywords
          if kw.arg == "timeout"
          and "stop_grace" in _seg(kw.value)]
checks["the wait around the recreate contains the grace"] = bool(_waits)
_budget = None
if _waits:
    w = _waits[0]
    if (isinstance(w, ast.BinOp) and isinstance(w.op, ast.Add)
            and isinstance(w.right, ast.Constant)):
        _budget = w.right.value
# Stated as the property, not the number: whatever grace is granted, the
# working budget left for pull, create and start is what it was before
# the grace existed.
checks["…and leaves the working budget the path had before"] = (
    _budget is not None and _budget >= 110)
if _budget is not None and _budget < 110:
    print(f"      → budget is {_budget}, was 110 before the grace existed")

# …and nothing in the recreate is still waiting a flat 120.
_flat = [n for n in ast.walk(_FN)
         if isinstance(n, ast.Call)
         for kw in n.keywords
         if kw.arg == "timeout" and isinstance(kw.value, ast.Constant)
         and kw.value.value == 120]
checks["…and no call there waits a flat 120 any more"] = _flat == []

# ── the standalone path is where the shape came from ─────────────────
# It waits `effective_stop + 30` rather than a fixed number, for exactly
# this reason; if it ever goes back to a constant, this says so.
_sized = [n for n in ast.walk(_TREE)
          if isinstance(n, ast.Assign)
          and any(isinstance(t, ast.Name) and t.id == "subprocess_timeout"
                  for t in n.targets)]
checks["the standalone stop still sizes its own wait"] = (
    bool(_sized) and isinstance(_sized[0].value, ast.BinOp))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
