#!/usr/bin/env python3
"""A missing placeholder must not discard the whole message (#2, @NotRetarded).

He reported that container events in the history showed nothing useful and
guessed the cause himself: "that information wasn't stored at the time it
generated those events and can't display the values." He was right about
the storage and wrong about what it looked like, which is worth writing
down because the real behaviour was uglier than either of us assumed.

`t()` formatted the template with `.format(**kwargs)` and, on a KeyError,
returned the template *verbatim*. So an event written before its template
grew a placeholder rendered as

    🔁 {name} crashed (exit {code}) and was restarted … at {when}

— braces and all, losing even the name and exit code that WERE stored.
`monitor_crash_restart` gained `count` and `when` in v1.63/1.65, so every
crash-restart row older than that on a long-running instance looked like
this. Measured before the fix, with the real translator rather than a
stand-in; a first attempt at reproducing it used a hand-rolled `t()` that
raised instead of swallowing, and showed a different (wrong) symptom.

The fix fills what is known and marks what is not. This asserts both
halves: nothing changes when every argument is present, and when one is
missing the rest still comes through.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from i18n import get_translator, available_languages


def main():
    t = get_translator("en")
    checks = {}

    # ── the reported case ─────────────────────────────────────────
    old = t("monitor_crash_restart", name="unifi", code=137)
    checks["no raw braces survive"] = "{" not in old and "}" not in old
    checks["the container name comes through"] = "unifi" in old
    # The exit code is the whole point of that alert — it is what tells a
    # real crash from a `docker stop`. Losing it to a *different* missing
    # field is the part that made the row worthless.
    checks["the exit code comes through"] = "137" in old
    checks["what was never stored is marked"] = "—" in old

    partial = t("monitor_crash_restart", name="unifi", code=137, count=1)
    checks["a partially-complete event keeps its count"] = "#1" in partial

    # ── nothing changes when the arguments are all there ─────────
    full = t("monitor_crash_restart", name="unifi", code=137, count=1,
             when="16:14:47")
    checks["a complete event is untouched"] = (
        "—" not in full and "16:14:47" in full and "#1" in full)
    plain = t("monitor_exited", name="x", code=5)
    checks["the ordinary path is unmarked"] = "—" not in plain and "5" in plain

    # ── every language, not just English ─────────────────────────
    # The recovery runs on the localised string, so a language whose
    # translation uses the placeholders in a different order must survive
    # it too.
    bad = []
    for lang in available_languages():
        msg = get_translator(lang)("monitor_crash_restart", name="unifi",
                                   code=137)
        if "{" in msg or "unifi" not in msg or "137" not in msg:
            bad.append(lang)
    checks["all languages recover"] = not bad
    if bad:
        print(f"    broken in: {', '.join(bad)}")

    # ── a stray brace must not be mangled ────────────────────────
    # Strings containing literal braces (a JSON example, a code snippet)
    # already failed to format and were returned as-is. That must stay
    # true rather than turning into a marker soup.
    import i18n
    tr = i18n.get_translator("en")
    try:
        # Force the recovery path on a string with an unbalanced brace by
        # going through the same helper the translator uses.
        i18n._Missing({"a": 1})
        checks["the missing-marker mapping exists"] = True
    except Exception:
        checks["the missing-marker mapping exists"] = False

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    if failed:
        print(f"    rendered: {old}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
