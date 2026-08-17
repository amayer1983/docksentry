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


def restore(bundle, config, store, persistent_keys):
    """Apply a backup bundle. Returns (restored, errors, dropped_links).

    Lifted verbatim out of the Web UI's import endpoint when Telegram
    needed the same thing (#2, @NotRetarded: "I'd love to see if it's
    possible to perform a /restore for Telegram by attaching that file").
    One implementation with two callers, rather than a second one that
    starts identical and quietly stops being.

    The care in here is not decoration. Settings go through the
    PERSISTENT_KEYS allow-list so a bundle cannot inject arbitrary
    attributes, and links go through the same validator the live write
    path uses — a backup is a file, not a trusted channel, and nothing
    about "the user picked it" says the user wrote it. Rejects are
    counted rather than swallowed, because a restore that reports
    success while quietly losing entries is worse than one that fails.
    """
    restored = []
    errors = []
    dropped_links = 0   # links rejected by is_safe_link (#52)
    # Settings — apply via the PERSISTENT_KEYS allowlist so
    # we don't accept arbitrary attribute injection.
    if isinstance(bundle.get("settings"), dict):
        try:
            for key, value in bundle["settings"].items():
                if key in persistent_keys:
                    setattr(config, key, value)
            config.save_persistent()
            restored.append("settings")
        except Exception as e:
            errors.append(f"settings: {str(e)[:100]}")
    # Lists — pinned, autoupdate, ask_major
    if isinstance(bundle.get("pinned"), list):
        store.save_pinned([str(x) for x in bundle["pinned"] if isinstance(x, str)])
        restored.append("pinned")
    if isinstance(bundle.get("autoupdate"), list):
        store.save_autoupdate([str(x) for x in bundle["autoupdate"] if isinstance(x, str)])
        restored.append("autoupdate")
    if isinstance(bundle.get("ask_major"), list):
        # No public save_ask_before_major — write through
        # the same _save the toggle uses. Coerce to str just
        # to be paranoid about malformed bundles.
        store._save(store.ask_before_major_file,
                    [str(x) for x in bundle["ask_major"] if isinstance(x, str)])
        restored.append("ask_major")
    # Dicts — groups, notes, links, update_windows. Just
    # write them through the existing _save_dict so the
    # atomic-write path applies.
    if isinstance(bundle.get("groups"), dict):
        store._save_dict(store.groups_file, bundle["groups"])
        restored.append("groups")
    if isinstance(bundle.get("notes"), dict):
        store._save_dict(store.notes_file, bundle["notes"])
        restored.append("notes")
    if isinstance(bundle.get("links"), dict):
        # Links are the one section that gets rendered as an
        # `<a href>` (#52) — everything else in a bundle ends
        # up as escaped text. Writing them through _save_dict
        # raw bypassed set_link and therefore the validator,
        # so a hand-edited backup file could plant
        # `javascript:…` in container_links.json and have the
        # Web UI hand it to the browser on the next render.
        # A backup is a file, not a trusted channel: it
        # arrives over an unauthenticated-by-content upload
        # and nothing about "the user picked it" says the
        # user wrote it.
        #
        # Every entry goes through the same is_safe_link the
        # live write path uses. Rejects are dropped and
        # COUNTED — swallowing them silently would restore a
        # bundle "successfully" while quietly losing data the
        # user believes is back.
        from container_store import is_safe_link as _is_safe_link
        clean_links = {}
        for k, v in bundle["links"].items():
            if isinstance(k, str) and isinstance(v, str) and _is_safe_link(v.strip()):
                clean_links[k] = v.strip()
            else:
                dropped_links += 1
        store._save_dict(store.links_file, clean_links)
        # The import toast prints `restored` verbatim, so the
        # count has to travel inside it to be seen at all.
        restored.append("links" if not dropped_links
                        else f"links ({dropped_links} unsafe dropped)")
        if dropped_links:
            errors.append(
                f"links: {dropped_links} entry/entries rejected by the "
                f"URL validator (not http/https, or unsafe characters)")
    if isinstance(bundle.get("update_windows"), dict):
        store._save_dict(store.update_windows_file, bundle["update_windows"])
        restored.append("update_windows")
    return restored, errors, dropped_links
