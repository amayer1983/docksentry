#!/usr/bin/env python3
"""A restore overwrites only what the bundle knew about (#2).

The trap: `restore()` replaced every list and dict wholesale. Import a
bundle taken from a single-host install — or from before a second host
existed — into a multi-host instance, and every `dock8520/` pin, note,
group and update window vanished without a word. famewolf's setup is
exactly one bad restore away from that.

The rule now: **a bundle replaces state only for hosts it speaks for;
state for hosts it never saw is kept.** Newer bundles declare their
hosts; older ones are inferred from their keys, and the inference errs
toward keeping — staleness is recoverable, a wipe is not. Kept entries
are counted and said, because a restore that silently decides what
survives is only one step better than one that silently wipes.
"""

import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import backup  # noqa: E402
from container_store import ContainerStore  # noqa: E402

checks = {}


def make_store(tmp):
    cfg = types.SimpleNamespace()
    for stem in ("pinned", "autoupdate", "update_windows",
                 "ask_before_major", "trust_running", "cooldown",
                 "protect_stop", "major_pending", "groups", "notes",
                 "links"):
        setattr(cfg, f"{stem}_file", os.path.join(tmp, f"{stem}.json"))
    return ContainerStore(cfg)


def make_config(tmp, hosts=()):
    return types.SimpleNamespace(
        settings_file=os.path.join(tmp, "settings.json"),
        docker_hosts=[{"name": h, "endpoint": f"ssh://root@{h}"}
                      for h in hosts],
        save_persistent=lambda: None)


# ── the disaster case: single-host bundle into a multi-host store ────
with tempfile.TemporaryDirectory() as tmp:
    store = make_store(tmp)
    store.save_pinned(["web", "dock8520/llama-server"])
    store.save_autoupdate(["dock8520/jellyfin"])
    store._save_dict(store.notes_file, {"dock8520/llama-server": "GPU box"})
    store._save_dict(store.groups_file, {
        "g1": {"name": "ai", "containers": ["dock8520/llama-server",
                                            "dock8520/audiomuse-worker"]},
        "g2": {"name": "mixed", "containers": ["web"]}})
    store._save_dict(store.update_windows_file, {"dock8520/jellyfin": "3-5"})

    bundle = {"pinned": ["db"], "autoupdate": [], "groups": {},
              "notes": {}, "links": {}, "update_windows": {}}
    cfg = make_config(tmp, hosts=("dock8520",))
    restored, errors, _ = backup.restore(bundle, cfg, store, [])

    checks["the bundle's own entries land"] = "db" in store.get_pinned()
    checks["the bundle's host is overwritten as asked"] = (
        "web" not in store.get_pinned())
    checks["…but the uncovered host's pins survive"] = (
        "dock8520/llama-server" in store.get_pinned())
    checks["…its auto-update list survives"] = (
        "dock8520/jellyfin" in store.get_autoupdate())
    checks["…its notes survive"] = (
        store.get_notes().get("dock8520/llama-server") == "GPU box")
    checks["…its update windows survive"] = (
        store.get_update_windows().get("dock8520/jellyfin") == "3-5")
    groups = store.get_groups()
    checks["a group living wholly on the uncovered host survives"] = (
        "g1" in groups)
    checks["a group of covered containers is the bundle's to define"] = (
        "g2" not in groups)
    checks["what was kept is said, with the host's name"] = any(
        "kept" in r and "dock8520" in r for r in restored)
    checks["nothing about the local overwrite is flagged as an error"] = (
        errors == [])

# ── a bundle that DECLARES the host may clear it ─────────────────────
with tempfile.TemporaryDirectory() as tmp:
    store = make_store(tmp)
    store.save_pinned(["dock8520/llama-server"])
    bundle = {"hosts": ["local", "dock8520"], "pinned": []}
    cfg = make_config(tmp, hosts=("dock8520",))
    restored, errors, _ = backup.restore(bundle, cfg, store, [])
    checks["a bundle that speaks for the host may clear it"] = (
        store.get_pinned() == [])
    checks["…and reports no kept entries"] = not any(
        "kept" in r for r in restored)

# ── an old bundle WITH prefixed keys covers those hosts ──────────────
with tempfile.TemporaryDirectory() as tmp:
    store = make_store(tmp)
    store.save_pinned(["dock8520/old-pin"])
    bundle = {"pinned": ["dock8520/new-pin"]}   # no hosts field
    cfg = make_config(tmp, hosts=("dock8520",))
    backup.restore(bundle, cfg, store, [])
    checks["prefixed keys in an old bundle claim their host"] = (
        store.get_pinned() == ["dock8520/new-pin"])

# ── foreign hosts: restored as data, said as a warning ───────────────
with tempfile.TemporaryDirectory() as tmp:
    store = make_store(tmp)
    bundle = {"hosts": ["local", "docknas"], "pinned": ["docknas/nginx"]}
    cfg = make_config(tmp, hosts=())    # single-host instance
    restored, errors, _ = backup.restore(bundle, cfg, store, [])
    checks["entries for an unmanaged host are restored, not dropped"] = (
        "docknas/nginx" in store.get_pinned())
    checks["…with a warning naming the host"] = any(
        "docknas" in e and "does not manage" in e for e in errors)
    checks["…that says how they become active"] = any(
        "DOCKER_HOSTS" in e for e in errors)

# ── new bundles declare their hosts ──────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    store = make_store(tmp)
    cfg = make_config(tmp, hosts=("dock8520", "docknas"))
    built = backup.build(cfg, store, "test")
    checks["a new bundle names every host it speaks for"] = (
        built["hosts"] == ["local", "dock8520", "docknas"])
    checks["…and the round trip parses"] = (
        json.loads(json.dumps(built))["hosts"][0] == "local")
    checks["bundle_hosts reads the declaration"] = (
        backup.bundle_hosts(built) == {"local", "dock8520", "docknas"})
    checks["…and infers local for a bare old bundle"] = (
        backup.bundle_hosts({"pinned": ["web"]}) == {"local"})

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
