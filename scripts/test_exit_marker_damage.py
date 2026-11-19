#!/usr/bin/env python3
"""A damaged exit marker is set aside, so it cannot hide the next crash.

Seen on a tester's box on 19.11.2026 (#63): the boot log said
"Could not read exit cause (non-fatal): 'utf-8' codec can't decode byte
0x90 in position 86". The file this program writes is 82 bytes of ASCII,
so something else had written into it. The read failed, and the file
stayed where it was. That is the real defect: `previous_run_died` asks
whether the marker is ABSENT, so while a damaged one sits there, a run
that is later SIGKILLed counts as one that said goodbye, and the boot
after it reports nothing.

Driven through the real `recovery.consume_marker` and
`recovery.previous_run_died` on files in a scratch directory. The
counter-direction is a good marker: read, removed, nothing set aside.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import recovery  # noqa: E402

checks = {}
d = tempfile.mkdtemp(prefix="ds-exit-marker-")
cfg = types.SimpleNamespace(
    version_state_file=os.path.join(d, "version_state.json"),
    last_exit_file=os.path.join(d, "last_exit.json"))
open(cfg.version_state_file, "w", encoding="utf-8").write("{}")


def _clear():
    for p in (cfg.last_exit_file, cfg.last_exit_file + ".damaged"):
        if os.path.exists(p):
            os.remove(p)


def consume():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        r = recovery.consume_marker(cfg.last_exit_file, "exit cause")
    return r, out.getvalue()


# the tester's case: valid JSON start, a non-UTF-8 byte at position 86
good = json.dumps({"reason": "signal", "signal": "SIGTERM", "done": True})
with open(cfg.last_exit_file, "wb") as f:
    f.write(good.encode().ljust(86, b" ") + b"\x90garbage")
r, said = consume()
checks["a marker that cannot be decoded reads as no marker"] = r is None
checks["…and is no longer where the next boot looks"] = (
    not os.path.exists(cfg.last_exit_file))
checks["…but set aside, not deleted"] = os.path.exists(
    cfg.last_exit_file + ".damaged")
checks["…and the log says so"] = (
    "set aside as last_exit.json.damaged" in said and "0x90" in said)

# The point of it: the run after that is SIGKILLed — no marker written.
checks["a kill after the damaged marker is reported as one"] = (
    recovery.previous_run_died(cfg) is True)

# JSON that is not an object is just as unusable
_clear()
open(cfg.last_exit_file, "w", encoding="utf-8").write("[1, 2]")
r, said = consume()
checks["a marker that is not an object is set aside too"] = (
    r is None and not os.path.exists(cfg.last_exit_file)
    and os.path.exists(cfg.last_exit_file + ".damaged"))

# counter-direction: a good marker
_clear()
open(cfg.last_exit_file, "w", encoding="utf-8").write(good)
checks["a good marker means the last run said goodbye"] = (
    recovery.previous_run_died(cfg) is False)
r, said = consume()
checks["…is read"] = r == json.loads(good)
checks["…removed"] = not os.path.exists(cfg.last_exit_file)
checks["…with nothing set aside and nothing said"] = (
    not os.path.exists(cfg.last_exit_file + ".damaged") and said == "")
r, said = consume()
checks["no marker at all is quiet"] = r is None and said == ""

# and the boot reads both of its markers through it
_main = open(os.path.join(os.path.dirname(__file__), "..", "app", "main.py"),
             encoding="utf-8").read()
checks["the boot reads the exit marker through it"] = (
    'consume_marker(config.last_exit_file, "exit cause")' in _main)
checks["…and the self-update marker"] = (
    "consume_marker(config.selfupdate_marker_file," in _main)

shutil.rmtree(d, ignore_errors=True)
bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
