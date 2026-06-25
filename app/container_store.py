#!/usr/bin/env python3
"""Persistent state for per-container flags (pinned, auto-update).

Used by TelegramBot, the scheduler and the Web UI. Storing the lists in a
small dedicated module instead of the Telegram bot lets headless setups
(no Telegram) still pin containers and toggle auto-update via the Web UI.
"""

import json
import os


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
        # Minimal validation: must start with http(s):// to render as
        # a clickable link in Telegram / Discord. Empty clears.
        if url and not (url.startswith("http://") or url.startswith("https://")):
            return False
        if url:
            links[name] = url[:500]  # cap to keep payload reasonable
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
