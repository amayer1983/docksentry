#!/usr/bin/env python3
"""The debug trace goes to Telegram only when a check actually failed (#63).

With debug on, `check_all` used to send its ENTIRE registry HTTP trace to
Telegram in code blocks — on every check, for every container. On the
owner's own instance, 21 containers deep with debug left on, a routine
`/check` came back as pages of code blocks. The full trace is always in
the console (`docker logs`) and the Web UI Logs page, so Telegram was
carrying a copy nobody asked for on the happy path.

The trace is genuinely useful for ONE thing: diagnosing why a specific
container's check failed (a registry 404, an auth challenge, a network
timeout). So it now fires only when `failed_checks` is non-empty. A
local-only image with no registry is a clean "Skipped (no local digest)",
not a failure — measured on the owner's instance: a normal check has zero
failures and therefore sends zero debug blocks.

Source-level guard (the dump lives deep inside `check_all`, which needs a
live registry to exercise): the condition must gate on `failed_checks`,
and must not be the old unconditional `debug and bot and debug_log`.
"""
import os
import re
import sys

src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "update_checker.py"), encoding="utf-8").read()

checks = {}

# The one place debug_log is handed to the bot.
i = src.index("bot.send_message(f\"```\\n{chunk}\\n```\")")
# The guarding `if` is the nearest one above it.
guard = src.rindex("if self.config.debug", 0, i)
cond = src[guard:src.index(":", guard)]

checks["the Telegram debug dump is gated on a real failure"] = (
    "failed_checks" in cond)
checks["…and is not the old unconditional dump"] = (
    cond.strip() != "if self.config.debug and bot and self.debug_log")
checks["…it still requires debug on and a bot to send through"] = (
    "self.config.debug" in cond and "bot" in cond)

# `failed_checks` must be in scope where the dump runs — same function.
fn_start = src.rindex("\n    def ", 0, guard)
fn_head = src[fn_start:fn_start + 60]
checks["the dump sits inside check_all, where failed_checks lives"] = (
    "def check_all" in fn_head)
checks["…and failed_checks is populated on a failed container check"] = (
    "failed_checks.add(" in src[fn_start:i])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
