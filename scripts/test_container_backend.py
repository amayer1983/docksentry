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

        # ── DockerBackend identity + factory ────────────────────────
        checks["DockerBackend.cli_binary == 'docker'"] = (
            DockerBackend.cli_binary == "docker")
        checks["get_backend returns a DockerBackend"] = isinstance(
            get_backend(types.SimpleNamespace()), DockerBackend)
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
