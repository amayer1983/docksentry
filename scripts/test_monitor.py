#!/usr/bin/env python3
"""Container state monitoring (#2, @NotRetarded).

Transition detection between snapshots plus the guard rails that keep the
feature from becoming a spam cannon:

- health healthy->unhealthy fires once; the recovery fires once
- zero exits are silent (one-shot jobs end normally all day long)
- non-zero exits and OOM kills notify, with code
- RestartCount increase while "running" = crashed + auto-restarted
- vanished containers are NOT events (removal is deliberate)
- first tick is a silent baseline
- whole tick skipped (and baseline dropped) while updates run
- per-(container, kind) cooldown suppresses flapping
- docksentry.monitor=false label opts a container out

Pure logic — docker reads are stubbed. Exits non-zero on any failure.
"""
import sys, os, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from monitor import ContainerMonitor
from update_checker import UpdateChecker


def c(status="running", health="", code=0, oom=False, restarts=0, labels=None):
    return {"status": status, "health": health, "exit_code": code,
            "oom": oom, "restarts": restarts, "labels": labels or {}}


def make_monitor(update_running=False, exclude=None):
    m = ContainerMonitor.__new__(ContainerMonitor)
    m.config = types.SimpleNamespace(monitor_enabled=True,
                                     exclude_containers=exclude or [])
    m.checker = types.SimpleNamespace(
        label_bool=UpdateChecker.label_bool,
        _own_container_name=lambda: "docksentry",
    )
    m.bot = types.SimpleNamespace(
        update_running=update_running,
        t=lambda key, **kw: key,
        send_message=lambda msg, **kw: m.sent.append(msg),
        notifier=None,
    )
    m.sent = []
    m._prev = None
    m._last_sent = {}
    return m


def kinds(events):
    return [(k, n) for k, n, _ in events]


def main():
    checks = {}
    diff = ContainerMonitor.diff

    # ── health transitions ──
    ev = diff({"a": c(health="healthy")}, {"a": c(health="unhealthy")})
    checks["healthy->unhealthy fires"] = kinds(ev) == [("unhealthy", "a")]
    ev = diff({"a": c(health="unhealthy")}, {"a": c(health="unhealthy")})
    checks["staying unhealthy is silent"] = ev == []
    ev = diff({"a": c(health="unhealthy")}, {"a": c(health="healthy")})
    checks["recovery fires"] = kinds(ev) == [("recovered", "a")]
    ev = diff({"a": c(health="")}, {"a": c(health="")})
    checks["no healthcheck -> no health events"] = ev == []

    # ── exits ──
    ev = diff({"a": c()}, {"a": c(status="exited", code=0)})
    checks["zero exit is silent"] = ev == []
    ev = diff({"a": c()}, {"a": c(status="exited", code=137)})
    checks["non-zero exit fires with code"] = ev == [("exited", "a", {"code": 137})]
    ev = diff({"a": c()}, {"a": c(status="exited", code=137, oom=True)})
    checks["oom exit fires as oom"] = kinds(ev) == [("oom", "a")]

    # ── crash + auto-restart ──
    ev = diff({"a": c(restarts=2)}, {"a": c(restarts=3)})
    checks["restart count increase fires"] = ev == [("crash_restart", "a", {"count": 3})]
    ev = diff({"a": c(restarts=3)}, {"a": c(restarts=0)})
    checks["count reset (recreate) is silent"] = ev == []

    # ── population changes ──
    ev = diff({"a": c()}, {})
    checks["vanished container is silent"] = ev == []
    ev = diff({}, {"a": c(status="exited", code=1)})
    checks["new container is baselined silently"] = ev == []

    # ── tick machinery ──
    m = make_monitor()
    m.snapshot = lambda: {"a": c(health="healthy"), "docksentry": c()}
    checks["first tick: silent baseline"] = m.tick() == []
    m.snapshot = lambda: {"a": c(health="unhealthy"), "docksentry": c(status="exited", code=1)}
    sent = m.tick()
    checks["second tick: notifies transition"] = kinds(sent) == [("unhealthy", "a")]
    checks["own container ignored"] = all(n != "docksentry" for _, n in kinds(sent))
    checks["message rendered via i18n key"] = m.sent == ["monitor_unhealthy"]

    # cooldown: same transition again within window stays quiet
    m.snapshot = lambda: {"a": c(health="healthy")}
    m.tick()  # recovery fires (different kind, allowed)
    m.snapshot = lambda: {"a": c(health="unhealthy")}
    checks["flap within cooldown suppressed"] = m.tick() == []

    # ── update-lock guard ──
    m2 = make_monitor()
    m2.snapshot = lambda: {"a": c()}
    m2.tick()
    m2.bot.update_running = True
    checks["tick skipped while updating"] = m2.tick() == []
    checks["baseline dropped while updating"] = m2._prev is None
    m2.bot.update_running = False
    m2.snapshot = lambda: {"a": c(status="exited", code=1)}
    checks["post-update tick re-baselines silently"] = m2.tick() == []

    # ── label opt-out ──
    m3 = make_monitor()
    lab = {"docksentry.monitor": "false"}
    m3.snapshot = lambda: {"a": c(health="healthy", labels=lab)}
    m3.tick()
    m3.snapshot = lambda: {"a": c(health="unhealthy", labels=lab)}
    checks["docksentry.monitor=false opts out"] = m3.tick() == []

    # ── exclude list ──
    m4 = make_monitor(exclude=["a"])
    m4.snapshot = lambda: {"a": c(health="healthy")}
    m4.tick()
    m4.snapshot = lambda: {"a": c(health="unhealthy")}
    checks["exclude_containers honored"] = m4.tick() == []

    # ── persistent event log (v1.48.1) ──
    import tempfile, json as _json
    d = tempfile.mkdtemp()
    evfile = os.path.join(d, "monitor_events.json")
    m5 = make_monitor()
    m5.config.monitor_events_file = evfile
    m5.snapshot = lambda: {"a": c()}
    m5.tick()
    m5.snapshot = lambda: {"a": c(status="exited", code=137)}
    m5.tick()
    with open(evfile) as f:
        evs = _json.load(f)
    checks["event: persisted to file"] = len(evs) == 1
    checks["event: structure complete"] = (
        evs[0]["kind"] == "exited" and evs[0]["container"] == "a"
        and evs[0]["detail"] == {"code": 137} and bool(evs[0]["timestamp"]))

    # cap: prefill beyond MAX_EVENTS, next record trims
    from container_store import atomic_write_json
    atomic_write_json(evfile, [{"timestamp": "t", "kind": "exited",
                                "container": "x", "detail": {}}] * 250)
    m5._last_sent = {}
    m5.snapshot = lambda: {"a": c()}
    m5.tick()
    m5.snapshot = lambda: {"a": c(status="exited", code=1)}
    m5.tick()
    with open(evfile) as f:
        evs = _json.load(f)
    checks["event: capped at MAX_EVENTS"] = len(evs) == m5.MAX_EVENTS
    checks["event: newest survives the cap"] = evs[-1]["container"] == "a"

    # a failed write must never break the tick
    m6 = make_monitor()
    m6.config.monitor_events_file = os.path.join(d, "nodir", "sub", "x.json")
    m6.snapshot = lambda: {"a": c()}
    m6.tick()
    m6.snapshot = lambda: {"a": c(status="exited", code=1)}
    try:
        sent = m6.tick()
        checks["event: write failure doesn't break tick"] = kinds(sent) == [("exited", "a")]
    except Exception:
        checks["event: write failure doesn't break tick"] = False

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
