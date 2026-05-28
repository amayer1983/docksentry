#!/usr/bin/env python3
"""Configuration from environment variables with persistent overrides."""

import json
import os


# Settings that can be changed via Web UI and persist across restarts
PERSISTENT_KEYS = [
    "cron_schedule", "exclude_containers", "auto_selfupdate", "auto_cleanup",
    "cleanup_grace_hours", "cleanup_backup_local_only", "cleanup_backup_days",
    "disk_warn_percent", "disk_warn_auto_cleanup",
    "quiet_hours_start", "quiet_hours_end",
    "weekly_report_enabled", "weekly_report_weekday", "weekly_report_hour",
    "web_setup_done", "ui_mode",
    "language", "web_password", "discord_webhook", "webhook_url", "debug",
    "telegram_topic_id", "telegram_allowed_users",
    "healthcheck_max_starting",
    "bot_label",
]


class Config:
    def __init__(self, bot_token, chat_id, cron_schedule, exclude_containers, data_dir,
                 auto_selfupdate, auto_cleanup, cleanup_grace_hours,
                 cleanup_backup_local_only, cleanup_backup_days,
                 disk_warn_percent, disk_warn_auto_cleanup,
                 quiet_hours_start, quiet_hours_end,
                 weekly_report_enabled, weekly_report_weekday, weekly_report_hour,
                 language, web_ui, web_port, web_password,
                 discord_webhook, webhook_url, telegram_topic_id,
                 telegram_allowed_users, healthcheck_max_starting,
                 bot_label):
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
        self.auto_cleanup = auto_cleanup
        self.cleanup_grace_hours = cleanup_grace_hours
        self.cleanup_backup_local_only = cleanup_backup_local_only
        self.cleanup_backup_days = cleanup_backup_days
        self.cleanup_backup_dir = os.path.join(data_dir, "cleanup-backups")
        # Disk space warning
        self.disk_warn_percent = disk_warn_percent
        self.disk_warn_auto_cleanup = disk_warn_auto_cleanup
        # Quiet hours (HH:MM strings, empty = feature off)
        self.quiet_hours_start = quiet_hours_start
        self.quiet_hours_end = quiet_hours_end
        # Weekly report
        self.weekly_report_enabled = weekly_report_enabled
        self.weekly_report_weekday = weekly_report_weekday  # 0=Mon..6=Sun
        self.weekly_report_hour = weekly_report_hour        # 0..23
        self.weekly_report_state_file = os.path.join(data_dir, "weekly_report_state.json")
        # First-run wizard flag — auto-true if env vars look configured
        self.web_setup_done = False
        # UI mode: "simple" hides advanced fields/cards; "advanced" shows
        # everything. New installs default simple via the wizard;
        # existing installs default advanced (preserved on migration).
        self.ui_mode = "advanced"
        # Per-container update windows (loaded by ContainerStore at runtime)
        self.update_windows_file = os.path.join(data_dir, "update_windows.json")
        # Per-container "ask before major update" flag
        self.ask_before_major_file = os.path.join(data_dir, "ask_before_major.json")
        # Pending major-confirmation queue (key: container_name → metadata)
        self.major_pending_file = os.path.join(data_dir, "major_confirmations.json")
        # Container groups (ordered update sequences)
        self.groups_file = os.path.join(data_dir, "groups.json")
        # Maintenance mode state ({"until": ISO|"forever"} or empty)
        self.maintenance_file = os.path.join(data_dir, "maintenance.json")
        # Per-container free-text notes
        self.notes_file = os.path.join(data_dir, "container_notes.json")
        # Last disk warning timestamp (rate-limit warnings to 1/day)
        self.disk_warn_state_file = os.path.join(data_dir, "disk_warn_state.json")
        # Deferred-check marker — written when AUTO_SELFUPDATE is about to
        # restart the container during a cron tick. The freshly-booted
        # process sees the marker and runs the container-update check
        # immediately instead of waiting for the next tick. Stale markers
        # (> 1h) are ignored so a failed self-update doesn't trigger a
        # phantom check on the next manual restart.
        self.deferred_check_file = os.path.join(data_dir, "deferred_check.json")
        self.language = language
        self.web_ui = web_ui
        self.web_port = web_port
        self.web_password = web_password
        self.discord_webhook = discord_webhook
        self.webhook_url = webhook_url
        self.telegram_topic_id = telegram_topic_id
        # Optional whitelist of Telegram user IDs (in addition to the
        # chat-origin check). Empty list means "any user in the
        # configured chat is allowed" — fine for 1:1 chats; useful in
        # groups where you don't want every member to be able to
        # trigger updates. Stored as a list of stringified IDs.
        self.telegram_allowed_users = telegram_allowed_users
        # Max seconds we wait for a freshly-updated container to leave
        # "starting" health-state and report "healthy". Slow apps like
        # GitLab / Nextcloud / Mastodon can need 10+ minutes. We also
        # respect the image's own Healthcheck.StartPeriod and use the
        # larger of (this default, start_period × 1.5) at runtime.
        self.healthcheck_max_starting = healthcheck_max_starting
        # Optional label prepended to every outgoing notification —
        # useful when multiple Docksentry instances post into the same
        # Telegram group / Discord channel so the user can tell which
        # host a message is from. Empty = no prefix (default, suitable
        # for single-host or single-DM setups). Stepping stone toward
        # the v2.0 multi-host refactor.
        self.bot_label = bot_label

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
        # Tighten permissions on existing settings file (covers upgrade case
        # where the file was created with the umask default, typically 0644).
        self._restrict_settings_perms()

    def save_persistent(self):
        """Save current settings to settings.json for persistence."""
        data = {}
        for key in PERSISTENT_KEYS:
            data[key] = getattr(self, key)
        try:
            with open(self.settings_file, "w") as f:
                json.dump(data, f, indent=2)
            self._restrict_settings_perms()
        except IOError as e:
            print(f"Failed to save settings: {e}")

    def _restrict_settings_perms(self):
        """Restrict settings.json to owner-only read/write (0600).

        settings.json contains webhook URLs that may include auth tokens,
        Telegram topic IDs, etc. — anyone who shares the data volume should
        not be able to read it.
        """
        try:
            os.chmod(self.settings_file, 0o600)
        except OSError:
            pass  # Best-effort; some filesystems (FAT, etc.) ignore chmod.

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
            auto_cleanup=os.environ.get("AUTO_CLEANUP", "false").lower() in ("true", "1", "yes"),
            cleanup_grace_hours=int(os.environ.get("CLEANUP_GRACE_HOURS", "24")),
            cleanup_backup_local_only=os.environ.get("CLEANUP_BACKUP_LOCAL_ONLY", "false").lower() in ("true", "1", "yes"),
            cleanup_backup_days=int(os.environ.get("CLEANUP_BACKUP_DAYS", "7")),
            disk_warn_percent=int(os.environ.get("DISK_WARN_PERCENT", "85")),
            disk_warn_auto_cleanup=os.environ.get("DISK_WARN_AUTO_CLEANUP", "false").lower() in ("true", "1", "yes"),
            quiet_hours_start=os.environ.get("QUIET_HOURS_START", ""),
            quiet_hours_end=os.environ.get("QUIET_HOURS_END", ""),
            weekly_report_enabled=os.environ.get("WEEKLY_REPORT_ENABLED", "false").lower() in ("true", "1", "yes"),
            weekly_report_weekday=int(os.environ.get("WEEKLY_REPORT_WEEKDAY", "0")),
            weekly_report_hour=int(os.environ.get("WEEKLY_REPORT_HOUR", "9")),
            language=os.environ.get("LANGUAGE", "en"),
            web_ui=os.environ.get("WEB_UI", "false").lower() in ("true", "1", "yes"),
            web_port=int(os.environ.get("WEB_PORT", "8080")),
            web_password=os.environ.get("WEB_PASSWORD", ""),
            discord_webhook=os.environ.get("DISCORD_WEBHOOK", ""),
            webhook_url=os.environ.get("WEBHOOK_URL", ""),
            telegram_topic_id=os.environ.get("TELEGRAM_TOPIC_ID", ""),
            telegram_allowed_users=[
                u.strip() for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
                if u.strip()
            ],
            healthcheck_max_starting=int(os.environ.get("HEALTHCHECK_MAX_STARTING", "600")),
            bot_label=os.environ.get("BOT_LABEL", "").strip(),
        )
