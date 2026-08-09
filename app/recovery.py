"""Picking up after Docksentry itself was killed.

Two things the next boot can say that the dying process could not.

**An update was in flight.** The rollback that guards every other failure
during a recreate lives in an `except` handler, and a SIGKILL raises
nothing — the process is simply gone. The container is left stopped and
renamed to `<name>_old`, and until now nobody looked for it. A user's
service could stay down indefinitely with no notification at all.
@NotRetarded's Docksentry exited 137 during an update and he only found out
because a third-party monitor told him (#2).

Recovery works from a journal written before the rename, not from the
`_old` suffix. Someone may legitimately run a container called `foo_old`,
and renaming theirs would be a worse bug than the one being fixed.

**The previous run ended badly.** The exit marker is only written on
SIGTERM/SIGINT; a hard kill writes nothing. The old code read an absent
marker as "first boot or unclean kill — we can't prove which" and stayed
silent. That was true when it was written and is not any more: since
v2.0.0 every successful start records its version, so a state file with no
exit marker beside it is a hard kill, provably.
"""

import json
import os
import time

#: Older than this and we no longer trust the journal to describe the
#: current state of the world — someone has had a day to intervene, and
#: renaming a container back on the strength of a stale note would be
#: worse than leaving it. Reported rather than acted on beyond this.
INFLIGHT_TRUST_SECONDS = 24 * 3600


def read_inflight(config):
    path = getattr(config, "inflight_file", "")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f) or None
    except (ValueError, OSError):
        return None


def clear_inflight(config):
    try:
        path = getattr(config, "inflight_file", "")
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def recover_interrupted_update(config, backend, t=None):
    """Finish or report an update that was cut off mid-swap.

    Returns a user-facing message, or "" when there is nothing to say.
    Never raises: this runs on the startup path and a failure here must
    not stop Docksentry from coming up.
    """
    rec = read_inflight(config)
    if not rec:
        return ""
    name = rec.get("name") or ""
    old_name = rec.get("old_name") or f"{name}_old"
    if not name:
        clear_inflight(config)
        return ""

    def _t(key, **kw):
        if t is None:
            return key
        return t(key, **kw)

    try:
        age = time.time() - float(rec.get("ts") or 0)
    except (TypeError, ValueError):
        age = 0

    try:
        present = _names(backend)
        live = name in present
        backup = old_name in present

        if live:
            # The swap completed; we died afterwards, or the container was
            # restored by hand. Nothing to repair.
            #
            # The backup, though, is ours and is now certainly stale — we
            # wrote the note that says so. This used to leave it, on the
            # stated grounds that "the cleanup grace period owns" it,
            # which is not true of anything: `cleanup_images` prunes
            # images, and `_prune_old_backups` deletes backup DIRECTORIES
            # on disk. Nothing in the process has ever removed a leftover
            # `<name>_old` CONTAINER. So every update whose process died
            # after the swap left one behind for good, and @LeeNX found
            # three of them and reasonably concluded his containers were
            # not updating (#56).
            #
            # Only when `name` is live: that is the proof the swap
            # finished and the backup is not the copy someone still
            # needs. And only the name from our own in-flight note — this
            # never goes looking for `*_old` containers in general,
            # because a container someone else named that way is theirs.
            if backup:
                backend.rm(old_name, force=True, timeout=30)
            clear_inflight(config)
            return ""

        if not backup:
            # Neither name exists. Something removed both, and inventing a
            # container here would be guesswork.
            clear_inflight(config)
            return _t("recovery_gone", name=name)

        if age > INFLIGHT_TRUST_SECONDS:
            # Old enough that the operator has had time to act. Say so;
            # do not move their containers around on a day-old note.
            clear_inflight(config)
            return _t("recovery_stale", name=name, old=old_name)

        r = backend.rename(old_name, name, timeout=15)
        if getattr(r, "returncode", 1) != 0:
            clear_inflight(config)
            return _t("recovery_failed", name=name, old=old_name)
        backend.run(["start", name], timeout=60)
        clear_inflight(config)
        return _t("recovery_restored", name=name)
    except Exception as e:                                  # pragma: no cover
        clear_inflight(config)
        return _t("recovery_failed", name=name, old=old_name) + f" ({e})"[:120]


def _names(backend):
    r = backend.ps(all=True, fmt="{{.Names}}", timeout=30)
    if getattr(r, "returncode", 1) != 0:
        raise RuntimeError("could not list containers")
    return {n for n in (r.stdout or "").split() if n}


def previous_run_died(config):
    """True when the last run was killed rather than asked to stop.

    The discriminator is the version state file: it is written on every
    successful start, so its presence proves a previous run existed, and
    the absence of an exit marker beside it proves that run never got to
    say goodbye. Neither alone is enough — which is why this was not
    reported before v2.0.0, when the state file did not exist.

    Called BEFORE the exit marker is consumed, so ordering matters.
    """
    state = getattr(config, "version_state_file", "")
    exit_marker = getattr(config, "last_exit_file", "")
    if not state or not exit_marker:
        return False
    return os.path.exists(state) and not os.path.exists(exit_marker)
