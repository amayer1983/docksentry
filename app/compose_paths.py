#!/usr/bin/env python3
"""Whose path is this, when a compose file cannot be found.

`com.docker.compose.project.config_files` records the path the thing
that CREATED the stack saw — and more often than not that thing is
itself a container. Portainer keeps stacks at `/data/compose/<id>/`
inside its own container; Dockge and Dockhand at `/app/data/stacks/`.
None of those paths exist on the host, so "mount that directory into
Docksentry" sends the reader looking for a directory that is not there.

Three people hit this in one week, each with a different manager and
each concluding their mount was wrong (#2, #65). It was not: the advice
was.

So when the path is recognisable, the message says whose it is and what
that means — the manager's data directory has to appear in Docksentry
under the SAME path, because that is the only string the label will ever
match.

Recognition is by prefix and nothing else. A path we do not recognise
gets no guess attached to it: the generic advice is imperfect but true,
and a confident wrong name is worse than no name.

Pure standard library, like the rest of the project.
"""

#: Prefix → the tool that writes it. Ordered longest-first at lookup so
#: a more specific prefix wins if one is ever nested inside another.
#:
#: Every entry here is one somebody has actually shown us. `/data/compose`
#: was measured on four containers of the owner's own hosts;
#: `/app/data/stacks` came out of @NotRetarded's label in #2. Nothing is
#: in here from memory.
#:
#: `/opt/stacks` deliberately is NOT, although Dockge uses it. Dockge
#: mounts it at the identical path by convention, so the label is a valid
#: HOST path and there is nothing to map — listing it would have told
#: somebody they have a problem they do not have. A manager only belongs
#: here when its internal path differs from the host's.
KNOWN = {
    "/data/compose/": "Portainer",
    "/app/data/stacks/": "Dockge or Dockhand",
}


def owner(path):
    """The stack manager whose filesystem `path` belongs to, or None."""
    if not path:
        return None
    for prefix in sorted(KNOWN, key=len, reverse=True):
        if path.startswith(prefix):
            return KNOWN[prefix]
    return None


def mount_root(path):
    """The part of `path` that has to be mounted, or None.

    For `/data/compose/83/docker-compose.yml` that is `/data/compose` —
    mounting the manager's data directory there covers every stack it
    holds, rather than one per stack.
    """
    if not path:
        return None
    for prefix in sorted(KNOWN, key=len, reverse=True):
        if path.startswith(prefix):
            return prefix.rstrip("/")
    return None
