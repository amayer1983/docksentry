#!/usr/bin/env python3
"""A container that keeps failing is reported less and less often.

@famewolf's `firefox-syncserver` reached restart #190 with sqlite unable
to open its database, and earned a message every thirty minutes until he
gave up and stopped the container. The alert was right every single time;
the fortieth copy of it carries nothing the first did not, and it is what
makes people mute the channel.

So the wait doubles per repeat, up to six hours — the same shape already
used for a host that will not answer. Two rules keep it honest: it never
goes silent, because a channel that stops mentioning a broken container
is how it gets forgotten; and a gap longer than the cap starts over,
because that is a new incident rather than the old one continuing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from monitor import ContainerMonitor  # noqa: E402

checks = {}

m = ContainerMonitor.__new__(ContainerMonitor)
m._alert_streak = {}
KEY = ("syncserver", "crash_restart")

first = m._cooldown_for(KEY)
checks["the first repeat waits the plain half hour"] = (
    first == ContainerMonitor.COOLDOWN_SECONDS)

waits = []
for _ in range(8):
    waits.append(m._cooldown_for(KEY))
    m._alert_streak[KEY] = m._alert_streak.get(KEY, 0) + 1

checks["the wait grows"] = (waits[2] > waits[1] and waits[3] > waits[2])
checks["…and stops growing at the cap"] = (
    waits[-1] == waits[-2] == ContainerMonitor.MAX_COOLDOWN_SECONDS)
checks["…so it never falls silent"] = all(w > 0 for w in waits)
checks["the cap is hours, not days"] = (
    ContainerMonitor.MAX_COOLDOWN_SECONDS <= 12 * 3600)

# What it buys: a day of a container nobody is going to fix tonight.
day, spent, n = 0, 0, 0
m._alert_streak[KEY] = 0
while spent < 24 * 3600:
    spent += m._cooldown_for(KEY)
    m._alert_streak[KEY] += 1
    day += 1
checks["a day of one crash loop is a handful of alerts, not fifty"] = (2 < day < 12)

# A different container is on its own schedule.
m._alert_streak = {KEY: 5}
checks["another container is unaffected"] = (
    m._cooldown_for(("other", "crash_restart"))
    == ContainerMonitor.COOLDOWN_SECONDS)
# …and so is a different KIND of alert on the same container.
checks["…and so is a different kind on the same one"] = (
    m._cooldown_for(("syncserver", "unhealthy"))
    == ContainerMonitor.COOLDOWN_SECONDS)

# The reset lives in the send loop; assert it is wired to the cap.
src = open(os.path.join(os.path.dirname(__file__), "..",
                        "app", "monitor.py"), encoding="utf-8").read()
checks["a long quiet spell starts the count over"] = (
    "if gap > self.MAX_COOLDOWN_SECONDS:" in src
    and "self._alert_streak[key] = 1" in src)
checks["…and the loop asks for the grown wait, not the flat one"] = (
    "now_ts - last < self._cooldown_for(key)" in src)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
