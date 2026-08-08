#!/usr/bin/env python3
"""The hosts this Docksentry instance manages (#7).

One instance, N hosts. Each host bundles the three things that have to
agree with each other:

  * a **backend** — the container CLI pointed at that host,
  * a **checker** — `UpdateChecker` driving that backend,
  * a **store view** — container state keyed to that host,

so nothing downstream has to thread a host name through every call. Ask
the registry for a host and everything you get back is already about it.

The machine Docksentry runs on is always present as `local`, first in the
list, and is NOT configured — an unset `DOCKER_HOSTS` therefore yields
exactly one host and behaves precisely as the single-host versions did.
Its state keys stay unprefixed, so existing installs need no migration.

Pure standard library, like the rest of the project.
"""

from container_backend import RemoteBackend, get_backend, resolve_cli
from container_store import LOCAL_HOST, HostScopedStore
from update_checker import UpdateChecker


class ManagedHost:
    """One host, with everything needed to act on it already bound to it."""

    def __init__(self, name, backend, checker, store, *, endpoint="",
                 is_local=False):
        self.name = name
        self.backend = backend
        self.checker = checker
        self.store = store
        #: Empty for the local host; the `-H` target for remote ones.
        self.endpoint = endpoint
        self.is_local = is_local

    def __repr__(self):
        where = "local" if self.is_local else self.endpoint
        return f"<ManagedHost {self.name} ({where})>"


class HostRegistry:
    """All managed hosts, `local` first.

    Deliberately a plain ordered collection rather than a lookup-only dict:
    almost every caller either walks every host (a scheduled check) or
    resolves one by name (a command aimed at `pve1`), and "local first"
    is the order users expect to see results in.
    """

    def __init__(self, hosts):
        self.hosts = list(hosts)

    def __iter__(self):
        return iter(self.hosts)

    def __len__(self):
        return len(self.hosts)

    @property
    def local(self):
        """The host Docksentry itself runs on. Always present."""
        for host in self.hosts:
            if host.is_local:
                return host
        return self.hosts[0]

    @property
    def names(self):
        return [h.name for h in self.hosts]

    @property
    def is_multi(self):
        """True once more than the local host is managed. Lets callers keep
        single-host output free of host labels that would only be noise."""
        return len(self.hosts) > 1

    def checkers(self):
        """`(checker, host_name)` for every managed host, local first.

        The name is deliberately blank while only one host is managed, so
        single-host log lines and messages stay exactly as they were
        instead of gaining an "on local" that says nothing. `main` always
        passes a registry, so keying that off mere presence would have
        changed every single-host line — it checks `is_multi`.

        This lived in `Scheduler._checkers` and nowhere else, which is
        precisely why the scheduled check walked every host and the two
        manual ones did not. Measured on a two-host demo before it moved
        here: the Web UI's "Check Updates" button reported

            Checking 2 containers for updates...
              Checking: nginx-proxy (…/library/nginx:latest)
              Checking: redis-cache (…/library/redis:alpine)

        — two of four, with both containers on the second host never
        looked at and nothing anywhere saying so. A manual check that
        silently covers half an estate is worse than one that fails,
        because it answers.

        """
        if not self.is_multi:
            return [(self.local.checker, "")]
        return [(h.checker, h.name) for h in self.hosts]

    def get(self, name):
        """Resolve a host by name, case-insensitively. None if unknown."""
        if not name:
            return None
        wanted = name.strip().lower()
        for host in self.hosts:
            if host.name == wanted:
                return host
        return None


def host_checkers(hosts, fallback):
    """`HostRegistry.checkers()`, tolerating no registry at all.

    Some call sites (tests that build a Web UI handler directly, older
    entry points) have a single checker and no registry. They still need
    the loop shape, so they get a one-element list rather than a special
    case of their own — a caller that has to remember to branch is a
    caller that will forget, which is the bug this whole helper exists
    to close.
    """
    if not hosts:
        return [(fallback, "")]
    return hosts.checkers()


def build_hosts(config, store):
    """Build the registry from `config`: the local host plus `DOCKER_HOSTS`.

    Remote hosts reuse whichever CLI the local one resolved to, so a Podman
    install talks Podman to its remotes too — mixing runtimes per host isn't
    something anyone has asked for, and guessing per host would mean probing
    each one at startup.

    A host that can't be reached is NOT detected here: this only constructs
    objects, it doesn't connect. Reachability shows up when a check runs, and
    is reported per host so one unreachable box can't stall the others.
    """
    local_backend = get_backend(config)
    hosts = [ManagedHost(
        LOCAL_HOST,
        local_backend,
        UpdateChecker(config, backend=local_backend),
        HostScopedStore(store, LOCAL_HOST),
        is_local=True,
    )]

    cli = resolve_cli(getattr(config, "container_cli", "auto"))
    for entry in getattr(config, "docker_hosts", None) or []:
        name, endpoint = entry["name"], entry["endpoint"]
        backend = RemoteBackend(endpoint, name=name, cli_binary=cli)
        hosts.append(ManagedHost(
            name,
            backend,
            UpdateChecker(config, backend=backend),
            HostScopedStore(store, name),
            endpoint=endpoint,
        ))
    return HostRegistry(hosts)


#: Sentinel returned by `split_host_target` for an explicit `@all`.
ALL_HOSTS = "\x00all"


def split_host_target(text):
    """Pull an `@host` token out of a command's argument text (#7).

    `/check @pve1` targets one host; `@all` targets every managed host.
    Returns `(remaining_text, target)` where target is a host name,
    `ALL_HOSTS`, or None when no `@token` was given.

    Only the FIRST `@token` is consumed and it may sit anywhere in the
    arguments, so `/update @nas sonarr` and `/update sonarr @nas` both
    work — people write it either way and neither is wrong.

    Deliberately NOT applied to the command word itself: Telegram's own
    group addressing is `/check@botname`, handled earlier and separately.
    That one has no space before the `@`, this one always does, so the
    two can't be confused.
    """
    parts = (text or "").split()
    for i, part in enumerate(parts):
        if not part.startswith("@") or len(part) < 2:
            continue
        name = part[1:].strip().lower()
        target = ALL_HOSTS if name == "all" else name
        return " ".join(parts[:i] + parts[i + 1:]).strip(), target
    return (text or "").strip(), None
