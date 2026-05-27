#!/usr/bin/env python3
"""Persistent state for per-container flags (pinned, auto-update).

Used by TelegramBot, the scheduler and the Web UI. Storing the lists in a
small dedicated module instead of the Telegram bot lets headless setups
(no Telegram) still pin containers and toggle auto-update via the Web UI.
"""

import json
import os


class ContainerStore:
    def __init__(self, config):
        self.pinned_file = config.pinned_file
        self.autoupdate_file = config.autoupdate_file
        self.update_windows_file = config.update_windows_file
        self.ask_before_major_file = config.ask_before_major_file
        self.major_pending_file = config.major_pending_file
        self.groups_file = config.groups_file
        self.notes_file = config.notes_file

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
        try:
            with open(path, "w") as f:
                json.dump(names, f)
        except IOError as e:
            print(f"Failed to save {path}: {e}")

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
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            print(f"Failed to save {path}: {e}")
