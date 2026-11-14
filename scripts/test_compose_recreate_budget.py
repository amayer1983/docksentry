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
import contextlib
import io
import os
import sys
import tempfile
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

from update_checker import UpdateChecker  # noqa: E402

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
# Driven now, not read. Every check here used to be an AST shape, and a
# shape is satisfiable without the property: `timeout=(stop_grace * 0)
# + 120` keeps the BinOp, the Add, the constant 120 and the word
# `stop_grace`, passes all four, and reinstates the exact #2 defect —
# grace granted, working budget back down to 120 (audit, 07.11.2026).
# The ratchet noticed only that the LINE had changed, which a harmless
# rewording would have done too.
#
# The comment above says driving this end to end needs "a dozen backend
# verbs". It needs three. `test_pull_failure_report.py` already reaches
# `_update_compose` with a real compose file on disk, and carrying it
# past a successful pull takes a stubbed health wait and nothing else.


class _Daemon:
    """Answers every compose verb, remembers how it was asked."""

    name = "local"

    def __init__(self):
        self.calls = []

    def run(self, args, timeout=None, **kw):
        self.calls.append((list(args), timeout))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def pull(self, ref, timeout=None, **kw):
        self.calls.append((["pull", ref], timeout))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def inspect(self, *a, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")


def _recreate(stop_timeout):
    """Run the real `_update_compose`; return (grace granted, wait given)."""
    work = tempfile.mkdtemp(prefix="ds-budget-")
    compose_file = os.path.join(work, "docker-compose.yml")
    with open(compose_file, "w", encoding="utf-8") as f:
        f.write("services:\n  app:\n    image: img:latest\n")
    cfg = types.SimpleNamespace(debug=False, pull_timeout=240,
                                docker_stop_timeout=stop_timeout)
    daemon = _Daemon()
    chk = UpdateChecker(cfg, backend=daemon)
    # Environment, not decision: image metadata, history, and the health
    # wait that would otherwise poll a container which does not exist.
    chk._get_image_created = lambda *a, **kw: "?"
    chk._get_image_size = lambda *a, **kw: "?"
    chk._get_image_version_label = lambda *a, **kw: None
    chk._save_history = lambda *a, **kw: None
    chk._layer_farewell = lambda *a, **kw: ""
    chk._wait_healthy = lambda *a, **kw: ("healthy", "running", "healthy")
    with contextlib.redirect_stdout(io.StringIO()):
        chk._update_compose("app", "img:latest", "stack", "app",
                            compose_file, work)
    for args, wait in daemon.calls:
        if "up" in args:
            # The grace may be absent — that is one of the ways this is
            # allowed to fail, and it has to come back as a red check
            # rather than a TypeError three lines down. A procedure that
            # crashes under a mutation reports nothing, and the ratchet
            # reads "did not finish" as caught when it is not.
            if "--timeout" not in args:
                return None, wait
            return int(args[args.index("--timeout") + 1]), wait
    return None, None


_grace_60, _wait_60 = _recreate(60)
_grace_300, _wait_300 = _recreate(300)

_have_both = None not in (_grace_60, _grace_300, _wait_60, _wait_300)

checks["the recreate grants the configured stop grace"] = (
    _grace_60 == 60 and _grace_300 == 300)
# The property, not the number: whatever grace is granted, what is left
# for pull, create and start is what the path had before the grace
# existed.
checks["…and still leaves the working budget it had before"] = (
    _have_both
    and _wait_60 - _grace_60 >= 110
    and _wait_300 - _grace_300 >= 110)
# …and the wait really moves with the grace, so neither check above can
# be satisfied by a constant.
checks["…and the wait grows with the grace, not independently of it"] = (
    _have_both and _wait_300 - _wait_60 == _grace_300 - _grace_60)
if _have_both:
    print(f"      grace {_grace_60}s → wait {_wait_60}s "
          f"(budget {_wait_60 - _grace_60}s); "
          f"grace {_grace_300}s → wait {_wait_300}s "
          f"(budget {_wait_300 - _grace_300}s)")
else:
    print(f"      grace={_grace_60}/{_grace_300} wait={_wait_60}/{_wait_300}"
          f" — the recreate did not grant a grace at all")

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
