#!/usr/bin/env python3
"""The crash alert says WHEN it measured, and says so even when idle (#66).

@NotRetarded got two crash alerts for the same container. The first had no
"Top CPU" line at all, the second did, and he read the gap as the history
having lost data. Both halves of that were our wording, not his mistake:

- The CPU line was dropped whenever the busiest container sat below
  `CPU_FLOOR`. `_cpu_snapshot` returns "" for that AND for "stats never
  answered", so silence covered two opposite facts and the reader could
  not tell a quiet host from a lost measurement.

- Two lines both said "at event time" and meant different times. The top
  lists come from the event watcher's snapshot, taken as the container
  died; the container's own line is read in the poll that follows, once it
  has restarted. His alert said "Unifi-OS-Server itself at event time:
  63.89MiB · 59%" — the cost of booting back up, presented as the state
  before the crash.

So: an explicit line for an idle box, and two separate labels that name
their moment. When there is no evidence and everything comes from one
fresh snapshot the labels agree again, because then it is one moment.

Pure logic — `docker stats` is stubbed. Exits non-zero on any failure.
"""

import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from monitor import ContainerMonitor

LANG_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "lang")
KEYS = ("monitor_top_memory", "monitor_top_memory_after",
        "monitor_top_cpu", "monitor_top_cpu_after",
        "monitor_top_cpu_quiet", "monitor_top_cpu_quiet_after",
        "monitor_victim_usage")


def _lang(code):
    with open(os.path.join(LANG_DIR, code + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def _longest_common(a, b):
    """Longest substring shared by two strings.

    Used to find the time phrase two labels have in common without
    hardcoding it per language — the phrase is what has to line up, and
    sixteen translations cannot each be spelled out here.
    """
    best = ""
    for i in range(len(a)):
        for j in range(i + len(best) + 1, len(a) + 1):
            if a[i:j] in b:
                best = a[i:j]
            else:
                break
    return best


class _Rows:
    returncode = 0

    def __init__(self, stdout):
        self.stdout = stdout


def _monitor(stdout, watcher=None):
    m = ContainerMonitor.__new__(ContainerMonitor)
    m.backend = types.SimpleNamespace(name="local",
                                      stats=lambda **kw: _Rows(stdout))
    m._snap_cache = None
    m.watcher = watcher
    # Both are local-only readings and irrelevant here; stubbed so the test
    # does not depend on the load of whatever box it runs on.
    m._host_memory = lambda: ""
    m._host_load = lambda: ""
    return m


def _render(en, kind, name, resources):
    """The alert text, in English, for one already-gathered picture."""
    m = ContainerMonitor.__new__(ContainerMonitor)
    m.sent = []
    m.bot = types.SimpleNamespace(
        t=lambda key, **kw: en[key].format(**kw),
        send_message=lambda msg, **kw: m.sent.append(msg),
        notifier=None)
    m.checker = types.SimpleNamespace(_tail_logs=lambda n, lines=10: "")
    m._latest = {}
    m._notify(kind, name, {"code": 137, "count": 1, "when": "16:14:47"},
              resources)
    return m.sent[0]


IDLE = "unifi|1GiB / 8GiB|2.00%\ndb|2GiB / 8GiB|1.00%"
BUSY = "hog|900MiB / 8GiB|198.40%\nunifi|1GiB / 8GiB|3.00%"


def main():
    checks = {}
    en = _lang("en")

    # ── A quiet host is a reading, not a silence ─────────────────
    quiet = _monitor(IDLE)
    checks["an idle box reports the floor it stayed under"] = (
        quiet._cpu_quiet() == "50")
    checks["a busy box has nothing to explain"] = (
        _monitor(BUSY)._cpu_quiet() == "")
    # No rows at all means the call failed. That is NOT the same fact, and
    # claiming the box was idle would be inventing a measurement.
    dead = _monitor("")
    checks["a stats call that failed stays silent"] = dead._cpu_quiet() == ""
    # The old contract is untouched: "" still means "no list to print".
    checks["the list itself is unchanged on an idle box"] = (
        quiet._cpu_snapshot() == "")

    res = quiet._resources_for("exited", "unifi")
    checks["the picture carries the quiet reading"] = res.get("cpu_quiet") == "50"
    checks["and no list to go with it"] = "cpu" not in res
    msg = _render(en, "exited", "unifi", res)
    # The regression this exists to catch: the CPU line vanishing without
    # a word, which is what sent #66 looking for lost data.
    checks["the alert still says something about CPU"] = "CPU" in msg
    checks["it names the floor"] = "50%" in msg
    checks["it is not an error message"] = not any(
        w in msg.lower() for w in ("error", "failed", "unavailable"))

    busy = _monitor(BUSY)
    busy_msg = _render(en, "exited", "hog",
                       busy._resources_for("exited", "hog"))
    checks["a real hog is still named"] = "hog 198%" in busy_msg

    # ── Two moments, two labels ──────────────────────────────────
    watcher = types.SimpleNamespace(
        evidence=lambda n: {"mem": "hog 9GiB", "cpu": "hog 190%"},
        exit_code=lambda n: 137,
        saw_oom=lambda n: False)
    seen = _monitor(BUSY, watcher=watcher)
    r_death = seen._resources_for("crash_restart", "unifi")
    checks["evidence is labelled as the death"] = r_death.get("at") == "death"
    checks["the top list is the evidence, not the fresh stats"] = (
        r_death.get("cpu") == "hog 190%")
    checks["the victim line is read in this pass"] = (
        r_death.get("victim") == "1GiB · 3%")

    death_msg = _render(en, "crash_restart", "unifi", r_death)
    top_death = [l for l in death_msg.split("\n") if l.startswith("Top CPU")][0]
    victim_line = [l for l in death_msg.split("\n") if l.startswith("unifi")][0]
    checks["the top list says when it died"] = (
        top_death == en["monitor_top_cpu"].format(list="hog 190%"))
    # The phrase the victim line and the "_after" label have in common is
    # the one that names the later moment — read out of the file rather
    # than spelled here, so a reworded translation cannot drift past it.
    after_phrase = _longest_common(en["monitor_top_cpu_after"],
                                   en["monitor_victim_usage"]).rstrip("{")
    checks["there is a phrase for the later moment"] = len(after_phrase) > 5
    checks["the victim line carries it"] = after_phrase in victim_line
    checks["the death list does not"] = after_phrase not in top_death

    # ── One moment, one wording ──────────────────────────────────
    # Without the watcher there is no death snapshot: the lists and the
    # victim line all come from the poll that noticed the death, so saying
    # "when it died" for the lists would be the same lie in reverse.
    blind = _monitor(BUSY)
    r_after = blind._resources_for("crash_restart", "unifi")
    checks["a fallback picture is labelled as the check after"] = (
        r_after.get("at") == "after")
    after_msg = _render(en, "crash_restart", "unifi", r_after)
    top_after = [l for l in after_msg.split("\n") if l.startswith("Top CPU")][0]
    victim_after = [l for l in after_msg.split("\n")
                    if l.startswith("unifi")][0]
    checks["the fallback list uses the other label"] = (
        top_after == en["monitor_top_cpu_after"].format(list="hog 198%"))
    checks["and now the two lines DO share their moment"] = (
        after_phrase in top_after and after_phrase in victim_after)

    # An event written before this carries no `at` and must keep the
    # wording the evidence path — the common one — actually took.
    old = {"mem": "hog 9GiB", "cpu": "hog 190%"}
    old_msg = _render(en, "exited", "unifi", old)
    checks["an event from before this reads as the death"] = (
        en["monitor_top_cpu"].format(list="hog 190%") in old_msg)

    # ── Every language, not just English ─────────────────────────
    for code in sorted(f[:-5] for f in os.listdir(LANG_DIR)
                       if f.endswith(".json")):
        d = _lang(code)
        checks[f"{code} has all seven labels"] = all(k in d for k in KEYS)
        if not all(k in d for k in KEYS):
            continue
        checks[f"{code} words the two moments differently"] = (
            d["monitor_top_cpu"] != d["monitor_top_cpu_after"] and
            d["monitor_top_memory"] != d["monitor_top_memory_after"])
        # The victim line belongs to the later moment, so it must share
        # its phrase with the "_after" label and not with the death one.
        shared = _longest_common(d["monitor_victim_usage"],
                                 d["monitor_top_cpu_after"])
        checks[f"{code} puts the victim in the later moment"] = (
            len(shared.strip(" :：")) >= 3 and
            shared not in d["monitor_top_cpu"])
        checks[f"{code} names the floor in the quiet line"] = (
            "{pct}" in d["monitor_top_cpu_quiet"] and
            "{pct}" in d["monitor_top_cpu_quiet_after"])
        checks[f"{code} keeps the list placeholder"] = all(
            "{list}" in d[k] for k in
            ("monitor_top_memory", "monitor_top_memory_after",
             "monitor_top_cpu", "monitor_top_cpu_after"))
        if code != "en":
            # These four were shipped as English placeholders in every
            # other language; a reader on a translated UI got half a
            # sentence in their language and half in ours.
            checks[f"{code} is actually translated"] = all(
                d[k] != en[k] for k in KEYS)

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
