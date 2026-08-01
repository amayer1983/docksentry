#!/usr/bin/env python3
"""Persistent state for per-container flags (pinned, auto-update).

Used by TelegramBot, the scheduler and the Web UI. Storing the lists in a
small dedicated module instead of the Telegram bot lets headless setups
(no Telegram) still pin containers and toggle auto-update via the Web UI.
"""

import json
import os
import urllib.parse


# ── Link safety (#20, #52) ─────────────────────────────────────────────
# Hard cap on stored/label link length. Long enough for any real
# changelog URL, short enough to keep notification payloads sane.
MAX_LINK_LENGTH = 500

# Characters that can break out of the contexts a link gets rendered in:
# HTML attributes (quotes, angle brackets), Markdown links (parentheses,
# brackets are handled by the parser but `(` `)` end the target), and
# shell/JS-ish escapes (backslash, backtick). `{}`, `|`, `^` are not
# legal in a URI unencoded anyway, so rejecting them costs nothing.
_UNSAFE_LINK_CHARS = frozenset('()<>"\'`\\{}|^')


def is_safe_link(url):
    """True when `url` is safe to render as an `href` / Markdown target.

    This is the ONLY thing standing between a user-controlled string and
    script execution in the Web UI. `html.escape()` does NOT help here:
    it encodes `<`, `>`, `&`, quotes — but it does not touch the URL
    *scheme*, so `javascript:alert(1)` survives escaping intact and fires
    on click. There is no CSP header, the UI is same-origin with every
    `/api/*` POST endpoint, and neither Basic-Auth nor the CSRF token
    protects against script running inside the page itself.

    Rules:
      - scheme must be exactly `http` or `https`, decided by
        `urllib.parse.urlparse` — NOT by `str.startswith()`. A
        startswith check is trivially defeated by `JavaScript:` (the
        scheme is case-insensitive) or by leading whitespace/control
        characters, both of which browsers strip before dispatching.
      - a hostname must be present, which also rejects scheme-relative
        values like `//evil.example` that inherit the current scheme.
      - no character that could terminate an HTML attribute or a
        Markdown link target, and no whitespace or control character.
      - at most MAX_LINK_LENGTH characters.

    Whitespace is rejected outright rather than stripped: a leading
    space (or tab, or newline) in front of `javascript:` is exactly the
    trick that defeats naive prefix checks, and no legitimate URL needs
    a literal space — that is what %20 is for.
    """
    if not isinstance(url, str) or not url:
        return False
    if len(url) > MAX_LINK_LENGTH:
        return False
    for ch in url:
        # Control characters (incl. the NUL/TAB/CR/LF that urlparse
        # silently strips but browsers act on), any kind of whitespace,
        # and the attribute/markdown breakers.
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
        if ch.isspace() or ch in _UNSAFE_LINK_CHARS:
            return False
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        # e.g. malformed IPv6 literal — treat as unsafe, not as a crash
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.hostname:
        return False
    return True


def atomic_write_json(path, data, **dump_kwargs):
    """Atomic JSON write — survive power-loss / OOM / mid-write kill
    without corrupting the target file.

    The naïve ``open(path, "w")`` truncates the target to 0 bytes
    immediately, before ``json.dump`` writes any content. A kill
    between truncate and close (host reboot, Docker daemon restart,
    OOM, power loss) leaves a 0-byte or partial-JSON file. Next boot
    parses it, fails with JSONDecodeError, and the loader falls back
    to empty defaults — silently wiping persistent state.

    Reported by @famewolf in #2 after three of his hosts simultaneously
    rebooted (likely unattended-upgrades) and all came back with empty
    config. Bug existed in this codebase since v1.7.0; v1.22.0 fixed
    settings.json + the _save_dict files; v1.22.1 extends coverage to
    every JSON write in the codebase (list-format files, history,
    pending updates, maintenance state, weekly-report state, post-
    selfupdate history fixup).

    Strategy: write to ``<path>.tmp``, ``flush()`` + ``os.fsync()`` to
    push bytes through the kernel page cache to disk, then
    ``os.replace()`` which is POSIX-atomic — either the new file is
    fully visible or the old one is still there, never a partial state.

    ``dump_kwargs`` is forwarded to ``json.dump`` so call sites can
    pass ``indent=2`` etc. as before.

    Errors are propagated to the caller — wrap in try/except where
    that's the current behaviour (most call sites print a warning and
    continue rather than crash on failed save).
    """
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, **dump_kwargs)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class ContainerStore:
    def __init__(self, config):
        self.pinned_file = config.pinned_file
        self.autoupdate_file = config.autoupdate_file
        self.update_windows_file = config.update_windows_file
        self.ask_before_major_file = config.ask_before_major_file
        self.trust_running_file = config.trust_running_file
        self.cooldown_file = config.cooldown_file
        self.protect_stop_file = config.protect_stop_file
        self.major_pending_file = config.major_pending_file
        self.groups_file = config.groups_file
        self.notes_file = config.notes_file
        self.links_file = config.links_file

    # ── Pinned ────────────────────────────────────────────────

    def get_pinned(self):
        return self._load(self.pinned_file)

    def save_pinned(self, names):
        self._save(self.pinned_file, names)

    def is_pinned(self, name):
        return name in self.get_pinned()

    def pin(self, name):
        pinned = self.get_pinned()
        if name not in pinned:
            pinned.append(name)
            self.save_pinned(pinned)
            return True
        return False

    def unpin(self, name):
        pinned = self.get_pinned()
        if name in pinned:
            pinned.remove(name)
            self.save_pinned(pinned)
            return True
        return False

    # ── Auto-update ───────────────────────────────────────────

    def get_autoupdate(self):
        return self._load(self.autoupdate_file)

    def save_autoupdate(self, names):
        self._save(self.autoupdate_file, names)

    def is_auto(self, name):
        return name in self.get_autoupdate()

    def toggle_auto(self, name):
        """Toggle auto-update for `name`. Returns the new state (True=on)."""
        current = self.get_autoupdate()
        if name in current:
            current.remove(name)
            self.save_autoupdate(current)
            return False
        else:
            current.append(name)
            self.save_autoupdate(current)
            return True

    # ── Update windows ────────────────────────────────────────
    # Stored as a dict { container_name: {"start": "HH:MM", "end": "HH:MM",
    # "weekdays": [0..6] } } where weekday 0 = Monday (Python convention).

    def get_update_windows(self):
        return self._load_dict(self.update_windows_file)

    def get_update_window(self, name):
        return self.get_update_windows().get(name)

    def set_update_window(self, name, start, end, weekdays):
        windows = self.get_update_windows()
        windows[name] = {
            "start": start,
            "end": end,
            "weekdays": sorted({int(d) for d in weekdays if 0 <= int(d) <= 6}),
        }
        self._save_dict(self.update_windows_file, windows)

    def clear_update_window(self, name):
        windows = self.get_update_windows()
        if name in windows:
            del windows[name]
            self._save_dict(self.update_windows_file, windows)

    # ── Ask-before-major ──────────────────────────────────────
    # List of container names that require confirmation for major
    # version bumps (semantic-version major component changes).

    def get_ask_before_major(self):
        return self._load(self.ask_before_major_file)

    def is_ask_before_major(self, name):
        return name in self.get_ask_before_major()

    def toggle_ask_before_major(self, name):
        names = self.get_ask_before_major()
        if name in names:
            names.remove(name)
        else:
            names.append(name)
        self._save(self.ask_before_major_file, names)
        return name in names

    # ── Trust running state over healthcheck (#9) ─────────────
    def get_trust_running(self):
        return self._load(self.trust_running_file)

    def is_trust_running(self, name):
        return name in self.get_trust_running()

    def toggle_trust_running(self, name):
        names = self.get_trust_running()
        if name in names:
            names.remove(name)
        else:
            names.append(name)
        self._save(self.trust_running_file, names)
        return name in names

    # ── Protect from stop (#38) ───────────────────────────────
    def get_protect_stop(self):
        return self._load(self.protect_stop_file)

    def is_protect_stop(self, name):
        return name in self.get_protect_stop()

    def toggle_protect_stop(self, name):
        names = self.get_protect_stop()
        if name in names:
            names.remove(name)
        else:
            names.append(name)
        self._save(self.protect_stop_file, names)
        return name in names

    # ── Per-container update cooldown (seconds) (#2) ──────────
    def get_cooldowns(self):
        return self._load_dict(self.cooldown_file)

    def get_cooldown(self, name):
        """Cooldown seconds for a container (0 = none). Clamped to [0, 600]."""
        try:
            return max(0, min(600, int(self.get_cooldowns().get(name, 0) or 0)))
        except (ValueError, TypeError):
            return 0

    def set_cooldown(self, name, seconds):
        """Set (or, when seconds<=0, clear) a container's update cooldown."""
        cds = self.get_cooldowns()
        try:
            seconds = max(0, min(600, int(seconds)))
        except (ValueError, TypeError):
            seconds = 0
        if seconds:
            cds[name] = seconds
        else:
            cds.pop(name, None)
        self._save_dict(self.cooldown_file, cds)
        return seconds

    # ── Pending major-version confirmations ───────────────────
    # Persisted across restarts — entries hold enough metadata for the
    # confirmation flow to resume the update on user click.

    def get_pending_major(self):
        return self._load_dict(self.major_pending_file)

    def add_pending_major(self, name, payload):
        pending = self.get_pending_major()
        pending[name] = payload
        self._save_dict(self.major_pending_file, pending)

    def remove_pending_major(self, name):
        pending = self.get_pending_major()
        if name in pending:
            del pending[name]
            self._save_dict(self.major_pending_file, pending)
            return True
        return False

    # ── Container groups ──────────────────────────────────────
    # Stored as a dict keyed by an opaque slug:
    #   { "media-stack": {
    #       "name": "Media Stack",
    #       "containers": ["plex", "sonarr", "radarr"],
    #       "wait_seconds": 30,
    #     }, ... }
    # A container can be in at most ONE group — set_group() removes it from
    # any other group automatically.

    def get_groups(self):
        return self._load_dict(self.groups_file)

    def get_group(self, group_id):
        return self.get_groups().get(group_id)

    def get_group_for_container(self, container_name):
        """Return (group_id, group_dict) for the container, or (None, None)."""
        for gid, g in self.get_groups().items():
            if container_name in (g.get("containers") or []):
                return gid, g
        return None, None

    def save_group(self, group_id, name, containers, wait_seconds=30,
                   restart_dependents=False):
        """Create or update a group. Removes the listed containers from any
        other group (one-group-per-container invariant).

        Args:
            restart_dependents: When True, updating the FIRST container in
                the list (= the "head", e.g. Gluetun) triggers a restart
                of all other group members AFTER the head reports healthy.
                Use for VPN-sidecar / shared-network-namespace setups
                where dependents lose connectivity when the namespace
                owner restarts.
        """
        groups = self.get_groups()
        cleaned = [c.strip() for c in containers if c and c.strip()]
        # Remove these containers from every other group
        for other_id, other in list(groups.items()):
            if other_id == group_id:
                continue
            other["containers"] = [c for c in (other.get("containers") or []) if c not in cleaned]
            if not other["containers"]:
                del groups[other_id]
        try:
            wait = max(0, min(int(wait_seconds), 600))
        except (ValueError, TypeError):
            wait = 30
        groups[group_id] = {
            "name": name.strip() or group_id,
            "containers": cleaned,
            "wait_seconds": wait,
            "restart_dependents": bool(restart_dependents),
        }
        self._save_dict(self.groups_file, groups)

    def delete_group(self, group_id):
        groups = self.get_groups()
        if group_id in groups:
            del groups[group_id]
            self._save_dict(self.groups_file, groups)
            return True
        return False

    def reorder_group_container(self, group_id, container_name, direction):
        """Move a container up (direction='up') or down ('down') one slot
        within its group. No-op when already at the edge."""
        groups = self.get_groups()
        g = groups.get(group_id)
        if not g:
            return False
        names = list(g.get("containers") or [])
        try:
            idx = names.index(container_name)
        except ValueError:
            return False
        if direction == "up" and idx > 0:
            names[idx - 1], names[idx] = names[idx], names[idx - 1]
        elif direction == "down" and idx < len(names) - 1:
            names[idx], names[idx + 1] = names[idx + 1], names[idx]
        else:
            return False
        g["containers"] = names
        groups[group_id] = g
        self._save_dict(self.groups_file, groups)
        return True

    # ── Container notes ───────────────────────────────────────
    # { container_name: "free-text note" }

    def get_notes(self):
        return self._load_dict(self.notes_file)

    def get_note(self, name):
        return self.get_notes().get(name, "")

    def set_note(self, name, text):
        notes = self.get_notes()
        text = (text or "").strip()
        if text:
            # Cap length so the file stays reasonable
            notes[name] = text[:2000]
        else:
            notes.pop(name, None)
        self._save_dict(self.notes_file, notes)

    # ── Container links (#20) ─────────────────────────────────
    # { container_name: "https://..." } — manual override of the
    # repo/changelog URL that appears as a markdown link wrapping
    # the container name in update notifications. Falls back to the
    # OCI `image.source` label auto-detection when not set.

    def get_links(self):
        return self._load_dict(self.links_file)

    def get_link(self, name):
        return self.get_links().get(name, "")

    def set_link(self, name, url):
        links = self.get_links()
        url = (url or "").strip()
        # Validation lives in the shared `is_safe_link` helper so the
        # Telegram `/setlink`, the Web UI and the `docksentry.link`
        # label (#52) all agree on what a link may look like. The old
        # `startswith("http://")` check here was not enough once the
        # value gets rendered as an `<a href>`. Empty clears the entry.
        if url and not is_safe_link(url):
            return False
        if url:
            links[name] = url
        else:
            links.pop(name, None)
        self._save_dict(self.links_file, links)
        return True

    # ── helpers ───────────────────────────────────────────────

    @staticmethod
    def _load(path):
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError):
            return []

    @staticmethod
    def _save(path, names):
        """Atomic list write — same rationale as _save_dict, but for
        list-shaped files (pinned, autoupdate, ask_before_major). The
        v1.22.0 fix patched the dict path but missed this one — fixed
        in v1.22.1 via the shared atomic_write_json helper.
        """
        try:
            atomic_write_json(path, names)
        except OSError as e:
            print(f"Failed to save {path}: {e}")
            try:
                os.unlink(path + ".tmp")
            except OSError:
                pass

    @staticmethod
    def _load_dict(path):
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, IOError):
            return {}

    @staticmethod
    def _save_dict(path, data):
        """Atomic dict write — see module-level atomic_write_json for
        the rationale. Refactored in v1.22.1 to share the helper with
        every other JSON write in the codebase.
        """
        try:
            atomic_write_json(path, data, indent=2)
        except OSError as e:
            print(f"Failed to save {path}: {e}")
            try:
                os.unlink(path + ".tmp")
            except OSError:
                pass


# ── Multi-host: one store, container names keyed per host (#7) ─────────

#: Separator between host name and container name in stored keys.
#: `/` can't occur in a Docker container name, so it can't collide with a
#: real name — and an unprefixed key therefore unambiguously means "local".
HOST_KEY_SEP = "/"

#: The implicit host: the machine Docksentry itself runs on. Its containers
#: are stored UNPREFIXED, which is why every existing data file stays valid
#: with no migration — a single-host install's state is already "local".
LOCAL_HOST = "local"


def host_key(host, name):
    """Storage key for `name` on `host` — `nas/nginx`, or plain `nginx`
    for the local host. Two hosts can each run an `nginx`; the flat lists
    in this module key on the container name alone, so without this they
    would collide."""
    if not host or host == LOCAL_HOST:
        return name
    return f"{host}{HOST_KEY_SEP}{name}"


def split_host_key(key):
    """Inverse of `host_key`: `nas/nginx` → `("nas", "nginx")`, and a bare
    `nginx` → `("local", "nginx")`."""
    host, sep, name = key.partition(HOST_KEY_SEP)
    if not sep:
        return LOCAL_HOST, key
    return host, name


def entry_host(entry):
    """The managed host an update / pending-update dict is about.

    `UpdateChecker.check_all` stamps every entry it writes with a `host`
    key. Entries written by a version older than #7 have none, and those
    are the local host — the only host such a version knew about. That one
    rule is why the pending file needed no migration and why a single-host
    install resolves every entry to `local` and keeps behaving identically.

    Lives here, next to `host_key`/`split_host_key`, so the orchestration
    (`UpdateEngine`) and the front ends (`TelegramBot`) cannot drift apart
    on what "which host is this about" means.
    """
    return (entry.get("host") or LOCAL_HOST) if isinstance(entry, dict) else LOCAL_HOST


class HostScopedStore:
    """A `ContainerStore` view bound to one host.

    Every container name going in is prefixed with the host, and every name
    coming out has the prefix stripped — so callers keep passing and seeing
    plain container names and never think about hosts. The per-host update
    engine and checker each get one of these; the local host's view is a
    no-op wrapper, so its keys stay exactly what they've always been.

    Deliberately explicit rather than a magic __getattr__ proxy: which
    arguments are container names differs per method (some take a name,
    some a list, `save_group` takes an id AND a list), and getting that
    wrong would silently write to the wrong key. Anything not listed here
    is host-independent and reached straight through `.store`.
    """

    def __init__(self, store, host=LOCAL_HOST):
        self.store = store
        self.host = host or LOCAL_HOST

    # ── key translation ───────────────────────────────────────
    def _k(self, name):
        return host_key(self.host, name)

    def _mine(self, keys):
        """Only this host's entries, with the prefix stripped."""
        out = []
        for key in keys:
            host, name = split_host_key(key)
            if host == self.host:
                out.append(name)
        return out

    def _mine_dict(self, data):
        out = {}
        for key, value in (data or {}).items():
            host, name = split_host_key(key)
            if host == self.host:
                out[name] = value
        return out

    def _foreign(self, keys):
        """Everything that is NOT this host's — preserved verbatim on save
        so writing one host's list can't wipe another's."""
        return [k for k in keys if split_host_key(k)[0] != self.host]

    # ── pinned ────────────────────────────────────────────────
    def get_pinned(self):
        return self._mine(self.store.get_pinned())

    def save_pinned(self, names):
        self.store.save_pinned(self._foreign(self.store.get_pinned())
                               + [self._k(n) for n in names])

    def is_pinned(self, name):
        return self.store.is_pinned(self._k(name))

    def pin(self, name):
        return self.store.pin(self._k(name))

    def unpin(self, name):
        return self.store.unpin(self._k(name))

    # ── auto-update ───────────────────────────────────────────
    def get_autoupdate(self):
        return self._mine(self.store.get_autoupdate())

    def save_autoupdate(self, names):
        self.store.save_autoupdate(self._foreign(self.store.get_autoupdate())
                                   + [self._k(n) for n in names])

    def is_auto(self, name):
        return self.store.is_auto(self._k(name))

    def toggle_auto(self, name):
        return self.store.toggle_auto(self._k(name))

    # ── update windows ────────────────────────────────────────
    def get_update_windows(self):
        return self._mine_dict(self.store.get_update_windows())

    def get_update_window(self, name):
        return self.store.get_update_window(self._k(name))

    def set_update_window(self, name, start, end, weekdays):
        return self.store.set_update_window(self._k(name), start, end, weekdays)

    def clear_update_window(self, name):
        return self.store.clear_update_window(self._k(name))

    # ── per-container toggles ─────────────────────────────────
    def get_ask_before_major(self):
        return set(self._mine(self.store.get_ask_before_major()))

    def is_ask_before_major(self, name):
        return self.store.is_ask_before_major(self._k(name))

    def toggle_ask_before_major(self, name):
        return self.store.toggle_ask_before_major(self._k(name))

    def get_trust_running(self):
        return set(self._mine(self.store.get_trust_running()))

    def is_trust_running(self, name):
        return self.store.is_trust_running(self._k(name))

    def toggle_trust_running(self, name):
        return self.store.toggle_trust_running(self._k(name))

    def get_protect_stop(self):
        return set(self._mine(self.store.get_protect_stop()))

    def is_protect_stop(self, name):
        return self.store.is_protect_stop(self._k(name))

    def toggle_protect_stop(self, name):
        return self.store.toggle_protect_stop(self._k(name))

    # ── cooldowns ─────────────────────────────────────────────
    def get_cooldowns(self):
        return self._mine_dict(self.store.get_cooldowns())

    def get_cooldown(self, name):
        return self.store.get_cooldown(self._k(name))

    def set_cooldown(self, name, seconds):
        return self.store.set_cooldown(self._k(name), seconds)

    # ── deferred major updates ────────────────────────────────
    def get_pending_major(self):
        return self._mine_dict(self.store.get_pending_major())

    def add_pending_major(self, name, payload):
        return self.store.add_pending_major(self._k(name), payload)

    def remove_pending_major(self, name):
        return self.store.remove_pending_major(self._k(name))

    # ── notes and links ───────────────────────────────────────
    def get_notes(self):
        return self._mine_dict(self.store.get_notes())

    def get_note(self, name):
        return self.store.get_note(self._k(name))

    def set_note(self, name, text):
        return self.store.set_note(self._k(name), text)

    def get_links(self):
        return self._mine_dict(self.store.get_links())

    def get_link(self, name):
        return self.store.get_link(self._k(name))

    def set_link(self, name, url):
        return self.store.set_link(self._k(name), url)

    # ── groups ────────────────────────────────────────────────
    # A group's *members* are container names and get prefixed; the group
    # id and its label don't. Groups deliberately do NOT span hosts (#7,
    # design question 4) — a group belongs to the host its members live on.
    def get_groups(self):
        out = {}
        for gid, group in (self.store.get_groups() or {}).items():
            members = self._mine(group.get("containers") or [])
            if members:
                out[gid] = dict(group, containers=members)
        return out

    def get_group(self, group_id):
        return self.get_groups().get(group_id)

    def get_group_for_container(self, container_name):
        """(group_id, group) for this host's container, else (None, None) —
        same shape as the underlying store. The group comes back with plain
        member names, like `get_group` does."""
        gid, group = self.store.get_group_for_container(self._k(container_name))
        if not gid:
            return None, None
        return gid, dict(group, containers=self._mine(group.get("containers") or []))

    def save_group(self, group_id, name, containers, wait_seconds=30, **kw):
        return self.store.save_group(group_id, name,
                                     [self._k(c) for c in containers],
                                     wait_seconds, **kw)

    def delete_group(self, group_id):
        return self.store.delete_group(group_id)

    def reorder_group_container(self, group_id, container_name, direction):
        return self.store.reorder_group_container(
            group_id, self._k(container_name), direction)
