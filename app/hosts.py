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

    def get(self, name):
        """Resolve a host by name, case-insensitively. None if unknown."""
        if not name:
            return None
        wanted = name.strip().lower()
        for host in self.hosts:
            if host.name == wanted:
                return host
        return None


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
