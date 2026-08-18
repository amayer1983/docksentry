#!/usr/bin/env python3
"""A self-update that stops us properly, and says when it did not (#62).

@NotRetarded's instance updated itself from 2.12.3 to 2.14.0 and died in
the middle with **exit 137** — SIGKILL. The update succeeded and
Docksentry announced it cheerfully:

    🚀 Docksentry updated: v2.12.3 → v2.14.0

with, in his words, "nothing about the exit code 137".

Two separate faults, and both are ours.

**The helper ran a bare `docker stop`.** No `-t`, so Docker's own default
of ten seconds applied and then it sent SIGKILL. Shutting *ourselves*
down is not faster than shutting anything else down — the web server,
the scheduler and the Discord gateway all have to come to a halt, and
the Discord one deliberately waits for a command still in flight. Every
other stop in this project was put on `DOCKER_STOP_TIMEOUT` in v2.8.3,
after @famewolf hit exactly this on slow containers. This one was
missed, the same way the `rename` calls were missed in that release and
had to be fixed again in 2.8.4.

**And we could not tell afterwards.** The shutdown handler writes its
exit marker *first*, deliberately, so it survives a SIGKILL — which
means the marker proves a signal arrived, not that shutting down ever
finished. A killed shutdown and a clean one looked identical, so the
next boot said nothing. It is written a second time at the end now, and
the difference between those two writes is the whole answer.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from telegram_bot import TelegramBot  # noqa: E402

checks = {}

# ── the stop the helper actually runs ────────────────────────────────
script = TelegramBot._build_selfupdate_script(
    "DockSentry", "--name DockSentry", "img:latest", stop_timeout=60)
checks["the swap stops us with an explicit timeout"] = (
    "docker stop -t 60 DockSentry" in script)
checks["…never bare, which would mean Docker's ten seconds"] = (
    "docker stop DockSentry" not in script)
checks["…and the default is generous rather than Docker's"] = (
    "stop -t 60 " in TelegramBot._build_selfupdate_script("a", "b", "c"))
# The rest of the swap has to be untouched — this is the script that can
# leave Docksentry dead if it goes wrong (#43).
for step in ("docker rename", "docker run -d", "docker rm ",
             "Selfupdate backup failed", "Selfupdate recreate failed"):
    checks[f"the swap still does: {step.strip()}"] = step in script

src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "telegram_bot.py"), encoding="utf-8").read()
checks["the timeout comes from DOCKER_STOP_TIMEOUT"] = (
    'getattr(self.config,\n                                             '
    '"docker_stop_timeout", 60)' in src
    or 'docker_stop_timeout' in src.split("_build_selfupdate_script(\n")[1][:300])
checks["…with the same floor the rest of the shutdown path uses"] = (
    "max(30, int(getattr(self.config" in src)

# ── telling a finished shutdown from a killed one ────────────────────
main = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "main.py"), encoding="utf-8").read()
checks["the exit marker starts out saying 'not finished'"] = (
    '"done": False' in main)
checks["…and is rewritten once everything has stopped"] = (
    '"done": True' in main)
checks["a shutdown that never finished is reported"] = (
    "killed_stopping" in main and "startup_killed_stopping" in main)
# Order matters: the first write has to happen before any service is
# stopped, or a SIGKILL loses it entirely — which is why it is written
# twice rather than once at the end.
_first = main.index('"done": False')
_stop = main.index("scheduler.stop()")
_second = main.index('"done": True')
checks["…the first write comes before anything is stopped"] = _first < _stop
checks["…and the second after"] = _second > _stop

# An older marker without the field must not be read as a kill.
i = main.index("killed_stopping = not")
checks["a marker from an older version is not mistaken for a kill"] = (
    '_exit.get("done", True)' in main[i:i + 120])

# The message has to be honest about the consequence, which is "probably
# nothing" — every file is written atomically. Crying wolf here would be
# the same mistake the data-loss alert made.
en = json.load(open(os.path.join(os.path.dirname(__file__), "..", "app",
                                 "lang", "en.json"), encoding="utf-8"))
msg = en.get("startup_killed_stopping", "")
checks["the message names the mechanism"] = "SIGKILL" in msg and "137" in msg
checks["…says what to change"] = "DOCKER_STOP_TIMEOUT" in msg
checks["…and does not claim damage it cannot show"] = "atomically" in msg

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
