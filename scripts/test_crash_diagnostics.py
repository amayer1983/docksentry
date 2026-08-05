#!/usr/bin/env python3
"""What a crash alert has to answer, and the clock it says it in (#2).

Three findings from @NotRetarded, reading his own alert back to me.

**The clock was UTC.** Docker stamps StartedAt with a trailing Z, and
`_clock` sliced the time out and printed it unchanged. His crash at 23:29
local arrived as `03:29:20` — exactly his UTC-4 offset. Worse than the
offset itself: the same alert carried two clocks in two zones, because the
event log's timestamp comes from `datetime.now()` and is local.

**The victim was missing from its own alert.** The top-N list frequently
does not contain the container that died — it released everything on the
way out. His Unifi normally sits at 1.5-1.7 GB, is the biggest thing on
that box, and appeared nowhere in the list. Reading that, you cannot tell
whether it had grown or had already gone.

**Exit 137 alone does not say what killed it.** With the kernel's OOM flag
it was memory; without it, something else — and that is the most useful
single bit in the message. It is deliberately three-valued: a bare "no"
that actually meant "we never looked" would send him hunting in the wrong
direction, so it is only stated when the event stream was there to see it.
`inspect` is not consulted for it — it reports the CURRENT run of a
restarted container, and it was measured false on rootless Podman for a
container the kernel really did kill.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


def main():
    checks = {}

    # ── the clock ────────────────────────────────────────────────
    # Set TZ before importing, and re-read it per case: time.tzset applies
    # to the process, and _clock uses astimezone() which honours it.
    import time as _time
    import importlib

    def clock_in(tz, iso):
        os.environ["TZ"] = tz
        _time.tzset()
        import monitor
        importlib.reload(monitor)
        return monitor._clock(iso)

    UTC_STAMP = "2026-08-05T03:29:20.123456789Z"
    checks["his 23:29 reads as 23:29, not 03:29"] = (
        clock_in("America/New_York", UTC_STAMP) == "23:29:20")
    checks["the same instant is 05:29 in Berlin"] = (
        clock_in("Europe/Berlin", UTC_STAMP) == "05:29:20")
    checks["UTC stays itself"] = clock_in("UTC", UTC_STAMP) == "03:29:20"

    # Shapes Docker and Podman actually emit.
    checks["no fractional part"] = (
        clock_in("Europe/Berlin", "2026-08-05T03:29:20Z") == "05:29:20")
    checks["an explicit offset is honoured"] = (
        clock_in("Europe/Berlin", "2026-08-05T05:29:20+02:00") == "05:29:20")
    # Podman writes microseconds, Docker nanoseconds; neither may throw.
    checks["microseconds parse"] = (
        clock_in("UTC", "2026-08-05T03:29:20.123456Z") == "03:29:20")
    # Nothing here may cost the alert its timestamp entirely.
    os.environ["TZ"] = "UTC"
    _time.tzset()
    import monitor
    importlib.reload(monitor)
    checks["rubbish yields empty, not a crash"] = monitor._clock("nonsense") == ""
    checks["None yields empty"] = monitor._clock(None) == ""
    checks["a bare date yields empty"] = monitor._clock("2026-08-05") == ""

    from monitor import ContainerMonitor

    # ── the victim's own usage ───────────────────────────────────
    m = ContainerMonitor.__new__(ContainerMonitor)
    m._snap_cache = [
        (1_750_000_000, 8.0, "Unifi-OS-Server", "1.63GiB"),
        (265_000_000, 2.0, "Dockge", "253.2MiB"),
    ]
    checks["the victim's own line is found"] = (
        m._own_usage("Unifi-OS-Server") == "1.63GiB · 8%")
    # A remote container arrives here prefixed with its host.
    checks["a host prefix is stripped for the lookup"] = (
        m._own_usage("dockmox/Unifi-OS-Server") == "1.63GiB · 8%")
    # Already gone by the time stats ran — silence, not a fabricated zero.
    checks["a vanished container yields nothing"] = m._own_usage("ghost") == ""

    # ── the OOM flag, three-valued ───────────────────────────────
    def probe(watcher):
        o = ContainerMonitor.__new__(ContainerMonitor)
        o.watcher = watcher
        o._host_memory = lambda: ""
        o._host_load = lambda: ""
        o._snap_cache = []
        o._event_snapshot = lambda: {}
        return o._resources_for("crash_restart", "x")

    saw = types.SimpleNamespace(exit_code=lambda n, **k: 137,
                                saw_oom=lambda n, **k: True,
                                evidence=lambda n, **k: "")
    didnt = types.SimpleNamespace(exit_code=lambda n, **k: 137,
                                  saw_oom=lambda n, **k: False,
                                  evidence=lambda n, **k: "")
    blind = types.SimpleNamespace(exit_code=lambda n, **k: None,
                                  saw_oom=lambda n, **k: False,
                                  evidence=lambda n, **k: "")
    checks["an observed OOM says yes"] = probe(saw).get("oom_flag") == "yes"
    checks["an observed non-OOM says no"] = probe(didnt).get("oom_flag") == "no"
    # The one that matters: no evidence must not be reported as "no".
    checks["no evidence says nothing at all"] = "oom_flag" not in probe(blind)
    checks["no watcher says nothing at all"] = "oom_flag" not in probe(None)

    # ── the wording exists in every language ─────────────────────
    from i18n import available_languages, get_translator
    missing = []
    for lang in available_languages():
        t = get_translator(lang)
        for key in ("monitor_victim_usage", "monitor_oom_flag_yes",
                    "monitor_oom_flag_no"):
            out = t(key, name="c", state="1GiB")
            if out == key or "{" in out:
                missing.append(f"{lang}/{key}")
    checks["every language renders the new lines"] = not missing
    if missing:
        print(f"    missing: {', '.join(missing[:6])}")

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
