"""Reap the orphans that land on us because we are PID 1.

A container's PID 1 inherits every process whose parent exits, and it is
expected to `wait()` for them; nothing else will. Docksentry is PID 1
and never did, which was invisible until 2.17.6 taught ssh to keep a
connection open: `ControlPersist` backgrounds an ssh master, the
`docker` client that started it exits, and the master is reparented to
us. When it finally ends, nobody collects it. One zombie per connection,
each holding a PID slot forever. Measured on a live install: **12074**
of them, 9839 in a single hour, until the container could not fork and
every host failed at once — the `tcp://` ones included.

An init as ENTRYPOINT would be the textbook answer, and 2.17.10 shipped
one. It could not be kept: changing ENTRYPOINT broke the self-update for
every container running older code, twice, and it will do so again for
any straggler that has not updated in the meantime. The entrypoint is an
interface. This module fixes the leak without touching it.

Two rules keep it safe.

Only **zombies** are touched, found by reading `/proc` — a process that
has already exited and only waits to be collected. A live process is
never signalled, never waited for, never looked at.

Only zombies **we did not start**. `subprocess.run()` waits for its own
children by PID, and a reaper calling `waitpid(-1)` would race it for
them and could take an exit status out from under it — an update would
then report a success it never had. That is the failure I refused to
ship and still refuse. So this collects only what is provably not ours:
a zombie whose command is `ssh`. Docksentry never spawns `ssh`; the
docker client does. Each one is collected with `waitpid(pid)` on its
own PID, which takes nothing from anyone else.

Everything else — a zombie of any other name — is left alone and
counted, so a second source of orphans would show up in the log rather
than being papered over.
"""
import os
import threading
import time

#: Commands we know we never start ourselves. A zombie of this name is a
#: leftover from something the docker client ran, and collecting it can
#: take nothing from `subprocess`. Grows only with a measured case.
FOREIGN = frozenset({"ssh"})

INTERVAL = 60.0

_thread = None
_lock = threading.Lock()
_stats = {"reaped": 0, "left": 0, "passes": 0}


def _zombie_children_of_pid1():
    """`[(pid, comm)]` for every zombie whose parent is PID 1."""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", "rb") as f:
                status = f.read(4096).decode("ascii", "replace")
        except OSError:
            continue                                  # gone between list and read
        state = ppid = comm = None
        for line in status.split("\n"):
            if line.startswith("State:"):
                state = line.split()[1] if len(line.split()) > 1 else ""
            elif line.startswith("PPid:"):
                ppid = line.split()[1] if len(line.split()) > 1 else ""
            elif line.startswith("Name:"):
                comm = line[5:].strip()
            if state is not None and ppid is not None and comm is not None:
                break
        if state == "Z" and ppid == "1":
            out.append((int(name), comm or ""))
    return out


def reap_once():
    """Collect the foreign zombies. Returns `(reaped, left)`.

    `left` is the number of zombies that are children of PID 1 and were
    NOT collected because their command is not on the list — those are
    either ours in the instant before `subprocess` collects them, or a
    new source that deserves to be seen rather than hidden.
    """
    if os.getpid() != 1:
        # Not PID 1 — somebody runs us under an init or outside a
        # container, and orphans are that init's problem, not ours.
        return 0, 0
    reaped = left = 0
    for pid, comm in _zombie_children_of_pid1():
        if comm not in FOREIGN:
            left += 1
            continue
        try:
            got, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue                                  # collected meanwhile
        if got == pid:
            reaped += 1
    with _lock:
        _stats["reaped"] += reaped
        _stats["left"] = left
        _stats["passes"] += 1
    return reaped, left


def stats():
    with _lock:
        return dict(_stats)


def _loop():
    first = True
    while True:
        try:
            reaped, left = reap_once()
            if reaped and first:
                # Say it once, so the log shows the leak is being handled
                # — and never again, so it does not become the leak's
                # noise instead of its fix.
                print(f"Reaper: collected {reaped} orphaned ssh process(es) "
                      f"left on PID 1 — will keep doing so quietly")
                first = False
            if left >= 50:
                # A zombie population we are NOT collecting means a
                # source this file does not know about. Worth a line.
                print(f"Reaper: {left} zombie(s) on PID 1 that are not ssh "
                      f"— not collecting those; something else leaks")
        except Exception as e:                        # noqa: BLE001
            print(f"Reaper pass failed (non-fatal): {e}")
        time.sleep(INTERVAL)


def start():
    """Start the reaper thread, once. Harmless when not PID 1."""
    global _thread
    if os.getpid() != 1:
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _thread = threading.Thread(target=_loop, name="reaper", daemon=True)
    _thread.start()
    return True
