"""Why an `ssh://` host will not answer (#2, @famewolf).

He set up key-based login between his machines, checked it worked, added
`DOCKER_HOSTS`, and then spent two days on an instance that reported
three managed hosts and could reach one. The error, once it stopped
being truncated, said `Permission denied (publickey)` — which is exactly
right and exactly unhelpful, because from where he was standing the keys
*did* work.

They did. On the host. Docksentry runs in a container, and a container
has its own filesystem: `ssh-copy-id` wrote to `/root/.ssh` on the
machine, and there is no `/root/.ssh` inside the image at all. Two
people have now hit this, so the message says it rather than leaving it
to be deduced.

Everything here is measured at the moment of failure — does that
directory exist, does that file exist — not inferred from the shape of
the error. An error we cannot explain gets no guess attached to it.
"""

import os


def _home_ssh():
    return os.path.join(os.path.expanduser("~"), ".ssh")


#: Failures that mean "the SSH layer refused us", as opposed to the host
#: being off, DNS being wrong, or Docker not being installed there. Only
#: for these is a key or a known_hosts entry the likely answer.
_AUTH = ("permission denied", "publickey", "no supported authentication",
         "authentication failed")
_HOSTKEY = ("host key verification failed", "remote host identification")


def hint(endpoint, error, ssh_dir=None, t=None):
    """A sentence to append to a host failure, or "".

    `ssh_dir` is injectable so a test can describe a container that has
    keys and one that does not, without either being true of the machine
    running the test.

    Note which side of the `-v` each path belongs on. `{dir}` is the
    path *inside* the container — `/root/.ssh`, since the image sets no
    `USER` — and the first version of these sentences put it on both
    sides. For a reader who is not root on the host, the left-hand side
    then named a directory that does not exist, Docker created an empty
    one, the keys were still missing, and the advice had looked right.
    The host side is `~/.ssh`, which the shell expands to whoever is
    running the command, and that is the user whose keys these are.

    `t` is the caller's translator. Without it the sentence comes out in
    English, which is what every caller did until 11.10.2026 — and the
    result was a translated finding with an untranslated sentence stuck
    underneath it in fifteen of the sixteen languages. The default
    stays, because a missing translator must not cost the reader the
    one sentence that explains the failure.
    """
    endpoint = (endpoint or "").strip()
    if not endpoint.startswith("ssh://"):
        return ""
    text = str(error or "").lower()
    ssh_dir = ssh_dir if ssh_dir is not None else _home_ssh()

    def _say(key):
        if t is not None:
            try:
                return t(key, dir=ssh_dir)
            except Exception:                               # noqa: BLE001
                pass
        return _EN[key].format(dir=ssh_dir)

    if any(k in text for k in _AUTH):
        if not os.path.isdir(ssh_dir):
            return _say("hostdiag_ssh_no_dir")
        if not os.path.isfile(os.path.join(ssh_dir, "known_hosts")):
            return _say("hostdiag_ssh_no_known_hosts")
        return _say("hostdiag_ssh_key_refused")

    if any(k in text for k in _HOSTKEY):
        if not os.path.isfile(os.path.join(ssh_dir, "known_hosts")):
            return _say("hostdiag_hostkey_unknown")
        return _say("hostdiag_hostkey_changed")

    return ""


#: The same five sentences as `lang/en.json`, kept here as the floor for
#: a caller with no translator. Held to the language files by
#: `test_hostdiag.py`, so the two cannot drift apart silently.
_EN = {
    "hostdiag_ssh_no_dir":
        "There is no `{dir}` inside this container. Keys live on the host "
        "— `ssh-copy-id` wrote them to your own home directory, and a "
        "container has its own filesystem. Mount them read-only: "
        "`-v ~/.ssh:{dir}:ro`",
    "hostdiag_ssh_no_known_hosts":
        "`{dir}` is mounted but has no `known_hosts`, so the first "
        "connection to a new host has nobody to ask. Mount the whole "
        "directory rather than the key alone.",
    "hostdiag_ssh_key_refused":
        "`{dir}` is mounted and carries a `known_hosts`, so the key "
        "itself is being refused — check that this container's key is in "
        "the remote `authorized_keys` for that user.",
    "hostdiag_hostkey_unknown":
        "No `known_hosts` in `{dir}`, so this host is unknown to the "
        "container even though it is known to you. Mount the whole "
        "`.ssh` directory: `-v ~/.ssh:{dir}:ro`",
    "hostdiag_hostkey_changed":
        "The host key changed, or `known_hosts` has an entry for a "
        "different address. SSH refuses on purpose here — check the entry "
        "rather than removing the check.",
}
