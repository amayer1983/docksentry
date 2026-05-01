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
