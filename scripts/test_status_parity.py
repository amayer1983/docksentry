#!/usr/bin/env python3
"""One /status, assembled once, rendered per front end (#2).

@NotRetarded put Discord's and Telegram's `/status` side by side:
Discord was missing Docksentry itself, missing health, missing uptime,
and showed `:latest` where a version would mean something. The owner's
diagnosis of the cause beat the report: he assumed a reply was built
once and then sent per connection — true for notifications since
`announce()`, and false for command replies, where each front end kept
its own assembly. Two assemblies is drift, by construction.

So the detail view is one collector and one renderer now, and the only
thing a front end may choose is its bold marker.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import status_render  # noqa: E402

checks = {}

STATE = {
    "name": "ollama", "state": "running", "health": "unhealthy",
    "uptime": "3h 12m", "image": "ollama/ollama:latest",
    "version": "v0.32.14", "short_id": "abc123def456",
    "ports": "11434→11434", "volumes": "1 mount",
    "restart_policy": "unless-stopped", "running": True, "exit_code": 0,
}


class Store:
    def get_pinned(self): return ["ollama"]
    def get_autoupdate(self): return []
    def get_protect_stop(self): return ["ollama"]
    def get_trust_running(self): return []
    def get_ask_before_major(self): return []
    def get_groups(self): return {"ai": {"name": "ai", "containers": ["ollama"]}}
    def get_notes(self): return {"ollama": "GPU box"}


info = status_render.collect(
    "ollama", STATE,
    stats=("312.4%", "8.2GiB / 61.3GiB", "1.2MB / 800kB", "5.1MB / 0B"),
    disk={"image_bytes": 73400000, "image_created": "2026-08-01",
          "layer_bytes": 2449357},
    store=Store(), pending=True,
    probe="exit 1: nvidia-smi: not found")

tg = status_render.lines(info, bold="*", host_tag=" @qnap")
dc = status_render.lines(info, bold="**", host_tag=" @qnap")

# ── the same content, by construction ────────────────────────────────
checks["both front ends get the same number of lines"] = len(tg) == len(dc)
checks["…and identical content once bold is stripped"] = (
    [l.replace("**", "").replace("*", "") for l in tg]
    == [l.replace("**", "").replace("*", "") for l in dc])

joined = " ".join(tg)
# ── everything he asked for is in it ─────────────────────────────────
checks["state and health"] = "running (unhealthy)" in joined
# The stanza layout, after the owner's "übersichtlicher?": the header
# carries state and uptime, groups are separated by blank lines, and a
# duplicate port mapping (tcp+udp rendering identically) shows once.
checks["the header leads with name, state and uptime"] = (
    tg[0].startswith("🩺") and "3h 12m" in tg[0])
checks["stanzas are separated by blank lines"] = tg.count("") >= 3
checks["uptime"] = "3h 12m" in joined
checks["live CPU and memory"] = "312.4%" in joined and "8.2GiB" in joined
checks["net and disk I/O ride along"] = (
    "1.2MB" in joined and "800kB" in joined and "5.1MB" in joined)
# A runtime that only answers two fields still yields the two that
# matter — Podman's stats output is leaner than Docker's.
two = status_render.collect("x", STATE, stats=("1%", "10MiB / 1GiB"))
checks["two-field stats still work"] = (
    two.get("cpu") == "1%" and "net_io" not in two)
checks["the version, not just the latest tag"] = "v0.32.14" in joined
checks["what the probe said, when unhealthy"] = "nvidia-smi: not found" in joined
checks["Docksentry's own flags"] = "pinned" in joined and "protected" in joined
checks["group and note"] = "`ai`" in joined and "GPU box" in joined
checks["a pending update is announced"] = "Update available" in joined
# NotRetarded's icon pass (#63): the header stethoscope, a bar chart on
# the live-cost line, and a network glyph on the ports line — the plug
# was near-invisible on Discord's dark theme. Pinned so a refactor does
# not quietly walk them back.
checks["ports carry a visible glyph, not the washed-out plug"] = (
    "🌐" in joined and "🔌" not in joined)
checks["the live-cost line uses the bar chart"] = "📊 CPU" in joined

# ── a stopped container tells you how it died ────────────────────────
stopped = status_render.collect("dead", dict(STATE, running=False,
                                             state="exited", health="",
                                             uptime="", exit_code=137))
sj = " ".join(status_render.lines(stopped))
checks["a stopped container shows its exit code"] = "137" in sj
checks["…and no uptime or load for something not running"] = (
    "⏱" not in sj and "CPU" not in sj)

# ── the overview line ────────────────────────────────────────────────
ov = status_render.overview_line("ollama", STATE)
checks["the overview leads with a health icon"] = ov.startswith("🔴")
checks["…names the version next to the image"] = "(v0.32.14)" in ov
checks["…and carries the uptime"] = "3h 12m" in ov
ov2 = status_render.overview_line("db", {"running": True, "health": "healthy",
                                         "image": "postgres:16",
                                         "uptime": "9d 1h"})
checks["healthy is green"] = ov2.startswith("🟢")
checks["…and an unversioned image shows no empty parens"] = "()" not in ov2

# ── missing pieces cost lines, not crashes ───────────────────────────
bare = status_render.collect("x", {"state": "running", "running": True,
                                   "image": "img"})
checks["no store, no stats, no problem"] = (
    len(status_render.lines(bare)) >= 2)

# ── the self-filter stays where it protects and leaves where it hid ──
from update_checker import UpdateChecker  # noqa: E402


def uc(rows):
    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(debug=False, exclude_containers=[],
                                     pinned_file="/nonexistent")
    ps = "\n".join(f"{n}|{i}" for n, i in rows)
    c._backend = types.SimpleNamespace(run=lambda args, timeout=None:
        types.SimpleNamespace(returncode=0, stdout=ps, stderr=""))
    c._trace = lambda *a, **k: None
    c._debug = lambda *a, **k: None
    c._own_container_name = lambda: "docksentry"
    c._labels_for = lambda names: {}
    c._get_pinned = lambda: []
    return c


rows = [("docksentry", "docksentry:latest"), ("nginx", "nginx:1")]
names_default = [c["name"] for c in uc(rows).get_running_containers()]
names_reader = [c["name"] for c in
                uc(rows).get_running_containers(include_self=True)]
checks["the update path still skips ourselves (PID 1)"] = (
    "docksentry" not in names_default)
checks["a reader sees the whole truth, ourselves included"] = (
    "docksentry" in names_reader)

# ── and both front ends actually use the shared pieces ───────────────
tsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "telegram_bot.py"), encoding="utf-8").read()
dsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "discord_bot.py"), encoding="utf-8").read()
checks["Telegram renders through status_render"] = (
    "status_render.lines(detail" in tsrc)
checks["…and Discord too"] = "status_render.lines(" in dsrc
checks["Discord's overview includes ourselves"] = (
    "include_self=True" in dsrc)
checks["…via the shared overview line"] = "overview_line" in dsrc

# Port dedupe and the I/O direction markers.
dup = status_render.collect("x", dict(STATE,
    ports="3001→3000, 53→53, 53→53, 8082→80"))
dj = " ".join(status_render.lines(dup))
checks["a tcp+udp double mapping shows once"] = dj.count("53→53") == 1
checks["net I/O carries direction arrows"] = "↓1.2MB ↑800kB" in joined
checks["disk I/O says read and write"] = "R 5.1MB · W 0B" in joined
# The disk facts the owner asked for — the two that cost milliseconds.
# Volume sizes are deliberately absent: Docker only surfaces them via
# `system df -v`, which walks every volume on the host (~7 s measured).
checks["the image states its size"] = "73.4MB" in joined
checks["…and its age"] = "built 2026-08-01" in joined
checks["the writable layer is named"] = "+2.45MB layer" in joined
checks["a zero layer is shown too — 0B is an answer, not noise"] = (
    "+0B layer" in " ".join(status_render.lines(
        status_render.collect("x", STATE, disk={"layer_bytes": 0}))))
nodisk = status_render.collect("x", STATE)
checks["no disk facts, no disk lines"] = "layer" not in " ".join(
    status_render.lines(nodisk))
# Matched as an argv token, not as prose — the docstring explaining WHY
# we skip `system df -v` contains the phrase, and the first version of
# this check failed on the explanation of the very rule it enforces.
checks["volume sizes are not fetched anywhere"] = (
    '"df"' not in tsrc and '"df"' not in dsrc)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
