#!/usr/bin/env python3
"""Test the "settings.json overrules an env var" detection (#53, @LeeNX).

Pure logic — no Docker, no Web UI. @LeeNX set `DEBUG=true` in his compose
file, saw `debug OFF` at startup and found `"debug": false` in his
settings.json. He never turned debug off: save_persistent() writes *all*
PERSISTENT_KEYS at once, so any unrelated Web UI save (language, bot
label, …) froze the then-current value, and from then on the env var was
dead without a word.

The precedence stays as it is — the saved value still wins. What's tested
here is that we now notice and say so:

  * a variable counts as set only when it is in os.environ AND differs
    from our own default — our Dockerfile `ENV`-declares 18 of these, so
    presence alone fires on every install (see the DOCKERFILE section),
  * the declared defaults match what from_env() actually falls back to,
  * the attribute → variable map is real, not `key.upper()` (MONITOR and
    MONITOR_INTERVAL are the two that break that rule),
  * and no secret value ever reaches the message.
"""
import sys, os, re, json, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from config import (Config, PERSISTENT_KEYS, PERSISTENT_ENV_VARS,
                    PERSISTENT_ENV_DEFAULTS, LOGGABLE_PERSISTENT_KEYS)

# Keys that legitimately have no env var of their own (Web-UI-only state).
NO_ENV_KEYS = {"web_setup_done", "ui_mode"}

DOCKERFILE = os.path.join(os.path.dirname(__file__), "..", "Dockerfile")


def dockerfile_env():
    """The `ENV` declarations from the real Dockerfile, persistent ones only.

    Read rather than hand-copied on purpose: this is exactly the
    environment every container starts with, and the bug being guarded
    against is what happens when that list grows.
    """
    out = {}
    with open(DOCKERFILE, encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^ENV\s+([A-Z0-9_]+)=(.*)$', line.strip())
            if not m:
                continue
            name, raw = m.group(1), m.group(2).strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
                raw = raw[1:-1]
            if name in set(PERSISTENT_ENV_VARS.values()):
                out[name] = raw
    return out


def build(env=None, saved=None):
    """Build a Config from a controlled environment + settings.json.

    Every variable we care about is cleared first so the host environment
    (a real LANGUAGE=de, a real DEBUG) can't decide the outcome.
    """
    data_dir = tempfile.mkdtemp(prefix="ds-envtest-")
    if saved is not None:
        with open(os.path.join(data_dir, "settings.json"), "w") as f:
            json.dump(saved, f)
    old = dict(os.environ)
    try:
        for var in PERSISTENT_ENV_VARS.values():
            os.environ.pop(var, None)
        os.environ["DATA_DIR"] = data_dir
        os.environ.update(env or {})
        return Config.from_env()
    finally:
        os.environ.clear()
        os.environ.update(old)


def keys_of(cfg):
    return sorted(o["key"] for o in cfg.env_overrides)


def main():
    checks = {}

    # ── the reported case: set, differs → reported ─────────────
    c = build({"DEBUG": "true"}, {"debug": False})
    checks["set + differs -> reported"] = keys_of(c) == ["debug"]
    checks["set + differs: saved value still wins"] = c.debug is False
    o = c.env_override("debug")
    checks["entry carries var name"] = o["var"] == "DEBUG"
    checks["entry carries both values"] = (o["env"], o["saved"]) == ("true", "false")
    checks["entry knows the Settings tab"] = o["tab"] == "General"
    checks["entry not marked secret"] = o["secret"] is False

    # ── set but agreeing → nothing to report ───────────────────
    c = build({"DEBUG": "true"}, {"debug": True})
    checks["set + same -> not reported"] = keys_of(c) == []

    # ── not set at all → nothing to report, even off-default ───
    # settings.json says true, the default is false, DEBUG is absent —
    # nobody is being overruled, so staying quiet is correct.
    c = build({}, {"debug": True})
    checks["unset + saved off-default -> not reported"] = keys_of(c) == []
    checks["unset + saved off-default: saved applies"] = c.debug is True

    # ── the accepted blind spot ───────────────────────────────
    # DEBUG=false set on purpose against a saved true is NOT reported: the
    # env value is our default, and at that point it is indistinguishable
    # from the Dockerfile's own ENV declarations. Documented at
    # _detect_env_overrides — asserted here so nobody "fixes" it back into
    # the eight-line wall it caused in v1.57.0.
    c = build({"DEBUG": "false"}, {"debug": True})
    checks["BLIND SPOT: env value == default is not reported"] = keys_of(c) == []
    checks["BLIND SPOT: saved value still wins regardless"] = c.debug is True

    # ── env var names that are NOT key.upper() ────────────────
    c = build({"MONITOR": "false"}, {"monitor_enabled": True})
    checks["MONITOR -> monitor_enabled"] = keys_of(c) == ["monitor_enabled"]
    c = build({"MONITOR_INTERVAL": "120"}, {"monitor_interval_seconds": 300})
    checks["MONITOR_INTERVAL -> monitor_interval_seconds"] = (
        keys_of(c) == ["monitor_interval_seconds"])
    # MONITOR_ENABLED is not a variable Docksentry reads — setting it must
    # not be mistaken for the real one.
    c = build({"MONITOR_ENABLED": "false"}, {"monitor_enabled": True})
    checks["MONITOR_ENABLED is not a real var -> ignored"] = keys_of(c) == []

    # ── the map itself ────────────────────────────────────────
    with open(os.path.join(os.path.dirname(__file__), "..", "app",
                           "config.py"), encoding="utf-8") as f:
        cfg_src = f.read()
    checks["every mapped var is really read by from_env()"] = all(
        f'_env("{v}"' in cfg_src for v in PERSISTENT_ENV_VARS.values())
    checks["every persistent key is mapped or knowingly env-less"] = (
        set(PERSISTENT_KEYS) - set(PERSISTENT_ENV_VARS) == NO_ENV_KEYS)
    checks["no map entry outside PERSISTENT_KEYS"] = (
        set(PERSISTENT_ENV_VARS) <= set(PERSISTENT_KEYS))

    # ── the declared defaults must be the real ones ───────────
    # PERSISTENT_ENV_DEFAULTS duplicates the fallbacks written inline in
    # from_env(). Build a Config with every variable stripped and with no
    # settings.json: whatever comes out IS the default, by definition.
    bare = build({}, None)
    wrong = {k: (getattr(bare, k), v) for k, v in PERSISTENT_ENV_DEFAULTS.items()
             if getattr(bare, k) != v}
    if wrong:
        print("     declared default != from_env() fallback:", wrong)
    checks["declared defaults match from_env() fallbacks"] = not wrong
    checks["every mapped key has a declared default"] = (
        set(PERSISTENT_ENV_DEFAULTS) == set(PERSISTENT_ENV_VARS))

    # ── the v1.57.0 regression: our own Dockerfile ────────────
    # `ENV CLEANUP_GRACE_HOURS="24"` and 17 siblings put these variables
    # into os.environ in every container. A presence-only check reported
    # eight overrides on a stock install; six of them were the Dockerfile
    # talking to itself. The saved values below are the ones from that
    # report.
    df = dockerfile_env()
    checks["Dockerfile declares persistent vars at all"] = len(df) >= 15
    checks["Dockerfile does not declare DEBUG"] = "DEBUG" not in df
    checks["Dockerfile ENV values all equal our defaults"] = all(
        getattr(build({n: v}, None), k) == PERSISTENT_ENV_DEFAULTS[k]
        for k, n in PERSISTENT_ENV_VARS.items() if n in df
        for v in [df[n]])

    real_saved = {
        "cleanup_grace_hours": 72,
        "disk_warn_percent": 75,
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "07:00",
        "weekly_report_enabled": True,
        "weekly_report_weekday": 4,
        "weekly_report_hour": 18,
        "language": "de",
        "debug": False,
    }
    c = build(df, real_saved)
    if c.env_overrides:
        print("     unexpected noise:", c.env_override_lines())
    checks["Dockerfile env + saved settings -> no lines at all"] = (
        c.env_override_lines() == [])

    # One genuinely user-set variable on top of the same environment: the
    # Discord webhook the user really did put in his compose file.
    c = build(dict(df, DISCORD_WEBHOOK="https://discord.com/api/webhooks/REAL"),
              dict(real_saved, discord_webhook="https://discord.com/api/webhooks/OLD"))
    checks["Dockerfile env + one real setting -> exactly one line"] = (
        len(c.env_override_lines()) == 1 and keys_of(c) == ["discord_webhook"])

    # And LeeNX's own case, in a real container environment: DEBUG isn't
    # declared in the Dockerfile, so his DEBUG=true survives the filter.
    c = build(dict(df, DEBUG="true"), real_saved)
    checks["Dockerfile env + LeeNX's DEBUG=true -> exactly that one line"] = (
        len(c.env_override_lines()) == 1 and keys_of(c) == ["debug"])

    # ── secrets: reported, but never with a value ─────────────
    SECRETS = {
        "web_password": ("WEB_PASSWORD", "env-hunter2", "saved-hunter2"),
        "discord_webhook": ("DISCORD_WEBHOOK",
                            "https://discord.com/api/webhooks/env-TOKEN",
                            "https://discord.com/api/webhooks/saved-TOKEN"),
        "webhook_url": ("WEBHOOK_URL", "https://ntfy.example/env-TOKEN",
                        "https://ntfy.example/saved-TOKEN"),
    }
    for key, (var, env_val, saved_val) in SECRETS.items():
        c = build({var: env_val}, {key: saved_val})
        entry = c.env_override(key)
        lines = c.env_override_lines()
        blob = " ".join(lines) + " " + json.dumps(entry)
        checks[f"secret {key}: reported"] = keys_of(c) == [key]
        checks[f"secret {key}: marked secret"] = entry["secret"] is True
        checks[f"secret {key}: no values in entry"] = (
            entry["env"] is None and entry["saved"] is None)
        checks[f"secret {key}: env value absent from message"] = env_val not in blob
        checks[f"secret {key}: saved value absent from message"] = saved_val not in blob
        checks[f"secret {key}: variable name still named"] = var in blob
        checks[f"secret {key}: saved value still wins"] = getattr(c, key) == saved_val

    # Telegram IDs are personal data — main.py already prints only the
    # *count* of allowed users, so the same rule applies here.
    c = build({"TELEGRAM_TOPIC_ID": "424242"}, {"telegram_topic_id": "99"})
    checks["telegram_topic_id treated as secret"] = (
        c.env_override("telegram_topic_id")["secret"] is True
        and "424242" not in " ".join(c.env_override_lines()))

    # Allow-list, not deny-list: a key nobody has classified yet must come
    # out secret. This is the rule that protects the *next* added key.
    checks["unknown key defaults to secret"] = (
        Config._display_value("some_future_key", "s3cret") is None)
    checks["allow-list stays inside PERSISTENT_KEYS"] = (
        LOGGABLE_PERSISTENT_KEYS <= set(PERSISTENT_KEYS))
    checks["no secret key on the allow-list"] = not (
        {"web_password", "discord_webhook", "webhook_url",
         "telegram_topic_id", "telegram_allowed_users"}
        & LOGGABLE_PERSISTENT_KEYS)

    # ── the startup line ──────────────────────────────────────
    c = build({"DEBUG": "true"}, {"debug": False})
    line = c.env_override_lines()[0]
    checks["line: one line per override"] = len(c.env_override_lines()) == 1
    checks["line: names the variable and its env value"] = "DEBUG=true" in line
    checks["line: names the saved value"] = "debug=false" in line
    checks["line: says where to change it"] = "Settings › General" in line
    checks["line: names settings.json"] = c.settings_file in line

    # A key with no Web UI field of its own must not send the user to a
    # tab that doesn't have it.
    c = build({"DOCKER_STOP_TIMEOUT": "90"}, {"docker_stop_timeout": 60})
    line = c.env_override_lines()[0]
    checks["line: field-less key points at settings.json only"] = (
        "Settings ›" not in line and "docker_stop_timeout" in line)

    # ── several at once, and non-bool types ───────────────────
    # LANGUAGE=fr, not "en" — "en" is the default and would be filtered.
    c = build({"LANGUAGE": "fr", "CRON_SCHEDULE": "0 4 * * *",
               "EXCLUDE_CONTAINERS": "a,b"},
              {"language": "de", "cron_schedule": "0 18 * * *",
               "exclude_containers": ["a", "b"]})
    checks["multiple overrides reported together"] = (
        keys_of(c) == ["cron_schedule", "language"])
    checks["list value that matches is not reported"] = (
        c.env_override("exclude_containers") is None)
    checks["string values rendered as-is"] = (
        c.env_override("language")["env"] == "fr"
        and c.env_override("language")["saved"] == "de")
    # Saved value that happens to equal the default is still an override
    # if the env says something else — the filter is on the ENV side only.
    c = build({"LANGUAGE": "fr"}, {"language": "en"})
    checks["saved value == default is still an override"] = keys_of(c) == ["language"]

    # ── no settings.json at all → nothing overruled ───────────
    c = build({"DEBUG": "true"}, None)
    checks["no settings.json -> nothing reported"] = c.env_overrides == []
    checks["no settings.json -> env applies"] = c.debug is True

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
