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

Everything in the update core now goes through here — reads, pull,
compose, the lifecycle verbs, recreate and housekeeping. The only argv
literals left that still name the CLI are three list constructions whose
first element is stripped before it reaches `run()` (the compose base, the
`docker run` builder whose output tests assert on, and network connect);
`run()` prepends the configured binary in every case.

Still deliberately outside: the self-update path in `telegram_bot.py`. It
launches a helper container to replace the process, so it cannot run
through the process it is replacing.

Pure standard library — the project's core promise.
"""
import shutil
import subprocess


class ContainerBackend:
    """Base backend. Subclasses set `cli_binary`.

    Everything funnels through `run()`; the read helpers only assemble argv.
    """

    cli_binary = None

    # Flags emitted BETWEEN the binary and the subcommand
    # (`docker -H ssh://box ps`, not `docker ps -H …`). Empty for local
    # backends, so their argv is exactly what it always was; a remote
    # backend fills it with its endpoint. Keeping it here rather than in
    # `run()`'s callers is what lets several backends — one per host —
    # coexist in one process without any call site knowing.
    global_args = ()

    # Human-readable host label. "local" for the machine we run on;
    # remote backends carry the configured host alias. This is what
    # multi-host uses to key state and to say WHICH box a message is about.
    name = "local"

    # Upper bound for any call that doesn't name its own timeout.
    #
    # This is a safety net, not a tuning knob. Without it a call inherits
    # subprocess's "wait forever", and a container CLI CAN wait forever: a
    # remote endpoint whose TCP session is dead-but-established, or an ssh
    # host that blackholes packets, never returns and never errors. The
    # Telegram bot dispatches commands inline in its long-poll loop, so one
    # such call doesn't fail one command — it stops the bot answering
    # anything, with no log line, until someone restarts the container.
    # Bounding it here rather than at each call site means a call site
    # added later can't reintroduce the hang by forgetting a keyword.
    #
    # Operations that legitimately take longer (pull, compose up, recreate,
    # stop with a grace period) all pass an explicit timeout already and
    # are unaffected.
    default_timeout = 60

    def build(self, args):
        """The argv this backend would run for `args`.

        Split out of `run()` so that a caller which cannot use `run()` —
        `event_watcher`, which needs a long-lived `Popen` reading a stream
        rather than a call that returns — still goes through the same
        construction. A second place assembling argv is exactly how the
        remote endpoint flag would come to be missing from one code path
        and present in another.
        """
        return [self.cli_binary] + list(self.global_args) + list(args)

    def run(self, args, *, timeout=None, text=True, input=None,
            capture_output=True, merge_stderr=False):
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
        argv = self.build(args)
        if timeout is None:
            timeout = self.default_timeout
        if merge_stderr:
            # Both streams into one, in the order the container wrote them.
            # `capture_output=True` cannot express this — it is shorthand
            # for two separate pipes.
            return subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=text,
                timeout=timeout,
                input=input,
            )
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
        """`docker logs [--tail N] NAME`, both streams merged.

        A container's output is split across stdout and stderr, and every
        caller of this used `result.stdout or result.stderr` — which takes
        stdout whenever it is non-empty and silently discards the other
        half. Measured: a container writing 30 lines to each showed 30 of
        60 in the Web UI, and the half thrown away was the ERROR output —
        the half you open a log page to read. It also made `--tail 50`
        appear to return fewer than 50, which is how it was reported
        (#2, @NotRetarded).

        Merged here rather than at the four call sites, so a fifth cannot
        get it wrong, and in the order the container wrote them rather
        than one stream after the other.
        """
        args = ["logs"]
        if tail is not None:
            args += ["--tail", str(tail)]
        args.append(name)
        return self.run(args, timeout=timeout, merge_stderr=True)

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
    """Docker CLI backend — the default production backend.

    `cli_binary = "docker"` makes every argv identical to what the code
    shelled out to before this seam existed.
    """

    cli_binary = "docker"


class PodmanBackend(ContainerBackend):
    """Podman CLI backend — speaks `podman` instead of `docker`.

    Podman's CLI is docker-compatible for everything Docksentry issues, and
    `podman compose` covers the compose verbs, so this really is just the
    binary name. The runtime differences that used to bite (#48 ulimit
    names, #49 PID/UTS `private`, #50 cgroup-v2 swappiness) are NOT handled
    here: they are normalisations of what *inspect reports* into what the
    *run flag* accepts, they live in `UpdateChecker._build_run_args`, and
    they are already correct on both runtimes.

    KNOWN LIMITATION — self-update: `TelegramBot`'s self-update path still
    issues literal `docker` commands and launches a `docker:cli` helper
    container, because it can't run inside the container it is replacing.
    That path is deliberately not migrated yet, so on Podman self-update
    still needs `docker` to resolve (the usual `docker`→`podman` alias).
    Everything else — checks, updates, recreates, rollback, lifecycle,
    cleanup — goes through this backend.
    """

    cli_binary = "podman"


class RemoteBackend(ContainerBackend):
    """A container CLI pointed at ANOTHER host.

    Both CLIs can drive a remote daemon from a global endpoint flag:
    `docker -H ssh://user@box ps`, `podman --url tcp://box:2375 ps`. Because
    that flag lives in `global_args`, every command Docksentry issues —
    reads, updates, recreates, rollback, compose — goes to that host with
    no call site aware of it. That is the whole point of having spent the
    effort on the seam.

    Several of these can exist side by side, one per configured host, which
    is what single-process multi-host needs.

    SSH endpoints rely on the CLI's own ssh handling, so key-based auth has
    to already work for the user Docksentry runs as (`ssh user@box` must
    succeed non-interactively). We deliberately don't reimplement ssh.

    NOT for self-update: Docksentry updates the instance it runs in, which
    is by definition local. Self-update stays on the local backend.
    """

    #: Remote calls fail faster than local ones: an unreachable host should
    #: give the caller its turn back quickly, and no healthy remote read
    #: takes anywhere near this long.
    default_timeout = 30

    #: Which global flag names the endpoint, per CLI. Docker documents `-H`.
    #: Podman documents `--url` — it happens to accept `-H` as an
    #: undocumented alias (verified on 4.9.3), but an alias that isn't in
    #: `--help` is not something to build on, so we emit what each CLI
    #: actually documents.
    ENDPOINT_FLAG = {"docker": "-H", "podman": "--url"}

    def __init__(self, endpoint, *, name=None, cli_binary="docker"):
        self.endpoint = endpoint
        self.cli_binary = cli_binary
        flag = self.ENDPOINT_FLAG.get(cli_binary, "-H")
        self.global_args = (flag, endpoint)
        self.name = name or endpoint


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


def resolve_cli(preference="auto"):
    """Decide which container CLI to speak.

    `docker` / `podman` force the choice. `auto` (the default) keeps the
    historical behaviour: Docker whenever the `docker` command exists —
    which includes every setup that aliases `docker` to Podman, as Podman
    users have been doing all along. Only when `docker` is genuinely absent
    and `podman` is present do we switch, which turns a previously broken
    install into a working one rather than changing any working one.
    """
    pref = (preference or "auto").strip().lower()
    if pref in ("docker", "podman"):
        return pref
    if shutil.which("docker"):
        return "docker"
    if shutil.which("podman"):
        return "podman"
    return "docker"


def get_backend(config):
    """Return the container backend for this deployment.

    Driven by `config.container_cli` (env `CONTAINER_CLI`): `auto` by
    default, or an explicit `docker` / `podman`. Remote-host selection will
    branch here too, once multi-host lands.
    """
    global _default_backend
    pref = getattr(config, "container_cli", "auto") if config is not None else "auto"
    backend = PodmanBackend() if resolve_cli(pref) == "podman" else DockerBackend()
    # Record it so the no-self call sites (default_backend()) resolve to the
    # same object instead of quietly building their own Docker one.
    _default_backend = backend
    return backend
