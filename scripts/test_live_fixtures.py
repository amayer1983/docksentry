#!/usr/bin/env python3
"""No procedure may own a name on the daemon that another run also owns.

Nine procedures drive a real daemon, and until 11.10.2026 every
identifier they created was a constant: `ds_ctf-a-even`, `ds_netns_head`,
`ds_protect_test`, `ds-selftest-pod`, the image tags, and the two ports
12375 and 8791. Each of those files begins by force-removing its names,
so two runs at the same time destroy each other and the loser reports a
defect that is not there.

Measured rather than argued. With a foreign container called
`ds_ctf-a-even` running, the old `test_glob.py` removed it:

    docker run -d --name ds_ctf-a-even alpine sleep 300
    python3 scripts/test_glob.py
    docker inspect ds_ctf-a-even   → Error: No such object

After the change the same container is still there afterwards. That is
the whole of the argument for `livefixture.TAG`.

The second half is the same mistake wearing different clothes: fourteen
`docker run` call sites discarded the return code. On a machine with no
route to a registry the container is simply absent, and the procedure
then reports globbing, stop protection or dependent recreation as
broken — a red pointing at code that is fine, in the one place a reader
has no reason to doubt the measurement. `livefixture.start` makes the
daemon's refusal say so and skips, keeping the verdicts already reached.

So this is the guard that keeps both from coming back. Read as a tree,
because a grep for `--name` is satisfied by a comment and a budget of
"at most N constants" passes with the worst one left in.
"""

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: Arguments after which a *constant* is an identifier on the daemon.
NAMING = ("--name", "--pod", "-t", "--tag")

#: The verbs that make something on a daemon rather than reading it.
MAKING = ("run", "create", "build", "pull", "rename", "tag", "commit")

checks = {}


def _procedures():
    for f in sorted(os.listdir(HERE)):
        if f.startswith("test_") and f.endswith(".py"):
            yield f, open(os.path.join(HERE, f), encoding="utf-8").read()


def _argv_sequences(tree):
    """Every list/tuple literal in the file, as its element nodes."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.List, ast.Tuple)):
            yield n.elts


def _per_run_names(tree):
    """Module-level names whose value derives from `livefixture`.

    `lf.name(...)`, `lf.free_port()`, anything built from `lf.TAG` — and
    transitively, so `STEM = f"ds_ctf{lf.TAG}"` followed by
    `NAMES = [f"{STEM}-a-even"]` counts as per-run.

    This is the half the first version of this file was missing
    entirely. It looked only at bare string literals sitting next to
    `--name`, so against the pre-change `test_glob.py` it flagged two
    of the fifteen fixed names `OPEN.md` enumerates: the other thirteen
    reached argv through a module constant or an f-string and were
    invisible. An audit reverted the file's own headline example and
    the guard stayed green.
    """
    safe = set()
    for _ in range(4):          # transitive closure, a few rounds is plenty
        before = len(safe)
        for n in ast.walk(tree):
            if not isinstance(n, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (n.targets if isinstance(n, ast.Assign)
                       else [n.target])
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            for t in targets:
                if isinstance(t, (ast.Tuple, ast.List)):
                    names += [e.id for e in t.elts
                              if isinstance(e, ast.Name)]
            if not names or n.value is None:
                continue
            if _derives_from_fixture(n.value, safe):
                safe.update(names)
        if len(safe) == before:
            break
    return safe


def _derives_from_fixture(node, safe):
    """Does this expression get its value from `livefixture`?"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                and sub.value.id in ("lf", "livefixture"):
            return True
        if isinstance(sub, ast.Name) and sub.id in safe:
            return True
    return False


def _fixed_module_names(tree, safe):
    """Module-level names bound to a literal, or to a list of literals.

    Only these are reported when they turn up next to a naming flag. A
    local, a loop variable or a function parameter is left alone — this
    is a shape scanner, not a dataflow analysis, and a false red is
    worse than a miss because it teaches people to ignore the guard.
    """
    fixed = set()
    for n in tree.body:
        if not isinstance(n, ast.Assign) or n.value is None:
            continue
        if _derives_from_fixture(n.value, safe):
            continue
        v = n.value
        literal = (isinstance(v, ast.Constant) and isinstance(v.value, str)) \
            or (isinstance(v, (ast.List, ast.Tuple))
                and v.elts
                and all(isinstance(e, ast.Constant)
                        and isinstance(e.value, str) for e in v.elts)) \
            or isinstance(v, ast.JoinedStr)
        if not literal:
            continue
        for tgt in n.targets:
            if isinstance(tgt, ast.Name):
                fixed.add(tgt.id)
            elif isinstance(tgt, (ast.Tuple, ast.List)):
                fixed.update(e.id for e in tgt.elts
                             if isinstance(e, ast.Name))
    return fixed


def _fixed_names(src):
    """`(lineno, flag, value)` for every identifier that is not per-run.

    A value counts as per-run when it derives from `livefixture` —
    directly, through a module constant, or inside an f-string. Anything
    else next to a naming flag is a collision waiting for a second run.
    """
    out = []
    tree = ast.parse(src)
    safe = _per_run_names(tree)
    fixed = _fixed_module_names(tree, safe)
    for elts in _argv_sequences(tree):
        texts = [e.value if isinstance(e, ast.Constant)
                 and isinstance(e.value, str) else None for e in elts]
        if not any(t in MAKING for t in texts if t):
            continue
        for i, t in enumerate(texts):
            if t not in NAMING or i + 1 >= len(elts):
                continue
            nxt = elts[i + 1]
            if _derives_from_fixture(nxt, safe):
                continue
            if isinstance(nxt, ast.Constant) and isinstance(nxt.value, str):
                out.append((nxt.lineno, t, nxt.value))
            elif (isinstance(nxt, ast.Subscript)
                  and isinstance(nxt.value, ast.Name)
                  and nxt.value.id in fixed):
                # `NAMES[0]`, which is how a list of module constants
                # reaches argv.
                out.append((nxt.lineno, t,
                            f"{nxt.value.id}[…] (module constant)"))
            elif isinstance(nxt, ast.Name) and nxt.id in fixed:
                # A module constant holding a literal — which is how
                # thirteen of the fifteen names this guard exists for
                # actually reached argv.
                out.append((nxt.lineno, t, f"{nxt.id} (module constant)"))
            elif isinstance(nxt, ast.JoinedStr) and not any(
                    isinstance(sub, ast.Name) and sub.id not in fixed
                    for sub in ast.walk(nxt)):
                # An f-string built only from literals and module
                # constants. One whose pieces come from somewhere this
                # scanner cannot follow is left alone: a false red is
                # worse than a miss, because it trains people to
                # ignore the guard.
                out.append((nxt.lineno, t, ast.unparse(nxt)))
    return out


def _fixed_ports(src):
    """`(lineno, name)` for a port bound from a constant.

    `livefixture.py` names 12375 and 8791 as part of what `free_port()`
    fixes, and the first version of this guard could not see them at
    all: `NAMING` had no `-p`, and the two were module constants besides.
    Set either one back and the file stayed green.
    """
    out = []
    tree = ast.parse(src)
    safe = _per_run_names(tree)
    # A published port, in any of the shapes argv takes.
    for elts in _argv_sequences(tree):
        texts = [e.value if isinstance(e, ast.Constant)
                 and isinstance(e.value, str) else None for e in elts]
        if not any(t in MAKING for t in texts if t):
            continue
        for i, t in enumerate(texts):
            if t in ("-p", "--publish") and i + 1 < len(elts):
                nxt = elts[i + 1]
                if not _derives_from_fixture(nxt, safe):
                    out.append((nxt.lineno, "published port"))
    # …and a port a test binds itself, which is how the podman procedure
    # reaches its service: `PORT = 8791` with no `-p` anywhere.
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign):
            continue
        for tgt in n.targets:
            if (isinstance(tgt, ast.Name) and "PORT" in tgt.id.upper()
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, int)):
                out.append((n.lineno, tgt.id))
    return out


def _unchecked_runs(src):
    """Call sites that start a container and throw the result away.

    Two shapes, and the second was missing: an expression statement
    where nothing is bound, and an assignment to a name the file never
    reads again — which launders exactly the same defect past a check
    that only looked for the first.
    """
    out = []
    tree = ast.parse(src)
    reads = [n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)]

    def _starts_container(call):
        if (isinstance(call.func, ast.Attribute)
                and call.func.attr == "start"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in ("lf", "livefixture")):
            # The sanctioned way: it reads the return code itself and
            # turns a refusal into a skip.
            return False
        for seq in call.args:
            if not isinstance(seq, (ast.List, ast.Tuple)):
                continue
            texts = [e.value for e in seq.elts
                     if isinstance(e, ast.Constant)
                     and isinstance(e.value, str)]
            if "run" in texts and any(
                    f in texts for f in ("-d", "--detach", "--rm")):
                return True
        return False

    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            if _starts_container(n.value):
                out.append(n.lineno)
        elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            if not _starts_container(n.value):
                continue
            bound = [t.id for t in n.targets if isinstance(t, ast.Name)]
            # Bound and never read again is the same as not bound at
            # all, and reads better to a scanner that only checks shape.
            if bound and not any(b in reads for b in bound):
                out.append(n.lineno)
    return out


#: Files allowed a constant: they never reach a daemon, or the constant
#: is namespaced inside something ephemeral. Each one is named, with the
#: reason, because "a few are fine" without a list is how all of them
#: come back.
ALLOWED = {
    # The name lives inside a throwaway dind that this procedure itself
    # created on a per-run port, so it cannot collide with anything.
    "test_remote_transports.py": {"probe"},
    # Writes a fake `docker` onto PATH; nothing reaches a daemon.
    "test_selfupdate_recovery.py": set(),
    # `-t` here is `docker stop -t 3`, a timeout, not a tag.
    "test_podman_live.py": set(),
}

_offenders = {}
_unchecked = {}
_ports = {}
for _file, _src in _procedures():
    if _file == os.path.basename(__file__):
        continue
    _fixed = [(ln, flag, val) for ln, flag, val in _fixed_names(_src)
              if val not in ALLOWED.get(_file, set())
              # A timeout, not a tag: `stop -t 3`, `pull --retry-delay`.
              and not (flag == "-t" and val.isdigit())]
    if _fixed:
        _offenders[_file] = _fixed
    _unchecked_here = _unchecked_runs(_src)
    if _unchecked_here:
        _unchecked[_file] = _unchecked_here
    _ports_here = _fixed_ports(_src)
    if _ports_here:
        _ports[_file] = _ports_here

checks["no procedure creates a daemon object under a fixed name"] = (
    not _offenders)
for _f, _items in _offenders.items():
    for _ln, _flag, _val in _items:
        print(f"      → {_f}:{_ln} creates {_flag} {_val!r} — use "
              f"livefixture.name()")

checks["no procedure binds a daemon port from a constant"] = not _ports
for _f, _items in _ports.items():
    for _ln, _what in _items:
        print(f"      → {_f}:{_ln} fixes {_what} — use livefixture.free_port()")

checks["no procedure starts a container and ignores the result"] = (
    not _unchecked)
for _f, _lines in _unchecked.items():
    print(f"      → {_f}:{_lines} starts a container and discards the "
          f"result — a failed pull then reads as a broken promise")

# ── and the scanner finds the shapes it is for ───────────────────────
# Both checks above pass trivially on a scanner that sees nothing, and
# the first version of this file very nearly was one: it caught an
# inline literal and nothing else, so against the pre-change
# `test_glob.py` it found two of fifteen names, saw neither fixed port,
# and a `_p = subprocess.run(...)` walked straight past it. Each shape
# below is one an audit got through on 11.10.2026.
_all = list(_procedures())
checks["the scanner read the whole suite"] = len(_all) > 150

#: (what it is, source, what must be found)
PROBES = [
    ("an inline literal", '''
subprocess.run(["docker", "run", "-d", "--name", "ds_fixed", "alpine"])
''', "names"),
    ("a module constant", '''
NAME = "ds_fixed"
subprocess.run(["docker", "run", "-d", "--name", NAME, "alpine"])
''', "names"),
    ("a list of module constants", '''
NAMES = ["ds_ctf-a-even", "ds_ctf-b-even"]
for n in NAMES:
    subprocess.run(["docker", "run", "-d", "--name", NAMES[0], "alpine"])
''', "names"),
    ("an f-string over a fixed stem", '''
STEM = "ds_ctf"
subprocess.run(["docker", "run", "-d", "--name", f"{STEM}-a-even", "alpine"])
''', "names"),
    ("a published port", '''
subprocess.run(["docker", "run", "-d", "-p", "127.0.0.1:12375:2375", "x"])
''', "ports"),
    ("a port a test binds itself", '''
PORT = 8791
''', "ports"),
    ("a result bound and never read", '''
_p = subprocess.run(["docker", "run", "-d", "--name", "x", "alpine"])
''', "runs"),
    ("a --detach spelling", '''
subprocess.run(["docker", "run", "--detach", "--name", "x", "alpine"])
''', "runs"),
]
_missed = []
for _what, _src, _kind in PROBES:
    _found = {"names": _fixed_names, "ports": _fixed_ports,
              "runs": _unchecked_runs}[_kind](_src)
    if not _found:
        _missed.append(_what)
checks["every shape the audit got through is caught now"] = not _missed
if _missed:
    print(f"      → still invisible: {_missed}")

# The counter-direction, and it is what stops the scanner from being
# tightened into something that flags everything: the shapes the suite
# uses now must come back clean.
CLEAN = [
    ("a per-run name through lf.start", '''
import livefixture as lf
lf.start(["run", "-d", "--name", lf.name("ds_x"), "alpine"])
'''),
    ("a per-run stem in an f-string", '''
import livefixture as lf
STEM = f"ds_ctf{lf.TAG}"
NAMES = [f"{STEM}-a-even"]
lf.start(["run", "-d", "--name", NAMES[0], "alpine"])
'''),
    ("a free port", '''
import livefixture as lf
PORT = lf.free_port()
lf.start(["run", "-d", "-p", f"127.0.0.1:{PORT}:2375", "x"])
'''),
    ("a result that is read", '''
import livefixture as lf
r = subprocess.run(["docker", "run", "-d", "--name", lf.name("x"), "alpine"])
if r.returncode != 0:
    raise SystemExit(0)
'''),
    ("a loop variable the scanner cannot follow", '''
import livefixture as lf
for n in [lf.name("a"), lf.name("b")]:
    lf.start(["run", "-d", "--name", n, "alpine"])
'''),
]
_false = [w for w, src in CLEAN
          if _fixed_names(src) or _fixed_ports(src) or _unchecked_runs(src)]
checks["…and the shapes the suite uses are not flagged"] = not _false
if _false:
    print(f"      → false positives: {_false}")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
