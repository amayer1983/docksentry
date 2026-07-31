#!/usr/bin/env python3
"""Container backend argv seam (v2 groundwork).

The whole point of `ContainerBackend` is that it changes NOTHING about what
reaches the shell: `DockerBackend` must build byte-identical `["docker", …]`
argv for every read operation the leaf modules use. The existing suite
mocks `subprocess.run` and branches on argv (test_self_detection.py,
test_monitor.py, test_link_render.py) — this file freezes that seam
directly, so a future refactor that quietly reorders a flag or drops one
gets caught here first.

subprocess is mocked — no Docker required. Exits non-zero on any failure.
"""
import sys
import os
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import container_backend
from container_backend import DockerBackend, get_backend


class _Rec:
    """Records the argv + kwargs the backend hands to subprocess.run and
    returns a stub CompletedProcess."""

    def __init__(self):
        self.cmd = None
        self.kwargs = None

    def run(self, cmd, **kw):
        self.cmd = cmd
        self.kwargs = kw
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def main():
    checks = {}
    rec = _Rec()
    real = container_backend.subprocess
    container_backend.subprocess = types.SimpleNamespace(run=rec.run)
    try:
        b = DockerBackend()

        def argv(call):
            call()
            return rec.cmd

        # ── the exact argv each read method must produce ────────────
        cases = {
            # (label, produced_argv, expected_argv)
            "ps -q (web _get_containers)":
                (argv(lambda: b.ps(quiet=True)),
                 ["docker", "ps", "-q"]),
            "ps -a --format Names (monitor / web groups all_ids)":
                (argv(lambda: b.ps(all=True, fmt="{{.Names}}")),
                 ["docker", "ps", "-a", "--format", "{{.Names}}"]),
            "inspect *refs (web _get_containers)":
                (argv(lambda: b.inspect(["a", "b"])),
                 ["docker", "inspect", "a", "b"]),
            "inspect single ref (web detail page)":
                (argv(lambda: b.inspect("nginx")),
                 ["docker", "inspect", "nginx"]),
            "inspect --format *refs (web groups_detect)":
                (argv(lambda: b.inspect(
                    ["x", "y"], fmt="{{.Name}}|{{.HostConfig.NetworkMode}}")),
                 ["docker", "inspect", "--format",
                  "{{.Name}}|{{.HostConfig.NetworkMode}}", "x", "y"]),
            "image inspect *imgs (web _get_containers)":
                (argv(lambda: b.image_inspect(["img1", "img2"])),
                 ["docker", "image", "inspect", "img1", "img2"]),
            "image inspect --format Size single (web detail page)":
                (argv(lambda: b.image_inspect("running", fmt="{{.Size}}")),
                 ["docker", "image", "inspect", "--format",
                  "{{.Size}}", "running"]),
            "logs --tail N NAME (web detail / logs page)":
                (argv(lambda: b.logs("nginx", tail=100)),
                 ["docker", "logs", "--tail", "100", "nginx"]),
            "logs NAME (no tail)":
                (argv(lambda: b.logs("nginx")),
                 ["docker", "logs", "nginx"]),
            "stats --no-stream --format (monitor _memory_snapshot)":
                (argv(lambda: b.stats(fmt="{{.Name}}|{{.MemUsage}}")),
                 ["docker", "stats", "--no-stream", "--format",
                  "{{.Name}}|{{.MemUsage}}"]),
            "info --format":
                (argv(lambda: b.info(fmt="{{.CgroupVersion}}")),
                 ["docker", "info", "--format", "{{.CgroupVersion}}"]),
            "version --format":
                (argv(lambda: b.version(fmt="{{json .}}")),
                 ["docker", "version", "--format", "{{json .}}"]),
            "system df --format":
                (argv(lambda: b.system_df(fmt="{{json .}}")),
                 ["docker", "system", "df", "--format", "{{json .}}"]),
            # ── write / lifecycle verbs ─────────────────────────────
            "pull":
                (argv(lambda: b.pull("nginx:latest")),
                 ["docker", "pull", "nginx:latest"]),
            "stop --time":
                (argv(lambda: b.stop("web", time=90)),
                 ["docker", "stop", "--time", "90", "web"]),
            "stop without --time":
                (argv(lambda: b.stop("web")), ["docker", "stop", "web"]),
            "kill":
                (argv(lambda: b.kill("web")), ["docker", "kill", "web"]),
            "start":
                (argv(lambda: b.start("web")), ["docker", "start", "web"]),
            "rename":
                (argv(lambda: b.rename("web", "web_old")),
                 ["docker", "rename", "web", "web_old"]),
            "rm":
                (argv(lambda: b.rm("web_old")), ["docker", "rm", "web_old"]),
            "rm -f":
                (argv(lambda: b.rm("web_old", force=True)),
                 ["docker", "rm", "-f", "web_old"]),
            "rm multiple":
                (argv(lambda: b.rm(["a", "b"])), ["docker", "rm", "a", "b"]),
            # The `until` filter matches image CREATION time, not pull time.
            # cleanup_images relies on that; keep the argv exact.
            "image prune -a --force --filter":
                (argv(lambda: b.image_prune(all=True, force=True, until="72h")),
                 ["docker", "image", "prune", "-a", "--force",
                  "--filter", "until=72h"]),
            "image prune bare":
                (argv(lambda: b.image_prune()), ["docker", "image", "prune"]),
            "image save":
                (argv(lambda: b.image_save("sha256:abc", "/tmp/x.tar")),
                 ["docker", "image", "save", "-o", "/tmp/x.tar", "sha256:abc"]),
            # Dynamically-assembled argv (compose, docker run, network
            # connect) goes through the generic run() so the executed
            # command stays byte-identical to the old call site. The
            # caller strips the leading CLI name; the backend re-adds it.
            "generic run: compose up":
                (argv(lambda: b.run(["compose", "-f", "/x/dc.yml", "-p", "proj",
                                     "up", "-d", "--no-deps",
                                     "--force-recreate", "web"])),
                 ["docker", "compose", "-f", "/x/dc.yml", "-p", "proj",
                  "up", "-d", "--no-deps", "--force-recreate", "web"]),
        }
        for label, (produced, expected) in cases.items():
            checks[f"argv: {label}"] = produced == expected
            checks[f"argv[0]=='docker': {label}"] = produced[0] == "docker"

        # ── run() forwards the standard kwargs unchanged ────────────
        b.ps(all=True, fmt="{{.Names}}", timeout=30)
        checks["run forwards timeout"] = rec.kwargs.get("timeout") == 30
        checks["run defaults capture_output=True"] = (
            rec.kwargs.get("capture_output") is True)
        checks["run defaults text=True"] = rec.kwargs.get("text") is True
        b.ps(quiet=True)
        checks["run leaves timeout=None when unset"] = (
            rec.kwargs.get("timeout") is None)

        # ── text mode per lifecycle verb ────────────────────────────
        # NOT uniform, deliberately: it mirrors what each historical call
        # site passed. stop/kill ran text=True and their callers do
        # `(r.stderr or "").strip()` — bytes there would raise. rm/rename/
        # start ran capture_output-only and never read the output.
        for verb, call, want_text in (
            ("stop", lambda: b.stop("web", time=10), True),
            ("kill", lambda: b.kill("web"), True),
            ("start", lambda: b.start("web"), False),
            ("rename", lambda: b.rename("a", "b"), False),
            ("rm", lambda: b.rm("a"), False),
            ("pull", lambda: b.pull("img"), True),
        ):
            call()
            checks[f"text mode: {verb} is {want_text}"] = (
                rec.kwargs.get("text") is want_text)

        # ── DockerBackend identity + factory ────────────────────────
        checks["DockerBackend.cli_binary == 'docker'"] = (
            DockerBackend.cli_binary == "docker")
        checks["get_backend returns a DockerBackend"] = isinstance(
            get_backend(types.SimpleNamespace()), DockerBackend)

        # ── Podman: same argv shape, different binary ───────────────
        # The whole point of the seam: one class swap re-points every
        # command, including the compose verbs (`podman compose …`).
        pb = container_backend.PodmanBackend()
        checks["PodmanBackend.cli_binary == 'podman'"] = (
            pb.cli_binary == "podman")
        pod_cases = {
            "podman ps": (argv(lambda: pb.ps(fmt="{{.Names}}")),
                          ["podman", "ps", "--format", "{{.Names}}"]),
            "podman pull": (argv(lambda: pb.pull("nginx")),
                            ["podman", "pull", "nginx"]),
            "podman stop": (argv(lambda: pb.stop("web", time=30)),
                            ["podman", "stop", "--time", "30", "web"]),
            "podman compose (generic run)":
                (argv(lambda: pb.run(["compose", "-f", "x.yml", "up", "-d"])),
                 ["podman", "compose", "-f", "x.yml", "up", "-d"]),
        }
        for label, (produced, expected) in pod_cases.items():
            checks[f"argv: {label}"] = produced == expected

        # ── Remote: the endpoint rides in front of every subcommand ──
        # `docker -H ssh://box ps`, never `docker ps -H …`. Because it sits
        # in global_args, no call site has to know the host exists.
        rb = container_backend.RemoteBackend("ssh://me@box", name="box")
        remote_cases = {
            "remote ps": (argv(lambda: rb.ps(fmt="{{.Names}}")),
                          ["docker", "-H", "ssh://me@box", "ps",
                           "--format", "{{.Names}}"]),
            "remote pull": (argv(lambda: rb.pull("nginx")),
                            ["docker", "-H", "ssh://me@box", "pull", "nginx"]),
            "remote rm -f": (argv(lambda: rb.rm("web", force=True)),
                             ["docker", "-H", "ssh://me@box", "rm", "-f", "web"]),
            "remote compose (generic run)":
                (argv(lambda: rb.run(["compose", "-f", "x.yml", "up", "-d"])),
                 ["docker", "-H", "ssh://me@box", "compose", "-f", "x.yml",
                  "up", "-d"]),
        }
        for label, (produced, expected) in remote_cases.items():
            checks[f"argv: {label}"] = produced == expected
        checks["remote carries its host name"] = rb.name == "box"
        checks["remote defaults name to the endpoint"] = (
            container_backend.RemoteBackend("tcp://h:2375").name == "tcp://h:2375")
        # Podman speaks the same global flag.
        rpb = container_backend.RemoteBackend("tcp://h:2375", cli_binary="podman")
        checks["argv: remote podman"] = (
            argv(lambda: rpb.ps()) == ["podman", "-H", "tcp://h:2375", "ps"])
        # …and the local backends must be untouched by all of this.
        checks["local backend emits no global args"] = (
            argv(lambda: b.ps()) == ["docker", "ps"])
        checks["local backend name is 'local'"] = b.name == "local"

        # ── CLI selection ───────────────────────────────────────────
        checks["resolve_cli('podman') → podman"] = (
            container_backend.resolve_cli("podman") == "podman")
        checks["resolve_cli('docker') → docker"] = (
            container_backend.resolve_cli("docker") == "docker")
        checks["resolve_cli('auto') picks one of the two"] = (
            container_backend.resolve_cli("auto") in ("docker", "podman"))
        checks["resolve_cli(junk) falls back to auto"] = (
            container_backend.resolve_cli("wat") in ("docker", "podman"))
        container_backend._default_backend = None
        checks["get_backend(container_cli='podman') → PodmanBackend"] = isinstance(
            get_backend(types.SimpleNamespace(container_cli="podman")),
            container_backend.PodmanBackend)
        container_backend._default_backend = None
        checks["get_backend(container_cli='docker') → DockerBackend"] = isinstance(
            get_backend(types.SimpleNamespace(container_cli="docker")),
            DockerBackend)
        # get_backend records what it handed out, so the no-self call sites
        # (default_backend()) don't silently build a second, Docker one.
        container_backend._default_backend = None
        chosen = get_backend(types.SimpleNamespace(container_cli="podman"))
        checks["default_backend() follows get_backend"] = (
            container_backend.default_backend() is chosen)
        container_backend._default_backend = None
    finally:
        container_backend.subprocess = real

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
