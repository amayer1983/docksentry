#!/usr/bin/env python3
"""Container state monitoring (#2, @NotRetarded).

Transition detection between snapshots plus the guard rails that keep the
feature from becoming a spam cannon:

- health is debounced: a one-pass flap is silent; a still-unhealthy
  container on the next pass fires once; recovery fires once (only if the
  unhealthy was confirmed)
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


def c(status="running", health="", code=0, oom=False, restarts=0, labels=None,
      started_at=""):
    return {"status": status, "health": health, "exit_code": code,
            "oom": oom, "restarts": restarts, "labels": labels or {},
            "started_at": started_at}


def make_monitor(update_running=False, exclude=None):
    m = ContainerMonitor.__new__(ContainerMonitor)
    m.config = types.SimpleNamespace(monitor_enabled=True,
                                     exclude_containers=exclude or [])
    m.checker = types.SimpleNamespace(
        label_bool=UpdateChecker.label_bool,
        _own_container_name=lambda: "docksentry",
        _tail_logs=lambda name, lines=10: "",
    )
    m._memory_snapshot = lambda top=3: ""
    m.bot = types.SimpleNamespace(
        update_running=update_running,
        t=lambda key, **kw: key,
        send_message=lambda msg, **kw: m.sent.append(msg),
        notifier=None,
    )
    m.sent = []
    m._prev = None
    m._last_sent = {}
    m._health_pending = {}
    m._alerted_unhealthy = set()
    return m


def kinds(events):
    return [(k, n) for k, n, _ in events]


def _mem_checks(checks):
    """The 'who did this to me?' diagnostics (#2, @NotRetarded).

    A container squeezed out by a neighbour frequently dies WITHOUT the
    kernel OOM-killing it, so attaching the memory snapshot to OOM alone
    meant the one alert that could point at the neighbour stayed silent in
    the commonest case. Host memory comes first because it answers the
    prior question: was the machine under pressure at all? Without it a
    top-consumers list invites blaming whoever is largest even when there
    were gigabytes free.
    """
    import inspect as _i
    src = _i.getsource(ContainerMonitor._notify)
    for kind in ("oom", "crash_restart", "exited"):
        checks[f"memory diagnostics attach to {kind}"] = (
            f'"{kind}"' in src.split("_memory_snapshot")[0].rsplit("if kind in", 1)[-1])
    checks["a recovery message stays clean"] = (
        '"recovered"' not in src.split("_memory_snapshot")[0].rsplit("if kind in", 1)[-1])
    # Host memory is read from /proc/meminfo, which inside a container
    # still reports the HOST's figures — that's the point.
    import types as _t
    local = ContainerMonitor.__new__(ContainerMonitor)
    local.backend = _t.SimpleNamespace(name="local")
    out = local._host_memory()
    checks["host memory reads as a human string"] = (out == "" or "GB" in out)
    checks["host memory carries no translatable words"] = not any(
        w in out for w in ("used", "total", "free"))
    # A monitor bound to a remote endpoint must stay silent: /proc/meminfo
    # describes THIS box, and printing it under a remote container's name
    # sends the reader hunting on the wrong machine.
    remote = ContainerMonitor.__new__(ContainerMonitor)
    remote.backend = _t.SimpleNamespace(name="nas", endpoint="ssh://nas")
    checks["remote monitor reports no host memory"] = remote._host_memory() == ""


def main():
    checks = {}
    _mem_checks(checks)
    diff = ContainerMonitor.diff

    # ── health transitions (debounced, #2 @famewolf) ──
    he = ContainerMonitor._health_events
    # diff() itself no longer emits health events — they live in _health_events
    checks["diff has no health events"] = (
        diff({"a": c(health="healthy")}, {"a": c(health="unhealthy")}) == []
        and diff({"a": c(health="unhealthy")}, {"a": c(health="healthy")}) == [])

    # first flip healthy->unhealthy: pending, NO alert
    ev, pend, alrt = he({"a": c(health="healthy")}, {"a": c(health="unhealthy")}, {}, set())
    checks["first unhealthy flip is silent (pending)"] = (
        ev == [] and pend == {"a": "healthy"} and alrt == set())
    # still unhealthy next pass: fire once, remember the pre-unhealthy state
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="unhealthy")}, pend, alrt)
    checks["confirmed unhealthy fires on next pass"] = (
        ev == [("unhealthy", "a", {"prev": "healthy"})] and alrt == {"a"} and pend == {})
    # already alerted, stays unhealthy: no repeat
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="unhealthy")}, pend, alrt)
    checks["confirmed unhealthy does not repeat"] = ev == [] and alrt == {"a"}
    # recovery of a confirmed unhealthy fires once and clears
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="healthy")}, pend, alrt)
    checks["recovery of confirmed fires once"] = (
        kinds(ev) == [("recovered", "a")] and alrt == set())
    # one-pass flap (pending then healthy again) -> ZERO events, no residue
    ev, pend, alrt = he({"a": c(health="healthy")}, {"a": c(health="unhealthy")}, {}, set())
    ev2, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="healthy")}, pend, alrt)
    checks["one-pass flap is fully silent"] = (
        ev == [] and ev2 == [] and pend == {} and alrt == set())
    # no healthcheck -> nothing ever
    ev, pend, alrt = he({"a": c(health="")}, {"a": c(health="")}, {}, set())
    checks["no healthcheck -> no health events"] = ev == [] and pend == {} and alrt == set()

    # ── recovery-by-restart: leaving unhealthy ends the episode (regression) ──
    # (a) confirmed unhealthy recovers via unhealthy->starting: one "recovered",
    #     alerted cleared, and a later unhealthy episode alerts normally again.
    ev, pend, alrt = he({"a": c(health="healthy")}, {"a": c(health="unhealthy")}, {}, set())
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="unhealthy")}, pend, alrt)
    checks["restart-recovery: confirmed first"] = alrt == {"a"}
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="starting")}, pend, alrt)
    checks["restart-recovery: ->starting fires recovered once"] = (
        kinds(ev) == [("recovered", "a")] and alrt == set() and pend == {})
    # a fresh episode after restart-recovery alerts again (no stuck flag)
    ev, pend, alrt = he({"a": c(health="starting")}, {"a": c(health="unhealthy")}, pend, alrt)
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="unhealthy")}, pend, alrt)
    checks["restart-recovery: later episode alerts again"] = (
        kinds(ev) == [("unhealthy", "a")] and alrt == {"a"})

    # (b) unhealthy->starting->healthy fires recovery ONCE, not twice.
    ev, pend, alrt = he({"a": c(health="healthy")}, {"a": c(health="unhealthy")}, {}, set())
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="unhealthy")}, pend, alrt)
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="starting")}, pend, alrt)
    r1 = kinds(ev)
    ev, pend, alrt = he({"a": c(health="starting")}, {"a": c(health="healthy")}, pend, alrt)
    r2 = kinds(ev)
    checks["restart-recovery: ->starting->healthy fires once, no double"] = (
        r1 == [("recovered", "a")] and r2 == [] and alrt == set())

    # pending-only blip that leaves unhealthy (never confirmed) stays silent
    ev, pend, alrt = he({"a": c(health="healthy")}, {"a": c(health="unhealthy")}, {}, set())
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {"a": c(health="starting")}, pend, alrt)
    checks["restart-recovery: unconfirmed blip stays silent"] = (
        ev == [] and pend == {} and alrt == set())

    # (c*) prune: an alerted name absent from cur is dropped (no stuck flag)
    ev, pend, alrt = he({"a": c(health="unhealthy")}, {}, {}, {"a"})
    checks["prune: vanished container drops stuck flag"] = pend == {} and alrt == set()

    # ── unhealthy-at-baseline / stopped-container false alert (#2 @famewolf) ──
    # (BUG 1) a RUNNING container unhealthy at both passes but never observed
    # flipping from healthy (empty pending) must NOT alert — it was already
    # unhealthy when the baseline was built.
    ev, pend, alrt = he({"a": c(health="unhealthy")},
                        {"a": c(health="unhealthy")}, {}, set())
    checks["running unhealthy-at-baseline: no spurious alert"] = (
        ev == [] and alrt == set())

    # (BUG 2) a STOPPED container frozen at unhealthy (its health field is a
    # stale value from its last run) must never produce a health event, and
    # must not linger in pending/alerted.
    ev, pend, alrt = he({"a": c(status="exited", health="unhealthy")},
                        {"a": c(status="exited", health="unhealthy")}, {}, set())
    checks["stopped frozen-unhealthy: no event ever"] = (
        ev == [] and pend == {} and alrt == set())
    # even if it somehow arrived carrying flags, they get cleared silently
    ev, pend, alrt = he({"a": c(status="exited", health="unhealthy")},
                        {"a": c(status="exited", health="unhealthy")},
                        {"a": "healthy"}, {"a"})
    checks["stopped frozen-unhealthy: stale flags cleared, no event"] = (
        ev == [] and pend == {} and alrt == set())

    # genuine transition still fires exactly once, with prev="healthy"
    ev, pend, alrt = he({"a": c(health="healthy")},
                        {"a": c(health="unhealthy")}, {}, set())
    checks["genuine flip: pending, silent"] = ev == [] and pend == {"a": "healthy"}
    ev, pend, alrt = he({"a": c(health="unhealthy")},
                        {"a": c(health="unhealthy")}, pend, alrt)
    checks["genuine flip: confirms once with prev=healthy"] = (
        ev == [("unhealthy", "a", {"prev": "healthy"})] and alrt == {"a"})
    ev, pend, alrt = he({"a": c(health="unhealthy")},
                        {"a": c(health="unhealthy")}, pend, alrt)
    checks["genuine flip: does not re-fire"] = ev == []

    # an alerted RUNNING container that STOPS (->exited): no "recovered" event,
    # flag cleared silently (stopping is not recovering; diff() reports the exit)
    ev, pend, alrt = he({"a": c(health="unhealthy")},
                        {"a": c(status="exited", health="unhealthy")}, {}, {"a"})
    checks["alerted then stops: no recovered event, flag cleared"] = (
        ev == [] and alrt == set() and pend == {})

    # invariant sweep: run every health scenario above and assert no emitted
    # unhealthy event ever carries prev == "unhealthy".
    _scenarios = [
        ({"a": c(health="unhealthy")}, {"a": c(health="unhealthy")}, {}, set()),
        ({"a": c(health="healthy")}, {"a": c(health="unhealthy")}, {}, set()),
        ({"a": c(health="unhealthy")}, {"a": c(health="unhealthy")},
         {"a": "healthy"}, set()),
        ({"a": c(status="exited", health="unhealthy")},
         {"a": c(status="exited", health="unhealthy")}, {}, set()),
    ]
    _bad_prev = False
    for _p, _cu, _pe, _al in _scenarios:
        _ev, _, _ = he(_p, _cu, _pe, _al)
        for _k, _n, _d in _ev:
            if _k == "unhealthy" and _d.get("prev") == "unhealthy":
                _bad_prev = True
    checks["no unhealthy event ever has prev=='unhealthy'"] = not _bad_prev

    # through tick(): a container already unhealthy when the baseline is built
    # (and still unhealthy after) never fires — the reproduced #2 bug.
    mbz = make_monitor()
    mbz.snapshot = lambda: {"a": c(health="unhealthy")}
    mbz.tick()                                    # silent baseline, unhealthy
    mbz.snapshot = lambda: {"a": c(health="unhealthy")}
    checks["tick: unhealthy-at-baseline never alerts"] = mbz.tick() == []
    mbz.snapshot = lambda: {"a": c(health="unhealthy")}
    checks["tick: still-unhealthy-at-baseline still silent"] = mbz.tick() == []

    # through tick(): a stopped container frozen at unhealthy never alerts
    msz = make_monitor()
    msz.snapshot = lambda: {"a": c(status="exited", health="unhealthy")}
    msz.tick()                                    # baseline
    msz.snapshot = lambda: {"a": c(status="exited", health="unhealthy")}
    checks["tick: stopped frozen-unhealthy never alerts"] = (
        msz.tick() == [] and "a" not in msz._alerted_unhealthy
        and "a" not in msz._health_pending)

    # ── exits ──
    ev = diff({"a": c()}, {"a": c(status="exited", code=0)})
    checks["zero exit is silent"] = ev == []
    ev = diff({"a": c()}, {"a": c(status="exited", code=137)})
    checks["non-zero exit fires with code"] = ev == [("exited", "a", {"code": 137})]
    ev = diff({"a": c()}, {"a": c(status="exited", code=137, oom=True)})
    checks["oom exit fires as oom"] = kinds(ev) == [("oom", "a")]

    # ── crash + auto-restart ──
    ev = diff({"a": c(restarts=2)}, {"a": c(restarts=3)})
    checks["restart count increase fires"] = (
        kinds(ev) == [("crash_restart", "a")] and ev[0][2]["count"] == 3)
    ev = diff({"a": c(restarts=3)}, {"a": c(restarts=0)})
    checks["count reset (recreate) is silent"] = ev == []

    # crash loop caught mid-backoff (#2, @NotRetarded): the container is almost
    # never sampled "running" — it sits in restart-backoff. Detection must key
    # off the RestartCount climb, not the status.
    # (a) caught as "restarting" (Docker) with the count up -> crash_restart.
    #     This is the previously-MISSED case.
    ev = diff({"a": c(status="restarting", restarts=4)},
              {"a": c(status="restarting", restarts=5)})
    checks["crash loop caught 'restarting' fires crash_restart"] = (
        kinds(ev) == [("crash_restart", "a")] and ev[0][2]["count"] == 5)
    # (b) caught as "exited" (Podman between attempts) with the count up ->
    #     crash_restart, NOT a plain "exited", because the count climbed.
    ev = diff({"a": c(status="exited", restarts=4)},
              {"a": c(status="exited", code=1, restarts=5)})
    checks["crash loop caught 'exited' + count up fires crash_restart"] = (
        kinds(ev) == [("crash_restart", "a")] and ev[0][2]["count"] == 5)
    # ── what the crash-restart alert has to CARRY ──
    # A crash-restart that only says "restart #1" is unreadable next to a
    # shutdown someone did by hand: it names no exit code and no time, so
    # the reader can't tell whether it describes their own action or
    # something that happened on its own. @famewolf spent an exchange on
    # exactly that. The exit code is the DYING run's — by the time we
    # sample, the container is up again and its own code is 0.
    ev = diff({"a": c(restarts=2, code=1)},
              {"a": c(restarts=3, started_at="2026-08-01T16:19:25.123456789Z")})
    detail = ev[0][2]
    checks["crash alert carries the exit code of the run that died"] = (
        detail["code"] == 1)
    checks["crash alert carries the restart time"] = detail["when"] == "16:19:25"
    checks["crash alert still carries the count"] = detail["count"] == 3
    # Docker not giving us a timestamp must not put junk in the message.
    ev = diff({"a": c(restarts=2, code=1)}, {"a": c(restarts=3)})
    checks["a missing timestamp degrades to empty, not to garbage"] = (
        ev[0][2]["when"] == "")

    # a genuine one-shot non-zero exit with the count UNCHANGED still fires the
    # plain "exited" (not reclassified as a crash loop).
    ev = diff({"a": c(status="running", restarts=2)},
              {"a": c(status="exited", code=1, restarts=2)})
    checks["one-shot exit, count unchanged, fires exited"] = (
        ev == [("exited", "a", {"code": 1})])
    # OOM precedence holds when the count is up: oom, not crash_restart.
    ev = diff({"a": c(restarts=2)},
              {"a": c(status="restarting", code=137, oom=True, restarts=3)})
    checks["oom precedence over crash_restart when count up"] = (
        kinds(ev) == [("oom", "a")])

    # ── population changes ──
    ev = diff({"a": c()}, {})
    checks["vanished container is silent"] = ev == []
    ev = diff({}, {"a": c(status="exited", code=1)})
    checks["new container is baselined silently"] = ev == []

    # ── tick machinery ──
    m = make_monitor()
    m.snapshot = lambda: {"a": c(health="healthy"), "docksentry": c()}
    checks["first tick: silent baseline"] = m.tick() == []
    # exits stay IMMEDIATE; the health flip is only pending on this pass
    m.snapshot = lambda: {"a": c(health="unhealthy"), "docksentry": c(status="exited", code=1)}
    sent = m.tick()
    checks["health flip pending: no notify yet"] = sent == []
    checks["own container ignored"] = all(n != "docksentry" for _, n in kinds(sent))
    # still unhealthy next pass -> confirmed alert fires now
    m.snapshot = lambda: {"a": c(health="unhealthy")}
    sent = m.tick()
    checks["confirmed unhealthy notifies on next pass"] = kinds(sent) == [("unhealthy", "a")]
    checks["message rendered via i18n key"] = m.sent == ["monitor_unhealthy"]

    # a confirmed unhealthy that recovers fires exactly one recovery
    m.snapshot = lambda: {"a": c(health="healthy")}
    sent = m.tick()
    checks["recovery of confirmed notifies once"] = kinds(sent) == [("recovered", "a")]

    # a one-pass flap through tick() produces ZERO notifications AND ZERO events
    mf = make_monitor()
    mf.snapshot = lambda: {"a": c(health="healthy")}
    mf.tick()                                    # baseline
    mf.snapshot = lambda: {"a": c(health="unhealthy")}
    f1 = mf.tick()                               # pending, silent
    mf.snapshot = lambda: {"a": c(health="healthy")}
    f2 = mf.tick()                               # resolved before confirm
    checks["one-pass flap: zero notifications"] = f1 == [] and f2 == [] and mf.sent == []

    # a confirmed-unhealthy container that VANISHES then reappears can alert
    # again through tick() — the prune must clear its stuck alerted flag.
    mv = make_monitor()
    mv.snapshot = lambda: {"a": c(health="healthy")}
    mv.tick()                                    # baseline
    mv.snapshot = lambda: {"a": c(health="unhealthy")}
    mv.tick()                                    # pending
    mv.snapshot = lambda: {"a": c(health="unhealthy")}
    checks["vanish/reappear: first episode alerts"] = kinds(mv.tick()) == [("unhealthy", "a")]
    mv.snapshot = lambda: {}                      # container removed
    mv.tick()
    checks["vanish/reappear: flag pruned on vanish"] = "a" not in mv._alerted_unhealthy
    mv._last_sent = {}                            # isolate the flag from cooldown
    mv.snapshot = lambda: {"a": c(health="healthy")}
    mv.tick()                                    # recreated, re-baselined healthy
    mv.snapshot = lambda: {"a": c(health="unhealthy")}
    mv.tick()                                    # pending again
    mv.snapshot = lambda: {"a": c(health="unhealthy")}
    checks["vanish/reappear: second episode alerts again"] = (
        kinds(mv.tick()) == [("unhealthy", "a")])

    # exits / OOM / crash_restart still fire IMMEDIATELY (unchanged)
    mi = make_monitor()
    mi.snapshot = lambda: {"a": c(), "b": c(), "d": c(restarts=1)}
    mi.tick()
    mi.snapshot = lambda: {"a": c(status="exited", code=1),
                           "b": c(status="exited", code=137, oom=True),
                           "d": c(restarts=2)}
    imm = kinds(mi.tick())
    checks["exit fires immediately"] = ("exited", "a") in imm
    checks["oom fires immediately"] = ("oom", "b") in imm
    checks["crash_restart fires immediately"] = ("crash_restart", "d") in imm

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

    # ── label opt-out (confirmed unhealthy still suppressed) ──
    m3 = make_monitor()
    lab = {"docksentry.monitor": "false"}
    m3.snapshot = lambda: {"a": c(health="healthy", labels=lab)}
    m3.tick()
    m3.snapshot = lambda: {"a": c(health="unhealthy", labels=lab)}
    m3.tick()
    checks["docksentry.monitor=false opts out"] = m3.tick() == []

    # ── exclude list (confirmed unhealthy still suppressed) ──
    m4 = make_monitor(exclude=["a"])
    m4.snapshot = lambda: {"a": c(health="healthy")}
    m4.tick()
    m4.snapshot = lambda: {"a": c(health="unhealthy")}
    m4.tick()
    checks["exclude_containers honored"] = m4.tick() == []

    # ── log tail + OOM memory snapshot in notifications (v1.49.0) ──
    m7 = make_monitor()
    m7.checker._tail_logs = lambda name, lines=10: "boom line 1\nboom line 2"
    m7._memory_snapshot = lambda top=3: "sencho 5.1GiB · unifi 1.9GiB"
    m7.snapshot = lambda: {"a": c()}
    m7.tick()
    m7.snapshot = lambda: {"a": c(status="exited", code=137, oom=True)}
    m7.tick()
    full = m7.sent[-1]
    checks["oom msg carries memory snapshot"] = "monitor_top_memory" in full
    checks["oom msg carries log tail"] = "boom line 1" in full
    m7._last_sent = {}
    m7.snapshot = lambda: {"a": c(health="unhealthy")}
    # confirmed unhealthy (pending flip observed last pass, still unhealthy,
    # running) -> fires
    m7._prev = {"a": c(health="unhealthy")}
    m7._health_pending = {"a": "healthy"}
    m7._alerted_unhealthy = set()
    m7.tick()
    checks["unhealthy msg carries tail, no snapshot"] = (
        "boom line 1" in m7.sent[-1] and "monitor_top_memory" not in m7.sent[-1])
    # recovery of a confirmed unhealthy -> fires clean
    m7._prev = {"a": c(health="unhealthy")}
    m7._alerted_unhealthy = {"a"}
    m7.snapshot = lambda: {"a": c(health="healthy")}
    m7.tick()
    checks["recovery msg stays clean"] = ("boom" not in m7.sent[-1]
                                          and m7.sent[-1].startswith("monitor_recovered"))
    # tail failure must never break notify
    m8 = make_monitor()
    def _boom(name, lines=10):
        raise RuntimeError("no logs")
    m8.checker._tail_logs = _boom
    m8.snapshot = lambda: {"a": c()}
    m8.tick()
    m8.snapshot = lambda: {"a": c(status="exited", code=1)}
    checks["tail failure doesn't break notify"] = len(m8.tick()) == 1

    # ── _mem_to_bytes parsing ──
    mb = ContainerMonitor._mem_to_bytes
    checks["mem: GiB"] = mb("1.5GiB") == 1.5 * 1024**3
    checks["mem: MiB"] = mb("412MiB") == 412 * 1024**2
    checks["mem: decimal MB"] = mb("820MB") == 820 * 1000**2
    checks["mem: plain bytes"] = mb("512B") == 512
    checks["mem: garbage -> 0"] = mb("n/a") == 0 and mb("") == 0

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
