#!/usr/bin/env python3
"""The mutation ratchet copies the program, not my local files, and
cleans up after itself.

`test_guard_mutations.py` breaks things in a copy of the tree. The copy
used to take everything but `.git` and caches, local notes, backups,
worktrees and the dev compose file with its real credentials included.
And it removed only `…/repo` afterwards, never the `guardmut-*`
directory around it. Counted in /tmp on 18.11.2026: 68 empty
`guardmut-*` directories and 8 full copies left by interrupted runs, 7
of them with the dev compose file inside.

Now the copy is what git counts as the program: tracked files plus new
ones no ignore rule covers. Driven through the real `main()` of the
ratchet, on a fake git checkout in a scratch directory, with `GUARDED`
and `UNGUARDED` emptied so only the copy, the control mutation and the
cleanup run. Both ignore mechanisms are tried, `.gitignore` and
`.git/info/exclude`. The counter-direction: a procedure written but not
committed yet has to arrive, or the ratchet would test the last commit
instead of the tree in front of it. The temp directory is a private
one, so "nothing is left" is measured, not assumed.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_guard_mutations as g  # noqa: E402

checks = {}

_scratch = tempfile.mkdtemp(prefix="ds-ratchet-copy-")
_root = os.path.join(_scratch, "root")
_tmp = os.path.join(_scratch, "tmp")
os.makedirs(_tmp)

_TRACKED = {
    # what the control mutation in main() needs to run
    "app/hostcheck.py": "def _gone(text):\n    return text\n",
    "scripts/test_hostcheck.py": "print('  ✅ fine')\nprint('PASS')\n",
    "docs/updates.md": "x\n",
    "docker-compose.yml": "services: {}\n",
    ".gitignore": "secret-compose.yml\nreports/\n",
    "app/gone.py": "x = 1\n",          # deleted from the work tree below
}
_NEW = {"scripts/test_not_committed_yet.py": "print('PASS')\n"}
_LOCAL = {
    "secret-compose.yml": "TELEGRAM_BOT_TOKEN: secret\n",
    "reports/r.txt": "x\n",
    "my-notes.md": "x\n",                # in .git/info/exclude
    "app/__pycache__/hostcheck.cpython-312.pyc": "x",
}
_FILES = {**_TRACKED, **_NEW, **_LOCAL}


def _write(rel, text):
    p = os.path.join(_root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def _git(*args):
    subprocess.run(["git", "-C", _root, *args], check=True,
                   capture_output=True)


os.makedirs(_root)
_git("init", "-q")
for rel, text in _TRACKED.items():
    _write(rel, text)
_git("add", "-A")
_git("-c", "user.name=t", "-c", "user.email=t@example.org",
     "commit", "-q", "-m", "fixture")
os.remove(os.path.join(_root, "app/gone.py"))
with open(os.path.join(_root, ".git/info/exclude"), "a",
          encoding="utf-8") as f:
    f.write("my-notes.md\n")
for rel, text in {**_NEW, **_LOCAL}.items():
    _write(rel, text)

_seen = {}
_real_red = g._red


def _red_and_look(tree, procedure, timeout=240):
    """The real `_red`, plus a note of what the copy holds right then."""
    if not _seen:
        _seen.update({rel: os.path.exists(os.path.join(tree, rel))
                      for rel in _FILES})
        _seen["<tree>"] = tree
    return _real_red(tree, procedure, timeout)


_saved = (g.ROOT, g.GUARDED, g.UNGUARDED, g._red, tempfile.tempdir)
g.ROOT, g.GUARDED, g.UNGUARDED, g._red = _root, [], [], _red_and_look
tempfile.tempdir = _tmp
_out = io.StringIO()
try:
    with contextlib.redirect_stdout(_out):
        _rc = g.main()
except Exception as e:                                    # noqa: BLE001
    _rc = None
    print(f"      → main() raised: {e!r}")
finally:
    g.ROOT, g.GUARDED, g.UNGUARDED, g._red, tempfile.tempdir = _saved

checks["the ratchet ran on the fake tree and came back green"] = _rc == 0
if _rc != 0:
    print("      → it said: " + " | ".join(_out.getvalue().splitlines()[-6:]))

for rel in ("app/hostcheck.py", "docs/updates.md", "docker-compose.yml",
            "scripts/test_not_committed_yet.py"):
    checks[f"the copy has {rel}"] = _seen.get(rel) is True
for rel in ("secret-compose.yml", "reports/r.txt", "my-notes.md",
            "app/__pycache__/hostcheck.cpython-312.pyc", "app/gone.py"):
    checks[f"…and not {rel}"] = _seen.get(rel) is False

_left = sorted(os.listdir(_tmp))
checks["nothing of the copy is left in the temp directory afterwards"] = (
    bool(_seen) and _left == [])
if _left:
    print(f"      → left behind: {_left}")

shutil.rmtree(_scratch, ignore_errors=True)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
