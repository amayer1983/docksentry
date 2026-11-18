#!/usr/bin/env python3
"""The shared fixture for the procedures that drive a real daemon.

Two defects it exists to remove, both found on 10.10.2026 and both of
the same kind: the fixture reporting its own trouble as the program's.

**A pull that fails is not a broken promise.** Fourteen call sites
started a container with `capture_output=True` and never looked at the
return code — counted with `test_live_fixtures._unchecked_runs` against
`v2.18.0-beta.39`, after an audit pointed out that "twelve" was a
number I had not counted. On a machine with no route to a registry, or against a
rate-limited one, the container simply is not there — and the procedure
then reports that globbing, protection or dependent recreation is
broken. That is the worst possible reading of a red check: it points at
code that is fine, in the one place a reader has no reason to doubt the
measurement. `start()` makes the daemon's refusal say so.

**Two runs must not destroy each other.** Every name on the daemon was
fixed — `ds_ctf-a-even`, `ds_netns_head`, `ds_protect_test` — and each
file begins by force-removing them. Two runs at the same time therefore
pull each other's containers out from underneath, and the loser reports
a defect. Same for the two fixed ports, 12375 and 8791: the second run
cannot bind and reports the transport broken. `name()` and `free_port()`
make them per-run.

Nothing here stands in for a decision. The daemon is real, the
containers are real, and what is measured is what the program does with
them — only the *identifiers* and the *verdict on a failed fixture*
change.
"""

import os
import socket
import subprocess
import sys

#: Unique per process, so two suites running at once do not share a
#: single container name. The pid is enough: a name only has to be
#: unique among the processes alive at the same moment, and it stays
#: readable in `docker ps` while a procedure is being debugged.
TAG = f"-{os.getpid()}"


def name(stem):
    """A daemon-side name belonging to this run and no other."""
    return f"{stem}{TAG}"


def free_port():
    """A TCP port nobody is listening on, as of this moment.

    There is a gap between letting go of the socket and the daemon
    binding it, and nothing can close that gap from here — but it beats
    a constant, which is not free even on the first run if something
    else on the machine happens to use it.
    """
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def skip(why, checks=None):
    """The environment could not hold up its end. Say so and stop.

    `checks` is the dict the procedure has filled so far. It is reported
    rather than thrown away: a fixture that cannot start a container has
    not invalidated the checks that already ran, and discarding them is
    how a skip quietly became a pass.
    """
    print(f"SKIP: {why}")
    code = 0
    bad = [k for k, v in (checks or {}).items() if not v]
    for k, v in (checks or {}).items():
        print(f"  {'✅' if v else '❌'} {k}")
    if checks:
        print("FAIL" if bad else "PASS")
        code = 1 if bad else 0
    else:
        # Said out loud rather than exiting silently: a procedure that
        # prints only `SKIP:` and ends with 0 is indistinguishable in
        # the run log from one that measured something, and
        # `test_guard_mutations.py` reads a missing verdict as
        # "unrunnable" rather than as a skip.
        print("SKIP (no checks had run yet)")
    sys.exit(code)


def daemon_available(cli="docker"):
    """Can we actually drive `cli` here? Never raises.

    Nine procedures asked this themselves on v2.19.0, each with a bare
    `subprocess.run(["docker", "info"]).returncode` (counted with
    `git grep` on the tag: dependents_recreate, glob, image_inherit,
    multihost_live, netns_recreate, protect_stop, reaper,
    remote_transports, trust_running). That answers the daemon question
    and not the binary one: with `docker` absent from PATH the call
    raises `FileNotFoundError` before any return code exists, so the
    procedure died with a traceback and no verdict line. `verify.py`
    scores that as FAIL, and the mutation ratchet scores it
    [unrunnable] — a crash at the top hides every check below it, and
    the report lands far from the cause. Measured on a PATH without
    docker, 16.11.2026 and again on the v2.19.0 tree on 18.11.2026:
    8 of the 10 daemon-driving procedures (those nine plus
    podman_live) exited 1 with a traceback; remote_transports and
    podman_live skipped.

    A daemon that is present but refusing is the same answer as no
    daemon at all for every caller here, so both come back False.
    """
    try:
        return subprocess.run([cli, "info"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def require_daemon(cli="docker", checks=None):
    """Skip the procedure unless `cli` can be driven here."""
    if not daemon_available(cli):
        skip(f"no {cli} daemon available", checks)


def start(args, cli="docker", checks=None):
    """Start a container; a refusal here is the environment's fault.

    `args` is everything after the binary, so `["run", "-d", …]`.
    Returns stdout stripped — the container id, for a `run -d`.
    """
    # A MISSING binary is the environment too, and it used to be the one
    # case that was not handled: `subprocess.run` raises
    # `FileNotFoundError`, which left the procedure to die with a
    # traceback and no verdict line. `verify.py` reads that as FAIL, and
    # `test_guard_mutations.py` reads it as [unrunnable]: every check
    # after the crash never runs, and the report points nowhere near the
    # missing binary. Measured 16.11.2026 with `docker` taken off PATH:
    # 8 of the 10 daemon-driving procedures exited 1 with a traceback
    # instead of skipping (audit).
    try:
        r = subprocess.run([cli] + list(args), capture_output=True, text=True)
    except (FileNotFoundError, PermissionError, OSError) as e:
        skip(f"{cli} is not usable here — {e}", checks)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        skip(f"{cli} could not start the container — "
             f"{detail[-1][:200] if detail else 'no reason given'}", checks)
    return (r.stdout or "").strip()


def rm(*names, cli="docker"):
    """Remove these, whether they exist or not."""
    for n in names:
        if n:
            try:
                subprocess.run([cli, "rm", "-f", n], capture_output=True)
            except OSError:
                # Tearing down what was never started is not a failure.
                pass
