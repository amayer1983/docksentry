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

The second half is the same mistake wearing different clothes: twelve
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


def _fixed_names(src):
    """`(lineno, flag, value)` for every constant identifier created."""
    out = []
    tree = ast.parse(src)
    for elts in _argv_sequences(tree):
        texts = [e.value if isinstance(e, ast.Constant)
                 and isinstance(e.value, str) else None for e in elts]
        if not any(t in MAKING for t in texts if t):
            continue
        for i, t in enumerate(texts):
            if t in NAMING and i + 1 < len(elts):
                nxt = elts[i + 1]
                if isinstance(nxt, ast.Constant) and isinstance(nxt.value, str):
                    out.append((nxt.lineno, t, nxt.value))
    return out


def _unchecked_runs(src):
    """Call sites that start a container and throw the result away.

    An expression statement: nothing is bound, nothing is tested, so a
    refusal is invisible.
    """
    out = []
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if not isinstance(n, ast.Expr) or not isinstance(n.value, ast.Call):
            continue
        call = n.value
        # `lf.start` is the sanctioned way: it reads the return code and
        # turns a refusal into a skip, so its result being unbound is
        # the point rather than the defect.
        if (isinstance(call.func, ast.Attribute)
                and call.func.attr == "start"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id in ("lf", "livefixture")):
            continue
        argv = [a for a in call.args
                if isinstance(a, (ast.List, ast.Tuple))]
        for seq in argv:
            texts = [e.value for e in seq.elts
                     if isinstance(e, ast.Constant)
                     and isinstance(e.value, str)]
            if "run" in texts and ("-d" in texts or "--rm" in texts):
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

checks["no procedure creates a daemon object under a fixed name"] = (
    not _offenders)
for _f, _items in _offenders.items():
    for _ln, _flag, _val in _items:
        print(f"      → {_f}:{_ln} creates {_flag} {_val!r} — use "
              f"livefixture.name()")

checks["no procedure starts a container and ignores the result"] = (
    not _unchecked)
for _f, _lines in _unchecked.items():
    print(f"      → {_f}:{_lines} starts a container and discards the "
          f"result — a failed pull then reads as a broken promise")

# ── and the scanner is not looking at an empty room ──────────────────
# Both checks above pass trivially if the walk finds nothing. These
# three say it is really reading the files.
_all = list(_procedures())
checks["the scanner read the whole suite"] = len(_all) > 150
_probe = '''
import subprocess
subprocess.run(["docker", "run", "-d", "--name", "ds_fixed", "alpine"])
'''
checks["…and a fixed name would be caught"] = (
    _fixed_names(_probe) == [(3, "--name", "ds_fixed")])
checks["…and a discarded result would be caught"] = (
    _unchecked_runs(_probe) == [3])
# The counter-direction: the shape the suite uses now must come back
# clean, or this procedure would be unsatisfiable rather than passing.
_good = '''
import livefixture as lf
lf.start(["run", "-d", "--name", lf.name("ds_x"), "alpine"])
'''
checks["…while a per-run name through lf.start is not"] = (
    _fixed_names(_good) == [] and _unchecked_runs(_good) == [])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
