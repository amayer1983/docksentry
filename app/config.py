#!/usr/bin/env python3
"""Configuration from environment variables with persistent overrides."""

import json
import os


def _strip_quotes(value):
    """Strip matching outer single/double quotes from an env-var value.

    Docker Compose passes env values literally — writing
    ``BOT_TOKEN="abc123"`` in a compose file lands in the runtime env as
    the string ``"abc123"`` (quotes included). Downstream parsing then
    fails: the Telegram API call uses the wrong token (with quotes in
    it), ``int()`` conversion on a quoted number raises ValueError, etc.
    Reported by @LeeNX in #30 against ``BOT_TOKEN`` quoting.

    Only strip when the FIRST and LAST chars are the same quote type
    (``"…"`` or ``'…'``). Anything else (mismatched, single quote,
    trailing whitespace) is left alone so we don't accidentally strip
    a legitimately-quoted password / token. Empty strings pass through.
    """
    if not value or len(value) < 2:
        return value
    if value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _env(key, default=""):
    """Read an env var and strip matching outer quotes. See _strip_quotes."""
    return _strip_quotes(os.environ.get(key, default))


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
    "bot_label", "docker_stop_timeout",
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
                 bot_label, docker_stop_timeout,
                 docker_username, docker_password,
                 docker_auth_config, docker_registry):
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
        # Per-container opt-in: after an update, accept `state=running` even
        # if the healthcheck reports `unhealthy`. For containers with brittle
        # healthchecks (e.g. VPN-sidecar dependents whose probe hits the
        # wrong namespace) that work fine but flap unhealthy. See #9.
        self.trust_running_file = os.path.join(data_dir, "trust_running_containers.json")
        # Pending major-confirmation queue (key: container_name → metadata)
        self.major_pending_file = os.path.join(data_dir, "major_confirmations.json")
        # Container groups (ordered update sequences)
        self.groups_file = os.path.join(data_dir, "groups.json")
        # Maintenance mode state ({"until": ISO|"forever"} or empty)
        self.maintenance_file = os.path.join(data_dir, "maintenance.json")
        # Per-container free-text notes
        self.notes_file = os.path.join(data_dir, "container_notes.json")
        # Per-container source/repo links (#20) — manual override of the
        # OCI `image.source` label auto-detection. Wraps the container
        # name in update notifications as a markdown link.
        self.links_file = os.path.join(data_dir, "container_links.json")
        # Last disk warning timestamp (rate-limit warnings to 1/day)
        self.disk_warn_state_file = os.path.join(data_dir, "disk_warn_state.json")
        # Deferred-check marker — written when AUTO_SELFUPDATE is about to
        # restart the container during a cron tick. The freshly-booted
        # process sees the marker and runs the container-update check
        # immediately instead of waiting for the next tick. Stale markers
        # (> 1h) are ignored so a failed self-update doesn't trigger a
        # phantom check on the next manual restart.
        self.deferred_check_file = os.path.join(data_dir, "deferred_check.json")
        # Written by the SIGTERM/SIGINT handler so the next boot can report
        # *why* it restarted (external stop signal vs unexpected exit) — so
        # users don't mistake a host reboot / `docker restart` for Docksentry
        # restarting itself. See #2 (@famewolf).
        self.last_exit_file = os.path.join(data_dir, "last_exit.json")
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
        # Minimum seconds we allow `docker stop` to take before falling
        # back to `docker kill`. Acts as a floor: the actual wait is
        # max(this, the container's own Config.StopTimeout). Default 60s
        # works for almost everything; raise for stacks with apps that
        # legitimately flush state for longer on shutdown (some DBs,
        # log aggregators). See #11.
        self.docker_stop_timeout = docker_stop_timeout
        # Docker registry authentication (#18). Three ways to supply
        # credentials, checked in priority order at startup:
        #   1. DOCKER_AUTH_CONFIG — path to an existing `config.json`
        #      (we set DOCKER_CONFIG to its parent dir; docker CLI picks
        #      the file up automatically). Best for users who already
        #      manage docker creds outside of Docksentry.
        #   2. DOCKER_USERNAME + DOCKER_PASSWORD — we run `docker login`
        #      once at startup. Simpler for users who don't already have
        #      a config.json.
        #   3. Neither — anonymous pulls (default).
        # DOCKER_REGISTRY lets the login point at a non-Docker-Hub
        # registry (ghcr.io, quay.io, internal Harbor, …). Empty = Hub.
        # Credentials are env-only on purpose: never persisted to
        # settings.json so they don't end up on the data volume.
        self.docker_username = docker_username
        self.docker_password = docker_password
        self.docker_auth_config = docker_auth_config
        self.docker_registry = docker_registry

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
        """Save current settings to settings.json for persistence.

        Atomic write via the shared ``atomic_write_json`` helper —
        see its docstring for the rationale. v1.22.1 refactored this
        to share the helper with every other JSON write in the codebase.
        """
        from container_store import atomic_write_json
        data = {}
        for key in PERSISTENT_KEYS:
            data[key] = getattr(self, key)
        try:
            atomic_write_json(self.settings_file, data, indent=2)
            self._restrict_settings_perms()
        except OSError as e:
            print(f"Failed to save settings: {e}")
            try:
                os.unlink(self.settings_file + ".tmp")
            except OSError:
                pass

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
        # All reads go through _env() which strips matching outer quote
        # pairs — Docker Compose passes `BOT_TOKEN="abc"` literally with
        # quotes (#30, @LeeNX). _env() is a thin wrapper around
        # os.environ.get with the strip. Boolean / int reads benefit too:
        # quoted `"true"` and `"5"` would otherwise miss the bool match
        # / break int() respectively.
        return cls(
            bot_token=_env("BOT_TOKEN"),
            chat_id=_env("CHAT_ID"),
            cron_schedule=_env("CRON_SCHEDULE", "0 18 * * *"),
            exclude_containers=[
                c.strip() for c in _env("EXCLUDE_CONTAINERS").split(",")
                if c.strip()
            ],
            data_dir=_env("DATA_DIR", "/data"),
            auto_selfupdate=_env("AUTO_SELFUPDATE", "false").lower() in ("true", "1", "yes"),
            auto_cleanup=_env("AUTO_CLEANUP", "false").lower() in ("true", "1", "yes"),
            cleanup_grace_hours=int(_env("CLEANUP_GRACE_HOURS", "24")),
            cleanup_backup_local_only=_env("CLEANUP_BACKUP_LOCAL_ONLY", "false").lower() in ("true", "1", "yes"),
            cleanup_backup_days=int(_env("CLEANUP_BACKUP_DAYS", "7")),
            disk_warn_percent=int(_env("DISK_WARN_PERCENT", "85")),
            disk_warn_auto_cleanup=_env("DISK_WARN_AUTO_CLEANUP", "false").lower() in ("true", "1", "yes"),
            quiet_hours_start=_env("QUIET_HOURS_START"),
            quiet_hours_end=_env("QUIET_HOURS_END"),
            weekly_report_enabled=_env("WEEKLY_REPORT_ENABLED", "false").lower() in ("true", "1", "yes"),
            weekly_report_weekday=int(_env("WEEKLY_REPORT_WEEKDAY", "0")),
            weekly_report_hour=int(_env("WEEKLY_REPORT_HOUR", "9")),
            language=_env("LANGUAGE", "en"),
            web_ui=_env("WEB_UI", "false").lower() in ("true", "1", "yes"),
            web_port=int(_env("WEB_PORT", "8080")),
            web_password=_env("WEB_PASSWORD"),
            discord_webhook=_env("DISCORD_WEBHOOK"),
            webhook_url=_env("WEBHOOK_URL"),
            telegram_topic_id=_env("TELEGRAM_TOPIC_ID"),
            telegram_allowed_users=[
                u.strip() for u in _env("TELEGRAM_ALLOWED_USERS").split(",")
                if u.strip()
            ],
            healthcheck_max_starting=int(_env("HEALTHCHECK_MAX_STARTING", "600")),
            bot_label=_env("BOT_LABEL").strip(),
            docker_stop_timeout=int(_env("DOCKER_STOP_TIMEOUT", "60")),
            docker_username=_env("DOCKER_USERNAME").strip(),
            # DOCKER_PASSWORD: NO .strip() — leading/trailing whitespace
            # can be a legitimate part of a password. We still strip
            # matching outer quote pairs via _env() since those are an
            # unambiguous Compose-quoting artefact.
            docker_password=_env("DOCKER_PASSWORD"),
            docker_auth_config=_env("DOCKER_AUTH_CONFIG").strip(),
            docker_registry=_env("DOCKER_REGISTRY").strip(),
        )
