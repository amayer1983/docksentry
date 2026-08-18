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


def hint(endpoint, error, ssh_dir=None):
    """A sentence to append to a host failure, or "".

    `ssh_dir` is injectable so a test can describe a container that has
    keys and one that does not, without either being true of the machine
    running the test.
    """
    endpoint = (endpoint or "").strip()
    if not endpoint.startswith("ssh://"):
        return ""
    text = str(error or "").lower()
    ssh_dir = ssh_dir if ssh_dir is not None else _home_ssh()

    if any(k in text for k in _AUTH):
        if not os.path.isdir(ssh_dir):
            return (
                f"There is no `{ssh_dir}` inside this container. Keys live "
                f"on the host — `ssh-copy-id` wrote them to your own home "
                f"directory, and a container has its own filesystem. Mount "
                f"them read-only: `-v {ssh_dir}:{ssh_dir}:ro`")
        if not os.path.isfile(os.path.join(ssh_dir, "known_hosts")):
            return (
                f"`{ssh_dir}` is mounted but has no `known_hosts`, so the "
                f"first connection to a new host has nobody to ask. Mount "
                f"the whole directory rather than the key alone.")
        return (
            f"`{ssh_dir}` is mounted and carries a `known_hosts`, so the "
            f"key itself is being refused — check that this container's "
            f"key is in the remote `authorized_keys` for that user.")

    if any(k in text for k in _HOSTKEY):
        if not os.path.isfile(os.path.join(ssh_dir, "known_hosts")):
            return (
                f"No `known_hosts` in `{ssh_dir}`, so this host is unknown "
                f"to the container even though it is known to you. Mount "
                f"the whole `.ssh` directory: `-v {ssh_dir}:{ssh_dir}:ro`")
        return (
            f"The host key changed, or `known_hosts` has an entry for a "
            f"different address. SSH refuses on purpose here — check the "
            f"entry rather than removing the check.")

    return ""
