#!/usr/bin/env python3
"""A timeout says how long we waited, not what we typed.

`subprocess.TimeoutExpired` stringifies as the whole command line:

    Command ''docker', '-H', 'tcp://10.10.10.20:2375', 'ps', '--format',
    '{{.Names}}|{{.Image}}'' timed out after 30 seconds

Technically true and useless to read — the argv is ours, the reader knows
what Docksentry runs, and the doubled quotes are Python printing a list
inside a string. Everything else the CLI says is still the most useful
thing available and goes through untouched; we only refuse to quote
Python at somebody.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import errfmt  # noqa: E402
import i18n  # noqa: E402

checks = {}

TIMEOUT = subprocess.TimeoutExpired(
    ["docker", "-H", "tcp://10.10.10.20:2375", "ps",
     "--format", "{{.Names}}|{{.Image}}"], 30)

line = errfmt.human(TIMEOUT)
checks["a timeout says how long we waited"] = "30" in line
checks["…and does not print the command back at the reader"] = (
    "docker" not in line and "--format" not in line)
checks["…nor the quoting Python produces for a list"] = "''" not in line

checks["a whole-number timeout has no decimal tail"] = (
    "30.0" not in errfmt.human(subprocess.TimeoutExpired(["x"], 30.0)))
checks["…but a real fraction keeps it"] = (
    "7.5" in errfmt.human(subprocess.TimeoutExpired(["x"], 7.5)))
# `rstrip(".0")` strips every trailing 0 and '.', so "30" became "3".
checks["trimming the tail does not eat the number"] = (
    "30" in errfmt.human(subprocess.TimeoutExpired(["x"], 30)))

checks["anything else is passed through"] = (
    errfmt.human(OSError("connection refused")) == "connection refused")
checks["…and still clipped when it is long"] = (
    len(errfmt.human(OSError("x" * 900))) < 900)

for code in sorted(f[:-5] for f in os.listdir(
        os.path.join(os.path.dirname(__file__), "..", "app", "lang"))
        if f.endswith(".json")):
    t = i18n.get_translator(code)
    out = errfmt.human(TIMEOUT, t)
    checks[f"{code} words it in its own language"] = ("30" in out
                                                      and "docker" not in out)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
