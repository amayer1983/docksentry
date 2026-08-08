#!/usr/bin/env python3
"""Configuration from environment variables with persistent overrides."""

import json
import os
import re


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


#: Host names we won't accept from config — `local` always means the
#: machine Docksentry itself runs on and can't be reassigned.
RESERVED_HOST_NAMES = ("local", "all")
#: `all` is reserved too: `@all` is the "every host" target in commands,
#: so a host actually named `all` could never be addressed individually.


def parse_docker_hosts(raw):
    """Parse `DOCKER_HOSTS` into a list of {"name", "endpoint"} dicts (#7).

    Format: ``name:endpoint, name2:endpoint2`` — e.g.
    ``pve1:ssh://root@pve1, nas:tcp://nas:2375``. Only the FIRST colon
    separates name from endpoint, because endpoints contain colons too.

    Fails soft, deliberately: a malformed entry is skipped with a warning
    rather than taking the whole instance down. A typo'd host should cost
    you that host, not your monitoring. Empty/unset input gives an empty
    list, which means "single-host, exactly as before".

    Names are lowercased and must be alphanumeric plus `-`/`_` so they can
    serve as routing tokens in commands and as state-key prefixes without
    quoting. Duplicates and the reserved `local` are dropped.
    """
    hosts = []
    seen = set()
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        name, sep, endpoint = entry.partition(":")
        name = name.strip().lower()
        endpoint = endpoint.strip()
        if not sep or not endpoint:
            print(f"DOCKER_HOSTS: skipping {entry!r} — expected name:endpoint")
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            print(f"DOCKER_HOSTS: skipping {entry!r} — host name must be "
                  f"alphanumeric (plus - and _)")
            continue
        if name in RESERVED_HOST_NAMES:
            why = ("means the machine Docksentry runs on" if name == "local"
                   else "means every host, as in `/check @all`")
            print(f"DOCKER_HOSTS: skipping {entry!r} — {name!r} is reserved: "
                  f"it {why}")
            continue
        if name in seen:
            print(f"DOCKER_HOSTS: skipping duplicate host {name!r}")
            continue
        seen.add(name)
        hosts.append({"name": name, "endpoint": endpoint})
    return hosts


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
    "monitor_enabled", "monitor_interval_seconds", "monitor_events_enabled",
    "smtp_tls_verify", "monitor_only_containers", "insecure_registries",
    "registry_mirrors", "min_image_age_days",
    "api_tokens",
    "discord_bot_token", "discord_app_id", "discord_guild_id",
    "discord_allowed_users",
    "smtp_host", "smtp_port", "smtp_user", "smtp_password",
    "smtp_from", "smtp_to", "smtp_tls",
    "ntfy_url",
    "ntfy_server",
    "ntfy_topic",
    "ntfy_token",
    "ntfy_user",
    "ntfy_password",
    "matrix_homeserver",
    "matrix_room",
    "matrix_token",
    "apprise_url",
    "apprise_urls",
    "apprise_tag",
    "gotify_url",
    "gotify_token",
]

# Attribute name → environment variable, for every persistent key that can
# be seeded from the environment. Written out by hand and checked against
# from_env() one key at a time — `key.upper()` is NOT the rule: MONITOR
# seeds monitor_enabled and MONITOR_INTERVAL seeds monitor_interval_seconds.
# web_setup_done and ui_mode have no env var at all and are absent here on
# purpose. Keep this in sync when a key is added to PERSISTENT_KEYS.
PERSISTENT_ENV_VARS = {
    "cron_schedule": "CRON_SCHEDULE",
    "exclude_containers": "EXCLUDE_CONTAINERS",
    "auto_selfupdate": "AUTO_SELFUPDATE",
    "auto_cleanup": "AUTO_CLEANUP",
    "cleanup_grace_hours": "CLEANUP_GRACE_HOURS",
    "cleanup_backup_local_only": "CLEANUP_BACKUP_LOCAL_ONLY",
    "cleanup_backup_days": "CLEANUP_BACKUP_DAYS",
    "disk_warn_percent": "DISK_WARN_PERCENT",
    "disk_warn_auto_cleanup": "DISK_WARN_AUTO_CLEANUP",
    "quiet_hours_start": "QUIET_HOURS_START",
    "quiet_hours_end": "QUIET_HOURS_END",
    "weekly_report_enabled": "WEEKLY_REPORT_ENABLED",
    "weekly_report_weekday": "WEEKLY_REPORT_WEEKDAY",
    "weekly_report_hour": "WEEKLY_REPORT_HOUR",
    "language": "LANGUAGE",
    "web_password": "WEB_PASSWORD",
    "discord_webhook": "DISCORD_WEBHOOK",
    "webhook_url": "WEBHOOK_URL",
    "debug": "DEBUG",
    "telegram_topic_id": "TELEGRAM_TOPIC_ID",
    "telegram_allowed_users": "TELEGRAM_ALLOWED_USERS",
    "healthcheck_max_starting": "HEALTHCHECK_MAX_STARTING",
    "bot_label": "BOT_LABEL",
    "docker_stop_timeout": "DOCKER_STOP_TIMEOUT",
    "monitor_enabled": "MONITOR",
    "monitor_interval_seconds": "MONITOR_INTERVAL",
    "monitor_events_enabled": "MONITOR_EVENTS",
    "smtp_tls_verify": "SMTP_TLS_VERIFY",
    "monitor_only_containers": "MONITOR_ONLY_CONTAINERS",
    "insecure_registries": "INSECURE_REGISTRIES",
    "registry_mirrors": "REGISTRY_MIRRORS",
    "min_image_age_days": "MIN_IMAGE_AGE_DAYS",
    "api_tokens": "API_TOKENS",
    "discord_bot_token": "DISCORD_BOT_TOKEN",
    "discord_app_id": "DISCORD_APP_ID",
    "discord_guild_id": "DISCORD_GUILD_ID",
    "discord_allowed_users": "DISCORD_ALLOWED_USERS",
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_user": "SMTP_USER",
    "smtp_password": "SMTP_PASSWORD",
    "smtp_from": "SMTP_FROM",
    "smtp_to": "SMTP_TO",
    "smtp_tls": "SMTP_TLS",
    "ntfy_url": "NTFY_URL",
    "ntfy_server": "NTFY_SERVER",
    "ntfy_topic": "NTFY_TOPIC",
    "ntfy_token": "NTFY_TOKEN",
    "ntfy_user": "NTFY_USER",
    "ntfy_password": "NTFY_PASSWORD",
    "matrix_homeserver": "MATRIX_HOMESERVER",
    "matrix_room": "MATRIX_ROOM",
    "matrix_token": "MATRIX_TOKEN",
    "apprise_url": "APPRISE_URL",
    "apprise_urls": "APPRISE_URLS",
    "apprise_tag": "APPRISE_TAG",
    "gotify_url": "GOTIFY_URL",
    "gotify_token": "GOTIFY_TOKEN",
}

# The value from_env() ends up with when the variable is absent — already
# parsed (bool / int / list), not the raw string, so the comparison in
# _detect_env_overrides is against the same type the attribute holds.
#
# These duplicate the defaults written inline in from_env(); keeping them
# honest is test_env_override.py's job — it builds a Config with every
# variable stripped from the environment and asserts each attribute comes
# out equal to its entry here, so a default changed in one place and not
# the other fails the suite instead of quietly skewing the detection.
#
# Deliberately NOT read from the Dockerfile: the image the process happens
# to run in is not the authority on what Docksentry's defaults are (it can
# be an old image, a rebuild, or no image at all when run from source).
PERSISTENT_ENV_DEFAULTS = {
    "cron_schedule": "0 18 * * *",
    "exclude_containers": [],
    "auto_selfupdate": False,
    "auto_cleanup": False,
    "cleanup_grace_hours": 24,
    "cleanup_backup_local_only": False,
    "cleanup_backup_days": 7,
    "disk_warn_percent": 85,
    "disk_warn_auto_cleanup": False,
    "quiet_hours_start": "",
    "quiet_hours_end": "",
    "weekly_report_enabled": False,
    "weekly_report_weekday": 0,
    "weekly_report_hour": 9,
    "language": "en",
    "web_password": "",
    "discord_webhook": "",
    "webhook_url": "",
    "debug": False,
    "telegram_topic_id": "",
    "telegram_allowed_users": [],
    "healthcheck_max_starting": 600,
    "bot_label": "",
    "docker_stop_timeout": 60,
    "monitor_enabled": True,
    "monitor_interval_seconds": 60,
    "monitor_events_enabled": True,
    "smtp_tls_verify": True,
    "monitor_only_containers": [],
    "insecure_registries": [],
    "registry_mirrors": [],
    "min_image_age_days": 0,
    "api_tokens": [],
    "discord_bot_token": "",
    "discord_app_id": "",
    "discord_guild_id": "",
    "discord_allowed_users": [],
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_to": "",
    "smtp_tls": "starttls",
    "ntfy_url": "",
    "ntfy_server": "",
    "ntfy_topic": "",
    "ntfy_token": "",
    "ntfy_user": "",
    "ntfy_password": "",
    "matrix_homeserver": "",
    "matrix_room": "",
    "matrix_token": "",
    "apprise_url": "",
    "apprise_urls": "",
    "apprise_tag": "",
    "gotify_url": "",
    "gotify_token": "",
}

# Persistent keys whose *value* may be printed (startup log, Web UI hint).
# Deliberately an ALLOW-list: everything not named here — including any key
# someone adds to PERSISTENT_KEYS later — is treated as a secret and gets
# reported by name only. Forgetting to add a harmless key here costs a bit
# of detail in one log line; forgetting to add a secret to a deny-list
# would put a password into `docker logs`.
LOGGABLE_PERSISTENT_KEYS = {
    "cron_schedule", "exclude_containers", "auto_selfupdate", "auto_cleanup",
    "cleanup_grace_hours", "cleanup_backup_local_only", "cleanup_backup_days",
    "disk_warn_percent", "disk_warn_auto_cleanup",
    "quiet_hours_start", "quiet_hours_end",
    "weekly_report_enabled", "weekly_report_weekday", "weekly_report_hour",
    "web_setup_done", "ui_mode", "language", "debug",
    "healthcheck_max_starting", "bot_label", "docker_stop_timeout",
    "monitor_enabled", "monitor_interval_seconds", "monitor_events_enabled",
    "smtp_tls_verify", "monitor_only_containers", "insecure_registries",
    "registry_mirrors", "min_image_age_days",
}
# NOT loggable, for the record: web_password, discord_webhook, webhook_url,
# discord_bot_token (credentials / tokenised URLs) and telegram_topic_id +
# telegram_allowed_users + discord_allowed_users + discord_guild_id +
# discord_app_id (personal data, or identifiers for someone's private
# server — main.py already prints only the *count* of allowed users for the
# same reason). The allow-list above means none of these needed adding
# anywhere to be treated as secret; they are named here so that a later
# reader can see the decision was made rather than missed.

# Which Settings tab a key is edited on. Only meaningful for keys that
# actually have a field in the Web UI form — a warning that doesn't say
# where to go isn't worth printing. Keys with no field of their own are
# absent and get the settings.json-only wording instead.
PERSISTENT_SETTINGS_TAB = {
    "language": "General",
    "cron_schedule": "General",
    "exclude_containers": "General",
    "debug": "General",
    "web_password": "General",
    "auto_selfupdate": "Updates",
    "healthcheck_max_starting": "Updates",
    "docker_stop_timeout": "Updates",
    "auto_cleanup": "Cleanup",
    "cleanup_grace_hours": "Cleanup",
    "cleanup_backup_days": "Cleanup",
    "cleanup_backup_local_only": "Cleanup",
    "disk_warn_percent": "Notifications",
    "disk_warn_auto_cleanup": "Notifications",
    "quiet_hours_start": "Notifications",
    "quiet_hours_end": "Notifications",
    "weekly_report_enabled": "Notifications",
    "weekly_report_weekday": "Notifications",
    "weekly_report_hour": "Notifications",
    "monitor_enabled": "Notifications",
    "monitor_events_enabled": "Notifications",
    "monitor_interval_seconds": "Notifications",
    "telegram_topic_id": "Connections",
    "telegram_allowed_users": "Connections",
    "bot_label": "Connections",
    "discord_webhook": "Connections",
    "discord_bot_token": "Connections",
    "discord_app_id": "Connections",
    "discord_guild_id": "Connections",
    "discord_allowed_users": "Connections",
    "smtp_host": "Connections",
    "smtp_port": "Connections",
    "smtp_user": "Connections",
    "smtp_password": "Connections",
    "smtp_from": "Connections",
    "smtp_to": "Connections",
    "smtp_tls": "Connections",
    "smtp_tls_verify": "Connections",
    "ntfy_url": "Connections",
    "ntfy_server": "Connections",
    "ntfy_topic": "Connections",
    "ntfy_token": "Connections",
    "ntfy_user": "Connections",
    "ntfy_password": "Connections",
    "matrix_homeserver": "Connections",
    "matrix_room": "Connections",
    "matrix_token": "Connections",
    "apprise_url": "Connections",
    "apprise_urls": "Connections",
    "apprise_tag": "Connections",
    "gotify_url": "Connections",
    "gotify_token": "Connections",
    "webhook_url": "Connections",
}


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
                 docker_auth_config, docker_registry,
                 smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
                 smtp_from="", smtp_to="", smtp_tls="starttls",
                 auto_update_all=False,
                 monitor_enabled=True, monitor_interval_seconds=60,
                 monitor_events_enabled=True, smtp_tls_verify=True,
                 monitor_only_containers=None, insecure_registries=None,
                 registry_mirrors=None, min_image_age_days=0,
                 api_tokens=None,
                 telegram_polling=True, debug=False, update_policy="all",
                 container_cli="auto", docker_hosts="",
                 discord_bot_token="", discord_app_id="", discord_guild_id="",
                 discord_allowed_users=None,
                 ntfy_url="",
                 ntfy_server="",
                 ntfy_topic="",
                 ntfy_token="",
                 ntfy_user="",
                 ntfy_password="",
                 matrix_homeserver="",
                 matrix_room="",
                 matrix_token="",
                 apprise_url="",
                 apprise_urls="",
                 apprise_tag="",
                 gotify_url="",
                 gotify_token=""):
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
        # DEBUG follows the same precedence as every other persistent key:
        # the env var seeds the initial value, and a later /debug or Web UI
        # toggle persisted to settings.json still overrides on load (see
        # _load_persistent — "debug" is in PERSISTENT_KEYS). That precedence
        # is unchanged since #53 — but it is now announced instead of
        # happening silently; see _detect_env_overrides.
        self.debug = debug
        self.auto_selfupdate = auto_selfupdate
        # Global "auto-update every checked container" (Watchtower-style),
        # opposed to the per-container auto-update opt-in (#45, @NotRetarded).
        self.auto_update_all = auto_update_all
        # Global default update policy (v1.53.0, roadmap #2): caps which
        # semver bump levels auto-apply — "all" (every bump), "minor"
        # (minor+patch, skip major) or "patch" (patch only). The
        # per-container `docksentry.policy` label overrides this. Env-only
        # (a wiring decision, not a runtime toggle) — deliberately NOT in
        # PERSISTENT_KEYS, mirroring TELEGRAM_POLLING. Invalid → "all"
        # (fail-open).
        pol = (update_policy or "all").strip().lower()
        self.update_policy = pol if pol in ("all", "minor", "patch") else "all"
        # Which container CLI to speak: auto | docker | podman. "auto" keeps
        # the historical behaviour (Docker whenever `docker` exists, which
        # covers the docker→podman alias setups people already run).
        cli = (container_cli or "auto").strip().lower()
        self.container_cli = cli if cli in ("auto", "docker", "podman") else "auto"
        # Extra Docker/Podman hosts this instance also manages (#7). Format
        # (per the issue): "name:endpoint, name2:endpoint2", e.g.
        # "pve1:ssh://root@pve1, nas:tcp://nas:2375". Split on the FIRST
        # colon only — endpoints contain colons themselves. The machine we
        # run on is always managed and is NOT listed here; it stays the
        # implicit "local" host, so an unset DOCKER_HOSTS means exactly
        # today's single-host behaviour.
        self.docker_hosts = parse_docker_hosts(docker_hosts)
        # Interactive Discord bot (v2.0). Distinct from DISCORD_WEBHOOK,
        # which only pushes notifications one way — a bot token lets
        # Docksentry receive slash commands.
        #
        # These were env-only until v2.3.0, on the stated grounds that "a
        # bot token is a credential and has no business in the settings
        # file on disk". That reasoning does not survive looking at the
        # file: settings.json already holds web_password in plaintext and
        # webhook URLs whose path *is* the credential, and it is written
        # 0600 precisely because of that. The rule was never "no secrets
        # in settings.json"; it was "no secrets in settings.json except
        # the ones already there", which is not a rule.
        #
        # What the env-only stance did cost was real: setting the bot up
        # meant editing compose and recreating the container, and
        # @NotRetarded wrote a screenshot-by-screenshot guide (#57) to get
        # people through it before asking whether it could just be in the
        # interface. It can. The token is masked in the form, never
        # rendered back into HTML, excluded from the loggable allow-list,
        # and redacted in the audit trail.
        self.discord_bot_token = (discord_bot_token or "").strip()
        self.discord_app_id = (discord_app_id or "").strip()
        self.discord_guild_id = (discord_guild_id or "").strip()
        # Who may drive the Discord bot. Empty means "anyone in the
        # configured guild" — which is already a real restriction, since
        # the guild itself is required. Set this when the server has
        # members who shouldn't be able to stop your containers.
        raw = discord_allowed_users
        if isinstance(raw, str):
            raw = [p for p in raw.replace(";", ",").split(",")]
        self.discord_allowed_users = [str(u).strip() for u in (raw or [])
                                      if str(u).strip()]

        # ── ntfy / Matrix / Apprise / Gotify ───────────────────
        # These four channels read os.environ directly until v2.4.0,
        # deliberately, so that a channel stayed one file. They are
        # here now because they have fields on the Connections page,
        # and BaseNotifier.setting() prefers what is here over the
        # variable. Passwords and tokens among them: settings.json is
        # 0600 and they are off the loggable allow-list, same as the
        # Web password and the SMTP one.
        self.ntfy_url = (ntfy_url or "").strip()
        self.ntfy_server = (ntfy_server or "").strip()
        self.ntfy_topic = (ntfy_topic or "").strip()
        self.ntfy_token = (ntfy_token or "").strip()
        self.ntfy_user = (ntfy_user or "").strip()
        # Not stripped — a password may legitimately end in a space.
        self.ntfy_password = ntfy_password or ""
        self.matrix_homeserver = (matrix_homeserver or "").strip()
        self.matrix_room = (matrix_room or "").strip()
        self.matrix_token = (matrix_token or "").strip()
        self.apprise_url = (apprise_url or "").strip()
        self.apprise_urls = (apprise_urls or "").strip()
        self.apprise_tag = (apprise_tag or "").strip()
        self.gotify_url = (gotify_url or "").strip()
        self.gotify_token = (gotify_token or "").strip()
        # Container state monitoring (#2, @NotRetarded): health transitions,
        # non-zero exits, OOM kills, crash-restarts. Interval floored at 15s.
        self.monitor_enabled = monitor_enabled
        self.monitor_events_enabled = monitor_events_enabled
        self.smtp_tls_verify = smtp_tls_verify
        self.monitor_only_containers = monitor_only_containers or []
        self.insecure_registries = insecure_registries or []
        self.registry_mirrors = registry_mirrors or []
        try:
            self.min_image_age_days = max(0, int(min_image_age_days or 0))
        except (TypeError, ValueError):
            self.min_image_age_days = 0
        self.api_tokens = api_tokens or []
        try:
            self.monitor_interval_seconds = max(15, int(monitor_interval_seconds))
        except (TypeError, ValueError):
            self.monitor_interval_seconds = 60
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
        # Per-container post-recreate cooldown (seconds). After updating a
        # container that declares one, Docksentry waits before recreating the
        # NEXT container in the same batch — so a heavy (GPU/RAM) container's
        # load peak settles before the next one starts and they don't contend
        # for memory. Opt-in, default 0. From #2 (@famewolf, GPU OOM).
        self.cooldown_file = os.path.join(data_dir, "update_cooldowns.json")
        # Per-container opt-in: refuse to STOP this container (Telegram +
        # Web UI hide/deny the Stop button) so you can't accidentally take
        # down something you depend on — e.g. the VPN/tunnel that carries
        # your remote access. Restart stays allowed. See #38 (@LeeNX).
        self.protect_stop_file = os.path.join(data_dir, "protect_stop_containers.json")
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
        self.monitor_events_file = os.path.join(data_dir, "monitor_events.json")
        # Who did what, through which front end (v2.1 audit trail).
        self.audit_file = os.path.join(data_dir, "audit.json")
        # Last version that completed a start here — how an ordinary
        # `docker pull` + `up -d` gets noticed at all.
        self.version_state_file = os.path.join(data_dir, "version_state.json")
        # An update that is mid-flight. Written before the container is
        # renamed out of the way and removed when it lands, so a hard kill
        # leaves a record the next boot can act on.
        self.inflight_file = os.path.join(data_dir, "update_inflight.json")
        # "A newer version exists" notes for pinned version tags (#33).
        # Advisory only — never a pending update.
        self.advisories_file = os.path.join(data_dir, "version_advisories.json")
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
        # Written just before a self-update recreate so the next boot knows
        # the imminent SIGTERM was self-inflicted (a self-update) and does
        # NOT mislabel it as an external restart in the startup message.
        # Manual /selfupdate doesn't write the deferred-check marker, so we
        # need this separate one to cover it. See #2 (@famewolf).
        self.selfupdate_marker_file = os.path.join(data_dir, "selfupdate_restart.json")
        # The self-update helper writes its stdout/stderr here (it mounts
        # /data). The next boot reads it: if the recreate rolled back, the
        # helper's error is finally visible instead of vanishing with the
        # --rm helper container (#43 — diagnosing why podman rejects the run).
        self.selfupdate_helper_log = os.path.join(data_dir, "selfupdate_helper.log")
        self.language = language
        self.web_ui = web_ui
        self.web_port = web_port
        self.web_password = web_password
        self.discord_webhook = discord_webhook
        self.webhook_url = webhook_url
        # Native e-mail / SMTP notification channel (#2). Active when host +
        # from + to are set. tls: "starttls" (default, port 587), "ssl"
        # (implicit TLS, port 465), or "none" (plain, e.g. internal relay).
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.smtp_from = smtp_from
        self.smtp_to = smtp_to
        self.smtp_tls = (smtp_tls or "starttls").lower()
        self.telegram_topic_id = telegram_topic_id
        # Optional whitelist of Telegram user IDs (in addition to the
        # chat-origin check). Empty list means "any user in the
        # configured chat is allowed" — fine for 1:1 chats; useful in
        # groups where you don't want every member to be able to
        # trigger updates. Stored as a list of stringified IDs.
        self.telegram_allowed_users = telegram_allowed_users
        # Send-only Telegram: keep sending notifications but DON'T poll
        # getUpdates. Telegram allows exactly one getUpdates consumer per
        # bot token — a second one (e.g. Home Assistant sharing the same
        # bot) gets "Conflict: terminated by other getUpdates request" on
        # a loop (#2, @famewolf). sendMessage never conflicts, so this lets
        # one bot fan out notifications from several apps while another app
        # owns the interactive side. Interactive commands are off in this
        # mode; control Docksentry via the Web UI instead.
        self.telegram_polling = telegram_polling
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

        # Snapshot what the environment produced for every persistent key
        # that looks deliberately set — taken before settings.json lands on
        # top, so we can say afterwards which of them the saved file
        # overrules (#53, @LeeNX). "Deliberately set" = present in
        # os.environ AND not equal to our own default; see
        # _detect_env_overrides for why both halves are needed.
        _env_seeded = {}
        for key, var in PERSISTENT_ENV_VARS.items():
            if var not in os.environ:
                continue
            value = getattr(self, key)
            if value == PERSISTENT_ENV_DEFAULTS[key]:
                continue
            _env_seeded[key] = value

        # Load persistent overrides from settings.json
        self._load_persistent()

        # Precedence is unchanged — the saved value still wins, on purpose:
        # flipping it would silently reset every user who set something in
        # the env once and later changed it in the Web UI. What was missing
        # is that nothing ever *said* so.
        self.env_overrides = self._detect_env_overrides(_env_seeded)

    def _detect_env_overrides(self, env_seeded):
        """Which explicitly set env vars does settings.json overrule?

        ``env_seeded`` maps key → the value the environment produced,
        captured before _load_persistent() ran, for variables that are both
        present in os.environ AND carrying something other than our own
        default (PERSISTENT_ENV_DEFAULTS).

        Presence alone does not work, however much it looks like the right
        test. Our own Dockerfile declares 18 of these variables with `ENV`
        — CRON_SCHEDULE, CLEANUP_GRACE_HOURS, DISK_WARN_PERCENT,
        QUIET_HOURS_*, WEEKLY_REPORT_*, LANGUAGE and more — all at exactly
        the built-in default. They are therefore in os.environ in *every*
        Docksentry container whether the user typed them or not, so a
        presence check reported eight lines on a normal install where two
        were real. Six wrong lines hide the two right ones; that is worse
        than not warning at all.

        KNOWN BLIND SPOT, accepted on purpose: setting a variable to
        exactly the default while a different value is saved is no longer
        reported — `DEBUG=false` in the compose file against
        ``"debug": true`` in settings.json stays silent. That case is real
        but rare, and it is indistinguishable from the Dockerfile's own
        `ENV DEBUG="false"`-style declarations without parsing the image,
        which would make the running container the authority on our
        defaults. Not a bug and not an oversight — the price of the
        warning being readable at all.

        The trap being warned about is unchanged. @LeeNX set ``DEBUG=true``
        and got "debug OFF" plus ``"debug": false`` in settings.json — not
        because he ever turned debug off, but because save_persistent()
        writes *all* PERSISTENT_KEYS at once, so any unrelated Web UI save
        froze the then-current value. Same trap for every persistent key.
        """
        overrides = []
        for key, env_value in env_seeded.items():
            saved_value = getattr(self, key, None)
            if saved_value == env_value:
                continue
            overrides.append({
                "key": key,
                "var": PERSISTENT_ENV_VARS[key],
                "env": self._display_value(key, env_value),
                "saved": self._display_value(key, saved_value),
                "secret": key not in LOGGABLE_PERSISTENT_KEYS,
                "tab": PERSISTENT_SETTINGS_TAB.get(key, ""),
            })
        return overrides

    def env_override(self, key):
        """Return the override entry for `key`, or None. For the Web UI."""
        for o in self.env_overrides:
            if o["key"] == key:
                return o
        return None

    @staticmethod
    def _display_value(key, value):
        """Human-readable value for a persistent key — None if it's secret.

        The allow-list check lives here rather than at every call site so
        there is exactly one place that can leak a value, and its default
        answer is "no".
        """
        if key not in LOGGABLE_PERSISTENT_KEYS:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value) or "(empty)"
        return str(value) if str(value) != "" else "(empty)"

    def env_override_lines(self):
        """Startup log lines for the overruled env vars (English, like the
        rest of the boot output — no translation files down here).

        Every line has to answer "where do I change it?", otherwise it just
        tells the user something is wrong and leaves him there.
        """
        lines = []
        for o in self.env_overrides:
            if o["secret"]:
                what = (f"{o['var']} is set in the environment, but the saved "
                        f"setting wins (values hidden)")
            else:
                what = (f"{o['var']}={o['env']} is set in the environment, but "
                        f"the saved setting {o['key']}={o['saved']} wins")
            if o["tab"]:
                # "Connections" is a page of its own, not a tab inside
                # Settings — telling someone to look under "Settings ›
                # Connections" sends them somewhere that does not exist,
                # which is the whole failure mode this line exists to
                # avoid.
                place = ("the Connections page" if o["tab"] == "Connections"
                         else f"Settings › {o['tab']}")
                where = (f"change it under {place}, or remove "
                         f"\"{o['key']}\" from {self.settings_file}")
            else:
                where = (f"remove \"{o['key']}\" from {self.settings_file} to "
                         f"fall back to the environment")
            lines.append(f"Env override: {what} — {where}.")
        return lines

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
            debug=_env("DEBUG", "false").lower() in ("true", "1", "yes"),
            auto_selfupdate=_env("AUTO_SELFUPDATE", "false").lower() in ("true", "1", "yes"),
            auto_update_all=_env("AUTO_UPDATE_ALL", "false").lower() in ("true", "1", "yes"),
            update_policy=_env("UPDATE_POLICY", "all"),
            container_cli=_env("CONTAINER_CLI", "auto"),
            docker_hosts=_env("DOCKER_HOSTS", ""),
            discord_bot_token=_env("DISCORD_BOT_TOKEN", ""),
            discord_app_id=_env("DISCORD_APP_ID", ""),
            discord_guild_id=_env("DISCORD_GUILD_ID", ""),
            discord_allowed_users=_env("DISCORD_ALLOWED_USERS", ""),
            ntfy_url=_env("NTFY_URL", ""),
            ntfy_server=_env("NTFY_SERVER", ""),
            ntfy_topic=_env("NTFY_TOPIC", ""),
            ntfy_token=_env("NTFY_TOKEN", ""),
            ntfy_user=_env("NTFY_USER", ""),
            ntfy_password=_env("NTFY_PASSWORD", ""),
            matrix_homeserver=_env("MATRIX_HOMESERVER", ""),
            matrix_room=_env("MATRIX_ROOM", ""),
            matrix_token=_env("MATRIX_TOKEN", ""),
            apprise_url=_env("APPRISE_URL", ""),
            apprise_urls=_env("APPRISE_URLS", ""),
            apprise_tag=_env("APPRISE_TAG", ""),
            gotify_url=_env("GOTIFY_URL", ""),
            gotify_token=_env("GOTIFY_TOKEN", ""),
            monitor_enabled=_env("MONITOR", "true").lower() in ("true", "1", "yes"),
            monitor_events_enabled=_env("MONITOR_EVENTS", "true").lower() in ("true", "1", "yes"),
            smtp_tls_verify=_env("SMTP_TLS_VERIFY", "true").lower() in ("true", "1", "yes"),
            monitor_only_containers=[x.strip() for x in _env("MONITOR_ONLY_CONTAINERS", "").split(",") if x.strip()],
            insecure_registries=[x.strip() for x in _env("INSECURE_REGISTRIES", "").split(",") if x.strip()],
            registry_mirrors=[x.strip() for x in _env("REGISTRY_MIRRORS", "").split(",") if x.strip()],
            min_image_age_days=_env("MIN_IMAGE_AGE_DAYS", "0"),
            api_tokens=[x.strip() for x in _env("API_TOKENS", "").split(",") if x.strip()],
            monitor_interval_seconds=int(_env("MONITOR_INTERVAL", "60") or 60),
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
            telegram_polling=_env("TELEGRAM_POLLING", "true").lower() in ("true", "1", "yes"),
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
            smtp_host=_env("SMTP_HOST").strip(),
            smtp_port=int(_env("SMTP_PORT", "587") or "587"),
            smtp_user=_env("SMTP_USER").strip(),
            smtp_password=_env("SMTP_PASSWORD"),  # no strip — see DOCKER_PASSWORD
            smtp_from=_env("SMTP_FROM").strip(),
            smtp_to=_env("SMTP_TO").strip(),
            smtp_tls=_env("SMTP_TLS", "starttls").strip(),
        )
