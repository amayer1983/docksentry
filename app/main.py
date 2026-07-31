#!/usr/bin/env python3
"""Docksentry - Main entry point."""

import os
import signal
import socket
import subprocess
import time
import sys

# Force IPv4-only by default — many containers lack IPv6 routing,
# causing "[Errno 101] Network unreachable" when Python prefers IPv6.
# Set DOCKSENTRY_IPV6=true to enable IPv6 (only if your network supports it).
if os.environ.get("DOCKSENTRY_IPV6", "false").lower() not in ("true", "1", "yes"):
    _orig_getaddrinfo = socket.getaddrinfo
    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = _ipv4_only_getaddrinfo

from config import Config
from container_store import ContainerStore
from telegram_bot import TelegramBot
from update_engine import UpdateEngine
from update_checker import UpdateChecker
from scheduler import Scheduler
from notifier import Notifier


def _setup_docker_auth(config):
    """Configure docker-CLI authentication if the user supplied
    credentials. Three modes, checked in priority order:

      1. DOCKER_AUTH_CONFIG path → point DOCKER_CONFIG at its parent
         dir. Docker CLI then reads the file automatically.
      2. DOCKER_USERNAME + DOCKER_PASSWORD → run `docker login` once.
         Falls back to anonymous on failure (with a printed warning)
         rather than blocking startup.
      3. Neither → anonymous pulls (existing behaviour).

    Bypasses Docker Hub's anonymous rate limit (100 / 6h / IP) for
    setups with many containers or NATted multi-host installs. See #18.
    """
    if config.docker_auth_config:
        path = config.docker_auth_config
        if os.path.isfile(path):
            os.environ["DOCKER_CONFIG"] = os.path.dirname(path) or "."
            print(f"Docker auth: using DOCKER_AUTH_CONFIG={path}")
        else:
            print(f"Docker auth: DOCKER_AUTH_CONFIG={path} not found, skipping")
        return

    if config.docker_username and config.docker_password:
        registry = config.docker_registry or "docker.io"
        try:
            r = subprocess.run(
                ["docker", "login", "-u", config.docker_username,
                 "--password-stdin", registry],
                input=config.docker_password.encode(),
                capture_output=True,
                timeout=30,
            )
            if r.returncode == 0:
                # Don't print the password — it's somewhere in stderr otherwise
                print(f"Docker auth: logged in to {registry} as {config.docker_username}")
            else:
                err = (r.stderr or b"").decode(errors="replace").strip()[:200]
                print(f"Docker auth: docker login failed ({registry}): {err}")
                print("Docker auth: continuing with anonymous pulls")
        except (subprocess.SubprocessError, OSError) as e:
            print(f"Docker auth: docker login error: {e}")
            print("Docker auth: continuing with anonymous pulls")


def main():
    config = Config.from_env()
    # Wire up Docker registry auth before any subprocess that calls
    # `docker pull` — registry / scheduler / etc. all rely on it.
    try:
        _setup_docker_auth(config)
    except Exception as e:
        print(f"Docker auth setup failed (non-fatal): {e}")

    # Telegram is now optional. At least one notification/control channel
    # must be configured, otherwise Docksentry has no way to talk to the
    # operator (and no way to be talked to).
    telegram_partial = bool(config.bot_token) ^ bool(config.chat_id)
    if telegram_partial:
        print("ERROR: BOT_TOKEN and CHAT_ID must be set together.")
        sys.exit(1)
    telegram_on = bool(config.bot_token and config.chat_id)
    has_any_channel = (
        telegram_on
        or config.web_ui
        or config.discord_webhook
        or config.webhook_url
        or bool(config.smtp_host and config.smtp_from and config.smtp_to)
    )
    if not has_any_channel:
        print("ERROR: configure at least one of: BOT_TOKEN+CHAT_ID, WEB_UI=true,")
        print("       DISCORD_WEBHOOK, WEBHOOK_URL — otherwise Docksentry has")
        print("       no way to notify or be controlled.")
        sys.exit(1)

    store = ContainerStore(config)
    # Update orchestration lives on a neutral engine (v2 groundwork); build
    # it once and hand it to the bot so both share the one update mutex.
    engine = UpdateEngine(config, store)
    bot = TelegramBot(config, store, engine)
    notifier = Notifier(config)
    bot.notifier = notifier
    checker = UpdateChecker(config)
    scheduler = Scheduler(config, checker, bot)
    # Container CLI seam (v2 groundwork): build one backend and hand it to
    # the consumers that were migrated to it. The monitor is constructed
    # lazily inside the scheduler and defaults to its own backend there.
    from container_backend import get_backend
    backend = get_backend(config)
    web = None

    # Graceful shutdown
    def shutdown(sig, frame):
        # Record the exit cause *first* — before stopping services, so the
        # marker survives even if Docker escalates to SIGKILL after the stop
        # timeout. The next boot reads this to tell the operator it was an
        # external stop signal (host reboot, `docker restart`, daemon
        # restart) rather than Docksentry restarting itself (#2, @famewolf).
        try:
            from container_store import atomic_write_json
            try:
                signame = signal.Signals(sig).name
            except (ValueError, AttributeError):
                signame = str(sig)
            atomic_write_json(config.last_exit_file,
                              {"reason": "signal", "signal": signame,
                               "ts": time.time()})
        except Exception as e:
            print(f"Could not record exit cause (non-fatal): {e}")
        print("Shutting down...")
        scheduler.stop()
        bot.stop()
        if web:
            web.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Detect whether we're a fresh start or a post-selfupdate restart.
    # The scheduler consumes the deferred-check marker inside start(),
    # so we have to check (and remember) BEFORE that. The scheduler's
    # deferred-resume sends a more informative "Restarted on vX —
    # checking your containers..." message, so we skip the generic
    # "Docksentry started" notification to avoid two near-identical
    # restart messages back-to-back. Reported by the user from a
    # real-world v1.17.0 deployment.
    post_selfupdate_restart = os.path.exists(config.deferred_check_file)

    # Why did we (re)start? The SIGTERM/SIGINT handler leaves a marker when
    # an external signal stopped the previous process; consume it here so the
    # startup notification can say "this was an external stop, not a
    # self-restart" (#2 — @famewolf's hosts reboot at midnight and the
    # generic "started" banner made it look self-inflicted). Absent marker =
    # first boot or an unclean kill (SIGKILL/OOM/power loss) — we don't claim
    # a cause we can't prove, so no suffix in that case.
    restart_signal = None
    try:
        if os.path.exists(config.last_exit_file):
            import json as _json
            with open(config.last_exit_file) as f:
                _exit = _json.load(f)
            if _exit.get("reason") == "signal":
                restart_signal = _exit.get("signal", "signal")
            os.unlink(config.last_exit_file)
    except Exception as e:
        print(f"Could not read exit cause (non-fatal): {e}")

    # A self-update restart ALSO arrives as SIGTERM (the recreate stops our
    # old container), but it is not an external stop. When our own
    # self-update marker is present, suppress the "external stop signal"
    # line — the version bump in the banner already tells the story (#2,
    # @famewolf). Stale markers (>1h) are ignored so a long-ago abandoned
    # self-update can't mask a later genuine external restart.
    selfupdate_restart = False
    try:
        if os.path.exists(config.selfupdate_marker_file):
            import json as _json, time as _time
            with open(config.selfupdate_marker_file) as f:
                _mark = _json.load(f)
            if _time.time() - float(_mark.get("ts", 0) or 0) < 3600:
                selfupdate_restart = True
            os.unlink(config.selfupdate_marker_file)
    except Exception as e:
        print(f"Could not read selfupdate marker (non-fatal): {e}")
    # A self-update is not an external signal, regardless of how we got here.
    if selfupdate_restart:
        restart_signal = None

    # Post-selfupdate fixup: when _save_selfupdate_history wrote the
    # entry before the swap, the new version wasn't known yet — it's
    # stored as `v{old} → ?`. We're the freshly-booted process and
    # know our own VERSION now, so patch the placeholder. Only touches
    # the last entry, only when it ends with "→ ?)" — leaves anything
    # already complete (manual edits, old entries, regular container
    # updates) untouched. Reported by @famewolf in #22.
    #
    # v1.22.2: decoupled from `post_selfupdate_restart`. The deferred-
    # check marker only exists for the AUTO-selfupdate path (cron +
    # AUTO_SELFUPDATE=true). Manual `/selfupdate` doesn't write it,
    # so manual-update placeholders were never patched. Reported by
    # @famewolf in #2. The `endswith("→ ?)")` guard makes the fixup
    # safe regardless of why we restarted — only the real placeholder
    # matches the pattern, so all other history paths are no-ops.
    try:
        import json as _json
        from version import VERSION as _NEW_VERSION
        if os.path.exists(config.history_file):
            with open(config.history_file) as f:
                _hist = _json.load(f)
            if _hist:
                last = _hist[-1]
                detail = last.get("detail", "")
                if detail.endswith("→ ?)"):
                    last["detail"] = detail[:-len("?)")] + f"v{_NEW_VERSION})"
                    # Atomic write (v1.22.1) — see
                    # container_store.atomic_write_json.
                    from container_store import atomic_write_json
                    atomic_write_json(config.history_file, _hist, indent=2)
                    print(f"History: patched selfupdate entry with new version v{_NEW_VERSION}")
    except Exception as e:
        print(f"History fixup failed (non-fatal): {e}")

    # One-shot migration: if a previous Docksentry version saved its own
    # container into the auto-update list (which routes through the
    # regular `docker stop` flow and kills PID 1 — #16), strip it and
    # notify so the user knows what happened and where to look. Use
    # cgroup-based detection so HOSTNAME-override compose setups don't
    # slip through.
    self_in_autoupdate = False
    try:
        own_name = checker._own_container_name()
        if own_name:
            auto_list = store.get_autoupdate()
            if own_name in auto_list:
                auto_list = [n for n in auto_list if n != own_name]
                store.save_autoupdate(auto_list)
                self_in_autoupdate = True
                print(f"Migration: removed {own_name!r} from auto-update list (use /selfupdate instead — see #16)")
    except Exception as e:
        print(f"Self-autoupdate migration check failed (non-fatal): {e}")

    # Start scheduler in background
    scheduler.start()

    # Start Web UI if enabled
    if config.web_ui:
        from web_ui import WebUI
        web = WebUI(config, checker, bot, store, config.web_port,
                    config.web_password, backend=backend)
        web.start()

    # Version + debug state up front — the container log is often the only
    # thing a headless user can paste, and "which version even is this?"
    # was the first question every time (#43, @LeeNX).
    from version import VERSION as _V
    print(f"Docksentry started. (v{_V}, debug {'ON' if config.debug else 'OFF'})")
    print(f"Schedule: {config.cron_schedule}")
    print(f"Excluded: {config.exclude_containers or 'none'}")
    print(f"Auto selfupdate: {'ON' if config.auto_selfupdate else 'OFF'}")
    print(f"Language: {config.language}")
    print(f"Telegram: {'ON' if telegram_on else 'OFF'}")
    if telegram_on and config.telegram_allowed_users:
        # Print count, not the IDs themselves — those are personal data.
        print(f"Telegram allowed-users whitelist: {len(config.telegram_allowed_users)} user(s)")
    if config.bot_label:
        print(f"Bot label: {config.bot_label}")
    if config.web_ui:
        print(f"Web UI: http://0.0.0.0:{config.web_port}")
    if config.discord_webhook:
        print("Discord: webhook configured")
    if config.webhook_url:
        # Don't log the full URL — it can contain auth tokens (Ntfy, Gotify,
        # Home Assistant) that would otherwise leak via `docker logs` or log
        # aggregators.
        print("Webhook: configured")

    # Say out loud which env vars settings.json is overruling (#53, @LeeNX).
    # He set DEBUG=true, read "debug OFF" two lines up, and had no way to
    # find out why. The saved value still wins — but silently winning was
    # the actual bug. Values for secrets are never in these lines; see
    # Config.env_override_lines / LOGGABLE_PERSISTENT_KEYS.
    for _line in config.env_override_lines():
        print(_line)

    # Send startup notification to all channels — unless we're resuming
    # from a self-update, in which case the scheduler's deferred-check
    # resume will send a more specific message (with version + "checking
    # your containers now").
    from version import VERSION
    from i18n import get_translator
    t = get_translator(config.language)

    # Data-loss alert (v1.22.0): if BOT_TOKEN is configured via env but
    # settings.json is missing or empty, surface this as a Telegram
    # message so the user knows immediately instead of silently
    # discovering it later via the Web UI's setup wizard. Reported by
    # @famewolf in #2 — three of his hosts simultaneously rebooted, all
    # three came back with the wizard up. We can detect this case
    # reliably: a fresh install with no env config wouldn't reach here
    # at all (main.py would refuse to start), so the combination
    # "Telegram configured AND settings.json missing" means real loss.
    settings_missing = (
        bot.enabled
        and not os.path.exists(config.settings_file)
        and not post_selfupdate_restart
    )

    if restart_signal:
        print(f"Restart cause: external stop signal ({restart_signal}) — not a self-restart")

    # Surface a failed self-update recreate (#43). The helper writes its
    # stdout/stderr to /data/selfupdate_helper.log; if the recreate rolled
    # back (podman rejected `docker run`, etc.) we finally have the reason
    # here instead of it vanishing with the --rm helper. On success the log
    # exists too but has no "rolling back" marker — consume it silently.
    try:
        _hlog = config.selfupdate_helper_log
        if os.path.exists(_hlog):
            with open(_hlog) as f:
                _hcontent = f.read()
            os.unlink(_hlog)
            if "rolling back" in _hcontent:
                _tail = _hcontent.strip()[-900:]
                _fail = t("selfupdate_recreate_failed", detail=_tail)
                if bot.enabled:
                    bot.send_message(_fail)
                if notifier.has_channels():
                    notifier.send_message(_fail)
                print("Self-update recreate failed — helper output:\n" + _hcontent)
    except Exception as e:
        print(f"Could not read selfupdate helper log (non-fatal): {e}")

    if not post_selfupdate_restart:
        startup_msg = t("startup_message", version=VERSION)
        if restart_signal:
            startup_msg += t("startup_reason_signal", signal=restart_signal)
        if bot.enabled:
            bot.send_message(startup_msg)
            if settings_missing:
                bot.send_message(t("data_loss_alert"))

    # One-shot migration notice if we just stripped self from auto-update.
    if self_in_autoupdate:
        notice = t("migration_self_autoupdate_removed")
        if bot.enabled:
            bot.send_message(notice)
        if notifier.has_channels():
            notifier.send_message(notice)
        if notifier.has_channels():
            notifier.send_message(startup_msg)

    # Start bot listener (blocking).
    # When Telegram is off this just blocks until shutdown — scheduler/Web UI
    # run in their own threads.
    bot.listen(checker, scheduler)


if __name__ == "__main__":
    main()
