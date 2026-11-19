"""What `/pin`, `/autoupdate`, `/protect`, `/note` and `/cooldown` DO (#63).

Six membership flags and three value settings, each written twice: once in
the Telegram dispatcher, once as a Discord command method. The same work,
in two places, drifting — measured before this module existed:

  * Discord had no `@all`; `_write_hosts_for` never looked for the
    sentinel, so `host: all` fell through to "unknown host";
  * Discord's `/note`, `/trustrunning` and `/askmajor` registered no host
    option at all, so they called `_write_hosts_for(None)` forever and
    were silently local-only while looking host-aware;
  * Telegram's container resolver had no guard around `backend.run`, so a
    host that refused the connection raised through the poll loop and the
    user got no answer at all — just a five-second pause;
  * `/unpin`'s rule — resolve against the STORED list, not against what is
    running, so a container removed from the host can still have its stale
    pin lifted — was implemented separately in both, correctly in both,
    which is luck rather than design.

So the behaviour lives here and the connections keep what is theirs: the
syntax they parse (`@nas` versus a `host:` option), the markup they emit,
whether they send one message per host or join them, and whether they show
the host tag at all.

**The return value carries keys, not sentences.** A front end calls
`t(reply.key, **reply.params)` and renders. That is what lets Telegram
answer per host while Discord joins into one clipped blob, and what keeps
the shared translations the single source of wording.
"""

from collections import namedtuple
from dataclasses import dataclass, field, replace


#: How a name is matched. `/unpin` matches against the stored list; the
#: rest against what the host is actually running. See the module
#: docstring for why that difference is deliberate.
FROM_RUNTIME = "runtime"
FROM_STORE = "store"

#: What a flag command does to the list.
ADD = "add"
REMOVE = "remove"
TOGGLE = "toggle"


@dataclass(frozen=True)
class Reply:
    """One thing to say, as a key the caller translates."""
    key: str
    params: dict = field(default_factory=dict)
    #: The managed host this is about; None on a single-host install,
    #: which is what every call site already walks as the pseudo-host.
    host: object = None
    #: Whether that host is the local one — the front end decides whether
    #: a tag is worth showing, and the two disagree on purpose.
    host_is_local: bool = True
    ok: bool = True
    #: List mode: this host's names, unprefixed. `values` carries the
    #: numbers when the flag has any (cooldown).
    items: tuple = ()
    values: dict = None
    #: An already-translated sentence, for the one case where the
    #: producer is not us: `cleanup_images` assembles its own message
    #: from four keys and a docker size, and re-deriving that here would
    #: mean re-implementing it. Set INSTEAD of `key`, never alongside.
    #: Everything else on this dataclass is a key, and should stay that
    #: way — this field is a door, not a corridor.
    text: str = ""


@dataclass(frozen=True)
class Outcome:
    replies: tuple = ()
    #: Set when nothing was written and nothing else will be tried — an
    #: unknown host, a value that will not parse. Both front ends already
    #: treat that differently from a per-host failure.
    fatal: Reply = None
    #: No host was named on a multi-host install, so this acted locally.
    #: Telegram appends a hint; Discord says it in the option description.
    defaulted_to_local: bool = False
    changed: bool = False
    #: One logical answer, however many replies it took. `/stop web*`
    #: across twenty containers is ONE thing the user asked, and Telegram
    #: sending it as twenty-one messages is a flood, not a report.
    #: Discord joins everything anyway, so this only changes one side.
    grouped: bool = False


@dataclass(frozen=True)
class FlagSpec:
    """One membership flag, described rather than coded."""
    read: str
    save: str = None
    toggle: str = None
    mode: str = TOGGLE
    resolve: str = FROM_RUNTIME
    k_on: str = ""
    k_off: str = ""
    k_noop: str = None
    k_list: str = None
    k_empty: str = None


FLAGS = {
    "pin": FlagSpec(read="get_pinned", save="save_pinned", mode=ADD,
                    resolve=FROM_RUNTIME, k_on="pin_added",
                    k_noop="pin_already", k_list="pin_list",
                    k_empty="pin_empty"),
    "unpin": FlagSpec(read="get_pinned", save="save_pinned", mode=REMOVE,
                      resolve=FROM_STORE, k_on="unpin_removed",
                      k_noop="unpin_not_found"),
    "autoupdate": FlagSpec(read="get_autoupdate", toggle="toggle_auto",
                           k_on="autoupdate_on", k_off="autoupdate_off",
                           k_list="autoupdate_list", k_empty="autoupdate_empty"),
    "protect": FlagSpec(read="get_protect_stop", toggle="toggle_protect_stop",
                        k_on="protect_on", k_off="protect_off",
                        k_list="protect_list", k_empty="protect_empty"),
    "trustrunning": FlagSpec(read="get_trust_running",
                             toggle="toggle_trust_running",
                             k_on="trust_on", k_off="trust_off",
                             k_list="trust_list", k_empty="trust_empty"),
    "askmajor": FlagSpec(read="get_ask_before_major",
                         toggle="toggle_ask_before_major",
                         k_on="askmajor_on", k_off="askmajor_off",
                         k_list="askmajor_list", k_empty="askmajor_empty"),
}


def item_parts(item):
    """One list entry as `(name, detail)`.

    Most lists are bare names; `/cooldown` carries a value per name. The
    front ends render both shapes through this rather than each deciding
    what a tuple in `items` means — the last time two connections each
    made that call, one of them grew a feature the other did not have.
    """
    if isinstance(item, (tuple, list)):
        name = item[0]
        return name, (item[1] if len(item) > 1 else "")
    return item, ""


def _is_local(host):
    return host is None or bool(getattr(host, "is_local", False))


#: What a command does to the hosts it reaches — the one thing that picks
#: the default when nobody named a host.
TARGET_MODES = ("read", "write")


def targets_for(mode, registry, host_token):
    """Which hosts a command acts on — the one place that decides it.

    `host_token` is already parsed — `@nas` on Telegram, a `host:` option
    on Discord, both arriving here as a name, `hosts.ALL_HOSTS`, or None.
    Parsing the syntax is the connection's job; deciding what the answer
    MEANS is not, which is why Discord had no `@all` until this moved.

    `mode` picks the no-token default, and that is the whole rule: a
    command that only LOOKS ("read") defaults to every host, one that
    CHANGES something ("write") stays on the local one. Turning
    auto-update on for `nginx` because no host was named must not turn it
    on for every host's `nginx` — the #7 state collision.

    Returns `(targets, fatal)`. `targets` is `[None]` on a single-host
    install — the pseudo-host every call site already walks, and what
    keeps single-host replies byte-for-byte what they were. `fatal` is a
    `Reply` when the named host isn't one we manage, with `targets == []`;
    callers send it and do nothing else, because falling back to "all" or
    to local would act on the wrong box.
    """
    if mode not in TARGET_MODES:
        raise ValueError(f"unknown target mode {mode!r}")
    multi = registry is not None and getattr(registry, "is_multi", False)
    if not multi:
        return [None], None
    from hosts import ALL_HOSTS
    if host_token is None:
        return (list(registry) if mode == "read" else [registry.local]), None
    if host_token == ALL_HOSTS:
        return list(registry), None
    host = registry.get(host_token)
    if host is None:
        names = ", ".join(f"`{n}`" for n in registry.names)
        return [], Reply("host_unknown", {"name": host_token, "hosts": names},
                         ok=False)
    return [host], None


def targets_for_read(registry, host_token):
    """`targets_for` for a command that only looks: no host named means
    every host."""
    return targets_for("read", registry, host_token)


def targets_for_write(registry, host_token):
    """`targets_for` for a command that changes something: no host named
    means the local one."""
    return targets_for("write", registry, host_token)


def resolve_container(partial, *, backend=None, names=None):
    """A partial name to a real one. Returns `(name, Reply|None)`.

    `names` given: match against that list — the `/unpin` rule. Otherwise
    ask the backend what it is running.

    The backend call is guarded. Telegram's copy was not, and a host that
    refused the connection raised through the poll loop: no answer, no
    log line the user would find, a five-second pause and silence.
    """
    if names is None:
        if backend is None:
            return None, Reply("chan_no_backend", ok=False)
        try:
            result = backend.run(["ps", "-a", "--format", "{{.Names}}"])
        except Exception as e:
            return None, Reply("chan_list_failed", {"error": str(e)[:80]},
                               ok=False)
        names = [n.strip() for n in (getattr(result, "stdout", "") or "").strip().split("\n")
                 if n.strip() and not n.strip().endswith("_old")]
    if partial in names:
        return partial, None
    matches = [n for n in names if n.lower().startswith(partial.lower())]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, Reply("resolve_multiple",
                           {"names": ", ".join(f"`{m}`" for m in matches)},
                           ok=False)
    return None, Reply("resolve_not_found", {"name": partial}, ok=False)


def _listed_names(result):
    """The names a `ps --format {{.Names}}` printed, minus our own `_old`
    rollback leftovers — an implementation detail of the update path."""
    return [n.strip() for n in (getattr(result, "stdout", "") or "").strip().split("\n")
            if n.strip() and not n.strip().endswith("_old")]


def resolve_container_checked(partial, *, backend, timeout=None):
    """`resolve_container`, but a host that did not answer says so.

    Returns `(name, reply, failure)`. `failure` is set when the listing
    itself failed — the exception the backend raised, or `(returncode,
    stderr)` when the CLI exited non-zero — and then `name` and `reply`
    are both None: nothing was looked at, so nothing is "not found".

    `resolve_container` reads a non-zero exit as an empty host, so a host
    whose SSH refuses answers "Container `web` not found." — measured in
    test_find_on_hosts, where it stays pinned, because that function
    keeps its callers until they move here.

    A plain name always looks at stopped containers too, like every
    partial-name lookup in both chats. `timeout` None leaves the CLI's
    own default and passes nothing.
    """
    cmd = ["ps", "-a", "--format", "{{.Names}}"]
    try:
        result = (backend.run(cmd) if timeout is None
                  else backend.run(cmd, timeout=timeout))
    except Exception as e:                                  # noqa: BLE001
        return None, None, e
    rc = getattr(result, "returncode", 0)
    if rc != 0:
        return None, None, (rc, getattr(result, "stderr", "") or "")
    name, reply = resolve_container(partial, names=_listed_names(result))
    return name, reply, None


class Unreachable(namedtuple("Unreachable", "host error hint")):
    """A host that did not answer: `(host, error_text, hint)`.

    `error` is meant for a person (a timeout says how long we waited);
    `hint` is hostdiag's sentence for an SSH host, or "". `reply()` is
    the `host_unreachable_short` one-liner Discord renders today, the
    hint goes underneath. Telegram still says `status_host_unreachable`.
    """
    __slots__ = ()

    def reply(self):
        from container_store import LOCAL_HOST
        return Reply("host_unreachable_short",
                     {"host": getattr(self.host, "name", "") or LOCAL_HOST,
                      "error": self.error},
                     host=self.host, host_is_local=_is_local(self.host),
                     ok=False)


#: One host's answer, as `on_host` sees it: exactly one of `names`
#: (non-empty), `error` (a Reply: none there, or ambiguous) and
#: `unreachable` is set.
HostFind = namedtuple("HostFind", "host names error unreachable")


@dataclass(frozen=True)
class FindResult:
    #: `(host, names)` for every host with a match, in walk order.
    found: tuple = ()
    #: One Reply per host that answered without a match, `host` set —
    #: or that has no backend at all (`chan_no_backend`).
    errors: tuple = ()
    #: One `Unreachable` per host that did not answer.
    unreachable: tuple = ()

    @property
    def first_error(self):
        """What to say when nothing was found anywhere: the first
        answering host's Reply. None when every host was unreachable —
        then there is nothing to call "not found"."""
        return self.errors[0] if self.errors else None


def _exit_text(rc, t):
    """A non-zero exit that said nothing on stderr, for a person."""
    if t is not None:
        try:
            return t("error_exit_code", code=rc)
        except Exception:                                   # noqa: BLE001
            pass
    return f"exit code {rc}"


def _human(err, t):
    import errfmt
    try:
        return errfmt.human(err, t=t)
    except Exception:                                       # noqa: BLE001
        return errfmt.human(err)


def find_on_hosts(targets, *, backend_for, arg, include_stopped=True,
                  on_host=None, timeout=None, t):
    """Find `arg` — a plain partial name or a glob — on each of `targets`.

    The hosts are asked one after the other, in the order given, and
    `on_host(HostFind)` runs for each before the next one is touched, so
    a chat can send a host's answer as soon as it has it. An exception
    from `on_host` is the caller's and is not swallowed.

    Unreachable is an exception from the backend OR a CLI exit ≠ 0: a
    dead host raises its timeout, a refused SSH returns 255 with empty
    output, and neither may come back as "not found" or take the whole
    command down. Nothing a host does is raised from here — but a bad
    argument from the caller is: only `backend_for` and the backend's
    own `run` are treated as the host's doing.

    `backend_for` answering None is "no backend", like the old
    resolvers say it (`chan_no_backend` in `errors`), not unreachable.

    `include_stopped` applies to a glob; a plain name always looks at
    stopped containers too (`resolve_container_checked`). `t` translates
    the timeout line and hostdiag's hint.
    """
    import lifecycle
    is_glob = lifecycle.is_glob(arg)
    found, errors, unreachable = [], [], []
    for host in targets:
        names, reply, failure = [], None, None
        try:
            backend = backend_for(host)
        except Exception as e:                              # noqa: BLE001
            backend, failure = None, e
        if failure is not None:
            pass
        elif backend is None:
            reply = Reply("chan_no_backend", ok=False)
        elif is_glob:
            names, failure = lifecycle.match_glob_checked(
                arg, backend=backend, include_stopped=include_stopped,
                timeout=timeout)
            if failure is None and not names:
                reply = Reply("glob_no_match", {"pattern": arg}, ok=False)
        else:
            name, reply, failure = resolve_container_checked(
                arg, backend=backend, timeout=timeout)
            names = [name] if name else []
        gone = None
        if failure is not None:
            if isinstance(failure, tuple):
                rc, stderr = failure
                raw = stderr
                text = (_human(stderr, t) if stderr.strip()
                        else _exit_text(rc, t))
            else:
                raw = str(failure)
                text = _human(failure, t)
            hint = ""
            try:
                import hostdiag
                hint = hostdiag.hint(getattr(host, "endpoint", ""), raw,
                                     t=t) or ""
            except Exception:                               # noqa: BLE001
                hint = ""
            gone = Unreachable(host, text, hint)
            unreachable.append(gone)
            names, reply = [], None
        elif names:
            found.append((host, tuple(names)))
        else:
            reply = replace(reply, host=host, host_is_local=_is_local(host))
            errors.append(reply)
        if on_host is not None:
            on_host(HostFind(host, tuple(names), reply, gone))
    return FindResult(tuple(found), tuple(errors), tuple(unreachable))


def apply_flag(spec, targets, *, store_for, backend_for=None, partial=None,
               defaulted_to_local=False):
    """Set, clear or toggle one flag — or list it when `partial` is None.

    `store_for` and `backend_for` are the connection's own one-line
    resolvers, passed rather than imported: the LOCAL host resolves to the
    front end's own objects, and routing through `update_engine.host_store`
    from here would be an import edge this module does not want.
    """
    replies = []
    changed = False

    if partial is None:                           # list mode — a read
        # An empty host is silence, not a "nothing here" line of its own:
        # a pin set on @srv30 must not come buried under four "none"s from
        # the hosts that have none. Only when nothing is set on ANY host
        # does one empty line answer the question — and a single-host
        # install still gets exactly that one line, host-less, byte-for-
        # byte as before (#7).
        any_listed = False
        for host in targets:
            current = list(getattr(store_for(host), spec.read)() or [])
            if current:
                any_listed = True
                replies.append(Reply(spec.k_list or "", host=host,
                                     host_is_local=_is_local(host),
                                     items=tuple(sorted(current))))
        if not any_listed:
            _h = targets[0] if len(targets) == 1 else None
            replies.append(Reply(spec.k_empty or "", host=_h,
                                 host_is_local=(_is_local(_h)
                                                if _h is not None else True)))
        return Outcome(tuple(replies), None, defaulted_to_local, False)

    for host in targets:
        store = store_for(host)
        local = _is_local(host)
        current = list(getattr(store, spec.read)() or [])

        if spec.resolve == FROM_STORE:
            name, err = resolve_container(partial, names=current)
            # "not in the list" reads better than "no such container" when
            # the list IS the question: `/unpin nginx` on a container that
            # was never pinned should say it is not pinned. Ambiguity keeps
            # the shared wording — that answer is the same either way.
            if err is not None and err.key == "resolve_not_found" and spec.k_noop:
                err = Reply(spec.k_noop, err.params, ok=False)
        else:
            name, err = resolve_container(
                partial, backend=backend_for(host) if backend_for else None)
        if err:
            replies.append(Reply(err.key, err.params, host=host,
                                 host_is_local=local, ok=False))
            continue

        if spec.mode == TOGGLE:
            on = bool(getattr(store, spec.toggle)(name))
            changed = True
            replies.append(Reply(spec.k_on if on else spec.k_off,
                                 {"name": name}, host=host,
                                 host_is_local=local))
        elif spec.mode == ADD:
            if name in current:
                replies.append(Reply(spec.k_noop, {"name": name}, host=host,
                                     host_is_local=local))
                continue
            current.append(name)
            getattr(store, spec.save)(current)
            changed = True
            replies.append(Reply(spec.k_on, {"name": name}, host=host,
                                 host_is_local=local))
        else:                                     # REMOVE
            current.remove(name)
            getattr(store, spec.save)(current)
            changed = True
            replies.append(Reply(spec.k_on, {"name": name}, host=host,
                                 host_is_local=local))
    return Outcome(tuple(replies), None, defaulted_to_local, changed)


def set_note(targets, *, store_for, backend_for, partial, text,
             defaulted_to_local=False):
    """`/note` — attach a note, or clear it when `text` is empty."""
    replies = []
    changed = False
    for host in targets:
        store = store_for(host)
        local = _is_local(host)
        name, err = resolve_container(partial, backend=backend_for(host))
        if err:
            replies.append(Reply(err.key, err.params, host=host,
                                 host_is_local=local, ok=False))
            continue
        store.set_note(name, text)
        changed = True
        # `text`, not `note`: that is what the template says, in all
        # sixteen languages. Renaming it during the extraction turned
        # every `/note` confirmation into "📝 Note on `web`: —" — saved
        # correctly, reported as nothing, in both chats at once (#63).
        replies.append(Reply("note_set" if text else "note_cleared",
                             {"name": name, "text": text}, host=host,
                             host_is_local=local))
    return Outcome(tuple(replies), None, defaulted_to_local, changed)


def set_cooldown(targets, *, store_for, backend_for, partial, seconds,
                 defaulted_to_local=False):
    """`/cooldown` — set, show or clear the per-container cooldown.

    `partial` of None lists every container that has one. `seconds` is
    the raw argument. It is parsed ONCE, before any host is
    touched, so a value that will not parse writes nothing anywhere;
    Telegram used to parse inside the loop and abort halfway, having
    already answered for the first host. `None` means "show the current
    value". The [0, 600] clamp stays in the store, which returns what it
    actually stored — so `9999` is answered as `600`, not as accepted.
    """
    if partial is None:
        # No container named: list what is set, the way every other flag
        # command lists its members. Telegram could do this and Discord
        # could not, which is the asymmetry this module exists to end —
        # and asking "which of my containers have a cooldown?" is a
        # perfectly reasonable question from either chat.
        # Empty hosts stay silent, one "none" only when nothing is set
        # anywhere — same rule as apply_flag's list mode, so a cooldown on
        # @srv30 is not buried under the others' "none"s.
        replies = []
        any_listed = False
        for host in targets:
            cds = dict(getattr(store_for(host), "get_cooldowns")() or {})
            if cds:
                any_listed = True
                replies.append(Reply("cooldown_list", host=host,
                                     host_is_local=_is_local(host),
                                     items=tuple((n, f"{v}s")
                                                 for n, v in cds.items())))
        if not any_listed:
            _h = targets[0] if len(targets) == 1 else None
            replies.append(Reply("cooldown_empty", host=_h,
                                 host_is_local=(_is_local(_h)
                                                if _h is not None else True)))
        return Outcome(tuple(replies), None, defaulted_to_local, False)

    show = seconds is None
    value = None
    if not show:
        try:
            value = int(seconds)
        except (TypeError, ValueError):
            return Outcome((), Reply("cooldown_bad_value",
                                     {"value": seconds}, ok=False),
                           defaulted_to_local, False)
    replies = []
    changed = False
    for host in targets:
        store = store_for(host)
        local = _is_local(host)
        name, err = resolve_container(partial, backend=backend_for(host))
        if err:
            replies.append(Reply(err.key, err.params, host=host,
                                 host_is_local=local, ok=False))
            continue
        if show:
            replies.append(Reply("cooldown_current",
                                 {"name": name,
                                  "seconds": store.get_cooldown(name)},
                                 host=host, host_is_local=local))
            continue
        applied = store.set_cooldown(name, value)
        changed = True
        replies.append(Reply("cooldown_set" if applied else "cooldown_cleared",
                             {"name": name, "seconds": applied}, host=host,
                             host_is_local=local))
    return Outcome(tuple(replies), None, defaulted_to_local, changed)


def set_link(targets, *, store_for, backend_for, partial, url,
             defaulted_to_local=False):
    """`/setlink` — the repo or changelog link for one container.

    The store validates the URL and says whether it took it: anything but
    http(s) is refused there, because a link is the one thing a chat and
    the Web UI both render as clickable, and the check belongs where the
    write happens rather than in each front end.
    """
    replies = []
    changed = False
    for host in targets:
        store = store_for(host)
        local = _is_local(host)
        name, err = resolve_container(partial, backend=backend_for(host))
        if err:
            replies.append(Reply(err.key, err.params, host=host,
                                 host_is_local=local, ok=False))
            continue
        if not url:
            store.set_link(name, "")
            changed = True
            replies.append(Reply("setlink_cleared", {"name": name}, host=host,
                                 host_is_local=local))
        elif store.set_link(name, url):
            changed = True
            replies.append(Reply("setlink_set", {"name": name, "url": url},
                                 host=host, host_is_local=local))
        else:
            # The store refused it. Telegram's wording says what IS
            # accepted, which is the more useful half of the answer.
            replies.append(Reply("setlink_invalid", {"url": url}, host=host,
                                 host_is_local=local, ok=False))
    return Outcome(tuple(replies), None, defaulted_to_local, changed)


def read_logs(targets, *, backend_for, partial, tail=30):
    """The last `tail` log lines of one container, from whichever host has
    it. Returns `(name, host, text, Reply|None)`.

    A container lives on one host as a rule, so the first host that has it
    wins and the misses stay quiet — the same rule `/status` follows.
    Only a sweep that found it nowhere answers with the error.

    `backend.logs()` rather than a hand-built argv: the stdout/stderr
    merge lives in that method, and a call site that built its own
    dropped half the output — the half worth reading (#2, @NotRetarded).
    """
    first_err = None
    for host in targets:
        backend = backend_for(host)
        name, err = resolve_container(partial, backend=backend)
        if err:
            if first_err is None:
                first_err = Reply(err.key, err.params, host=host,
                                  host_is_local=_is_local(host), ok=False)
            continue
        try:
            r = backend.logs(name, tail=tail, timeout=10)
        except Exception as e:
            return name, host, "", Reply("logs_failed",
                                         {"name": name, "error": str(e)[:80]},
                                         host=host,
                                         host_is_local=_is_local(host),
                                         ok=False)
        text = (getattr(r, "stdout", "") or "") or (getattr(r, "stderr", "") or "")
        if not text.strip():
            return name, host, "", Reply("logs_empty", {"name": name},
                                         host=host,
                                         host_is_local=_is_local(host))
        return name, host, text.strip(), None
    return None, None, "", first_err


def reclaimable(targets, *, checker_for):
    """How much each host could free. Returns `(replies, total)`.

    A host that will not answer is reported rather than dropped: silence
    there reads as "nothing to reclaim", which is the opposite of what an
    unreachable host means.
    """
    replies = []
    total = 0
    for host in targets:
        checker = checker_for(host)
        local = _is_local(host)
        if checker is None:
            continue
        try:
            # Prefer the breakdown: it RAISES when `docker system df` itself
            # fails (e.g. a socket-proxy blocking /system/df, HTTP 403), so a
            # host we cannot read surfaces as "could not be checked" below
            # instead of a silent "nothing to reclaim". reclaimable_bytes()
            # deliberately swallows that to 0 for the auto-cleanup path, so it
            # cannot be the signal here — read the breakdown directly.
            detail = {}
            if hasattr(checker, "reclaimable_breakdown"):
                detail = checker.reclaimable_breakdown() or {}
                free = int(detail.get("images", 0) or 0)
            else:
                free = int(checker.reclaimable_bytes() or 0)
            grace = None
            if hasattr(checker, "grace_holds_back"):
                grace = checker.grace_holds_back()
        except Exception as e:
            replies.append(Reply("host_check_failed",
                                 {"host": getattr(host, "name", "local"),
                                  "error": str(e)[:80]},
                                 host=host, host_is_local=local, ok=False))
            continue
        total += free
        replies.append(Reply("chan_reclaim_none" if free <= 0
                             else "chan_reclaim_some",
                             {"tag": "", "size": free}, host=host,
                             host_is_local=local,
                             values={"bytes": free, "breakdown": detail,
                                     "grace": grace}))
    return tuple(replies), total



def cleanup(targets, *, checker_for, guarded_run, defaulted_to_local=False):
    """Run image cleanup on every named host. Returns an `Outcome`.

    `guarded_run(checker)` is the caller's mutex wrapper — it returns
    `(ok, message)` from `cleanup_images`, or `(None, message)` when an
    update flow holds the lock. It stays outside because the lock lives
    on the engine, and because the scheduler and the Web UI call the same
    wrapper without going anywhere near a chat.

    Every managed host, not just the local one. @famewolf ran `/cleanup`
    while dockmox was drowning in wrongly-pulled images and got an answer
    for one machine out of three — "the cleanup is only running
    locally?" (#2). Telegram learned to walk them all; Discord did not,
    and this is where that stops being a per-chat decision.

    One unreachable host reports and does not stop the rest: the whole
    point of running this is the box that is full, and giving up at the
    first refused connection is how you never reach it.
    """
    replies = []
    changed = False
    for host in targets:
        local = _is_local(host)
        checker = checker_for(host)
        if checker is None:
            replies.append(Reply("chan_no_backend", host=host,
                                 host_is_local=local, ok=False))
            continue
        try:
            ok, msg = guarded_run(checker)
        except Exception as e:
            replies.append(Reply("cleanup_error", {"error": str(e)[:200]},
                                 host=host, host_is_local=local, ok=False))
            continue
        # `ok is None` means "skipped, an update holds the lock" — not a
        # failure, and it must not be dressed as one. The message is
        # already the translated `cleanup_busy`.
        replies.append(Reply("", host=host, host_is_local=local,
                             ok=ok is not False, text=str(msg)))
        changed = changed or bool(ok)
    return Outcome(tuple(replies), None, defaulted_to_local, changed)

def audit_container(targets, *, backend_for, checker_for, partial):
    """Which inspect fields a recreate would NOT carry over.

    Returns `(name, host, findings, Reply|None)`. `findings` is the dict
    `UpdateChecker._audit_inspect_coverage` produces — `host_unknown`,
    `config_unknown`, `host_dropped` — and the front ends lay it out.

    Sweeps the hosts like `/logs` does, first one that has the container
    wins. Discord used to look at `targets[0]` alone, so `/audit` on a
    container that lives on the second managed host answered "not found"
    while Telegram found it (#63).
    """
    import json as _json
    first_err = None
    for host in targets:
        backend = backend_for(host)
        name, err = resolve_container(partial, backend=backend)
        if err:
            if first_err is None:
                first_err = Reply(err.key, err.params, host=host,
                                  host_is_local=_is_local(host), ok=False)
            continue
        try:
            r = backend.run(["inspect", name], timeout=10)
            if getattr(r, "returncode", 1) != 0:
                # The daemon's own words. "Couldn't inspect `web`" on its
                # own tells you nothing you did not already know from the
                # command not working.
                return name, host, None, Reply(
                    "audit_inspect_failed",
                    {"name": name,
                     "error": (getattr(r, "stderr", "") or "").strip()[:80]},
                    host=host, host_is_local=_is_local(host), ok=False)
            inspect = _json.loads(r.stdout)[0]
        except Exception as e:
            return name, host, None, Reply(
                "audit_inspect_failed", {"name": name, "error": str(e)[:80]},
                host=host, host_is_local=_is_local(host), ok=False)
        from update_checker import UpdateChecker
        findings = UpdateChecker._audit_inspect_coverage(
            checker_for(host), inspect)
        return name, host, findings, None
    return None, None, None, first_err


def update_history(path, *, wanted="", limit=10):
    """The last `limit` update-history rows, newest first.

    Returns `(rows, Reply|None)`. A row is a plain dict — the front ends
    render the icon, the indent and the container name themselves.

    The file is instance-wide, not per host: an update on the NAS and one
    at home go into the same log, which is why this takes a path rather
    than a host. Both front ends read it separately before, with their
    own idea of how many rows to show and what a missing file means.
    """
    import json as _json
    import os as _os
    if not path or not _os.path.exists(path):
        return [], Reply("history_empty", ok=False)
    try:
        with open(path, encoding="utf-8") as f:
            history = _json.load(f)
    except (OSError, ValueError):
        return [], Reply("history_empty", ok=False)
    if not isinstance(history, list) or not history:
        return [], Reply("history_empty", ok=False)
    rows = [h for h in history if isinstance(h, dict)]
    if wanted:
        needle = wanted.strip().lower()
        rows = [h for h in rows
                if needle in str(h.get("container", "")).lower()]
        if not rows:
            return [], Reply("chan_history_none_for", {"name": wanted},
                             ok=False)
    # Legacy v1.16.1 rows carry a different calendar glyph. Normalised
    # here so both front ends render the same stored string — they each
    # did their own replace before, which is two chances to forget.
    out = []
    for h in reversed(rows[-limit:]):
        h = dict(h)
        h["detail"] = str(h.get("detail", "")).replace("📅", "🗓️")
        out.append(h)
    return out, None
