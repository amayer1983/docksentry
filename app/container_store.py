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
