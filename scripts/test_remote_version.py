#!/usr/bin/env python3
"""Old→new version in the "Updates Available" notification (#44, @LeeNX).

Covers the pure `_version_badge` renderer and `get_remote_image_meta`, which
reads the remote image's OCI config blob for its version label + build date.
The registry layer (`_registry_get`) is mocked — no network. Exits non-zero
on any failure.
"""
import sys, os, json, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker
from telegram_bot import TelegramBot


def main():
    chk = UpdateChecker(types.SimpleNamespace(debug=False))
    checks = {}

    # ── _version_badge (pure render) ──
    vb = TelegramBot._version_badge
    checks["badge: old → new arrow"] = vb({"old_version": "1.2.3", "new_version": "1.3.0"}) == " 🔖 v1.2.3 → v1.3.0"
    checks["badge: only old (current)"] = vb({"old_version": "1.2.3"}) == " 🔖 v1.2.3"
    checks["badge: only new"] = vb({"new_version": "1.3.0"}) == " 🔖 v1.3.0"
    checks["badge: equal collapses to one"] = vb({"old_version": "1.2.3", "new_version": "1.2.3"}) == " 🔖 v1.2.3"
    checks["badge: none → empty"] = vb({}) == ""

    blob = json.dumps({"created": "2026-06-29T10:00:00Z",
                       "config": {"Labels": {"org.opencontainers.image.version": "1.3.0"}}}).encode()

    # ── single-arch manifest ──
    man_single = json.dumps({"config": {"digest": "sha256:CFG"}}).encode()
    def reg_single(host, registry, repository, path, accept=None):
        if path.startswith("manifests/"): return man_single
        if path.startswith("blobs/"):     return blob
        return None
    chk._registry_get = reg_single
    checks["single-arch: version + created"] = (
        chk.get_remote_image_meta("registry-1.docker.io", "lib/x", "latest")
        == {"version": "1.3.0", "created": "2026-06-29"})

    # ── multi-arch index: must pick linux/amd64, then its config ──
    index = json.dumps({"manifests": [
        {"platform": {"os": "linux", "architecture": "arm64"}, "digest": "sha256:ARM"},
        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:AMD"}]}).encode()
    man_amd = json.dumps({"config": {"digest": "sha256:CFG"}}).encode()
    def reg_multi(host, registry, repository, path, accept=None):
        if path == "manifests/latest":      return index
        if path == "manifests/sha256:AMD":  return man_amd
        if path == "blobs/sha256:CFG":      return blob
        return None
    chk._registry_get = reg_multi
    checks["multi-arch: picks amd64 + extracts version"] = (
        chk.get_remote_image_meta("registry-1.docker.io", "lib/x", "latest")
        == {"version": "1.3.0", "created": "2026-06-29"})

    # ── registry failure → {} (notification just omits the line) ──
    chk._registry_get = lambda *a, **k: None
    checks["registry failure → {}"] = chk.get_remote_image_meta("r", "x", "latest") == {}

    # ── empty remote digest is a FAILURE, not a spurious update (Fix 2) ──
    # _get_remote_digest returns "" (not None) for a 200 manifest with no
    # Docker-Content-Digest header. "" must be treated as unreachable, never
    # as a digest that mismatches the local one.
    import tempfile
    tmp = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(debug=False,
                                pending_file=os.path.join(tmp, "pending.json"))
    dchk = UpdateChecker(cfg)
    dchk.get_running_containers = lambda: [{"name": "app", "image": "reg/repo:tag"}]
    dchk._parse_image = lambda img: ("reg", "repo", "tag")
    dchk._get_local_digests = lambda img: ["sha256:LOCAL"]
    # Since #53 the check keys on the running image ID; fake it so no
    # `docker inspect` is shelled out. Running image == tag image here.
    dchk._container_image_id = lambda name: "sha256:RUNNING"

    dchk._get_remote_digest = lambda registry, repository, tag: ""
    checks["empty remote digest → no update"] = dchk.check_all() == []

    # control: a real, differing remote digest DOES report an update
    dchk._get_image_size = lambda img: 0
    dchk._get_image_created = lambda img: ""
    dchk._get_image_version_label = lambda img: ""
    dchk._parse_semver = lambda t: None
    dchk.get_remote_image_meta = lambda *a, **k: {}
    dchk._get_remote_digest = lambda registry, repository, tag: "sha256:REMOTE"
    checks["differing remote digest → one update"] = len(dchk.check_all()) == 1

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
