"""Stop, start and restart a container — the part that is not a chat (#63).

Both bots carried their own copy of this: the three refusals, the two CLI
calls, the protect check that reads a label before it reads the toggle.
Two copies of a rule is two chances to word it differently, and they did:
one chat said "⏳ Updates in progress — stop refused so it can't interfere
with a running update. Try again once they finish." and the other said
"An update is running — `stop` is refused until it finishes." Same event,
same day, two answers depending on which app you happened to have open.

The refusals, in the order they are checked and for the reasons they
exist:

  * Never stop or restart Docksentry itself. PID 1 dies before the
    recreate can finish — the same class of accident as #16. `/selfupdate`
    is the supported way.
  * Nothing while an update flow runs. A stop during the post-update
    health wait reads as unhealthy and triggers a rollback of a perfectly
    good update; a restart can land on a container that is mid
    stop/rename/recreate. The update machinery does not come through
    here, so this can never block an update's own steps.
  * Never stop a stop-protected container (#38) — typically the VPN or
    tunnel carrying the owner's remote access, i.e. the one whose loss
    also costs them the way back in. Restart stays allowed: brief
    downtime is acceptable, a permanent stop is the dangerous one.

Globs (#40, @LeeNX) live here rather than in one chat, which is why
Discord could not do `/stop web*` and Telegram could. Each match runs
through the same guards; nothing is exempt for being part of a batch.
"""

import fnmatch

from container_flags import Outcome, Reply, _is_local, resolve_container

#: Actions that could take Docksentry itself down with them.
_SELF_RISK = ("stop", "restart")


def is_glob(pattern):
    """True if this looks like a pattern rather than a (partial) name."""
    return any(c in (pattern or "") for c in "*?[")


def match_glob(pattern, *, backend, include_stopped=True):
    """Every container name matching `pattern`, case-insensitively.

    Stopped ones included by default — `/start web*` is only useful if it
    can see the containers that are not running. Our own `_old` rollback
    leftovers are filtered out: they are an implementation detail of the
    update path, and stopping one means nothing.
    """
    cmd = ["ps", "--format", "{{.Names}}"]
    if include_stopped:
        cmd.insert(1, "-a")
    try:
        result = backend.run(cmd)
    except Exception:
        return []
    names = [n.strip() for n in (getattr(result, "stdout", "") or "").strip().split("\n")
             if n.strip() and not n.strip().endswith("_old")]
    pl = (pattern or "").lower()
    return sorted(n for n in names if fnmatch.fnmatch(n.lower(), pl))


def is_protected(name, checker, store):
    """True if `name` must not be stopped.

    A `docksentry.protect` label on the container wins over the stored
    toggle (#42, @LeeNX): GitOps-style, the compose file is the source of
    truth for people who keep one. A failing inspect falls back to the
    toggle rather than to "not protected", so a flaky host can never
    accidentally unprotect the container you most wanted protected.
    """
    try:
        lab = checker.label_bool(checker.get_container_labels(name), "protect")
    except Exception:
        lab = None
    if lab is not None:
        return lab
    if store is None:
        return False
    try:
        return bool(store.is_protect_stop(name))
    except Exception:
        return False


def _guard(action, name, checker, store, update_running):
    """The refusal, or None. Order matters — see the module docstring."""
    if checker is None:
        return Reply("chan_no_backend_for", {"name": name}, ok=False)
    if action in _SELF_RISK:
        try:
            if checker._would_kill_self(name):
                return Reply("lifecycle_refused_self",
                             {"action": action, "name": name}, ok=False)
        except Exception:
            pass
    if update_running:
        return Reply("lifecycle_busy", {"action": action}, ok=False)
    if action == "stop" and is_protected(name, checker, store):
        return Reply("lifecycle_refused_protected", {"name": name}, ok=False)
    return None


def _run(action, name, checker, backend):
    """The CLI call itself. Returns a Reply."""
    if action == "stop":
        ok, detail = checker._stop_container(name)
        if ok:
            return Reply("lifecycle_stopped", {"name": name})
        return Reply("lifecycle_stop_failed",
                     {"name": name, "error": str(detail)[:200]}, ok=False)

    if action == "start":
        argv, timeout, k_ok, k_bad = (["start", name], 30,
                                      "lifecycle_started",
                                      "lifecycle_start_failed")
    elif action == "restart":
        # Graceful stop + start, with a generous timeout: gitlab and
        # gluetun both take their time coming down.
        argv, timeout, k_ok, k_bad = (["restart", "--time", "30", name], 120,
                                      "lifecycle_restarted",
                                      "lifecycle_restart_failed")
    else:
        return Reply("chan_unknown_action", {"action": action}, ok=False)

    try:
        r = backend.run(argv, timeout=timeout)
    except Exception as e:
        return Reply(k_bad, {"name": name, "error": str(e)[:200]}, ok=False)
    if getattr(r, "returncode", 1) == 0:
        return Reply(k_ok, {"name": name})
    return Reply(k_bad, {"name": name,
                         "error": (getattr(r, "stderr", "") or "").strip()[:200]},
                 ok=False)


def act(action, targets, *, backend_for, checker_for, store_for, partial,
        update_running=False, defaulted_to_local=False):
    """Run `action` on whatever `partial` names — one container or a glob.

    Returns an `Outcome` of the same shape every other core module
    returns, so both chats render it through the renderer they already
    have.

    A glob that matches nothing on ANY host is one `glob_no_match`, not
    one per host: "no container called web* here, and none there either"
    is the same answer said twice.
    """
    if not (partial or "").strip():
        return Outcome((), Reply("lifecycle_usage", ok=False),
                       defaulted_to_local, False)
    partial = partial.strip()

    replies = []
    changed = False

    if is_glob(partial):
        matched_any = False
        for host in targets:
            backend = backend_for(host)
            local = _is_local(host)
            names = match_glob(partial, backend=backend)
            if not names:
                continue
            matched_any = True
            checker, store = checker_for(host), store_for(host)
            replies.append(Reply("glob_action_header",
                                 {"action": action, "count": len(names),
                                  "pattern": partial},
                                 host=host, host_is_local=local))
            for name in names:
                r = _guard(action, name, checker, store, update_running)
                if r is None:
                    r = _run(action, name, checker, backend)
                    changed = changed or r.ok
                # No host tag on the individual lines: the header above
                # already names the host, and repeating `@nas` on each of
                # twenty lines is the kind of thoroughness that makes a
                # report harder to read, not easier.
                replies.append(Reply(r.key, r.params, ok=r.ok))
        if not matched_any:
            return Outcome((), Reply("glob_no_match", {"pattern": partial},
                                     ok=False),
                           defaulted_to_local, False)
        return Outcome(tuple(replies), None, defaulted_to_local,
                       changed, grouped=True)

    resolved_any = False
    first_err = None
    for host in targets:
        backend = backend_for(host)
        local = _is_local(host)
        name, err = resolve_container(partial, backend=backend)
        if err:
            # Held back, not sent: on a multi-host install a container
            # lives on one box, so "not found" from the other two is
            # noise around the one real answer. Only if NO host has it
            # does the error become the answer.
            first_err = first_err or err
            continue
        resolved_any = True
        r = _guard(action, name, checker_for(host), store_for(host),
                   update_running)
        if r is None:
            r = _run(action, name, checker_for(host), backend)
            changed = changed or r.ok
        replies.append(Reply(r.key, r.params, host=host, host_is_local=local,
                             ok=r.ok))
    if not resolved_any:
        return Outcome((), first_err or Reply("resolve_not_found",
                                              {"name": partial}, ok=False),
                       defaulted_to_local, False)
    return Outcome(tuple(replies), None, defaulted_to_local, changed)
