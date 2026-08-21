#!/usr/bin/env python3
"""Container facts live in the neutral core, not in a front end (#63).

Second step of the core extraction: `container_info.py` reads a
container's state, stats and disk facts, and both bots call it as equals.
Discord used to reach into the Telegram bot instance for all three
(`bot._container_state`, `bot._container_stats`, `bot._disk_facts`) —
that coupling is what this pins gone.
"""
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}

import container_info  # noqa: E402
for fn in ("state", "stats", "disk_facts"):
    checks[f"container_info.{fn} exists"] = callable(getattr(container_info, fn, None))

tsrc = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
for gone in ("_container_state", "_container_stats", "_disk_facts"):
    checks[f"telegram_bot no longer defines {gone}"] = (
        f"def {gone}" not in tsrc)
checks["…and calls the core instead"] = (
    "container_info.state(" in tsrc and "container_info.stats(" in tsrc
    and "container_info.disk_facts(" in tsrc)

dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
checks["Discord calls the core"] = "container_info.state(" in dsrc
checks["…and stops borrowing it from the Telegram bot"] = (
    "bot._container_state" not in dsrc
    and "bot._container_stats" not in dsrc
    and "bot._disk_facts" not in dsrc)

# ── behavioural: state() parses an inspect against any backend ───────
INSPECT = (
    '[{"Name":"/web","State":{"Status":"running","Running":true,'
    '"StartedAt":"2020-01-01T00:00:00Z","ExitCode":0},'
    '"Config":{"Image":"nginx:1"},"Image":"sha256:abcdef0123456789",'
    '"HostConfig":{"RestartPolicy":{"Name":"unless-stopped"}},"Mounts":[]}]')

class Backend:
    def run(self, argv, timeout=None):
        if argv[:1] == ["inspect"]:
            return types.SimpleNamespace(returncode=0, stdout=INSPECT, stderr="")
        if argv[:2] == ["image", "inspect"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

st = container_info.state(Backend(), "web")
checks["state() returns a dict from a backend it was handed"] = (
    isinstance(st, dict) and st.get("running") is True
    and st.get("restart_policy") == "unless-stopped"
    and st.get("image") == "nginx:1")
checks["…with no dependency on any bot instance"] = (
    st.get("short_id") == "abcdef012345")

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
