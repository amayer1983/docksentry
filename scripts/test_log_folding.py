#!/usr/bin/env python3
"""A crash loop's log tail says its error once, not five times (#63).

The owner's `tika` update went from v26.04 to v4.0.0, crash-looped, and
was rolled back — correctly. But the report filled a phone screen with
one error repeated five times, because that is what a restart loop
writes: the same stack trace on every attempt.

Folded, the same diagnostic fits in three lines and leaves room for the
lines that differ, which are the ones worth reading.

Blocks, not lines. The repeat is `Error: …` / `Caused by: …` alternating,
so a line-by-line fold sees no two identical lines in a row and does
nothing at all — which is what the first version of this did.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker as U  # noqa: E402

checks = {}
fold = U._collapse_repeats

# ── the real case ────────────────────────────────────────────────────
TIKA = "\n".join([
    "Error: Could not find or load main class org.apache.tika.server.core.TikaServerCli",
    "Caused by: java.lang.ClassNotFoundException: org.apache.tika.server.core.TikaServerCli",
] * 5)
out = fold(TIKA)
checks["a repeated two-line trace is folded"] = out.count("Caused by") == 1
checks["…and says how many times it repeated"] = "5×" in out
checks["…keeping the error itself intact"] = "TikaServerCli" in out
checks["…and it is much shorter"] = len(out) < len(TIKA) / 2

# ── what must NOT be touched ─────────────────────────────────────────
plain = "line one\nline two\nline three\nline four\nline five"
checks["a log with nothing repeating is left alone"] = fold(plain) == plain
checks["a short log is left alone"] = fold("a\nb") == "a\nb"
checks["empty stays empty"] = fold("") == ""

# Twice is not a loop — a service that logs a warning on start and again
# on reload has not crash-looped, and folding two occurrences hides more
# than it saves.
twice = "boot\nwarn\nboot\nwarn"
checks["two occurrences are not folded"] = fold(twice) == twice

# The lines before the loop survive: they are usually the cause.
with_head = "starting up\nreading config\n" + "\n".join(["crash", "trace"] * 4)
out = fold(with_head)
checks["the lines before the loop are kept"] = (
    "starting up" in out and "reading config" in out)
checks["…and only the loop is folded"] = out.count("crash") == 1

# ── it is actually wired into the log tail ───────────────────────────
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "update_checker.py"), encoding="utf-8").read()
i = src.index("def _tail_logs")
body = src[i:src.index("\n    @staticmethod", i)]
checks["_tail_logs folds before it truncates"] = (
    "_collapse_repeats(text)" in body
    and body.index("_collapse_repeats") < body.index("text[-1500:]"))

# ── and an empty health reads as nothing, not as `health=` ───────────
# tika has no health probe, so every crash-loop message about it printed
# a bare `health=` — which reads like a broken template, not like "there
# is nothing here".
note = U._state_note
checks["a container without a health probe reports only its state"] = (
    note("restarting", "") == "state=restarting")
checks["…and one with a probe reports both"] = (
    note("running", "unhealthy") == "state=running, health=unhealthy")
# All four message sites go through it — the first fix patched one of
# them and left three printing `health=`.
# Five sites: compose-crashloop, compose-unhealthy, rollback-crashloop,
# rollback-unhealthy, and the --rm container that has nothing to roll
# back to. The first fix patched one of them and left four printing
# `health=` — counting beats reading here.
user_msgs = [ln for ln in src.split("\n")
             if "health={health}" in ln and "_debug(" not in ln
             and "if health else" not in ln]   # the helper itself
checks["no user-facing message interpolates health raw"] = user_msgs == []
checks["every crash/health message uses the helper"] = (
    src.count("self._state_note(state, health)") == 5)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
