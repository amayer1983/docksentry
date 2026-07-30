#!/usr/bin/env python3
"""Running-image update check (#53, @LeeNX).

The core-update-detection bug: `check_all` used to compare the digest the
TAG resolves to against the remote digest. When someone pulls `:latest`
forward but never recreates the container, the tag and the image the
container is actually running drift apart — and the tag-based check then
reads tag==remote and reports "up to date" while the container keeps
running the old image. Docksentry went blind to a real available update.

The fix keys the local side on the container's RUNNING image (its `.Image`
ID → that image's RepoDigests), the same thing the Web UI does since #46.

Docker and the registry are faked out — no network, no daemon, same style
as test_check_scoped.py. Exits non-zero on any failure.
"""
import sys, os, io, json, types, tempfile, contextlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker

RUN_ID = "sha256:RUNID"        # the container's .Image (running image ID)
IMAGE = "reg/app:latest"       # the reference the container was created from


def make_checker(*, running_digests, tag_digests, remote,
                 versions=None, run_id=RUN_ID, tag_id="", meta=None, debug=False):
    """Fake checker over a single container `app` running image `IMAGE`.

    running_digests -- bare-sha256 RepoDigests of the image the container is
                       ACTUALLY running (looked up via its .Image id)
    tag_digests     -- RepoDigests of whatever `IMAGE`'s tag resolves to now
    remote          -- the remote Docker-Content-Digest of the tag
    versions        -- optional {ref: version-label} for the diagnostic line
    run_id          -- what `_container_image_id` returns ("" = unreadable)
    tag_id          -- what `_image_id(IMAGE)` returns: the image id the tag
                       points at now ("" = unreadable). Only consulted on the
                       digestless-running-image fallback path.
    """
    versions = versions or {}
    tmp = tempfile.mkdtemp()
    pending_file = os.path.join(tmp, "pending.json")
    cfg = types.SimpleNamespace(debug=debug, pending_file=pending_file)
    chk = UpdateChecker(cfg)

    chk.get_running_containers = lambda: [{"name": "app", "image": IMAGE}]
    chk._parse_image = lambda img: ("reg", "app", "latest")
    chk._container_image_id = lambda name: run_id
    chk._image_id = lambda ref: tag_id

    def local_digests(ref):
        if ref == run_id:
            return list(running_digests)
        if ref == IMAGE:
            return list(tag_digests)
        return []
    chk._get_local_digests = local_digests
    chk._get_local_repo_digests = lambda ref: [f"reg/app@{d}" for d in local_digests(ref)]

    chk._get_remote_digest = lambda registry, repository, tag: remote
    chk._get_image_size = lambda ref: 0
    chk._get_image_created = lambda ref: ""
    chk._get_image_version_label = lambda ref: versions.get(ref, "")
    chk._parse_semver = lambda t: None
    chk.get_remote_image_meta = lambda *a, **k: (meta or {})
    chk._registry_environment = lambda: "host linux/arm64, mirrors: none"
    return chk, pending_file


def run(chk):
    with contextlib.redirect_stdout(io.StringIO()):
        return chk.check_all()


def main():
    checks = {}
    log_divergence = ""

    # ── 1. Normal, up to date: running == tag == local, remote == local ──
    chk, _ = make_checker(running_digests=["sha256:LOCAL"],
                          tag_digests=["sha256:LOCAL"], remote="sha256:LOCAL")
    checks["normal up-to-date: no update (unchanged)"] = run(chk) == []

    # ── 2. Normal, update available: running == tag, remote is newer ─────
    chk, _ = make_checker(running_digests=["sha256:LOCAL"],
                          tag_digests=["sha256:LOCAL"], remote="sha256:REMOTE")
    res = run(chk)
    checks["normal update: one update (unchanged)"] = [u["name"] for u in res] == ["app"]

    # ── 3. THE BUG (divergence): container runs the OLD image, tag was ───
    #    pulled forward to the remote's version. Old tag-based check saw
    #    tag==remote and said "up to date"; must now be UPDATE AVAILABLE.
    chk, _ = make_checker(running_digests=["sha256:V201"],
                          tag_digests=["sha256:V230"], remote="sha256:V230")
    res = run(chk)
    checks["divergence: UPDATE AVAILABLE (not 'up to date')"] = [u["name"] for u in res] == ["app"]

    # ── 4. Digestless running image → clean fallback, NO false update ────
    #    Running image carries no RepoDigests (locally built / digestless).
    #    Fall back to the tag image; when the tag says up-to-date we must
    #    NOT invent an update.
    chk, _ = make_checker(running_digests=[],
                          tag_digests=["sha256:LOCAL"], remote="sha256:LOCAL")
    checks["digestless running img, tag current: no false update"] = run(chk) == []

    #    …and a genuinely newer remote on the fallback path still reports.
    chk, _ = make_checker(running_digests=[],
                          tag_digests=["sha256:LOCAL"], remote="sha256:REMOTE")
    checks["digestless running img, tag stale: fallback still reports"] = len(run(chk)) == 1

    #    Unreadable running image id (docker inspect hiccup) → same fallback.
    chk, _ = make_checker(running_digests=["ignored"], run_id="",
                          tag_digests=["sha256:LOCAL"], remote="sha256:LOCAL")
    checks["unreadable running id: falls back, no crash, no false update"] = run(chk) == []

    #    Both sides digestless → skipped, never a phantom update.
    chk, _ = make_checker(running_digests=[], tag_digests=[], remote="sha256:REMOTE")
    checks["no digests anywhere: skipped, no false update"] = run(chk) == []

    # ── 4b. Digestless running image, but the container is ALREADY on the ─
    #    tag image (run_id == tag_id) — the image just happens to have no
    #    RepoDigest. Nothing is behind anything → clean fallback, no update.
    chk, _ = make_checker(running_digests=[], tag_digests=["sha256:LOCAL"],
                          remote="sha256:LOCAL",
                          run_id="sha256:SAME", tag_id="sha256:SAME")
    checks["digestless, run_id==tag_id: no false update (fallback)"] = run(chk) == []

    # ── 4c. Digestless running image AND the tag is digestless too (a purely
    #    locally-built image the user rebuilds but never recreates). IDs
    #    differ, but there is no registry image to pull → must NOT invent an
    #    update. This is the case the tag-has-RepoDigests guard protects.
    chk, _ = make_checker(running_digests=[], tag_digests=[], remote="sha256:REMOTE",
                          run_id="sha256:AAA", tag_id="sha256:BBB")
    checks["digestless running + digestless tag, ids differ: no false update"] = run(chk) == []

    # ── 5. The exact #53 case (LeeNX's podman/arm64 gitea-runner) ────────
    #    His actual failure on v1.57.1: the container runs image b1addbb…
    #    (2.0.1). `:latest` was pulled forward to 15cc00a… (2.3.0) but the
    #    container was never recreated, so 2.0.1 lost its RepoDigest and is
    #    now DANGLING — the digest comparison can't fire, and v1.57.1 fell
    #    back to comparing the tag (2.3.0) against remote (2.3.0) and said
    #    "up to date". The tag image 15cc00a… still carries a real RepoDigest
    #    (66d8096 == remote), so this is a pullable update the container is
    #    behind. Must be UPDATE AVAILABLE; old_version = the RUNNING 2.0.1,
    #    new_version = the remote 2.3.0. (This is the case that would fail on
    #    v1.57.1 — the running image is digestless, not digest-divergent.)
    chk, _ = make_checker(
        running_digests=[],                        # 2.0.1 is dangling: NO RepoDigest
        tag_digests=["sha256:66d8096"],            # tag image is real: RepoDigest == remote
        remote="sha256:66d8096",                   # registry :latest = 2.3.0
        run_id="sha256:b1addbb",                   # image id the container runs (2.0.1)
        tag_id="sha256:15cc00a",                   # image id :latest points at now (2.3.0)
        versions={"sha256:b1addbb": "2.0.1", IMAGE: "2.3.0"},
        meta={"version": "2.3.0", "created": "2026-07-01"},
        debug=True)
    res = run(chk)
    checks["#53 gitea-runner: UPDATE AVAILABLE"] = [u["name"] for u in res] == ["app"]
    checks["#53 gitea-runner: old_version is the running 2.0.1"] = (
        res and res[0].get("old_version") == "2.0.1")
    checks["#53 gitea-runner: new_version is the remote 2.3.0"] = (
        res and res[0].get("new_version") == "2.3.0")
    log = "\n".join(chk.debug_log)
    checks["#53 gitea-runner: divergence spelled out in the log"] = (
        "2.0.1" in log and "2.3.0" in log and "not" in log.lower())
    # Capture the divergence-case log so a human can read how it renders.
    log_divergence = log

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)

    if log_divergence:
        print("\n--- divergence-case debug log (container runs 2.0.1, tag/remote 2.3.0) ---")
        print(log_divergence)
        print("--- end ---")

    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
