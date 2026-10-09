#!/usr/bin/env python3
"""Rebuilding a sidecar that vanishes when you stop it.

A container sharing a VPN sidecar's network namespace (`network_mode:
"container:gluetun"`) cannot simply be restarted after the head is
recreated: it still references the dead old container ID, so a plain
`docker restart` fails (#8). It has to be rebuilt from its own inspect
data, rejoining the head's new ID.

The part that is easy to get wrong is the order, and it is one line. An
`AutoRemove` container — `--rm`, which plenty of compose files set on
sidecars — **ceases to exist the moment it stops**. So its configuration
has to be read BEFORE the stop, or there is nothing left to rebuild from
and the user's container is simply gone.

`docs/updates.md` and README:190 promise this works. On 06.10.2026 a
mutation sweep moved the `inspect` to after the stop — which is exactly
the bug — and the suite stayed green: the only check guarding it was
`"Capture config up front" in checker`, and that string occurs nowhere
but in the comment above the line. Rewording the comment would have
broken the check; moving the code would not.

So this drives `recreate_dependent` against a host where the container
really does disappear on stop, and asks whether it came back.
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

checks = {}

CONFIG = [{
    "Name": "/qbittorrent",
    "Config": {"Image": "linuxserver/qbittorrent:4.6", "Env": [],
               "Labels": {}},
    # The STALE id, on purpose. This is the whole of #8: after the head
    # is recreated it has a new container id, and the sidecar's own
    # inspect still records the dead one. Rebuilding from the inspect
    # alone rejoins a container that no longer exists, which is exactly
    # the failure `netns_name` was added to prevent — so the fixture has
    # to disagree with the caller, or dropping `netns_name` changes
    # nothing and the check cannot fall.
    "HostConfig": {"AutoRemove": True,
                   "NetworkMode": "container:0dead0dead0d",
                   "RestartPolicy": {"Name": "unless-stopped"},
                   "Binds": [], "PortBindings": {}},
    "Mounts": [],
}]


class _Host:
    """A host whose `--rm` container really does vanish on stop."""

    def __init__(self, auto_remove=True, run_fails=False):
        self.auto_remove = auto_remove
        self.run_fails = run_fails
        self.exists = {"qbittorrent"}
        self.did = []
        self.argv = []
        self.inspect_after_stop = None    # was inspect asked too late?
        self.stopped = False

    def run(self, args, timeout=None):
        verb = args[0]
        self.did.append(" ".join(str(a) for a in args[:2]))
        # The WHOLE command, kept: `did` only held the first two words,
        # so "it rejoins the head's namespace" was satisfied by the
        # word "run" and passed with `netns_name` dropped entirely.
        self.argv.append([str(a) for a in args])
        if verb == "inspect":
            name = args[-1]
            if name == "qbittorrent" and self.stopped and self.auto_remove:
                # The real failure: the container is gone, so there is
                # nothing to read its configuration from.
                self.inspect_after_stop = True
                return types.SimpleNamespace(
                    returncode=1, stdout="[]",
                    stderr="Error: No such object: qbittorrent")
            if name in self.exists or name == "linuxserver/qbittorrent:4.6":
                return types.SimpleNamespace(
                    returncode=0, stdout=json.dumps(CONFIG), stderr="")
            return types.SimpleNamespace(returncode=1, stdout="[]",
                                         stderr="No such object")
        if verb == "stop":
            self.stopped = True
            if self.auto_remove:
                self.exists.discard("qbittorrent")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if verb == "run":
            if self.run_fails:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr="port already allocated")
            self.exists.add("qbittorrent")
            return types.SimpleNamespace(returncode=0, stdout="id", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def rm(self, name, force=False, timeout=None):
        self.did.append(f"rm {name}")
        self.argv.append(["rm", name])
        self.exists.discard(name)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def stop(self, name, timeout=None):
        return self.run(["stop", name])

    def start(self, name, timeout=None):
        self.did.append(f"start {name}")
        self.argv.append(["start", name])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def rename(self, src, dst, timeout=None):
        # In `argv` too: the named backend methods do not go through
        # `run`, so a check that reads only `argv` missed the rollback's
        # rename entirely.
        self.did.append(f"rename {src} {dst}")
        self.argv.append(["rename", src, dst])
        if src in self.exists:
            self.exists.discard(src)
            self.exists.add(dst)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="No such container")


def _checker(host):
    c = UpdateChecker.__new__(UpdateChecker)
    c._backend = host
    c.config = types.SimpleNamespace(docker_stop_timeout=10,
                                     container_cli="docker")
    c._debug = lambda *_a, **_k: None
    c._lifecycle_timeout = lambda: 30
    c._container_exists = lambda n: n in host.exists
    c._rename_container = lambda src, dst: (
        host.rename(src, dst).returncode == 0)
    c._stop_container = lambda n: host.stop(n)
    c._get_image_defaults = lambda img: {}
    c._image_config = lambda img: {}
    c._cgroup_version = lambda backend: 2
    return c


# ── the case the comment is about ────────────────────────────────────
host = _Host(auto_remove=True)
ok, detail = _checker(host).recreate_dependent("qbittorrent", "gluetun")
checks["an AutoRemove sidecar is rebuilt after its head moved"] = ok is True
checks["…and the config was read before the stop, not after"] = (
    host.inspect_after_stop is not True)
checks["…so it exists again afterwards"] = "qbittorrent" in host.exists
_run_argv = next((a for a in host.argv if a and a[0] == "run"), [])
checks["…and it rejoins the head's CURRENT container, not the dead one"] = (
    "--network" in _run_argv
    and _run_argv[_run_argv.index("--network") + 1] == "container:gluetun")
checks["…so the id from its own stale inspect is not reused"] = (
    "container:0dead0dead0d" not in _run_argv)

# ── order, stated ────────────────────────────────────────────────────
# `inspect` must come before `stop`. This is the whole finding, said as
# a sequence rather than as a word in a comment.
_verbs = [d.split()[0] for d in host.did]
checks["inspect comes before stop"] = (
    "inspect" in _verbs and "stop" in _verbs
    and _verbs.index("inspect") < _verbs.index("stop"))

# ── an ordinary sidecar still works the same way ─────────────────────
# Without `--rm` the container survives the stop and is renamed aside as
# a backup; the rebuild path must not depend on which of the two it is.
plain = _Host(auto_remove=False)
ok, _ = _checker(plain).recreate_dependent("qbittorrent", "gluetun")
checks["a sidecar that survives its stop is rebuilt too"] = ok is True
checks["…having been renamed aside first"] = any(
    d.startswith("rename qbittorrent qbittorrent_old") for d in plain.did)

# ── and a failed rebuild rolls back ──────────────────────────────────
# The backup exists precisely for this. An `--rm` container has none, so
# there is nothing to restore and the honest answer is the failure —
# which is still better than the silent loss this file is about.
broken = _Host(auto_remove=False, run_fails=True)
ok, detail = _checker(broken).recreate_dependent("qbittorrent", "gluetun")
checks["a rebuild that fails reports the failure"] = (
    ok is False and "port already allocated" in detail)
checks["…and puts the backup back"] = any(
    a[:3] == ["rename", "qbittorrent_old", "qbittorrent"] for a in broken.argv)

# ── a host that cannot be inspected at all ───────────────────────────
missing = _Host(auto_remove=True)
missing.exists.clear()
ok, detail = _checker(missing).recreate_dependent("qbittorrent", "gluetun")
# `"inspect" in detail` also matched "inspect parse failed", which is a
# different failure. The one this case produces is the lookup itself.
checks["a container that is already gone is reported, not guessed at"] = (
    ok is False and detail == "inspect failed")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
