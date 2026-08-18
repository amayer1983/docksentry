#!/usr/bin/env python3
"""Say why the SSH host refuses, not just that it did (#2, @famewolf).

He set up key-based login between his three machines, tested it, added
`DOCKER_HOSTS`, and lost two days to an instance that reported three
managed hosts and could reach one. When the error finally arrived
untruncated it said `Permission denied (publickey)` — correct, and
useless from where he was standing, because the keys *did* work.

They did. On the host. Docksentry runs in a container, and a container
has its own filesystem: `ssh-copy-id` wrote to `/root/.ssh` on the
machine, and the image has no `/root/.ssh` at all. He is the second
person to hit this, which is the point at which a message should stop
making people deduce it.

Everything checked here is a **measurement taken at the moment of
failure** — does that directory exist, does that file exist — not a
guess from the shape of the error. An error we cannot explain gets no
guess attached to it, because a confident wrong hint is worse than
none: it sends somebody looking in the place we named.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import hostdiag  # noqa: E402

checks = {}

REFUSED = ("could not list containers (rc=1): error during connect: command "
           "ssh -l root -- docknas.lan docker system dial-stdio has exited "
           "with exit status 255, stderr=ssh: Permission denied "
           "(publickey,password).")
HOSTKEY = ("error during connect: command ssh … has exited with exit status "
           "255, stderr=Host key verification failed.")
REFUSED_CONN = ("error during connect: ssh: connect to host nas port 22: "
                "Connection refused")

with tempfile.TemporaryDirectory() as tmp:
    empty = os.path.join(tmp, "nothing")            # no .ssh at all
    keyonly = os.path.join(tmp, "keyonly")          # key, no known_hosts
    full = os.path.join(tmp, "full")                # both
    os.makedirs(keyonly); os.makedirs(full)
    open(os.path.join(keyonly, "id_ed25519"), "w").write("k")
    open(os.path.join(full, "id_ed25519"), "w").write("k")
    open(os.path.join(full, "known_hosts"), "w").write("h")

    # ── his case ─────────────────────────────────────────────────────
    h = hostdiag.hint("ssh://root@docknas.lan", REFUSED, ssh_dir=empty)
    checks["a refused key with no .ssh in the container is explained"] = bool(h)
    checks["…saying the container has its own filesystem"] = (
        "container has its own filesystem" in h)
    checks["…and giving the mount that fixes it"] = f"-v {empty}:{empty}:ro" in h

    # ── mounted, but only the key ────────────────────────────────────
    h = hostdiag.hint("ssh://root@nas", REFUSED, ssh_dir=keyonly)
    checks["a mount without known_hosts is a different answer"] = (
        "known_hosts" in h and "-v" not in h.split("known_hosts")[0])

    # ── mounted properly: then it really is the key ──────────────────
    h = hostdiag.hint("ssh://root@nas", REFUSED, ssh_dir=full)
    checks["with both present it points at authorized_keys instead"] = (
        "authorized_keys" in h)

    # ── host key, which is a different failure ───────────────────────
    h = hostdiag.hint("ssh://root@nas", HOSTKEY, ssh_dir=empty)
    checks["an unknown host key gets its own explanation"] = (
        "unknown to the container" in h)
    h = hostdiag.hint("ssh://root@nas", HOSTKEY, ssh_dir=full)
    checks["…and with known_hosts present it does not suggest mounting"] = (
        "-v " not in h and "changed" in h)
    checks["…and never suggests turning the check off"] = (
        "StrictHostKeyChecking" not in h and "rather than removing" in h)

    # ── what must stay silent ────────────────────────────────────────
    checks["a refused connection is not blamed on keys"] = (
        hostdiag.hint("ssh://root@nas", REFUSED_CONN, ssh_dir=empty) == "")
    checks["a tcp host gets no ssh advice"] = (
        hostdiag.hint("tcp://nas:2375", REFUSED, ssh_dir=empty) == "")
    checks["…nor does a context endpoint"] = (
        hostdiag.hint("context://nas", REFUSED, ssh_dir=empty) == "")
    checks["an error we cannot place gets no guess"] = (
        hostdiag.hint("ssh://root@nas", "something entirely else",
                      ssh_dir=empty) == "")
    checks["…and neither does an empty one"] = (
        hostdiag.hint("ssh://root@nas", "", ssh_dir=empty) == "")

# ── it reaches all three places a host failure is reported ───────────
APP = os.path.join(os.path.dirname(__file__), "..", "app")
sched = open(os.path.join(APP, "scheduler.py"), encoding="utf-8").read()
tele = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
checks["the scheduled check carries the hint"] = "hostdiag.hint" in sched
checks["…and so does /check"] = tele.count("hostdiag.hint") >= 1
checks["…and /status"] = tele.count("hostdiag.hint") >= 2
# The hint must never be the reason a check fails.
for src, name in ((sched, "scheduler"), (tele, "telegram_bot")):
    i = src.index("import hostdiag")
    checks[f"a broken hint cannot break {name}"] = (
        "except Exception" in src[i:i + 700])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
