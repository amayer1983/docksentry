#!/usr/bin/env python3
"""Per-host container state (#7, multi-host).

Two hosts can each run an `nginx`. The state lists key on the container
name, so without host keying they'd collide. `HostScopedStore` prefixes
names going in and strips them coming out, and — critically — the local
host stays UNPREFIXED so every existing install's data files keep working
untouched.

Real ContainerStore against a temp dir; no Docker, no network.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from container_store import (ContainerStore, HostScopedStore,   # noqa: E402
                             host_key, split_host_key, LOCAL_HOST)

checks = {}

# ── the key format ───────────────────────────────────────────────────
checks["local host key is bare"] = host_key("local", "nginx") == "nginx"
checks["no host given → bare"] = host_key(None, "nginx") == "nginx"
checks["remote host key is prefixed"] = host_key("nas", "nginx") == "nas/nginx"
checks["round-trip remote"] = split_host_key("nas/nginx") == ("nas", "nginx")
checks["bare key reads as local"] = split_host_key("nginx") == ("local", "nginx")


def _cfg(d):
    names = ["pinned", "autoupdate", "update_windows", "ask_before_major",
             "trust_running", "cooldown", "protect_stop", "major_pending",
             "groups", "notes", "links"]
    return types.SimpleNamespace(
        **{f"{n}_file": os.path.join(d, f"{n}.json") for n in names})


with tempfile.TemporaryDirectory() as d:
    raw = ContainerStore(_cfg(d))
    local = HostScopedStore(raw, LOCAL_HOST)
    nas = HostScopedStore(raw, "nas")

    # ── same name on two hosts must not collide ──────────────────────
    local.pin("nginx")
    checks["pin on local doesn't pin on nas"] = not nas.is_pinned("nginx")
    nas.pin("nginx")
    checks["both hosts can pin the same name"] = (
        local.is_pinned("nginx") and nas.is_pinned("nginx"))
    checks["each host sees only its own"] = (
        local.get_pinned() == ["nginx"] and nas.get_pinned() == ["nginx"])
    # On disk they're distinct keys, and local's is unprefixed.
    checks["stored as distinct keys"] = sorted(raw.get_pinned()) == [
        "nas/nginx", "nginx"]

    # ── unpinning one leaves the other alone ─────────────────────────
    nas.unpin("nginx")
    checks["unpin on nas leaves local pinned"] = (
        local.is_pinned("nginx") and not nas.is_pinned("nginx"))

    # ── a bulk save must not wipe the other host ─────────────────────
    nas.pin("redis")
    local.save_pinned(["a", "b"])
    checks["bulk save keeps the other host's entries"] = (
        nas.get_pinned() == ["redis"])
    checks["bulk save applies to own host"] = sorted(local.get_pinned()) == ["a", "b"]

    # ── pre-existing (unprefixed) data belongs to local ──────────────
    raw.save_pinned(["legacy-container"])
    checks["existing flat data reads as local"] = (
        local.get_pinned() == ["legacy-container"] and nas.get_pinned() == [])

    # ── dict-shaped state ────────────────────────────────────────────
    local.set_cooldown("db", 30)
    nas.set_cooldown("db", 90)
    checks["cooldowns are per host"] = (
        local.get_cooldown("db") == 30 and nas.get_cooldown("db") == 90)
    checks["cooldown listing is per host"] = (
        local.get_cooldowns() == {"db": 30} and nas.get_cooldowns() == {"db": 90})

    local.set_note("db", "primary")
    checks["notes are per host"] = (
        local.get_note("db") == "primary" and not nas.get_note("db"))

    # ── toggles ──────────────────────────────────────────────────────
    nas.toggle_auto("sonarr")
    checks["auto-update is per host"] = (
        nas.is_auto("sonarr") and not local.is_auto("sonarr"))
    checks["auto listing is per host"] = (
        nas.get_autoupdate() == ["sonarr"] and local.get_autoupdate() == [])

    # ── groups belong to the host their members live on ──────────────
    nas.save_group("g1", "media", ["gluetun", "sonarr"], 30)
    checks["group members stored host-keyed"] = (
        raw.get_group("g1")["containers"] == ["nas/gluetun", "nas/sonarr"])
    checks["group reads back with plain names"] = (
        nas.get_group("g1")["containers"] == ["gluetun", "sonarr"])
    checks["other host doesn't see the group"] = local.get_group("g1") is None
    # Returns a (group_id, group) tuple — (None, None) when there's no hit.
    nas_hit = nas.get_group_for_container("sonarr")
    local_hit = local.get_group_for_container("sonarr")
    checks["group lookup by member is host-scoped"] = (
        nas_hit[0] == "g1" and local_hit == (None, None))
    checks["group lookup returns plain member names"] = (
        nas_hit[1]["containers"] == ["gluetun", "sonarr"])


# ── the view must return the SAME SHAPES as the store it wraps ───────
# A view that hands back a set where the store returns a list works for
# `in` and then breaks the first caller that sorts, indexes or serialises
# it — and only on multi-host installs, which is the worst place to find
# out.
with tempfile.TemporaryDirectory() as d:
    raw2 = ContainerStore(_cfg(d))
    view = HostScopedStore(raw2, "nas")
    for meth in ("get_pinned", "get_autoupdate", "get_ask_before_major",
                 "get_trust_running", "get_protect_stop"):
        a = type(getattr(raw2, meth)())
        b = type(getattr(view, meth)())
        checks[f"{meth} returns the same type as the store"] = a is b
    for meth in ("get_update_windows", "get_cooldowns", "get_pending_major",
                 "get_notes", "get_links", "get_groups"):
        a = type(getattr(raw2, meth)())
        b = type(getattr(view, meth)())
        checks[f"{meth} returns the same type as the store"] = a is b


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
