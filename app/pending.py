"""`pending_updates.json`, read and written in one place.

The file is one flat list holding every managed host's pending updates
(#7), each entry carrying a `host` key; an entry without one is the
local host's. Telegram, Discord and the Web UI each used to read it with
their own idea of a missing, corrupt or wrongly-shaped file, and Telegram
and Discord each had their own read-modify-write around it.

This module returns data, never a sentence: what a caller says about an
empty list stays the caller's. Each caller's own filter (Discord's
"named entries only", the Web UI's "one host") sits on top of `read`.

An entry's identity is `(host, name)`, not the name: two boxes may each
run an `nginx`, and a removal matching on the name alone would take both.

The write is atomic; the merge is not. Two writers finishing at the
same moment each read the old list and the later one wins. Moving the
removals here does not change that; it puts their arithmetic in one
place.

Not on this module yet: the checker's own read and rewrite
(`check_all`).
"""
import json
import os

from container_store import LOCAL_HOST, atomic_write_json, entry_host


def load(path):
    """The list in the file, or None when there is no usable list:
    no path, no file, unreadable, not JSON, not UTF-8, not a list.

    For the caller that has to tell "no usable file" from "the file says
    nothing is pending": a single update leaves `[]` behind when it took
    the last entry, and a tap on that answers differently from a tap
    with no file at all. Everyone else wants `read`."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def read(path, *, named_only=False):
    """The entries as a list; empty when there is no usable list.

    Entries come back as they are in the file, non-dicts included —
    a reader that counted them before still counts them. `named_only`
    keeps only the dicts that carry a name (what Discord acts on).
    """
    data = load(path)
    if data is None:
        return []
    if named_only:
        return [e for e in data if isinstance(e, dict) and e.get("name")]
    return data


def entries_for(entries, host_names):
    """The dict entries in `entries` that belong to one of `host_names`,
    in file order. An entry without a `host` key is the local host's."""
    names = set(host_names)
    return [e for e in entries
            if isinstance(e, dict) and entry_host(e) in names]


def key_of(entry):
    """`(host, name)` — the identity of one pending entry."""
    return entry_host(entry), entry.get("name")


def write(path, entries):
    """Replace the file's content with `entries` (atomic)."""
    atomic_write_json(path, entries)


def clear(path):
    """Delete the file. One that is already gone, or no path, is fine."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def remove(path, keys, *, delete_when_empty):
    """Drop `keys` from the file and return what is left.

    `keys` are `(host, name)` pairs; a bare string is the local host's
    entry. Entries that are not dicts cannot match a pair and are kept.

    When there is no usable list (see `load`) nothing is touched and the
    answer is None — a removal must not turn a file it cannot read into
    an empty one. When nothing is left, the file is deleted if
    `delete_when_empty`, otherwise it is written as `[]`. The rewrite
    happens even when no key matched, as both removals always did.
    """
    data = load(path)
    if data is None:
        return None
    wanted = {(LOCAL_HOST, k) if isinstance(k, str) else tuple(k)
              for k in keys}
    remaining = [e for e in data
                 if not isinstance(e, dict) or key_of(e) not in wanted]
    if remaining or not delete_when_empty:
        write(path, remaining)
    else:
        clear(path)
    return remaining
