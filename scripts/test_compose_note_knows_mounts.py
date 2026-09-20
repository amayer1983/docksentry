#!/usr/bin/env python3
"""«Mount it» is the wrong thing to say to somebody who did.

@NotRetarded's Dockmon update carried the note that
`/app/data/stacks/QNAP/dockmon/compose.yaml` is a path inside Dockge or
Dockhand and that he should mount that manager's data directory at
`/app/data/stacks`. He had been round this before — the same note for
his Dozzle stack, two exact mount lines from me on 01.09., and on 04.09.
"I'm running the latest beta. I'm not getting the detail page." His next
word on it was "seems to persist again".

The note was right that we cannot see the file and wrong about what to
do, because it never looked. It is built from the path prefix alone: any
path under a known manager's directory gets the same sentence, whether
nothing is mounted or the mount is already there and points at the wrong
folder. Our own code says so in the comment above it — "three people hit
this in one week, each concluding their own mount was wrong. It was not
— the advice was." The Web UI learned to look on 01.09.; the message
that actually reaches people did not.

So the note now asks the one question that separates the two cases: is
something already mounted that should make this path visible? Nothing →
mount it, as before. Something → say which mount we found and that the
file is not in it, which leaves exactly one thing to check.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import compose_paths                                       # noqa: E402

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")
P = "/app/data/stacks/QNAP/dockmon/compose.yaml"

# ── which mount covers a path ────────────────────────────────────────
checks["nothing mounted is nothing found"] = (
    compose_paths.covering_mount(P, []) is None)
checks["a mount above the path is found"] = (
    compose_paths.covering_mount(P, [("/share/stacks", "/app/data/stacks")])
    == ("/share/stacks", "/app/data/stacks"))
checks["…and the closest one wins"] = (
    compose_paths.covering_mount(
        P, [("/a", "/app/data"), ("/b", "/app/data/stacks")])
    == ("/b", "/app/data/stacks"))
checks["a mount beside it is not a cover"] = (
    compose_paths.covering_mount(P, [("/x", "/app/data/other")]) is None)
# A prefix that is not a path boundary must not match: `/app/data/stack`
# is a different directory from `/app/data/stacks`.
checks["a name that merely starts the same is not a cover"] = (
    compose_paths.covering_mount(P, [("/x", "/app/data/stack")]) is None)
checks["the file mounted on its own counts"] = (
    compose_paths.covering_mount(P, [("/f", P)]) == ("/f", P))
checks["a mount without a destination is ignored"] = (
    compose_paths.covering_mount(P, [("/x", "")]) is None)

# ── the note picks its words from that ───────────────────────────────
src = open(os.path.join(ROOT, "app", "update_checker.py"),
           encoding="utf-8").read()
_note = src.split("_owner = compose_paths.owner(config_file)")[1][:1400]
checks["the note asks what we already mount"] = (
    "compose_paths.covering_mount(" in _note and "self._own_mounts()" in _note)
checks["…and says so instead of 'mount it'"] = (
    'compose_fallback_mounted' in _note
    and _note.index("compose_fallback_mounted")
    < _note.index("compose_fallback_managed"))

# Our own mounts are read once, not per container in a batch.
checks["our own mounts are asked once, not per update"] = (
    "_own_mounts_cache" in src)

# ── and the wording names both sides of the problem ──────────────────
import json                                                # noqa: E402
en = json.load(open(os.path.join(ROOT, "app", "lang", "en.json"),
                    encoding="utf-8"))
msg = en.get("compose_fallback_mounted", "")
checks["the message exists"] = bool(msg)
checks["…names the file, the source and where it is mounted"] = all(
    t in msg for t in ("{file}", "{src}", "{dest}"))
checks["…and points at the source rather than repeating the advice"] = (
    "wrong directory" in msg and "Mount " not in msg)
checks["every language carries it"] = all(
    "compose_fallback_mounted" in json.load(
        open(os.path.join(ROOT, "app", "lang", f), encoding="utf-8"))
    for f in os.listdir(os.path.join(ROOT, "app", "lang"))
    if f.endswith(".json"))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
