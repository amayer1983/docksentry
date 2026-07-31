#!/usr/bin/env python3
"""Container CLI backend — the single seam over the `docker` command line.

Until now every module shelled out to `subprocess.run(["docker", …])`
directly, scattered across the codebase. v2 wants a Podman-native,
multi-host story, and that is impossible to reach while the CLI name and
argv construction live inline at dozens of call sites.

This module introduces the seam and nothing more. `ContainerBackend.run()`
is the *only* window onto `subprocess` for the operations that go through
it; the typed read-methods build the argv and hand it to `run()`.
`DockerBackend` sets `cli_binary = "docker"`, so the argv it produces is
byte-for-byte what the old call sites produced — the behaviour is
definitionally unchanged, and the argv-mocking tests stay green.

Scope: the READ operations came first; the update/lifecycle core (pull,
compose, stop/kill/rm/rename/start, recreate, housekeeping) is migrating
in small serial slices behind the same seam. Read call sites inside
`update_checker.py` are deliberately still on direct `subprocess` calls —
several tests patch `update_checker.subprocess.run`, so rerouting those
would silently bypass the mocks and leave the tests green while testing
nothing. That wave needs the mocks moved first.

Pure standard library — the project's core promise.
"""
import subprocess


class ContainerBackend:
    """Base backend. Subclasses set `cli_binary`.

    Everything funnels through `run()`; the read helpers only assemble argv.
    """

    cli_binary = None

    def run(self, args, *, timeout=None, text=True, input=None,
            capture_output=True):
        """The one and only call into `subprocess` for this backend.

        Prepends `cli_binary` to `args` and runs it. The keyword handling
        (timeout / text / capture_output / input) is centralised here so
        every read-method gets identical semantics. Returns the
        `subprocess.CompletedProcess` untouched — callers keep reading
        `.returncode` / `.stdout` / `.stderr` exactly as before.

        With the defaults (timeout=None, input=None, text=True,
        capture_output=True) this is byte-identical to the historical
        ``subprocess.run(argv, capture_output=True, text=True)``.
        """
        argv = [self.cli_binary] + list(args)
        return subprocess.run(
            argv,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            input=input,
        )

    # ── typed read operations ───────────────────────────────────────
    # Each one builds EXACTLY the argv the historical call site built —
    # same flags, same order — then delegates to run(). Verified per
    # call-site against monitor.py / web_ui.py.

    @staticmethod
    def _as_list(refs):
        """Accept a single ref string or an iterable of refs."""
        if isinstance(refs, str):
            return [refs]
        return list(refs)

    def ps(self, *, all=False, quiet=False, fmt=None, timeout=None):
        """`docker ps [-a] [-q] [--format FMT]`."""
        args = ["ps"]
        if all:
            args.append("-a")
        if quiet:
            args.append("-q")
        if fmt is not None:
            args += ["--format", fmt]
        return self.run(args, timeout=timeout)

    def inspect(self, refs, *, fmt=None, timeout=None):
        """`docker inspect [--format FMT] REF...`."""
        args = ["inspect"]
        if fmt is not None:
            args += ["--format", fmt]
        args += self._as_list(refs)
        return self.run(args, timeout=timeout)

    def image_inspect(self, images, *, fmt=None, timeout=None):
        """`docker image inspect [--format FMT] IMAGE...`."""
        args = ["image", "inspect"]
        if fmt is not None:
            args += ["--format", fmt]
        args += self._as_list(images)
        return self.run(args, timeout=timeout)

    def logs(self, name, *, tail=None, timeout=None):
        """`docker logs [--tail N] NAME`."""
        args = ["logs"]
        if tail is not None:
            args += ["--tail", str(tail)]
        args.append(name)
        return self.run(args, timeout=timeout)

    def info(self, *, fmt=None, timeout=None):
        """`docker info [--format FMT]`."""
        args = ["info"]
        if fmt is not None:
            args += ["--format", fmt]
        return self.run(args, timeout=timeout)

    def version(self, *, fmt=None, timeout=None):
        """`docker version [--format FMT]`."""
        args = ["version"]
        if fmt is not None:
            args += ["--format", fmt]
        return self.run(args, timeout=timeout)

    def stats(self, *, fmt=None, timeout=None):
        """`docker stats --no-stream [--format FMT]`.

        Always `--no-stream`: a streaming `stats` never returns, so a read
        backend has no business emitting one.
        """
        args = ["stats", "--no-stream"]
        if fmt is not None:
            args += ["--format", fmt]
        return self.run(args, timeout=timeout)

    def system_df(self, *, fmt=None, timeout=None):
        """`docker system df [--format FMT]`."""
        args = ["system", "df"]
        if fmt is not None:
            args += ["--format", fmt]
        return self.run(args, timeout=timeout)

    # ── write / lifecycle operations ────────────────────────────────
    # Migrated from update_checker.py in serial slices. Same rule as the
    # read helpers: build EXACTLY the argv the historical call site built,
    # then delegate to run(). Anything whose argv is assembled dynamically
    # (docker run from _build_run_args, network connect) deliberately does
    # NOT get a typed method — those go through the generic run() so the
    # executed argv stays byte-identical.

    def pull(self, ref, *, timeout=None):
        """`docker pull REF`."""
        return self.run(["pull", ref], timeout=timeout)

    # text= is NOT uniform across these, and that is deliberate: it mirrors
    # what each historical call site passed. `stop`/`kill` ran with
    # text=True and their callers read `.stderr` as a str — handing those
    # bytes would break `.strip()`. `start`/`rename`/`rm` ran with
    # capture_output only and never read the output, so text=False keeps
    # them byte-faithful. The argv is identical either way.

    def stop(self, name, *, time=None, timeout=None):
        """`docker stop [--time N] NAME`. Text mode — callers read stderr."""
        args = ["stop"]
        if time is not None:
            args += ["--time", str(time)]
        args.append(name)
        return self.run(args, timeout=timeout)

    def kill(self, name, *, timeout=None):
        """`docker kill NAME`. Text mode — callers read stderr."""
        return self.run(["kill", name], timeout=timeout)

    def start(self, name, *, timeout=None, text=False):
        """`docker start NAME`.

        `text` defaults to False (the rollback path discards the output);
        pass text=True where the caller reads `.stderr` as a str.
        """
        return self.run(["start", name], timeout=timeout, text=text)

    def restart(self, name, *, time=None, timeout=None):
        """`docker restart [--time N] NAME`. Text mode — callers read stderr."""
        args = ["restart"]
        if time is not None:
            args += ["--time", str(time)]
        args.append(name)
        return self.run(args, timeout=timeout)

    def rename(self, src, dst, *, timeout=None):
        """`docker rename SRC DST`."""
        return self.run(["rename", src, dst], timeout=timeout, text=False)

    def rm(self, names, *, force=False, timeout=None):
        """`docker rm [-f] NAME...`."""
        args = ["rm"]
        if force:
            args.append("-f")
        args += self._as_list(names)
        return self.run(args, timeout=timeout, text=False)

    def image_prune(self, *, all=False, force=False, until=None, timeout=None):
        """`docker image prune [-a] [--force] [--filter until=X]`.

        NOTE the `until` filter matches image CREATION time, not pull time —
        that semantic is load-bearing for the cleanup grace period and is
        asserted by the coordination tests. Keep the argv exact.
        """
        args = ["image", "prune"]
        if all:
            args.append("-a")
        if force:
            args.append("--force")
        if until is not None:
            args += ["--filter", f"until={until}"]
        return self.run(args, timeout=timeout)

    def image_save(self, ref, path, *, timeout=None):
        """`docker image save -o PATH REF`."""
        return self.run(["image", "save", "-o", path, ref], timeout=timeout)


class DockerBackend(ContainerBackend):
    """Docker CLI backend — the current, only production backend.

    `cli_binary = "docker"` makes every argv identical to what the code
    shelled out to before this seam existed.
    """

    cli_binary = "docker"


_default_backend = None


def default_backend():
    """Process-wide backend for call sites that have no `self`.

    A handful of container reads live on `@staticmethod`/`@classmethod`
    helpers (`resolve_own_id`, `inspect_self`, `_image_config`,
    `_cgroup_version`, …) that can't reach an instance's `self.backend`.
    They go through this shared instance instead. `get_backend()` records
    what it hands out, so once main.py has built the real backend these
    resolve to the same object; before that it lazily falls back to Docker,
    which is what those helpers used to hardcode anyway.
    """
    global _default_backend
    if _default_backend is None:
        _default_backend = DockerBackend()
    return _default_backend


def get_backend(config):
    """Return the container backend for this deployment.

    For now this is always Docker. A Podman-native backend (and, later,
    remote-host selection) will branch here off config — the placeholder
    is intentional; the selection logic does NOT belong in this wave.
    """
    # v2: inspect config here to pick DockerBackend / PodmanBackend / a
    # remote-host backend. Kept deliberately trivial for now.
    global _default_backend
    backend = DockerBackend()
    # Record it so the no-self call sites (default_backend()) resolve to the
    # same object instead of quietly building their own Docker one.
    _default_backend = backend
    return backend
