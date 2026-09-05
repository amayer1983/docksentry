#!/usr/bin/env python3
"""A container that finishes cleanly and comes back has not crashed.

@famewolf's batch upscaler exits 0 after each video and `restart: always`
starts it on the next one. Docksentry announced every loop as
`vhs-batch-upscaler crashed (exit 0) …` — a sentence that contradicts
itself, about a container doing exactly what it was told.

Two separate mistakes were behind it. The detector fires on RestartCount
alone, which is right: it is what catches a crash loop on both runtimes.
But the CODE it printed came from `docker inspect`, and a container that
is running again reports `ExitCode: 0` — measured. So the number was not
"it exited cleanly", it was "we have no idea", and the two are opposites
in a crash alert.

Now: the event stream records the code for a clean exit too (no snapshot,
so it stays cheap), a known zero means silence, and a code we never saw
is said to be unknown rather than printed as a zero.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import i18n  # noqa: E402

checks = {}

src = open(os.path.join(os.path.dirname(__file__), "..",
                        "app", "monitor.py")).read()
watch = open(os.path.join(os.path.dirname(__file__), "..",
                          "app", "event_watcher.py")).read()

# ── the watcher now knows a clean exit happened ──────────────────────
checks["a clean exit is recorded, not thrown away"] = (
    "_clean = action ==" in watch and 'snap = "" if _clean' in watch)
checks["…and still costs no snapshot"] = (
    watch.index('snap = "" if _clean') < watch.index("self._snapshot()",
                                                     watch.index("_clean =")))

# ── a known zero is silence ──────────────────────────────────────────
checks["a crash-restart with a known exit 0 is dropped"] = (
    'e[0] == "crash_restart" and e[2].get("code") == 0' in src)
checks["…after the real code has been resolved, not before"] = (
    src.index("_with_real_exit_code(e) for e") < src.index('e[2].get("code") == 0'))

# ── an unknown code is not a zero ────────────────────────────────────
checks["an unresolved code becomes None, not 0"] = (
    'detail["code"] = None' in src)
# A NON-zero snapshot code is real — we only ever sampled it because the
# container was sitting exited — so it must survive.
checks["a real snapshot code is not thrown away"] = (
    'elif detail.get("code"):' in src)
# And with no watcher at all the zero is still meaningless, so that path
# must reach the same normalisation instead of returning early. Without
# this, every crash-restart on an install without the event stream would
# have been silenced by the new filter.
checks["an install without the event stream still gets its alert"] = (
    src.index("watcher.exit_code(name) if watcher else None")
    < src.index('detail["code"] = None'))
checks["…and gets a wording that admits it"] = (
    "monitor_crash_restart_nocode" in src)

en = i18n.get_translator("en")
line = en("monitor_crash_restart_nocode", name="app", count=3, when="12:00:00")
checks["the no-code wording says the restart happened"] = ("restart" in line.lower())
checks["…and does not print a number for the exit code"] = ("exit 0" not in line)
checks["…and says why there is none"] = ("not known" in line.lower())

for lang in sorted(f[:-5] for f in os.listdir(
        os.path.join(os.path.dirname(__file__), "..", "app", "lang"))
        if f.endswith(".json")):
    t = i18n.get_translator(lang)
    out = t("monitor_crash_restart_nocode", name="app", count=3, when="12:00:00")
    checks[f"{lang} has the wording, with the name in it"] = (
        "app" in out and "{" not in out)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
