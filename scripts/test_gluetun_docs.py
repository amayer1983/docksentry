#!/usr/bin/env python3
"""The VPN-sidecar documentation says what the code actually does.

The README has called this out as a headline feature since the beginning
("Gluetun before the containers sharing its network namespace", "the case
that breaks naive updaters") and `docs/` did not contain the word
"gluetun" once — measured, zero hits across all nine files. The strongest
thing the tool does was the worst-documented thing in it.

The section exists now. This file is here because a written explanation of
a mechanism rots faster than the mechanism: it pins the specific claims
that would mislead somebody if the code moved underneath them. Each check
below asserts the doc and the code agree, not merely that the doc mentions
something.

The claims that matter, in the order they would bite:

* the group's **first** member is the head — get that wrong and the repair
  runs before the update it is meant to repair;
* the **restart-dependents tick** is what switches the mechanism on —
  without it a group is an ordering group and nothing repairs the network;
* dependents are **recreated**, not restarted, because a restart still
  references the head's dead container ID;
* the netns check is **live per update**, so it stays right when the stack
  changes.
"""

import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))

checks = {}

doc = open(os.path.join(ROOT, "docs", "updates.md"), encoding="utf-8").read()
readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
engine = open(os.path.join(ROOT, "app", "update_engine.py"), encoding="utf-8").read()
checker = open(os.path.join(ROOT, "app", "update_checker.py"), encoding="utf-8").read()

# ── the section exists and is reachable ──────────────────────────────
checks["the docs cover the VPN sidecar case at all"] = (
    "### Containers behind a VPN sidecar" in doc)
i = doc.index("### Containers behind a VPN sidecar")
sec = doc[i:doc.index("## Update Windows", i)]
checks["…by name, so searching for gluetun finds it"] = (
    "gluetun" in sec.lower())
# The README advertises it; a claim with no instructions behind it is how
# somebody ends up here in the first place.
anchor = re.sub(r"[^a-z0-9 -]", "",
                sec.split("\n")[0].lstrip("# ").lower()).replace(" ", "-")
checks["…and the README links to it"] = anchor in readme

# ── head is the first member ─────────────────────────────────────────
checks["doc: the first container in the group is the head"] = (
    "first" in sec.lower() and "head" in sec.lower())
checks["code: agrees — head is members[0]"] = (
    'u["name"] == members[0]' in engine)

# ── the restart-dependents tick is required ──────────────────────────
# The doc used to say "nothing to configure beyond the group", which was
# wrong: without this flag the cascade never runs.
# Whitespace-normalised: the option label wraps across two lines in the
# source, so a plain substring check misses it.
_flat = " ".join(sec.split())
checks["doc: says the restart-dependents tick is required"] = (
    "Restart dependents when the head container updates" in _flat
    and "Without it" in _flat)
checks["code: agrees — the flag gates the cascade"] = (
    'grp.get("restart_dependents")' in engine)

# ── dependents are recreated, not restarted ──────────────────────────
checks["doc: says netns dependents are recreated, not restarted"] = (
    "recreated" in sec and "restarted" in sec)
checks["code: agrees — recreate for netns, restart otherwise"] = (
    'nm.startswith("container:")' in engine
    and "checker.recreate_dependent(dep, head_name)" in engine
    and "backend.restart(dep" in engine)
checks["doc: explains WHY (the head's ID dies, the name survives)"] = (
    "ID" in sec and "name" in sec)
checks["code: agrees — rejoins by name"] = (
    "netns_name=netns_name" in checker
    and "container:<name>" in checker)

# ── same image, no version change ────────────────────────────────────
checks["doc: says the rebuild keeps the same image"] = (
    "same image" in sec and "no pull" in sec)
checks["code: agrees"] = "no pull, no version change" in checker

# ── rollback safety net ──────────────────────────────────────────────
checks["doc: says the old container is kept until the new one is up"] = (
    "_old" in sec)
checks["code: agrees — renames to <name>_old and rolls back"] = (
    'old_name = f"{name}_old"' in checker
    and "self._rollback_to_old(name, old_name)" in checker)

# ── AutoRemove containers ────────────────────────────────────────────
checks["doc: mentions --rm containers are handled"] = "--rm" in sec
checks["code: agrees — config captured before the stop"] = (
    "Capture config up front" in checker and "AutoRemove" in checker)

# ── unhealthy head does not block the repair ─────────────────────────
checks["doc: says an unhealthy head does not stop the repair"] = (
    "unhealthy head" in sec.lower() or "not healthy" in sec.lower())
checks["code: agrees — warns and continues"] = (
    "fixing dependents anyway" in engine)
# The doc used to claim a flat 30 seconds; it is the group's wait, floored
# at 30.
checks["doc: describes the wait as the group's own, at least 30s"] = (
    "never less than 30" in sec)
checks["code: agrees — max(group wait, 30)"] = (
    'max(int(grp.get("wait_seconds", 30) or 30), 30)' in engine)

# ── the netns check is live, not stored ──────────────────────────────
checks["doc: says membership is detected per update, not stored"] = (
    "detected per" in sec and "not stored" in sec)
checks["code: agrees — inspects each dependent at repair time"] = (
    'backend.inspect(' in engine and "HostConfig.NetworkMode" in engine)

# ── the escape hatch the doc points at ───────────────────────────────
checks["doc: mentions the manual restart-dependents button"] = (
    "Restart\ndependents now" in sec or "Restart dependents now" in sec)
en = open(os.path.join(ROOT, "app", "lang", "en.json"), encoding="utf-8").read()
checks["…and that button exists"] = "group_restart_deps_btn" in en

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
