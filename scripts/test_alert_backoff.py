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

# ── the gate every kind of alert passes through ──────────────────────
# This used to ask only whether `_cooldown_for` appeared ANYWHERE in the
# file. It did — in the branch that handles everything except deaths.
# Crashes have their own gate a few lines above, it used the flat
# COOLDOWN_SECONDS, and nothing here looked. @famewolf's syncserver is a
# crash loop; the backoff written for it never applied to it, and a live
# install ran paperless-ngx to restart #2091 at a full alert every half
# hour. So both gates are asserted, by name.
_deaths = src.split("deaths = [")[1].split("mass = (")[0]
_others = src.split("for kind, name, detail in others:")[1].split("return sent")[0]
checks["the crash gate asks for the grown wait"] = (
    "self._cooldown_for((n, k))" in _deaths)
checks["…and so does every other kind"] = (
    "self._cooldown_for(key)" in _others)
checks["neither gate uses the flat wait any more"] = (
    "< self.COOLDOWN_SECONDS" not in _deaths
    and "< self.COOLDOWN_SECONDS" not in _others)

# ── and the count is kept in ONE place ───────────────────────────────
# Two copies of the bookkeeping is how one of them came to be missing.
checks["both branches count through the same helper"] = (
    src.count("self._bump_streak(") == 2 and "def _bump_streak(" in src)

# ── behaviour: a crash loop really does back off ─────────────────────
import types                                                # noqa: E402
sim = ContainerMonitor.__new__(ContainerMonitor)
sim._alert_streak = {}
sim._last_sent = {}
sim.config = types.SimpleNamespace(monitor_mass_stop_enabled=True)
K = ("paperless", "crash_restart")
sent_at, t = [], 0.0
for _ in range(12 * 60):                     # twelve hours, one tick a minute
    t += 60
    if t - sim._last_sent.get(K, 0) < sim._cooldown_for(K):
        continue
    sim._bump_streak(K, t)
    sim._last_sent[K] = t
    sent_at.append(t)
gaps = [round((b - a) / 60) for a, b in zip(sent_at, sent_at[1:])]
checks["a crash loop costs a handful of alerts in twelve hours"] = (
    2 <= len(sent_at) <= 6)
checks["…with the gap doubling, not flat"] = gaps[:3] == [30, 60, 120]

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
