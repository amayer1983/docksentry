"""One backup bundle, built in one place (#2, @famewolf).

The Web UI has been able to export this since v1.22.0, written the day
after he first lost a config. Since then he has asked for two more ways
to get at it, and both come from the same place he ended up in again on
16 August:

    I would REALLY REALLY like it if backups stored a local copy so
    restores are not dependent on another machine to get going again […]
    In every case I've lost a config having a copy in the docksentry
    container directory would have solved the issue.

and

    Can we get a /backup option in telegram that sends the backup as a
    file VIA telegram?

Both are the same bundle. It used to be assembled inline inside the HTTP
handler, which is fine for exactly one caller and wrong for three — the
second copy is the one that quietly stops matching the first.

Deliberately not included: `update_history.json` (large, and it
regenerates) and `pending_updates.json` (transient by definition).
"""

import json
import os
import re
from datetime import datetime

SCHEMA_VERSION = 1

#: Keep this many automatic copies. Small on purpose: these exist so a
#: wiped volume has *something* next to it, not as an archive.
KEEP = 5

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
#: A Docker container id as a hostname — 12 or 64 hex characters.
_CONTAINER_ID = re.compile(r"[0-9a-f]{12}|[0-9a-f]{64}")


def instance_slug(config):
    """A short name for this instance, for filenames.

    `BOT_LABEL` first: it is the value people already set to tell their
    instances apart in a shared Telegram group, so it is the one they
    will recognise on a file. Container hostname as the fallback, and
    an empty string when there is neither.
    """
    who = (getattr(config, "bot_label", "") or "").strip()
    if not who:
        host = os.environ.get("HOSTNAME", "").strip()
        # In a container HOSTNAME is usually the container id, and
        # `docksentry-backup-9cef9348bc8f-…` is no more use than no name
        # at all — worse, it looks like it means something. Only take a
        # hostname somebody chose. (Seen on this developer's own instance
        # the first time the file was written.)
        who = "" if _CONTAINER_ID.fullmatch(host) else host
    return _UNSAFE.sub("-", who).strip("-.")[:40]


def filename(config, when=None):
    """`docksentry-backup-<instance>-<timestamp>.json`.

    He backs three hosts up to one PC and ended up with three files
    called `docksentry-backup-20260816-15xxxx.json` and "no clue what
    host they are from" — and restoring the wrong one puts another
    machine's groups and pins on this one. With neither a label nor a
    hostname, the old name is kept.
    """
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    who = instance_slug(config)
    return f"docksentry-backup-{who + '-' if who else ''}{stamp}.json"


def build(config, store, version, when=None):
    """Everything worth keeping, as a dict."""
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (when or datetime.now()).isoformat(timespec="seconds"),
        "docksentry_version": version,
        "instance": instance_slug(config),
        "settings": {},
        "pinned": [],
        "autoupdate": [],
        "ask_major": [],
        "groups": {},
        "notes": {},
        "links": {},
        "update_windows": {},
    }
    # Settings come off disk rather than off the live config: the file
    # holds what was actually saved, while the object also carries every
    # env-seeded default. Restoring the latter onto another host would
    # hand it that host's environment, frozen.
    settings_file = getattr(config, "settings_file", "")
    if settings_file and os.path.exists(settings_file):
        try:
            with open(settings_file) as f:
                bundle["settings"] = json.load(f)
        except (IOError, ValueError):
            pass
    bundle["pinned"] = store.get_pinned()
    bundle["autoupdate"] = store.get_autoupdate()
    bundle["ask_major"] = store.get_ask_before_major()
    bundle["groups"] = store.get_groups()
    bundle["notes"] = store.get_notes()
    bundle["links"] = store.get_links()
    bundle["update_windows"] = store.get_update_windows()
    return bundle


def payload(config, store, version, when=None):
    """The bundle as bytes, ready to send or write."""
    return json.dumps(build(config, store, version, when),
                      indent=2, ensure_ascii=False).encode("utf-8")


def local_dir(config):
    return os.path.join(getattr(config, "data_dir", "/data"), "backups")


def write_local(config, store, version, keep=KEEP, when=None):
    """Drop a copy next to the data and prune the old ones.

    Returns the path written, or "" if it could not be written — a
    failure here must never take down whatever triggered it.

    The obvious objection is that a backup living in the volume it is
    backing up protects against nothing. It does protect against the
    thing that actually keeps happening: a *container* recreated onto a
    fresh anonymous volume with the real directory sitting there intact,
    a settings.json lost while the rest of the directory survives, or a
    restore attempted from a laptop that is not to hand. It is not a
    substitute for the file you keep somewhere else, and does not claim
    to be.
    """
    directory = local_dir(config)
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, filename(config, when))
        data = payload(config, store, version, when)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        return ""
    prune(directory, keep)
    return path


def prune(directory, keep=KEEP):
    """Keep the newest `keep` backups, delete the rest. Returns removed."""
    try:
        names = sorted(n for n in os.listdir(directory)
                       if n.startswith("docksentry-backup-")
                       and n.endswith(".json"))
    except OSError:
        return []
    # The timestamp is fixed-width and last, so lexical order is
    # chronological order for any one instance — and mixing instances in
    # one directory does not happen: this is a container's own volume.
    removed = []
    for name in names[:-keep] if keep > 0 else names:
        try:
            os.unlink(os.path.join(directory, name))
            removed.append(name)
        except OSError:
            pass
    return removed


def newest_local(config):
    """The most recent local copy, or "" — for offering a restore."""
    directory = local_dir(config)
    try:
        names = sorted(n for n in os.listdir(directory)
                       if n.startswith("docksentry-backup-")
                       and n.endswith(".json"))
    except OSError:
        return ""
    return os.path.join(directory, names[-1]) if names else ""


#: Every file the bundle is built from. Used to answer "has anything
#: changed since the last copy?" without reading them all.
STATE_FILES = ("settings.json", "groups.json", "pinned_containers.json",
               "autoupdate_containers.json", "ask_before_major.json",
               "container_notes.json", "container_links.json",
               "update_windows.json")


def state_mtime(config):
    """When any of the backed-up files last changed. 0 if none exist."""
    data_dir = getattr(config, "data_dir", "/data")
    newest = 0.0
    for name in STATE_FILES:
        try:
            newest = max(newest, os.path.getmtime(os.path.join(data_dir, name)))
        except OSError:
            continue
    return newest


def write_local_if_stale(config, store, version, max_age_hours=12,
                         min_gap_seconds=300, keep=KEEP, when=None):
    """A copy when there is something new to copy, and not more often.

    Two guards, and both are there because the naive version fails:

    * **Age.** Restart loops are real — a bad compose value, a container
      failing its healthcheck — and a copy per boot walks the whole
      retention window in minutes, leaving five backups of the same
      broken minute and nothing older.

    * **Change.** Age alone is worse, and this was caught in testing
      rather than reasoned about: a fresh install writes a copy on its
      first boot, when nothing has been configured yet and `settings`
      is empty. Configure everything an hour later, lose settings.json,
      restart — and the copy being kept is the empty one from before you
      started. It restored nothing, correctly, and looked like the whole
      feature was broken. So a copy is also written whenever the state
      has changed since the last one, with a short gap so that a burst
      of saves produces one file rather than ten.

    Returns the path written, or "" when there was nothing to do.
    """
    now = (when or datetime.now()).timestamp()
    newest = newest_local(config)
    if newest:
        try:
            taken = os.path.getmtime(newest)
        except OSError:
            taken = 0.0
        age = now - taken
        changed_since = state_mtime(config) > taken
        # The gap exists to collapse a burst of saves into one file, so
        # it belongs on the request path and not on a boot. Callers that
        # cannot burst pass 0 — a startup blocked for five minutes after
        # an unrelated recent copy is how a genuinely needed one gets
        # skipped, which is exactly what happened the first time this was
        # tried against a real container.
        if min_gap_seconds and age < min_gap_seconds:
            return ""
        if age < max_age_hours * 3600 and not changed_since:
            return ""
    return write_local(config, store, version, keep=keep, when=when)
