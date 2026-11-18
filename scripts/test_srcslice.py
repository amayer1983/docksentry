#!/usr/bin/env python3
"""A source slice whose anchor is gone reads as a red check, not a crash.

`srcslice.py` is what the source-reading procedures cut app/ code with
while the core split moves that code around. Its whole value is the
missing-anchor path, and on an untouched tree no procedure ever takes
that path, so nothing else in the suite would notice if it started
raising again, returned an empty string, or stopped saying why. This
drives each of those directly.

Every call runs inside `_try`, so a helper that raises shows up here as
a FAIL with the exception named. A crash would hide every check below
it, and `test_guard_mutations.py` cannot count a crash as caught.
"""

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import srcslice  # noqa: E402

checks = {}


def _try(fn, *args, **kw):
    """(result, printed, exception) of one call."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            return fn(*args, **kw), out.getvalue(), None
    except Exception as e:  # noqa: BLE001 — a raise is the thing measured
        return None, out.getvalue(), e


SRC = ("class A:\n"
       "    def one(self):\n"
       "        return 1\n"
       "\n"
       "    def two(self):\n"
       "        return 2\n")

# ── the anchors are there: the cut is the one asked for ──────────────
got, said, err = _try(srcslice.section, SRC, "    def one", "\n    def ",
                      label="one")
checks["a present start and end cut out exactly that piece"] = (
    err is None
    and got == "    def one(self):\n        return 1\n")
checks["…and say nothing while doing it"] = said == ""

# An end anchor that is also how the start begins must not cut to
# nothing: it is looked for behind the whole start.
got, _, err = _try(srcslice.section, SRC, "\n    def one", "\n    def ",
                   label="one")
checks["an end that matches the start's own text is looked for after it"] = (
    err is None
    and got == "\n    def one(self):\n        return 1\n")

got, _, err = _try(srcslice.section, SRC, "    def two", label="two")
checks["no end runs to the end of the source"] = (
    err is None and got == "    def two(self):\n        return 2\n")
got, _, err = _try(srcslice.section, SRC, "    def two", length=11,
                   label="two")
checks["…or for the given length"] = err is None and got == "    def two"

# ── the start anchor is gone ─────────────────────────────────────────
got, said, err = _try(srcslice.section, SRC, "def three", "\n    def ",
                      label="A.three")
checks["a missing start gives None, not a ValueError"] = (
    err is None and got is None)
# '' would make every `"x" not in block` pass on a branch that is gone.
checks["…and not an empty string"] = got != ""
checks["…and says so with a FAIL line naming it"] = (
    "  FAIL  A.three: anchor not found" in said.split("\n"))
checks["…and what it was looking for"] = "def three" in said

# ── the end anchor is gone ───────────────────────────────────────────
got, said, err = _try(srcslice.section, SRC, "    def two", "\n    def ",
                      label="A.two")
checks["a missing end gives None, not the rest of the file"] = (
    err is None and got is None)
checks["…and says so too"] = "  FAIL  A.two: anchor not found" in said

# ── position ─────────────────────────────────────────────────────────
got, _, err = _try(srcslice.position, SRC, "def two", label="p")
checks["position finds the first occurrence"] = (
    err is None and got == SRC.index("def two"))
got, said, err = _try(srcslice.position, SRC, "def three", label="p")
checks["…and a missing one is None with a FAIL line"] = (
    err is None and got is None and "FAIL  p: anchor not found" in said)
got, _, err = _try(srcslice.position, None, "x", label="p")
checks["…also when the text itself is already None"] = (
    err is None and got is None)


# ── methods ──────────────────────────────────────────────────────────
class B:
    def here(self):
        return "here"


got, _, err = _try(srcslice.method_source, B, "here")
checks["method_source reads a method that is there"] = (
    err is None and got is not None and 'return "here"' in got)
got, said, err = _try(srcslice.method_source, B, "gone")
checks["…and a method that is gone is None, not an AttributeError"] = (
    err is None and got is None)
checks["…named in its FAIL line"] = "FAIL  B.gone: anchor not found" in said
# `B.__module__` is a string: there is no source to read.
got, said, err = _try(srcslice.method_source, B, "__module__")
checks["…and something with no source is None too"] = (
    err is None and got is None and "FAIL" in said)

got, _, err = _try(srcslice.attr, B(), "here")
checks["attr hands back what is there"] = (
    err is None and callable(got) and got() == "here")
got, said, err = _try(srcslice.attr, B(), "gone", label="B.gone")
checks["…and None with a FAIL line for what is not"] = (
    err is None and got is None and "FAIL  B.gone: anchor not found" in said)

# ── order ────────────────────────────────────────────────────────────
checks["in_order holds for needles in order"] = srcslice.in_order(
    SRC, "def one", "def two")
checks["…not for needles out of order"] = not srcslice.in_order(
    SRC, "def two", "def one")
got, _, err = _try(srcslice.in_order, SRC, "def one", "def three")
checks["…and a missing needle is False, not a ValueError"] = (
    err is None and got is False)
# Missing in front, where find's -1 would sort "before" everything.
got, _, err = _try(srcslice.in_order, SRC, "def three", "def one")
checks["…also when the missing one is the first"] = (
    err is None and got is False)
got, _, err = _try(srcslice.in_order, None, "def one")
checks["…as is a slice that was never found"] = err is None and got is False

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
