#!/usr/bin/env python3
"""A damaged settings.json must not be able to keep the instance down.

Found by the audit of 18.10.2026. `_load_persistent` caught the parse
error and carried on — but `saved` was only ever bound *inside* the
`try`, and the last line of the method passes it to
`_migrate_web_password`. So the handled error turned into an unhandled
one, during `__init__`:

    UnboundLocalError: cannot access local variable 'saved'
       config.py:1121 in _load_persistent

The process died at startup, the restart read the same file and died
again. Without filesystem access there was no way out of that, and the
file that caused it can come from the very race this release also
fixes — `container_store` had no locks at all.

Three things have to hold at once, and they pull against each other,
which is why each is driven separately below:

* the process starts,
* the damaged file is still there afterwards to be recovered from,
* and somebody is told, because starting from environment defaults
  looks exactly like a healthy instance until the first alert goes
  nowhere.

The config object is built with `__new__` and the three collaborators
the method calls are stood in for — those touch the filesystem and the
password store and are not what is being measured. `_load_persistent`
itself is the real one; that is the decision under test.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import config as config_mod  # noqa: E402

checks = {}
root = tempfile.mkdtemp(prefix="ds-cfg-")


def _load(content):
    """Run the real `_load_persistent` over a file holding `content`.

    Returns (raised, stdout, directory). `content=None` means no file.
    """
    d = tempfile.mkdtemp(dir=root)
    path = os.path.join(d, "settings.json")
    if content is not None:
        with open(path, "w") as f:
            f.write(content)

    c = config_mod.Config.__new__(config_mod.Config)
    c.settings_file = path
    c._loaded_keys = set()
    c.language = "en"
    # Environment, not decision: these two write to disk and to the
    # password store. `_load_persistent` is left real.
    c._restrict_settings_perms = lambda: None
    c._migrate_web_password = lambda saved: None

    out = io.StringIO()
    raised = None
    try:
        with contextlib.redirect_stdout(out):
            c._load_persistent()
    except Exception as e:
        raised = e
    return raised, out.getvalue(), d, c


DAMAGE = {
    "truncated": '{"language": "de"',
    "empty": "",
    "garbage": "not json at all",
    "a list": '["de"]',
    "a bare string": '"de"',
    "null": "null",
}

for label, content in DAMAGE.items():
    raised, out, d, _c = _load(content)

    checks[f"{label}: the process still starts"] = raised is None
    # The old failure was a specific one and deserves naming: a check
    # for "no exception" passes for the wrong reason if the method
    # starts swallowing everything.
    checks[f"{label}: …and not with UnboundLocalError"] = not isinstance(
        raised, UnboundLocalError)

    left = sorted(os.listdir(d))
    kept = [f for f in left if ".corrupt-" in f]
    checks[f"{label}: the damaged file is kept, not deleted"] = len(kept) == 1
    checks[f"{label}: …and moved out of the way"] = "settings.json" not in left
    if kept:
        with open(os.path.join(d, kept[0])) as f:
            checks[f"{label}: …with its contents intact"] = f.read() == content
    else:
        checks[f"{label}: …with its contents intact"] = False
    checks[f"{label}: the operator is told"] = "ERROR" in out and (
        "damaged" in out)

# ── the counter-directions ───────────────────────────────────────────
# Without these, every check above is satisfied by a method that
# quarantines unconditionally and ignores the file.

raised, out, d, c = _load('{"language": "de"}')
checks["a healthy file is read, not quarantined"] = (
    raised is None and os.listdir(d) == ["settings.json"])
checks["…and its values are applied"] = c.language == "de"
checks["…quietly"] = "ERROR" not in out

raised, out, d, c = _load(None)
checks["a missing file is not damage"] = (
    raised is None and os.listdir(d) == [] and "ERROR" not in out)

# An empty JSON object is a legitimate file, not damage — a fresh
# install writes one.
raised, out, d, c = _load("{}")
checks["an empty object is a legitimate file"] = (
    raised is None and os.listdir(d) == ["settings.json"]
    and "ERROR" not in out)

# ── repeated failures must not overwrite each other ──────────────────
# The quarantine name carries a timestamp, and the timestamp has
# second resolution. A crash loop quarantines faster than that. Written
# without the counter first, and this is what caught it: three
# quarantines inside one second left one file.

d = tempfile.mkdtemp(dir=root)
path = os.path.join(d, "settings.json")
c = config_mod.Config.__new__(config_mod.Config)
c.settings_file = path
c._loaded_keys = set()
c.language = "en"
c._restrict_settings_perms = lambda: None
c._migrate_web_password = lambda saved: None
_loop_raised = None
for i in range(3):
    with open(path, "w") as f:
        f.write(f'{{"attempt": {i}')
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            c._load_persistent()
    except Exception as e:
        # Caught, not allowed to escape. A procedure that dies with a
        # traceback produces no verdict at all, and the mutation sweep
        # counts "crashed" as "did not notice" — on purpose, because a
        # crash is not a caught mutation. So the failure is recorded
        # as a failed check instead.
        _loop_raised = e
        break
_kept = sorted(f for f in os.listdir(d) if ".corrupt-" in f)
checks["repeated damage does not raise"] = _loop_raised is None
checks["three failures in one second keep three files"] = len(_kept) == 3
_bodies = set()
for f in _kept:
    with open(os.path.join(d, f)) as fh:
        _bodies.add(fh.read())
checks["…and all three are different"] = len(_bodies) == 3

# ── an unreadable file is NOT moved ──────────────────────────────────
# Different case, deliberately treated differently: a permission
# problem says nothing about the file's contents, and moving somebody's
# settings aside because of a transient read error would be the bug.

if os.geteuid() != 0:
    d = tempfile.mkdtemp(dir=root)
    path = os.path.join(d, "settings.json")
    with open(path, "w") as f:
        f.write('{"language": "de"}')
    os.chmod(path, 0o000)
    c = config_mod.Config.__new__(config_mod.Config)
    c.settings_file = path
    c._loaded_keys = set()
    c.language = "en"
    c._restrict_settings_perms = lambda: None
    c._migrate_web_password = lambda saved: None
    out = io.StringIO()
    raised = None
    try:
        with contextlib.redirect_stdout(out):
            c._load_persistent()
    except Exception as e:
        raised = e
    os.chmod(path, 0o600)
    checks["an unreadable file does not stop the start"] = raised is None
    checks["…and is left exactly where it is"] = (
        os.listdir(d) == ["settings.json"])
    checks["…and is still reported"] = "WARNING" in out.getvalue()
else:
    # Running as root defeats the permission bit, and a check that
    # cannot fail is worse than no check.
    print("  (übersprungen: als root sind Dateirechte wirkungslos)")

shutil.rmtree(root, ignore_errors=True)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
