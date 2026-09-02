"""Is /data actually somewhere that survives? (#2, @famewolf)

He lost his settings over and over — on a container recreate, after a
`docker compose down`/`up`, after a self-update — and every time the only
thing Docksentry said was "possible data loss", which told him what had
happened and nothing about why. He restored from backup, reconfigured
three hosts, and wrote: "I'm afraid to restart them."

The cause was one line in his compose file:

    - /mnt/dockerdata/docker/containers/docksentry/config:/app/data

We read and write `/data`. Nothing in this image has ever looked at
`/app/data`. So his bind mount held nothing, and the real `/data` fell to
the `VOLUME ["/data"]` in our Dockerfile — an **anonymous** volume, a
fresh one for every new container, discarded the moment the old container
goes. Which is exactly why it "worked all this time" and then lost
everything on every recreate: within one container's life the settings
were there.

None of that was visible from inside. It is, though — the container can
read its own mounts, and both mistakes are obvious in them:

  * a mount at `/data` whose volume name is a 64-character hex string is
    anonymous, and will not survive the next recreate;
  * a bind mount at some *other* path that was plainly meant to be the
    data directory is a directory nobody reads.

So we look, and say so on startup instead of waiting for the loss and
then describing it. Findings only — the caller decides how loudly to say
them.
"""

import json
import os
import posixpath
import re

# Anonymous volumes get a 64-hex id for a name. A named volume never
# looks like that, so this is a reliable tell rather than a heuristic.
_ANON = re.compile(r"^[0-9a-f]{64}$")

# Destinations that are plainly an attempt at "put Docksentry's data
# here". Deliberately short: mounting /etc/localtime, a socket or a certs
# directory is normal and must not be nagged about, so a mount only
# qualifies when its own last element says "data" or "config" — or when
# it is the specific wrong path that started this (`/app/data`, from our
# own WORKDIR, which is an easy guess and completely inert).
_MEANT_AS_DATA = {"data", "config", "docksentry"}


def read_mounts(backend, own_name):
    """The running container's mounts, or None if we cannot tell.

    None is not "no mounts" — it means the question could not be asked
    (no self-detection, an inspect that failed, output we do not
    understand). Nothing may be concluded from it, which is why it is a
    different value from the empty list.
    """
    if not own_name:
        return None
    try:
        r = backend.run(["inspect", "--format", "{{json .Mounts}}", own_name],
                        timeout=15)
    except Exception:
        return None
    if getattr(r, "returncode", 1) != 0 or not (r.stdout or "").strip():
        return None
    try:
        mounts = json.loads(r.stdout.strip())
    except (ValueError, TypeError):
        return None
    return mounts if isinstance(mounts, list) else None


#: Where our data used to live before it moved off a name half the
#: ecosystem uses for its own.
LEGACY_DATA_DIR = "/data"

#: …and where it lives now. Kept here rather than imported from
#: `config` so this module stays readable on its own.
DEFAULT_DATA_DIR = "/docksentry"


def analyse(mounts, data_dir="/data"):
    """Findings about where the data directory really lives.

    Each finding is a dict with a `kind` and enough detail to write a
    message the user can act on:

      wrong_mount   a bind mount that looks intended as the data dir but
                    is at a path nothing reads
      anonymous     `data_dir` is an anonymous volume — gone on recreate
      unmounted     `data_dir` is not a mount at all, so it lives in the
                    container's writable layer — gone on recreate

    An empty list means everything checked out. `mounts is None` means we
    could not look, and yields nothing rather than a guess.
    """
    if mounts is None:
        return []

    findings = []
    data_dir = data_dir.rstrip("/") or "/"
    own = None
    for m in mounts:
        if not isinstance(m, dict):
            continue
        dest = (m.get("Destination") or "").rstrip("/") or "/"
        if dest == data_dir:
            own = m
            continue
        # Anything *inside* the data directory is ours by definition —
        # /data/compose is a documented mount and must not be flagged.
        if dest.startswith(data_dir + "/"):
            continue
        # `/data` is where our data used to live and where half the
        # ecosystem keeps its own — Portainer's stacks among them. Once
        # our directory moved off it, the "looks like a data dir"
        # heuristic started accusing whatever somebody had deliberately
        # mounted there, and told them to make it Docksentry's database.
        # A mount at the old path is either ours from before or plainly
        # somebody else's; neither is a finding.
        # Neither the path our data used to live at nor the one it lives
        # at now. `/data` is everybody's name, and `/docksentry` is ours —
        # the image declares a VOLUME there, so Docker creates an
        # anonymous one on every install that mounts its data somewhere
        # else. Flagging it told every existing user their data directory
        # was wrong and handed them the anonymous volume's path to mount
        # instead, which is the one place it would have been thrown away
        # (#2, @famewolf, on all three of his hosts within hours of the
        # release).
        if dest in (LEGACY_DATA_DIR, DEFAULT_DATA_DIR):
            continue
        # An anonymous volume is Docker's doing, never a choice somebody
        # made — so it cannot be a data directory they "meant". Offering
        # its path as the mount to use is worse than saying nothing: that
        # path is exactly the one thrown away on the next recreate, which
        # is what the message warns about two lines further up.
        if m.get("Type") == "volume" and _ANON.match(str(m.get("Name") or "")):
            continue
        if (posixpath.basename(dest).lower() in _MEANT_AS_DATA
                or dest == "/app/data"):
            findings.append({
                "kind": "wrong_mount",
                "dest": dest,
                "source": m.get("Source") or "",
                "data_dir": data_dir,
            })

    # A mount that merely *looks* like a data directory is only evidence
    # of anything when ours is actually in trouble. This used to fire on
    # its own, and once the data directory moved off `/data` it started
    # accusing whatever the user had mounted there — telling somebody who
    # had deliberately mounted Portainer's volume at `/data` to make it
    # Docksentry's database, which would have buried our state inside
    # another tool's volume. If our own directory is on a named mount, it
    # is persisted, and where anything else sits is not our business.
    if own is None:
        findings.append({"kind": "unmounted", "data_dir": data_dir})
    elif (own.get("Type") == "volume"
          and _ANON.match(str(own.get("Name") or ""))):
        findings.append({
            "kind": "anonymous",
            "data_dir": data_dir,
            "source": own.get("Source") or "",
        })
    return findings


def describe(findings):
    """Plain log lines, in the order they should be read.

    English only, like the other startup diagnostics (`Env override:`,
    the plaintext-host warning). The Telegram/Discord version is
    translated; this one goes into `docker logs`, where everything else
    is English anyway.
    """
    lines = []
    for f in findings:
        d = f.get("data_dir", "/data")
        if f["kind"] == "wrong_mount":
            lines.append(
                f"Storage: {f['source']} is mounted at {f['dest']}, but "
                f"Docksentry never reads or writes there — its data lives "
                f"in {d}. Change the mount to {f['source']}:{d}.")
        elif f["kind"] == "anonymous":
            lines.append(
                f"Storage: {d} is an anonymous volume, so every setting, "
                f"group and pin is discarded the next time this container "
                f"is recreated — including by a self-update. Mount a "
                f"directory or a named volume at {d}.")
        elif f["kind"] == "unmounted":
            lines.append(
                f"Storage: nothing is mounted at {d}, so everything saved "
                f"there lives in the container's writable layer and goes "
                f"with the container. Mount a directory or a named volume "
                f"at {d}.")
    return lines


def summary_key(findings):
    """The one translated key that best explains a loss, or "".

    A wrong mount outranks an anonymous volume: when both are present the
    anonymous volume is only a *consequence* of the mount going to the
    wrong place, and naming the cause is more use than naming the effect.
    """
    kinds = [f["kind"] for f in findings]
    for kind in ("wrong_mount", "anonymous", "unmounted"):
        if kind in kinds:
            return "storage_" + kind
    return ""


def first(findings, kind):
    for f in findings:
        if f["kind"] == kind:
            return f
    return None


def check(backend, own_name, data_dir=None):
    """Convenience wrapper: inspect, analyse, return the findings."""
    return analyse(read_mounts(backend, own_name),
                   data_dir or os.environ.get("DATA_DIR", "/data"))
