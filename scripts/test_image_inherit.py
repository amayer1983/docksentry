#!/usr/bin/env python3
"""Image-inherited config must not be pinned onto the new image (#35).

A container's inspect Config is the image's defaults MERGED WITH the user's
overrides, and Docker records no distinction. Replicating all of it on recreate
hands the NEW image the OLD image's values. Found via unifi-os-server, which
ships its version as `ENV APP_VERSION=...` and so kept reporting 5.1.19 after a
successful update; the same trap applies to LABEL, USER, WORKDIR, STOPSIGNAL
and HEALTHCHECK.

Covers, per field: inherited value dropped, genuine user override kept,
inherited=None restores replicate-everything behaviour.

Part 2 verifies against a REAL Docker daemon that these fields actually are
image-inherited — the assumption a stale code comment got wrong about
HEALTHCHECK. Skipped automatically when no daemon is reachable.

Exits non-zero on any failure.
"""
import sys, os, json, subprocess, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker


def flags(args, flag):
    """Values of every occurrence of `flag` in a docker run argv."""
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def build(container_cfg, inherited):
    config = {
        "Config": container_cfg,
        "HostConfig": {},
        "Mounts": [],
        "NetworkSettings": {"Networks": {}},
    }
    return UpdateChecker._build_run_args(
        config, "img:new", "c1",
        image_defaults={"Entrypoint": None, "Cmd": None},
        inherited=inherited,
    )


def main():
    checks = {}

    # The old image's own Config, and a container that inherited all of it
    # while adding real user overrides on top.
    old_img = {
        "Env": ["PATH=/usr/bin", "APP_VERSION=5.1.19"],
        "Labels": {"org.opencontainers.image.version": "1.3.0", "vendor": "acme"},
        "User": "app",
        "WorkingDir": "/app",
        "StopSignal": "SIGTERM",
        "Healthcheck": {"Test": ["CMD-SHELL", "curl -f localhost || exit 1"],
                        "Interval": 30000000000},
    }
    container = {
        "Env": old_img["Env"] + ["TZ=Europe/Berlin"],
        "Labels": dict(old_img["Labels"], **{"com.docker.compose.project": "stack"}),
        "User": old_img["User"],
        "WorkingDir": old_img["WorkingDir"],
        "StopSignal": old_img["StopSignal"],
        "Healthcheck": old_img["Healthcheck"],
        "Image": "img:old",
    }

    a = build(container, old_img)

    # -- Env --
    checks["env: inherited APP_VERSION dropped"] = "APP_VERSION=5.1.19" not in flags(a, "-e")
    checks["env: user TZ kept"] = "TZ=Europe/Berlin" in flags(a, "-e")

    # -- Labels --
    lbl = flags(a, "--label")
    checks["label: inherited OCI version dropped"] = not any(
        x.startswith("org.opencontainers.image.version=") for x in lbl)
    checks["label: inherited vendor dropped"] = "vendor=acme" not in lbl
    checks["label: compose label kept"] = "com.docker.compose.project=stack" in lbl

    # -- User / WorkingDir / StopSignal --
    checks["user: inherited not pinned"] = flags(a, "--user") == []
    checks["workdir: inherited not pinned"] = flags(a, "--workdir") == []
    checks["stopsignal: inherited not pinned"] = flags(a, "--stop-signal") == []

    # -- Healthcheck: the rollback-loop case --
    # New image ships a FIXED healthcheck; pinning the old broken one makes
    # our own post-update health gate roll the update back forever.
    checks["healthcheck: inherited not pinned"] = flags(a, "--health-cmd") == []
    checks["healthcheck: no interval either"] = flags(a, "--health-interval") == []

    # -- Genuine overrides must all survive --
    over = dict(container,
                User="1000:1000", WorkingDir="/data", StopSignal="SIGKILL",
                Healthcheck={"Test": ["CMD-SHELL", "my-own-check"]})
    b = build(over, old_img)
    checks["override: user kept"] = flags(b, "--user") == ["1000:1000"]
    checks["override: workdir kept"] = flags(b, "--workdir") == ["/data"]
    checks["override: stopsignal kept"] = flags(b, "--stop-signal") == ["SIGKILL"]
    checks["override: healthcheck kept"] = flags(b, "--health-cmd") == ["my-own-check"]

    # -- Disabled healthcheck is an override, not an inheritance --
    off = dict(container, Healthcheck={"Test": ["NONE"]})
    checks["override: --no-healthcheck kept"] = "--no-healthcheck" in build(off, old_img)

    # -- inherited=None -> replicate everything (backward compatible) --
    c = build(container, None)
    checks["None: replicates env"] = "APP_VERSION=5.1.19" in flags(c, "-e")
    checks["None: replicates labels"] = "vendor=acme" in flags(c, "--label")
    checks["None: replicates user"] = flags(c, "--user") == ["app"]
    checks["None: replicates healthcheck"] = flags(c, "--health-cmd") != []

    # -- Part 2: verify against a real daemon that these ARE inherited --
    have_docker = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    if have_docker:
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "Dockerfile"), "w") as f:
            f.write('FROM alpine:latest\n'
                    'LABEL org.opencontainers.image.version="9.9.9"\n'
                    'USER root\nWORKDIR /tmp\nSTOPSIGNAL SIGTERM\n'
                    'HEALTHCHECK --interval=7s CMD echo ok\n'
                    'CMD ["sleep","60"]\n')
        tag, cname = "dsinherit:test", "dsinherit_c"
        ok = subprocess.run(["docker", "build", "-q", "-t", tag, d],
                            capture_output=True).returncode == 0
        if ok:
            subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
            subprocess.run(["docker", "run", "-d", "--name", cname, tag],
                           capture_output=True)
            raw = subprocess.run(["docker", "inspect", cname],
                                 capture_output=True, text=True)
            try:
                ccfg = json.loads(raw.stdout)[0]["Config"]
            except (ValueError, IndexError, KeyError):
                ccfg = {}
            # The container never overrode any of these, yet they are present:
            checks["real: image LABEL lands in container Config"] = (
                (ccfg.get("Labels") or {}).get("org.opencontainers.image.version") == "9.9.9")
            checks["real: image USER lands in container Config"] = ccfg.get("User") == "root"
            checks["real: image WORKDIR lands in container Config"] = ccfg.get("WorkingDir") == "/tmp"
            # The one a stale comment claimed was impossible:
            checks["real: image HEALTHCHECK lands in container Config"] = bool(
                (ccfg.get("Healthcheck") or {}).get("Test"))
            # And our filter neutralises all of them.
            img_cfg = UpdateChecker._image_config(tag)
            got = UpdateChecker._build_run_args(
                {"Config": ccfg, "HostConfig": {}, "Mounts": [],
                 "NetworkSettings": {"Networks": {}}},
                tag, cname, image_defaults={"Entrypoint": None, "Cmd": None},
                inherited=img_cfg)
            checks["real: filter drops every inherited field"] = (
                flags(got, "--label") == [] and flags(got, "--user") == []
                and flags(got, "--workdir") == [] and flags(got, "--health-cmd") == []
                and flags(got, "--stop-signal") == [])
            subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
    else:
        print("  (no docker daemon - skipping real-daemon verification)")

    for k, v in checks.items():
        print(("  PASS" if v else "  FAIL"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
