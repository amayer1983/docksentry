#!/usr/bin/env python3
"""Show what the health check said, not what the container said.

The owner's `ollama` was rolled back and reported like this:

    ❌ ollama: Health check failed (state=running, health=unhealthy)
       — rolled back
    Last logs:
      … msg="Listening on [::]:11434 (version 0.32.14)"
      … msg="discovering available GPUs..."
      … msg="model list cache hydration complete" models=5 failures=0

Ten lines of a textbook-clean startup, and nothing in them to act on —
because they are the wrong lines. What failed was the **probe**, and a
probe's output does not go to the container's stdout. It goes to
`.State.Health.Log[].Output`, with the exit code of the command Docker
ran, and we were not looking there.

Read *before* the rollback, on purpose: `_rollback_to_old` restores the
previous container under the same name, so an inspect afterwards would
quote the old container's health log and present it as the reason the
new one failed.
"""

import json
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

checks = {}


def checker(answers):
    """`answers` maps the inspect format string to (rc, stdout)."""
    calls = []

    class Backend:
        def run(self, args, timeout=None):
            calls.append(args)
            fmt = args[2] if len(args) > 2 else ""
            rc, out = answers.get(fmt, (1, ""))
            return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(debug=False)
    c._backend = Backend()
    c._debug = lambda *a, **k: None
    c._trace = lambda *a, **k: None
    return c, calls


HEALTH = "{{json .State.Health}}"
LEGACY = "{{json .State.Healthcheck}}"

# ── the case that started it ─────────────────────────────────────────
c, _ = checker({HEALTH: (0, json.dumps({
    "Status": "unhealthy",
    "Log": [{"ExitCode": 1, "Output": "curl: (7) Failed to connect to "
                                      "localhost port 11434"},
            {"ExitCode": 1, "Output": "curl: (7) Failed to connect to "
                                      "localhost port 11434"}]}))})
out = c._health_output("ollama")
checks["the probe's own output is reported"] = "Failed to connect" in out
checks["…with the exit code of the command Docker ran"] = "exit 1" in out
checks["…and the most recent attempts, not the first ever"] = (
    out.count("exit 1") == 2)

# A probe that fails without printing anything still says something.
c, _ = checker({HEALTH: (0, json.dumps({
    "Status": "unhealthy", "Log": [{"ExitCode": 137, "Output": ""}]}))})
checks["a silent failure still names its exit code"] = (
    "exit 137" in c._health_output("x"))

# Long output is trimmed rather than pasted whole into a chat message.
c, _ = checker({HEALTH: (0, json.dumps({
    "Log": [{"ExitCode": 1, "Output": "x" * 5000}]}))})
checks["a torrent of output is trimmed"] = len(c._health_output("x")) < 400

# Multi-line output is flattened — a health probe that prints a stack
# trace should not turn one message into forty lines.
c, _ = checker({HEALTH: (0, json.dumps({
    "Log": [{"ExitCode": 2, "Output": "line one\nline two\n\nline three"}]}))})
checks["…and multi-line output is flattened"] = (
    "\n" not in c._health_output("x").split("exit 2: ")[1])

# ── nothing to say, so nothing is said ───────────────────────────────
c, _ = checker({})
checks["a container with no healthcheck says nothing"] = (
    c._health_output("x") == "")
c, _ = checker({HEALTH: (0, "null")})
checks["…and neither does a null health block"] = c._health_output("x") == ""
c, _ = checker({HEALTH: (0, "not json at all")})
checks["…nor output we cannot parse"] = c._health_output("x") == ""
c, _ = checker({HEALTH: (0, json.dumps({"Status": "healthy", "Log": [
    {"ExitCode": 0, "Output": ""}]}))})
checks["a passing probe adds no noise"] = c._health_output("x") == ""


class Exploding:
    def run(self, *a, **k):
        raise subprocess.SubprocessError("daemon gone")


c, _ = checker({})
c._backend = Exploding()
checks["a daemon that will not answer costs nothing"] = (
    c._health_output("x") == "")

# Podman spelled it differently on older versions; both are tried.
c, calls = checker({LEGACY: (0, json.dumps({
    "Log": [{"ExitCode": 1, "Output": "probe said no"}]}))})
checks["the older Podman spelling is tried too"] = (
    "probe said no" in c._health_output("x"))

# ── read before the rollback, or it quotes the wrong container ───────
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "update_checker.py"), encoding="utf-8").read()
lines = src.splitlines()
probe_at = [i for i, l in enumerate(lines) if "probe = self._health_output(" in l]
checks["both failure paths collect it"] = len(probe_at) == 2
rollback_at = [i for i, l in enumerate(lines)
               if "self._rollback_to_old(name, old_name)" in l]
after = [r for r in rollback_at if r > probe_at[-1]]
checks["…and the rollback path reads it first"] = bool(after) and (
    probe_at[-1] < after[0])
checks["the message labels it as the health check"] = (
    src.count('Health check said:') == 2)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
