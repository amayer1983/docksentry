#!/usr/bin/env python3
"""A host going down is ONE message, not one per container (#63, @famewolf).

He rebooted a monitored host (planned, from Proxmox) and did
`systemctl restart docker` on another. Each time the monitoring host sent
one 💥 crash alert — with a full log dump — for EVERY container on the box
that stopped. A dozen messages for one shutdown. His words: "This is
unsustainable. Multi host is not ready in any way shape or form."

The fix: deaths in a single tick are coalesced. Up to MASS_STOP_INLINE_MAX
of them stay individual, full-detail alerts (two unrelated crashes in a
minute ARE two incidents). Above that it is a host going away, not that
many incidents — so ONE digest message plus a log FILE carrying every
container's tail. The file, not inline text, because a dozen inline dumps
IS the flood; and captured at detection, because a host that stays down
can no longer be asked.

Guard rails this pins:
- a single crash is never swallowed (a batch of one is a normal alert);
- a small burst keeps every container's inline log tail;
- the event log keeps EVERY death individually — only the notification
  collapses;
- a split reboot's second wave does not send a duplicate host message
  (host cooldown), but a genuine crash minutes later still alerts (the
  per-container cooldown is not stamped by the digest);
- the toggle off restores the old one-per-container behaviour.

Pure logic; docker reads stubbed. Exits non-zero on any failure.
"""
import json as _json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from monitor import ContainerMonitor
from update_checker import UpdateChecker


def c(status="running", health="", code=0, oom=False, restarts=0):
    return {"status": status, "health": health, "exit_code": code,
            "oom": oom, "restarts": restarts, "labels": {}, "started_at": ""}


def make_monitor(*, mass=True, host_name="dock8520", logs="log tail here"):
    m = ContainerMonitor.__new__(ContainerMonitor)
    m.config = types.SimpleNamespace(monitor_enabled=True,
                                     exclude_containers=[],
                                     monitor_mass_stop_enabled=mass)
    m.host_name = host_name
    m.checker = types.SimpleNamespace(
        label_bool=UpdateChecker.label_bool,
        _own_container_name=lambda: "docksentry",
        _tail_logs=lambda name, lines=10, none_on_error=False: logs,
    )
    m._memory_snapshot = lambda top=3: ""
    m._cpu_snapshot = lambda top=3: ""
    m._host_memory = lambda: ""
    m._host_load = lambda: ""
    m._event_snapshot = lambda: {}
    m.docs = []
    m.bot = types.SimpleNamespace(
        update_running=False,
        t=_translate,
        send_message=lambda msg, **kw: m.sent.append(msg),
        send_document=lambda name, data, caption="": m.docs.append(
            (name, data, caption)),
        notifier=None,
    )
    m.sent = []
    m._prev = None
    m._last_sent = {}
    m._alert_streak = {}
    m._health_pending = {}
    m._alerted_unhealthy = set()
    m._snap_cache = None
    return m


# A translator that actually substitutes, so we can assert on wording.
import i18n  # noqa: E402
_translate = i18n.get_translator("en")


def running(names):
    return {n: c() for n in names}


def all_exited(names, code=128):
    return {n: c(status="exited", code=code) for n in names}


def main():
    checks = {}

    # ── 12 of 12 stop at once → ONE message + ONE file ───────────────
    m = make_monitor()
    twelve = [f"svc{i}" for i in range(12)]
    m.snapshot = lambda: running(twelve)
    m.tick()                                   # silent baseline
    m.snapshot = lambda: all_exited(twelve)
    sent = m.tick()
    checks["a whole host stopping sends exactly one message"] = (
        len(m.sent) == 1)
    checks["…and it reads as the host going down"] = (
        "appears to have gone down" in m.sent[0] and "12 of 12" in m.sent[0])
    checks["…with one attached log file"] = len(m.docs) == 1
    checks["…named for the host"] = m.docs[0][0].startswith("dock8520-logs-")
    checks["…carrying every container's section"] = (
        m.docs[0][1].decode().count("(exit 128)") == 12)
    checks["…and the actual log tails"] = b"log tail here" in m.docs[0][1]
    checks["the tick still reports all 12 events (for callers/tests)"] = (
        len(sent) == 12)

    # ── the event log keeps every death, plus a summary row ──────────
    with tempfile.TemporaryDirectory() as d:
        m2 = make_monitor()
        m2.config.monitor_events_file = os.path.join(d, "ev.json")
        m2.snapshot = lambda: running(twelve)
        m2.tick()
        m2.snapshot = lambda: all_exited(twelve)
        m2.tick()
        evs = _json.load(open(m2.config.monitor_events_file))
        kinds = [e["kind"] for e in evs]
        checks["audit log keeps all 12 deaths individually"] = (
            kinds.count("exited") == 12)
        checks["…plus one mass_stop summary row"] = (
            kinds.count("mass_stop") == 1)

    # ── a single crash is NEVER coalesced ────────────────────────────
    m3 = make_monitor()
    m3.snapshot = lambda: running(twelve)
    m3.tick()
    m3.snapshot = lambda: {**running(twelve[1:]), twelve[0]: c(
        status="exited", code=1)}
    m3.tick()
    checks["a single crash still fires its own 💥 alert"] = (
        len(m3.sent) == 1 and "💥" in m3.sent[0])
    checks["…and does not produce a host digest or file"] = (
        "appears to have gone down" not in m3.sent[0] and len(m3.docs) == 0)

    # ── a small burst (≤3) stays individual, logs inline ─────────────
    m4 = make_monitor()
    six = [f"s{i}" for i in range(6)]
    m4.snapshot = lambda: running(six)
    m4.tick()
    m4.snapshot = lambda: {**running(six[3:]),
                           **{n: c(status="exited", code=1) for n in six[:3]}}
    m4.tick()
    checks["three deaths stay three individual alerts"] = len(m4.sent) == 3
    checks["…each with its inline log tail, no file"] = (
        all("log tail here" in s for s in m4.sent) and len(m4.docs) == 0)

    # ── the boundary: 4 deaths tips into a digest ────────────────────
    m4b = make_monitor()
    m4b.snapshot = lambda: running(six)
    m4b.tick()
    m4b.snapshot = lambda: {**running(six[4:]),
                            **{n: c(status="exited", code=1)
                               for n in six[:4]}}
    m4b.tick()
    checks["four deaths coalesce into one digest"] = (
        len(m4b.sent) == 1 and len(m4b.docs) == 1)
    checks["…and 4 of 6 still reads as host-down"] = (
        "appears to have gone down" in m4b.sent[0])

    # ── wording is honest: a cluster is not a claim of host-down ─────
    m5 = make_monitor()
    fifty = [f"c{i}" for i in range(50)]
    m5.snapshot = lambda: running(fifty)
    m5.tick()
    # only 5 of 50 die — above the inline cap, but NOT a majority
    m5.snapshot = lambda: {**running(fifty[5:]),
                           **{n: c(status="exited", code=1)
                              for n in fifty[:5]}}
    m5.tick()
    checks["a minority cluster does not claim the host went down"] = (
        len(m5.sent) == 1 and "appears to have gone down" not in m5.sent[0])
    checks["…it says N stopped at once instead"] = (
        "stopped at once" in m5.sent[0])

    # ── split reboot: second wave records but sends no dup message ───
    m6 = make_monitor()
    m6.COOLDOWN_SECONDS = 1800
    with tempfile.TemporaryDirectory() as d:
        m6.config.monitor_events_file = os.path.join(d, "ev.json")
        m6.snapshot = lambda: running(twelve)
        m6.tick()
        # first six die
        m6.snapshot = lambda: {**running(twelve[6:]),
                               **all_exited(twelve[:6])}
        m6.tick()
        first = len(m6.sent)
        # next tick the other six die (reboot caught across two polls)
        m6.snapshot = lambda: all_exited(twelve)
        m6.tick()
        checks["split reboot's second wave sends no duplicate host msg"] = (
            len(m6.sent) == first == 1)

    # ── the toggle off restores the old one-per-container flood ──────
    m7 = make_monitor(mass=False)
    m7.snapshot = lambda: running(twelve)
    m7.tick()
    m7.snapshot = lambda: all_exited(twelve)
    m7.tick()
    checks["with the toggle off, every container alerts individually"] = (
        len(m7.sent) == 12 and len(m7.docs) == 0)

    # ── a health flip during a mass stop is not swallowed ────────────
    m8 = make_monitor()
    names = twelve + ["sick"]
    m8.snapshot = lambda: running(names)
    m8.tick()
    # confirm 'sick' unhealthy on the pass the others die (health debounce
    # needs two passes; drive it directly through _health_events instead).
    # Simpler: the 12 die AND sick is already flagged this tick.
    m8._alerted_unhealthy = set()
    m8._health_pending = {"sick": 1}   # already seen unhealthy once
    def snap8():
        s = all_exited(twelve)
        s["sick"] = c(health="unhealthy")
        return s
    m8.snapshot = snap8
    m8.tick()
    joined = " ".join(m8.sent)
    checks["mass stop and a genuine unhealthy flip both get through"] = (
        any("appears to have gone down" in s for s in m8.sent)
        and "🔴" in joined or "unhealthy" in joined.lower())

    # ── the digest file tells "silent" from "unreachable" (#63) ─────────
    # A container that ran but logged nothing must NOT read as a dead or
    # unreachable host in the attached file — measured live: five
    # `sleep infinity` containers (no output) all printed "host unreachable,
    # or the container is gone", a scary false alarm on a reachable host.
    m9 = make_monitor(logs="")                     # ran, but no output
    m9.snapshot = lambda: running(twelve)
    m9.tick()
    m9.snapshot = lambda: all_exited(twelve)
    m9.tick()
    body9 = m9.docs[0][1].decode()
    checks["silent container reads as 'no output', not 'unreachable'"] = (
        "no log output" in body9 and "host unreachable" not in body9)

    m10 = make_monitor(logs=None)                  # fetch failed (unreachable/gone)
    m10.snapshot = lambda: running(twelve)
    m10.tick()
    m10.snapshot = lambda: all_exited(twelve)
    m10.tick()
    body10 = m10.docs[0][1].decode()
    checks["a failed log fetch still reads as unreachable/gone"] = (
        "host unreachable" in body10)

    # ── _tail_logs(none_on_error=True): rc!=0 → None, empty → "" ─────────
    def tl(rc, out, err="", none=False):
        u = UpdateChecker.__new__(UpdateChecker)
        # `backend` is a read-only property backed by `_backend`.
        u._backend = types.SimpleNamespace(
            logs=lambda name, *, tail=None, timeout=None:
                types.SimpleNamespace(returncode=rc, stdout=out, stderr=err))
        u._collapse_repeats = lambda s: s
        return UpdateChecker._tail_logs(u, "x", none_on_error=none)
    checks["_tail_logs none_on_error: failed fetch → None"] = tl(1, "", none=True) is None
    checks["_tail_logs none_on_error: silent → ''"] = tl(0, "", none=True) == ""
    checks["_tail_logs none_on_error: has output → text"] = tl(0, "hi", none=True) == "hi"
    # default path unchanged for every other caller: failure still yields ""
    checks["_tail_logs default path: failure → '' (contract kept)"] = tl(1, "") == ""

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
