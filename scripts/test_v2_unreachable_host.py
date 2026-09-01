#!/usr/bin/env python3
"""One unreachable host must not take the whole V2 status page down.

`_host_views` appends `{"unreachable": name, ...}` for a host it could not
reach — deliberately, so the status table can show it as a line instead of
failing. `web_v2` read `view["host"]` unconditionally and raised KeyError,
which killed `/api/v2/status` outright. The V2 page polls that endpoint
every 30 seconds, so on a multi-host install with one host down — the
normal case, not the exotic one — the page simply never filled in.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_v2  # noqa: E402

checks = {}


def _live(name, containers):
    return {"host": name, "containers": containers, "pending_names": [],
            "pinned": [], "auto_list": [], "groups": {}, "notes": {},
            "links": {}, "advisories": {}, "updating": {}, "own": ""}


DEAD = {"unreachable": "nas", "endpoint": "tcp://10.0.0.5:2375",
        "reason": "connection refused"}
ONE = [{"name": "nginx", "image": "nginx:1", "health": "", "state": "running",
        "labels": {}, "short_id": "abc", "version": ""}]


def _payload(views):
    return web_v2.payload(views, lambda h, n: f"{h}/{n}")


out = _payload([_live("local", ONE), DEAD])
checks["a dead host does not raise"] = True
checks["the live host's rows still come through"] = (
    len(out["containers"]) == 1 and out["containers"][0]["name"] == "nginx")
checks["the dead host is still named"] = (
    any(h["name"] == "nas" for h in out["hosts"]))
checks["…and marked, not shown as an empty host"] = (
    any(h["name"] == "nas" and h.get("unreachable") for h in out["hosts"]))
checks["it carries no phantom containers"] = (
    all(h["containers"] == 0 for h in out["hosts"] if h["name"] == "nas"))
checks["the count counts only real rows"] = (out["stats"]["containers"] == 1)

# Every host down is the same answer, not a different failure.
out = _payload([DEAD])
checks["all hosts down still answers"] = (
    out["containers"] == [] and len(out["hosts"]) == 1)

# The client has to be able to tell them apart without a translation.
_js = open(os.path.join(os.path.dirname(__file__), "..",
                        "app", "static", "v2.js")).read()
checks["the host picker marks it too"] = ("unreachable" in _js)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
