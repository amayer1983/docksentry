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

import collections
import math
import threading
import time

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
    plaintext = []
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
        if _is_plaintext_remote(endpoint):
            plaintext.append((name, endpoint))
    if plaintext:
        # Said once, at startup, where somebody actually reads it. #60,
        # @NotRetarded, who reasonably believed his reverse proxy covered
        # this — it does not: the proxy sits in front of the Web UI, and
        # this connection runs the other way, out to the remote daemon.
        # Nothing in the docs said so, and the README example put a
        # `tcp://` host next to an `ssh://` one without a word.
        print("")
        for name, endpoint in plaintext:
            print(f"WARNING: host {name!r} uses {endpoint} — the Docker API "
                  f"over plain tcp:// is unencrypted AND unauthenticated.")
        print("         Anyone who can reach that port controls that "
              "machine's Docker, which is root on it.")
        print("         Use ssh://, or keep tcp:// on a network only "
              "Docksentry can reach (see docs/security.md).")
        print("         If that endpoint is a socket proxy on a private "
              "Docker network, this warning does not apply to you.")
        print("")
    return HostRegistry(hosts)


#: The only names where a plain `tcp://` demonstrably does not cross a
#: network. A socket-proxy sidecar reached by its container name does not
#: cross one either, but nothing here can tell `socket-proxy` from a LAN
#: hostname, so that case DOES warn. Accepted rather than guessed at: the
#: message names the host and says what to check, and a false warning
#: somebody can resolve in ten seconds beats a missing one about a port
#: that hands out root.
_LOCAL_HOSTNAMES = ("localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0")


def _is_plaintext_remote(endpoint):
    """True for a `tcp://` endpoint that leaves this machine.

    Deliberately only `tcp://`. `ssh://` is encrypted, `unix://` never
    leaves the box, and `context://` is whatever the stored connection
    says — we would be guessing, and a wrong warning is worse than none.
    """
    endpoint = (endpoint or "").strip()
    if not endpoint.startswith("tcp://"):
        return False
    hostpart = endpoint[len("tcp://"):].split("/")[0]
    host = hostpart.rsplit(":", 1)[0] if ":" in hostpart else hostpart
    return host.strip().lower() not in _LOCAL_HOSTNAMES


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


# one from paying it again, and nothing anywhere remembered that the
# host had just failed: the status page walked its hosts on EVERY
# request, so a single dead host cost the page ten seconds per reload,
# per `/api/status` poll, and per Prometheus scrape — for a machine we
# had already found unreachable a second earlier.
#
# So we remember, briefly. Not a verdict on the host — a note that says
# "asked just now, got nothing, do not ask again for a moment". Short
# enough that a host coming back is noticed within a minute, long
# enough that reloading a page does not re-run the whole wait.

#: How long a host that failed to answer is skipped before we try it
#: again. Short on purpose: this trades at most this much staleness for
#: not making every reader wait on the same dead machine.
UNREACHABLE_COOLDOWN = 60.0

_unreachable = {}
_unreachable_lock = threading.Lock()


#: A host that keeps failing is asked less and less often. One minute is
#: right for a machine rebooting; it is wrong for the endpoint someone
#: typo'd into DOCKER_HOSTS months ago, which then costs its full timeout
#: on every page load a minute apart — reported as "reload takes 10-15
#: seconds". Doubling per failure reaches the cap after five tries, and
#: any success clears it, so a host that comes back is still noticed on
#: the next attempt.
MAX_UNREACHABLE_COOLDOWN = 900.0

_failures = {}


def mark_unreachable(name, reason, *, cooldown=None):
    """Remember that `name` just failed, with the CLI's own words."""
    if not name:
        return
    with _unreachable_lock:
        if cooldown is None:
            n = _failures.get(name, 0) + 1
            _failures[name] = n
            cooldown = min(UNREACHABLE_COOLDOWN * (2 ** (n - 1)),
                           MAX_UNREACHABLE_COOLDOWN)
        _unreachable[name] = (time.monotonic() + cooldown, reason or "")


def mark_reachable(name):
    """Forget any failure for `name` — it answered."""
    if not name:
        return
    with _unreachable_lock:
        _unreachable.pop(name, None)
        _failures.pop(name, None)


def unreachable_for(name):
    """`(seconds_left, reason)` while `name` is still being skipped,
    `(0.0, "")` once it is due for another try.

    Callers show the remembered reason rather than inventing one, and
    say how long until the retry — a cached failure that does not admit
    it is cached is exactly the kind of message that sends someone
    looking in the wrong place.
    """
    if not name:
        return 0.0, ""
    with _unreachable_lock:
        entry = _unreachable.get(name)
        if entry is None:
            return 0.0, ""
        until, reason = entry
        left = until - time.monotonic()
        if left <= 0:
            del _unreachable[name]
            return 0.0, ""
        return left, reason


def forget_unreachable():
    """Drop the whole memory — for tests, and for a config reload that
    may have changed the endpoints out from under it."""
    with _unreachable_lock:
        _unreachable.clear()
        _failures.clear()


# The chat walks (find_on_hosts, check_on_hosts, collect_overview) take
# the same memory, but only for READS: a /status or a /check right after
# a host failed should not wait out its timeout again, while an /update
# must always ask — a host that came back a second ago is exactly the one
# somebody is trying to update. And only for remote hosts, like the page:
# the cooldown is for a machine at the far end of a wire, not for our own
# daemon, which nothing marks. A walk without the cache (every write)
# never skips a host and never marks one dead, but a host that answers
# it is forgotten all the same: an /update that just reached nas must
# not leave the next /status calling nas unreachable for the rest of its
# cooldown. Discord's reads take the cache since W7d, the Web's Check
# Updates since W7w; Discord's name lookup and container autocomplete
# read the memory directly (the autocomplete never writes to it). It is
# one memory: a host a Telegram, Discord or Web Check read found dead is
# skipped by the other two and by the Web status page's `walk` too, with
# the words that read remembered, and each such failure moves the same
# backoff (test_status_overview_core pins it). The Web Check remembers
# its words in English (it has no `t`), the chats in the bot's language.

def _remote_name(host):
    if host is None or getattr(host, "is_local", False):
        return ""
    return getattr(host, "name", "") or ""


def remembered_dead(host):
    """`(reason, retry_in)` while a remote `host` is still in its cooldown
    — the words it failed with and the seconds left, rounded up: a host
    with 0.4 s to go is still skipped, so it must not say "0s". The
    local host and the single-host pseudo-host None never are."""
    name = _remote_name(host)
    if not name:
        return None
    left, reason = unreachable_for(name)
    if left > 0:
        return reason, math.ceil(left)
    return None


def remember(host, failed_with):
    """A read's outcome for a remote `host`: the words it failed with, or
    None when it answered. The local host is left alone."""
    name = _remote_name(host)
    if not name:
        return
    if failed_with is None:
        mark_reachable(name)
    else:
        mark_unreachable(name, failed_with)


def skipped_words(reason, retry_in, t=None):
    """What to say for a host skipped because its last try failed: its
    remembered words, and that it is not asked again for `retry_in` s.
    Nothing about how old the words are: with the backoff a host can be
    skipped for fifteen minutes, so "a moment ago" was not always true.

    The words go through errfmt.human first. The memory holds whatever
    the walk that failed kept — the overview and the Web page keep the
    CLI's raw line, which for a timeout is Python's argv — and once the
    seconds clause is behind it, errfmt.human can no longer tell it is a
    timeout and would quote the argv at the reader. Words that already
    are a person's pass through unchanged. They come first in every
    language for the same reason: a line that ENDS in "timed out after
    N seconds" reads as a bare timeout."""
    import errfmt
    try:
        reason = errfmt.human(reason or "", t)
    except Exception:                                       # noqa: BLE001
        reason = errfmt.human(reason or "")
    if t is not None:
        try:
            return t("host_skipped_retry", error=reason, seconds=retry_in)
        except Exception:                                   # noqa: BLE001
            pass
    return f"{reason} (from the last try, not asked again for {retry_in}s)"


def skipped_hint(host, reason, t=None):
    """hostdiag's sentence for a skipped `host`, from the words it failed
    with — the one a fresh failure with those words gets. The hint only
    looks at the words and at this container's ~/.ssh, so a host nobody
    asked still gets it. "" when there is none; a hint that throws costs
    the hint, never the walk."""
    try:
        import hostdiag
        return hostdiag.hint(getattr(host, "endpoint", ""), reason or "",
                             t=t) or ""
    except Exception:                                       # noqa: BLE001
        return ""


class StillUnreachable(Exception):
    """A host a read did not ask, because its last try failed and it is
    still in its cooldown. `str()` is `skipped_words`; `reason` and
    `retry_in` are its parts."""

    def __init__(self, words, reason="", retry_in=0):
        super().__init__(words)
        self.reason = reason
        self.retry_in = retry_in


#: One host's part of a `walk`, as data — no wording, no markup.
#:
#: `result` is what `fetch` returned for a host that answered. A host
#: that did not answer has `reason`, the words for it: the remembered
#: ones when it was `skipped` (still in its cooldown, `retry_in` whole
#: seconds left), or `describe(error)` when it failed just now. A host
#: that answered has neither.
HostWalk = collections.namedtuple(
    "HostWalk", "host result skipped reason retry_in error",
    defaults=(None, False, None, None, None))


#: How many hosts a walk asks at once.
MAX_WALK_WORKERS = 8


def walk(targets, fetch, *, max_workers=MAX_WALK_WORKERS, use_cache=True,
         describe=str):
    """Ask every host in `targets` with `fetch(host)`, side by side, and
    hand back one `HostWalk` per host in the order given.

    This is the walk the status page used to carry itself: a host
    still in its cooldown is not asked (unless `use_cache` is False), a
    host whose `fetch` raises is remembered with `describe(error)`, and a
    host that answers is forgotten. The memory is read for every host
    before any is asked, and written for all of them, in the order
    given, once all have answered and before the caller builds anything
    from the answers. The page used to write each mark while it built
    that host's row, so a row that raised left the hosts after it
    unmarked; now they are remembered either way.

    `fetch` runs in worker threads and may raise; that is how it says a
    host is dead. It must not touch anything the caller changes
    meanwhile.
    """
    targets = list(targets or ())
    skip = {}
    todo = []
    for i, host in enumerate(targets):
        if use_cache:
            left, cached = unreachable_for(host.name)
            if left > 0:
                skip[i] = (cached, int(left))
                continue
        todo.append(i)

    def _one(i):
        try:
            return fetch(targets[i]), None
        except Exception as exc:                   # noqa: BLE001
            return None, exc

    got = {}
    if todo:
        import concurrent.futures as _cf
        workers = min(max_workers, MAX_WALK_WORKERS, len(todo))
        with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for i, out in zip(todo, ex.map(_one, todo)):
                got[i] = out

    walked = []
    for i, host in enumerate(targets):
        if i in skip:
            cached, left = skip[i]
            walked.append(HostWalk(host, skipped=True, reason=cached,
                                   retry_in=left))
            continue
        result, err = got[i]
        if err is not None:
            why = describe(err)
            mark_unreachable(host.name, why)
            walked.append(HostWalk(host, reason=why, error=err))
            continue
        mark_reachable(host.name)
        walked.append(HostWalk(host, result=result))
    return walked
