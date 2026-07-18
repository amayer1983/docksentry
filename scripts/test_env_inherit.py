#!/usr/bin/env python3
"""Environment inheritance on recreate (#35, @NotRetarded).

A container's Config.Env is the image's own ENV merged with the user's -e
overrides; Docker records no distinction. Replicating all of it on recreate
pins the NEW image's defaults to the OLD image's values — so unifi-os-server,
which carries its version as `ENV APP_VERSION=5.1.21`, kept booting as 5.1.19
after a successful update: the new image really was running, we just handed it
the stale value on the command line.

Covers:
1. inherited entries are dropped, user overrides survive
2. the exact #35 scenario end-to-end
3. inherited_env=None keeps the old replicate-everything behaviour
4. a var whose value the user changed is still an override (kept)

Pure logic, no Docker needed. Exits non-zero on any failure.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker


def env_of(args):
    """Extract the values of every -e flag from a docker run argv."""
    return [args[i + 1] for i, a in enumerate(args) if a == "-e"]


def build(container_env, inherited_env):
    config = {
        "Config": {"Env": container_env, "Image": "img:tag"},
        "HostConfig": {},
        "Mounts": [],
        "NetworkSettings": {"Networks": {}},
    }
    args = UpdateChecker._build_run_args(
        config, "img:tag", "c1", image_defaults={"Entrypoint": None, "Cmd": None},
        inherited_env=inherited_env,
    )
    return env_of(args)


def main():
    checks = {}

    # ── 1. inherited dropped, overrides kept ──
    old_image_env = ["PATH=/usr/bin", "APP_VERSION=5.1.19", "APP_MODEL=UOSSERVER"]
    container_env = old_image_env + ["TZ=America/New_York", "UOS_SYSTEM_IP=192.168.0.50"]
    got = build(container_env, old_image_env)
    checks["inherited PATH dropped"] = "PATH=/usr/bin" not in got
    checks["inherited APP_VERSION dropped"] = "APP_VERSION=5.1.19" not in got
    checks["user TZ kept"] = "TZ=America/New_York" in got
    checks["user UOS_SYSTEM_IP kept"] = "UOS_SYSTEM_IP=192.168.0.50" in got
    checks["only the 2 overrides replicated"] = len(got) == 2

    # ── 2. the #35 scenario: new image bumps its own version ──
    # Old image shipped 5.1.19; the container inherited it. On update the new
    # image ships 5.1.21 — we must NOT pass the old value, or the entrypoint
    # echoes 5.1.19 forever despite running the new image.
    got = build(container_env, old_image_env)
    checks["#35: stale APP_VERSION not forced onto new image"] = not any(
        e.startswith("APP_VERSION=") for e in got
    )

    # ── 3. None → replicate everything (backward compatible) ──
    got_none = build(container_env, None)
    checks["None replicates every entry"] = got_none == container_env

    # ── 4. user CHANGED an image default → still an override, must survive ──
    # Same key as the image, different value: not a verbatim match, so kept.
    changed = ["PATH=/usr/bin", "APP_MODEL=CUSTOM"]
    got = build(changed, old_image_env)
    checks["changed value kept as override"] = "APP_MODEL=CUSTOM" in got
    checks["unchanged value still dropped"] = "PATH=/usr/bin" not in got

    # ── 5. empty / missing env is harmless ──
    checks["empty container env → no -e"] = build([], old_image_env) == []

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
