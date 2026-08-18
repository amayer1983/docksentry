#!/usr/bin/env python3
"""A recreated container keeps its GPU (#62's neighbour).

The owner's `ollama`, deployed from a Portainer stack with a GPU, was
updated by Docksentry — and the recreate dropped `DeviceRequests`. The
NVIDIA runtime therefore never injected `nvidia-smi` into the new
container, his healthcheck probes exactly that binary, and every update
failed and rolled back, forever. The rollback was the only thing that
kept him off CPU inference.

The comment in the skip list said "may add in a future release if
requested". His server requested.

**Honesty about the test basis:** this development machine has no GPU,
so every shape here comes from Docker's documentation of what
`docker run --gpus …` writes into `HostConfig.DeviceRequests`, not from
a live NVIDIA box. The owner's ollama is the live verification, with
the rollback as the net.

The flag's value is CSV to Docker's own parser, so a field that itself
contains commas — a device list, a capability list — must arrive as a
quoted CSV field: literal double quotes inside the argv element. That
is not shell quoting (we exec without a shell); the quotes are part of
the value.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker as U  # noqa: E402

checks = {}

G = U._gpus_args

# ── the four shapes `docker run` documents ───────────────────────────
checks["--gpus all round-trips byte for byte"] = G(
    [{"Driver": "nvidia", "Count": -1, "DeviceIDs": None,
      "Capabilities": [["gpu"]], "Options": {}}]) == ["--gpus", "all"]
checks["…also when the driver field is empty"] = G(
    [{"Driver": "", "Count": -1, "Capabilities": [["gpu"]]}]) == ["--gpus", "all"]
checks["a count survives as a count"] = G(
    [{"Count": 2, "Capabilities": [["gpu"]]}]) == ["--gpus", "count=2"]
checks["one device needs no quoting"] = G(
    [{"Count": 0, "DeviceIDs": ["GPU-uuid1"], "Capabilities": [["gpu"]]}]
) == ["--gpus", "device=GPU-uuid1"]
checks["a device list is a quoted CSV field"] = G(
    [{"Count": 0, "DeviceIDs": ["0", "1"], "Capabilities": [["gpu"]]}]
) == ["--gpus", '"device=0,1"']
checks["extra capabilities are quoted and keep gpu first"] = G(
    [{"Count": -1, "Capabilities": [["gpu", "utility", "compute"]]}]
) == ["--gpus", 'count=all,"capabilities=gpu,compute,utility"']
checks["a non-nvidia driver is named"] = "driver=custom" in " ".join(G(
    [{"Driver": "custom", "Count": 1, "Capabilities": [["gpu"]]}]))

# ── what must produce nothing ────────────────────────────────────────
checks["no DeviceRequests, no flag"] = G(None) == [] and G([]) == []
checks["a malformed entry produces nothing rather than garbage"] = G(
    ["not a dict"]) == []
# `--gpus` is a single-value flag; a second request has no CLI spelling.
# Emitting half of one silently would be this bug all over again — the
# audit reports it instead.
checks["two requests are not squeezed into one flag"] = G(
    [{"Count": -1}, {"Count": 1}]) == []

# ── wired into the recreate, not just written next to it ─────────────
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "update_checker.py"), encoding="utf-8").read()
i = src.index("def _build_run_args")
body = src[i:src.index("\n    def ", i + 10)]
checks["the recreate carries the flag"] = (
    '_gpus_args(host.get("DeviceRequests"))' in body)

# The full path: an inspect dict with a GPU goes in, --gpus comes out.
inspect = {
    "Config": {"Image": "ollama/ollama", "Env": [], "Labels": {}},
    "HostConfig": {"DeviceRequests": [{"Driver": "nvidia", "Count": -1,
                                       "Capabilities": [["gpu"]]}],
                   "RestartPolicy": {"Name": "unless-stopped"},
                   "NetworkMode": "bridge", "Binds": []},
    "NetworkSettings": {"Networks": {}},
    "Name": "/ollama",
}
args = U._build_run_args(inspect, "ollama/ollama:latest", "ollama")
joined = " ".join(args)
checks["end to end: the rebuilt run has the GPU"] = "--gpus all" in joined
checks["…and still the rest of its config"] = "--restart" in joined

# ── the audit no longer keeps the skip list to itself ────────────────
checks["DeviceRequests is honoured now"] = (
    "DeviceRequests" in U._HONORED_HOSTCONFIG)
checks["…and out of the silent-skip list"] = (
    "DeviceRequests" not in U._SKIPPED_HOSTCONFIG)

c = U.__new__(U)
c.config = types.SimpleNamespace(debug=False)
c._debug = lambda *a, **k: None
findings = c._audit_inspect_coverage({
    "HostConfig": {"DeviceCgroupRules": ["c 189:* rmw"],
                   "StorageOpt": {"size": "20G"}},
    "Config": {},
})
checks["a set-but-skipped field is reported, not silent"] = (
    "DeviceCgroupRules" in (findings.get("host_dropped") or []))
checks["…all of them"] = "StorageOpt" in (findings.get("host_dropped") or [])
findings2 = c._audit_inspect_coverage({"HostConfig": {}, "Config": {}})
checks["an unset skip-field is not noise"] = not findings2.get("host_dropped")

# And both front ends render the new section.
tsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "telegram_bot.py"), encoding="utf-8").read()
dsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "discord_bot.py"), encoding="utf-8").read()
checks["Telegram's /audit shows it"] = "audit_section_dropped" in tsrc
checks["…and Discord's"] = "Skipped on purpose" in dsrc

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
