#!/usr/bin/env python3
"""Configuration from environment variables with persistent overrides."""

import json
import os


# Settings that can be changed via Web UI and persist across restarts
PERSISTENT_KEYS = [
    "cron_schedule", "exclude_containers", "auto_selfupdate",
    "language", "web_password", "discord_webhook", "webhook_url", "debug",
]


class Config:
    def __init__(self, bot_token, chat_id, cron_schedule, exclude_containers, data_dir,
                 auto_selfupdate, language, web_ui, web_port, web_password,
                 discord_webhook, webhook_url):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.cron_schedule = cron_schedule
        self.exclude_containers = exclude_containers
        self.data_dir = data_dir
        self.pending_file = os.path.join(data_dir, "pending_updates.json")
        self.history_file = os.path.join(data_dir, "update_history.json")
        self.pinned_file = os.path.join(data_dir, "pinned_containers.json")
        self.autoupdate_file = os.path.join(data_dir, "autoupdate_containers.json")
        self.settings_file = os.path.join(data_dir, "settings.json")
        self.debug = False
        self.auto_selfupdate = auto_selfupdate
        self.language = language
        self.web_ui = web_ui
        self.web_port = web_port
        self.web_password = web_password
        self.discord_webhook = discord_webhook
        self.webhook_url = webhook_url

        # Load persistent overrides from settings.json
        self._load_persistent()

    def _load_persistent(self):
        """Load saved settings from settings.json, overriding ENV defaults."""
        if not os.path.exists(self.settings_file):
            return
        try:
            with open(self.settings_file) as f:
                saved = json.load(f)
            for key in PERSISTENT_KEYS:
                if key in saved:
                    setattr(self, key, saved[key])
        except (json.JSONDecodeError, IOError):
            pass

    def save_persistent(self):
        """Save current settings to settings.json for persistence."""
        data = {}
        for key in PERSISTENT_KEYS:
            data[key] = getattr(self, key)
        try:
            with open(self.settings_file, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            print(f"Failed to save settings: {e}")

    @classmethod
    def from_env(cls):
        return cls(
            bot_token=os.environ.get("BOT_TOKEN", ""),
            chat_id=os.environ.get("CHAT_ID", ""),
            cron_schedule=os.environ.get("CRON_SCHEDULE", "0 18 * * *"),
            exclude_containers=[
                c.strip() for c in os.environ.get("EXCLUDE_CONTAINERS", "").split(",")
                if c.strip()
            ],
            data_dir=os.environ.get("DATA_DIR", "/data"),
            auto_selfupdate=os.environ.get("AUTO_SELFUPDATE", "false").lower() in ("true", "1", "yes"),
            language=os.environ.get("LANGUAGE", "en"),
            web_ui=os.environ.get("WEB_UI", "false").lower() in ("true", "1", "yes"),
            web_port=int(os.environ.get("WEB_PORT", "8080")),
            web_password=os.environ.get("WEB_PASSWORD", ""),
            discord_webhook=os.environ.get("DISCORD_WEBHOOK", ""),
            webhook_url=os.environ.get("WEBHOOK_URL", ""),
        )
