#!/usr/bin/env python3
"""A failing procedure has to leave something in the run log.

The audit run of 18.11.2026 logged one line for a red procedure,
"FAIL guard_mutations 0 checks", and nothing else. `verify.py` printed
the last lines of a procedure that failed without a check, and this one
had printed none, so whether it was killed or exited on its own could
not be told afterwards. Now the exit code, or the signal, is always
there when there is no check to name.

Driven through the real `run_one` and `collect`, on throwaway
procedures in a temporary directory: one killed by SIGKILL, one that
exits 3 without a word, one that prints a little and exits 1, and one
that passes. The last two are the counter-direction — the note must
come on top of what a procedure said, not instead of it, and a green
procedure gets none.

The second half is the sentence the runner writes at the top of
VERIFICATION.md. It said "The suite runs locally, not in CI" while
ci.yml ran this very file; it is held against the workflow files here,
so the two cannot drift apart again.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import verify  # noqa: E402

checks = {}

_d = tempfile.mkdtemp(prefix="ds-runner-")
_PROCS = {
    "test_killed.py": "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n",
    "test_silent_exit.py": "import sys\nsys.exit(3)\n",
    "test_said_a_bit.py": "print('loading the fixture')\nraise SystemExit(1)\n",
    "test_green.py": "print('  ✅ it works')\nprint('PASS')\n",
}
try:
    for _n, _src in _PROCS.items():
        with open(os.path.join(_d, _n), "w", encoding="utf-8") as _f:
            _f.write(_src)

    def _tail(name):
        try:
            return verify.run_one(os.path.join(_d, name))
        except Exception as e:                            # noqa: BLE001
            print(f"      → run_one raised on {name}: {e!r}")
            return ("crashed", [], False, False, "")

    st, ck, _sk, _dis, tail = _tail("test_killed.py")
    checks["a procedure killed by a signal is FAIL and names the signal"] = (
        st == "fail" and "SIGKILL" in tail)
    checks["…and says it printed nothing at all"] = "no output" in tail

    st, ck, _sk, _dis, tail = _tail("test_silent_exit.py")
    checks["one that exits 3 without a word shows exit code 3"] = (
        st == "fail" and "exit code 3" in tail)
    if "exit code 3" not in tail:
        print(f"      → tail was {tail!r}")

    st, ck, _sk, _dis, tail = _tail("test_said_a_bit.py")
    checks["what a procedure did print is kept, with the exit code after it"] = (
        st == "fail" and "loading the fixture" in tail
        and "exit code 1" in tail and "no output" not in tail)

    st, ck, _sk, _dis, tail = _tail("test_green.py")
    checks["a green procedure gets no note at all"] = (
        st == "pass" and tail == "" and len(ck) == 1)

    # And through `collect`, which is what writes the log: the note has
    # to reach the printed lines, not stop inside the return value.
    _saved = verify.SCRIPTS
    verify.SCRIPTS = _d
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            verify.collect()
    except Exception as e:                                # noqa: BLE001
        print(f"      → collect raised: {e!r}")
    finally:
        verify.SCRIPTS = _saved
    _log = buf.getvalue().splitlines()
    _after = lambda name: next(                           # noqa: E731
        (_log[i + 1] for i, l in enumerate(_log[:-1]) if f" {name} " in l
         and "FAIL" in l), "")
    checks["the run log prints the signal under the red line"] = (
        "SIGKILL" in _after("killed"))
    checks["…and the exit code under a silent one"] = (
        "exit code 3" in _after("silent_exit"))
    if "SIGKILL" not in _after("killed"):
        print(f"      → the log was: {_log}")
finally:
    shutil.rmtree(_d, ignore_errors=True)

# ── the sentence at the top of VERIFICATION.md ───────────────────────
_head = verify.render([])
_wf = {n: open(os.path.join(ROOT, ".github", "workflows", n),
               encoding="utf-8").read()
       for n in ("ci.yml", "docker-publish.yml")}
_ci_runs_suite = all("python3 scripts/verify.py" in t for t in _wf.values())
checks["both workflows run the suite (the premise of the report's header)"] = (
    _ci_runs_suite)
checks["…so the report does not say the suite runs only locally"] = (
    "not in CI" not in _head and "runs locally" not in _head)
checks["…and does say a tag on a red tree is not published"] = (
    "not published" in _head)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
