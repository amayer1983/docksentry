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
from broadcast import Broadcast


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
    # The startup guard has to see the switch too. Otherwise
    # CHANNEL_TELEGRAM_ENABLED=false alongside WEB_UI=false passes it —
    # the token is set, so it counts — and boots an instance with no way
    # to report anything and no interface to turn the switch back on.
    # Recoverable only by editing compose, which is exactly the state
    # this check exists to refuse.
    telegram_usable = telegram_on and getattr(
        config, "channel_telegram_enabled", True)
    has_any_channel = (
        telegram_usable
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
    # Every managed host (#7): the machine we run on, plus anything in
    # DOCKER_HOSTS. With none configured this is a one-item registry whose
    # single entry is the local host, so nothing about a single-host
    # install changes. Built before the engine and the bot so BOTH can be
    # handed the registry — that's what makes `/check @nas` resolvable and
    # what keeps `nas`'s pins, cooldowns and groups out of the local host's
    # state (and vice versa).
    from hosts import build_hosts
    host_registry = build_hosts(config, store)
    # Update orchestration lives on a neutral engine (v2 groundwork); build
    # it once and hand it to the bot so both share the one update mutex.
    engine = UpdateEngine(config, store, hosts=host_registry)
    bot = TelegramBot(config, store, engine, hosts=host_registry)
    # One audit log shared by every front end (v2.1). Web UI attaches
    # its own to the HTTP server; the bots carry it directly.
    from audit import AuditLog
    audit_log = AuditLog(config)
    bot.audit = audit_log
    notifier = Notifier(config)
    bot.notifier = notifier
    # One all-channel seam, built here and handed to both front ends (#63).
    # It used to live on the Telegram bot, so Discord had to reach into that
    # instance to say anything outside its own channel — an all-channel seam
    # that only one front end could reach is how "unattended message goes to
    # Telegram alone" happened three times (#57, #61).
    broadcast = Broadcast(telegram=bot, notifier=notifier)
    bot.broadcast = broadcast
    checker = host_registry.local.checker
    if host_registry.is_multi:
        print(f"Managing {len(host_registry)} hosts: "
              f"{', '.join(host_registry.names)}")
    scheduler = Scheduler(config, checker, bot, hosts=host_registry)
    # Container CLI seam: the Web UI's views act on the machine Docksentry
    # runs on, so they share the local host's backend rather than building a
    # second one. The monitor is constructed lazily inside the scheduler and
    # defaults to its own backend there.
    backend = host_registry.local.backend
    web = None

    # Graceful shutdown
    # Filled in once the Discord bot exists; the signal handler below is
    # defined before it and reads through this rather than closing over a
    # name that doesn't exist yet.
    _discord_ref = {}

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
                               "ts": time.time(), "done": False})
        except Exception as e:
            print(f"Could not record exit cause (non-fatal): {e}")
        print("Shutting down...")
        scheduler.stop()
        bot.stop()
        # Resolved at call time, so it's fine that the Discord bot is
        # built further down — by the time a signal arrives it exists (or
        # is still None, which this tolerates).
        #
        # This one BLOCKS, briefly and on purpose: its `stop()` waits for
        # any command still running (bounded by DiscordBot.SHUTDOWN_GRACE)
        # before returning. A SIGTERM landing in the middle of a
        # Discord-triggered /update used to kill the process between
        # `docker stop` and `docker run`, leaving the container down.
        if _discord_ref.get("bot"):
            _discord_ref["bot"].stop()
        if web:
            web.stop()
        # Rewritten now that everything has actually stopped. The marker
        # above proves a signal *arrived*; this proves we got to the end
        # of shutting down, and the difference is the whole point: a
        # `docker stop` that runs out of patience sends SIGKILL, and
        # without this the next boot could not tell that from a clean
        # exit. @NotRetarded's instance died with 137 mid-self-update and
        # Docksentry reported the update as a success and said nothing
        # about it (#62).
        try:
            from container_store import atomic_write_json as _aw
            _aw(config.last_exit_file,
                {"reason": "signal", "signal": signame,
                 "ts": time.time(), "done": True})
        except Exception:
            pass
        print("Shutdown complete.")

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
    # Asked BEFORE the exit marker is consumed below — the whole point is
    # that the marker is ABSENT after a hard kill (#2, @NotRetarded, whose
    # Docksentry exited 137 and said nothing about it).
    from recovery import previous_run_died, recover_interrupted_update
    hard_kill = previous_run_died(config)

    restart_signal = None
    killed_stopping = False
    try:
        if os.path.exists(config.last_exit_file):
            import json as _json
            with open(config.last_exit_file) as f:
                _exit = _json.load(f)
            if _exit.get("reason") == "signal":
                restart_signal = _exit.get("signal", "signal")
                # A stop that never finished. `done` is written after
                # every service has come to a halt, so its absence means
                # the process was killed partway through — almost always
                # a `docker stop` timeout expiring into SIGKILL (#62).
                killed_stopping = not _exit.get("done", True)
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

    # Neither is a restart somebody asked for. The stop still arrives as
    # SIGTERM — it is our own SIGTERM — so without this marker the banner
    # reports an external stop signal and adds "Docksentry did not
    # restart itself", which is exactly backwards.
    requested_restart = False
    try:
        if os.path.exists(config.restart_request_file):
            import json as _json, time as _time
            with open(config.restart_request_file) as f:
                _req = _json.load(f)
            # Stale markers are ignored, same rule as the self-update one:
            # an abandoned request must not mask a genuine external stop
            # an hour later.
            if _time.time() - float(_req.get("ts", 0) or 0) < 3600:
                requested_restart = True
                restart_signal = None
            os.unlink(config.restart_request_file)
    except Exception as e:
        print(f"Could not read the restart request (non-fatal): {e}")

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

    # Labels on OUR OWN container that describe how another instance would
    # treat us — and that therefore do nothing here. Say so out loud.
    #
    # @LeeNX in #51: "I am not a fan of things that get ignored, it's a
    # pattern that you don't know you might be doing something wrong and
    # things look like they're just breaking." He is right, and the silent
    # ignore was mine: the #51 fix made `docksentry.auto` on our own
    # container inert without telling anyone. Inert-and-silent is the same
    # class of defect the issue was about, one layer down.
    try:
        own_name = checker._own_container_name()
        if own_name:
            own_labels = checker.get_container_labels(own_name) or {}
            for key, instead in (
                    ("auto", "AUTO_SELFUPDATE (or Settings › Updates)"),
                    ("enable", "AUTO_SELFUPDATE (or Settings › Updates)"),
                    ("exclude", "AUTO_SELFUPDATE (or Settings › Updates)"),
            ):
                if f"docksentry.{key}" in own_labels:
                    print(f"NOTE: docksentry.{key} is set on Docksentry's own "
                          f"container and has NO effect there — that label "
                          f"tells another instance how to treat this "
                          f"container, and no other instance is watching us. "
                          f"Self-update is governed by {instead}.")
    except Exception as e:
        print(f"Own-label check failed (non-fatal): {e}")

    # UPDATE_POLICY only bites when a version can actually be read, and on
    # most real hosts it cannot. The policy compares an old and a new
    # version string; those come from the OCI `image.version` label, or
    # failing that from a full `x.y.z` image tag. Almost nobody runs
    # `:1.2.3` — they run `:latest`, `:17`, `:31-apache`, `:main` — and when
    # nothing parses, the bump level is unknown and the update is allowed
    # through. That fail-open is deliberate, but the operator set
    # "patch only" and was never told it does not apply, which is the part
    # that is wrong: a `postgres:16` → `17` jump sails past a setting whose
    # entire purpose was to stop it. Measured on this developer's host: 0 of
    # 14 distinct images carried a parseable version.
    try:
        policy = (getattr(config, "update_policy", "all") or "all").lower()
        if policy != "all":
            covered = uncovered = 0
            for c in checker.get_running_containers():
                ver = ""
                try:
                    ver = checker.image_version_label(c.get("image", "")) or ""
                except Exception:
                    ver = ""
                if not (ver and checker._parse_semver(ver)):
                    _, _, tag = checker._parse_image(c.get("image", ""))
                    ver = tag if (tag and checker._parse_semver(tag)) else ""
                if ver:
                    covered += 1
                else:
                    uncovered += 1
            if uncovered:
                print(f"NOTE: UPDATE_POLICY={policy} can only hold back "
                      f"{covered} of {covered + uncovered} containers. The "
                      f"other {uncovered} carry no readable version (tags "
                      f"like :latest, :17 or :main), so their bump size "
                      f"cannot be judged and they update as if the policy "
                      f"were 'all'. Use docksentry.pin, "
                      f"docksentry.auto=false or MONITOR_ONLY_CONTAINERS to "
                      f"hold those back.")
    except Exception as e:
        print(f"Update-policy coverage check failed (non-fatal): {e}")

    # Start scheduler in background
    scheduler.start()

    # Interactive Discord bot (v2.0) — a second front-end onto the same
    # update engine. Only starts when DISCORD_BOT_TOKEN and
    # DISCORD_APP_ID are set; a failure here is never fatal, because a
    # Discord problem must not take down update checking.
    discord_bot = None
    try:
        from discord_bot import DiscordBot
        # `telegram=bot` is only used to hand a queued self-update on
        # after a Discord-triggered update releases the shared lock —
        # the same handoff the Web UI does. No Discord command reaches
        # into the Telegram bot for anything else.
        discord_bot = DiscordBot(config, store, engine, hosts=host_registry,
                                 checker=checker, telegram=bot,
                                 broadcast=broadcast)
        discord_bot.audit = audit_log
        if discord_bot.enabled:
            if discord_bot.start():
                _discord_ref["bot"] = discord_bot
            else:
                discord_bot = None
    except Exception as e:
        print(f"Discord bot failed to start (non-fatal): {e}")
        discord_bot = None

    def restart_discord():
        """Stop the running Discord bot and start one from the current
        config. Returns `(running, code, detail)` for the Web UI.

        The construction lives here rather than in `web_ui`, because this
        is the only place that holds everything a DiscordBot needs — the
        store, the engine, the host registry, the checker and the Telegram
        bot it hands a queued self-update to. Handing the Web UI a
        callback keeps it from having to learn any of that; it knows the
        credentials changed and nothing else.

        Never raises. A Discord problem must not be able to take down a
        settings save — the same rule that makes the start-up block above
        non-fatal, and there it matters even more: the settings are
        already written to disk by the time this runs.
        """
        old = _discord_ref.pop("bot", None)
        if old:
            try:
                old.stop()
            except Exception as e:
                print(f"Discord bot stop failed (non-fatal): {e}")
        try:
            fresh = DiscordBot(config, store, engine, hosts=host_registry,
                               checker=checker, telegram=bot,
                               broadcast=broadcast)
            fresh.audit = audit_log
            if not fresh.enabled:
                # No token, or a token with no application id. This is
                # also how the bot is switched OFF from the interface:
                # clear the token, save, and nothing is left running.
                return False, "disabled", ""
            if fresh.start():
                _discord_ref["bot"] = fresh
                warn = fresh.last_warning or ("", "")
                return True, warn[0], warn[1]
            code, detail = fresh.last_error or ("error", "")
            return False, code, detail
        except Exception as e:
            print(f"Discord bot restart failed (non-fatal): {e}")
            return False, "error", str(e)[:200]

    # Start Web UI if enabled
    if config.web_ui:
        from web_ui import WebUI
        web = WebUI(config, checker, bot, store, config.web_port,
                    config.web_password, backend=backend,
                    hosts=host_registry,
                    restart_discord=restart_discord)
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
    # "ON" meant "BOT_TOKEN and CHAT_ID are set", which stopped being the
    # same thing as "Telegram will do anything" once it gained a switch.
    # A boot line that says ON while the next line says it is switched
    # off is worse than no line.
    _tg_switch = getattr(config, "channel_telegram_enabled", True)
    print(f"Telegram: {'ON' if telegram_on and _tg_switch else 'OFF'}"
          + ("" if _tg_switch else " (switched off on the Connections page)"))
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

    # Where does /data actually live? (#2, @famewolf)
    #
    # He lost his settings on every recreate because his compose file
    # bind-mounted to `/app/data`, a path nothing in this image reads,
    # which left the real `/data` on the anonymous volume our Dockerfile's
    # `VOLUME ["/data"]` creates — new one per container, gone with the
    # old one. From inside, both mistakes are plainly visible in our own
    # mounts, so look before the loss instead of describing it after.
    storage_findings = []
    try:
        import storage_check
        storage_findings = storage_check.check(
            backend, checker._own_container_name(), config.data_dir)
        for _line in storage_check.describe(storage_findings):
            print(_line)
    except Exception as e:
        print(f"Storage check failed (non-fatal): {e}")

    # Data-loss alert (v1.22.0): BOT_TOKEN configured via env but no
    # settings.json. Originally reported by @famewolf in #2 when three of
    # his hosts rebooted at once and all three came back with the setup
    # wizard up.
    #
    # The old comment here claimed this combination "means real loss".
    # Measured on a fresh env-only install, three boots: /data ends up
    # holding version_state.json and nothing else, because
    # `save_persistent()` only ever runs from a user action. So somebody
    # who configures everything through the environment and never saves
    # anything in the Web UI has no settings.json, has lost nothing, and
    # was being told about "possible data loss" on every single restart.
    # The alert cried wolf at exactly the people with nothing to lose.
    #
    # So the question is no longer "is settings.json here" but "was it
    # ever here" — which we now record rather than infer, with a marker
    # written alongside every save. A missing settings.json with the
    # marker present is real loss and worth shouting about. Without the
    # marker it is a fresh volume or an env-only install, and shouting at
    # those people is what the alert had been doing all along.
    settings_missing = (
        bot.enabled
        and not os.path.exists(config.settings_file)
        and not post_selfupdate_restart
    )
    settings_ever_saved = config.settings_ever_saved()

    # A copy next to the data, and a way back from it (#2, @famewolf).
    #
    #   "I would REALLY REALLY like it if backups stored a local copy so
    #    restores are not dependent on another machine to get going
    #    again […] and an option to load most recent when no config is
    #    found."
    #
    # The obvious objection — a backup inside the volume it backs up
    # protects against nothing — is answered by what keeps actually
    # happening: settings.json going while the rest of the directory
    # survives, or a restore needed from a browser on the very machine
    # that is broken. It is not a substitute for the copy you keep
    # elsewhere, and does not pretend to be.
    restored_from = ""
    try:
        import backup as _backup
        if settings_missing and settings_ever_saved:
            newest = _backup.newest_local(config)
            if newest:
                restored_from = config.restore_settings_from(newest)
                if restored_from:
                    print(f"Restored settings from {newest} — settings.json "
                          f"was missing and this directory has held one "
                          f"before.")
                    settings_missing = False
        # One copy per boot, and only when the newest is stale, so a
        # restart loop cannot churn through the retention window and
        # leave five copies of the same minute.
        #
        # Skipped only on a boot where settings are demonstrably lost and
        # we could not put them back: archiving the damage would evict the
        # good copies, and five wipes in a row would leave five backups of
        # nothing. A *fresh* install still gets one — groups, pins and
        # links live in their own files, so "no settings.json yet" does
        # not mean "nothing worth keeping" (found while testing this: an
        # instance driven entirely from compose, with groups configured,
        # was getting no automatic backup at all).
        if not (settings_missing and settings_ever_saved):
            _backup.write_local_if_stale(config, store, VERSION,
                                         min_gap_seconds=0)
    except Exception as e:
        print(f"Local backup step failed (non-fatal): {e}")
    # …but a storage finding beats the inference either way: if the data
    # directory demonstrably cannot survive a recreate, say so even when
    # this particular boot happens to look tidy.
    storage_key = ""
    try:
        storage_key = storage_check.summary_key(storage_findings)
    except Exception:
        pass

    if killed_stopping:
        # Into the log as well, not only the notification channels. An
        # instance with no channels configured — or one whose channels
        # are the thing that is broken — would otherwise have no record
        # of it at all, which is how #62 stayed invisible in the first
        # place.
        print("Restart cause: the previous run was KILLED before it finished "
              "shutting down (exit 137). `docker stop` timed out; raise "
              "DOCKER_STOP_TIMEOUT if this repeats.")
    if requested_restart:
        print("Restart cause: requested (restart button or /restart)")
    elif restart_signal:
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

    # ── what changed in the version that just booted ───────────
    # The self-update path already announces itself, but that is the
    # minority route: most people run `docker pull` + `up -d`, and that
    # was completely silent. Features shipped and sat unused because
    # nobody was told they existed (#2 — the maintainer's own point).
    #
    # Read BEFORE the marker is written, and only announced when a
    # PREVIOUS version was recorded. On a first-ever boot there is
    # nothing to compare against, and "updated to v1.75.0" would be a
    # plain untruth on a fresh install.
    whatsnew_msg = ""
    try:
        import json as _json
        _prev = ""
        _vpath = getattr(config, "version_state_file", "")
        if _vpath and os.path.exists(_vpath):
            with open(_vpath) as f:
                _prev = str((_json.load(f) or {}).get("version") or "")
        if _prev and _prev != VERSION:
            from whatsnew import summary as _whatsnew
            whatsnew_msg = _whatsnew(_prev, VERSION, t)
        if _vpath and _prev != VERSION:
            from container_store import atomic_write_json
            atomic_write_json(_vpath, {"version": VERSION})
    except Exception as e:
        print(f"Could not build the what's-new notice (non-fatal): {e}")

    # An update caught mid-swap leaves a container stopped and renamed,
    # and the rollback that guards every other failure cannot run when the
    # process is killed outright. Repaired before anything else starts, so
    # a service that is down comes back before the next check runs.
    recovery_msg = ""
    try:
        recovery_msg = recover_interrupted_update(config, backend, t)
        if recovery_msg:
            print(f"Recovery: {recovery_msg}")
    except Exception as e:
        print(f"Recovery check failed (non-fatal): {e}")

    if not post_selfupdate_restart:
        startup_msg = t("startup_message", version=VERSION)
        if requested_restart:
            startup_msg += t("startup_reason_requested")
        elif restart_signal:
            startup_msg += t("startup_reason_signal", signal=restart_signal)
        if bot.enabled:
            bot.send_message(startup_msg)
        # …and every other channel. This line went to Telegram alone,
        # while the hard-kill note and the what's-new note two blocks down
        # have always gone to both — so a Discord, e-mail or ntfy user
        # never saw "restarted on vX" at all. Found by @NotRetarded (#57)
        # while testing the bot's own channel: the start announcement
        # arrived and nothing else did, and he reasonably concluded the
        # rest of the notifications were broken too. They are not; this
        # one message simply never had a second recipient.
        if notifier.has_channels():
            notifier.send_message(startup_msg)
        # Three different things used to share one alarming message.
        # Now: name the cause when we can see it, stay quiet when there is
        # nothing to mourn, and keep the old warning for the case it was
        # actually written for.
        storage_msg = ""
        if storage_key:
            _f = storage_check.first(storage_findings,
                                     storage_key[len("storage_"):]) or {}
            storage_msg = t(storage_key,
                            data_dir=_f.get("data_dir", config.data_dir),
                            source=_f.get("source", ""),
                            dest=_f.get("dest", ""))
        elif settings_missing and settings_ever_saved:
            # This one is real: a settings.json existed in this data
            # directory and is not there any more.
            print(f"Data loss: {config.settings_file} is gone, but this data "
                  f"directory has held one before. Restore a backup via Web "
                  f"UI → Settings → Import.")
            storage_msg = t("data_loss_alert")
        elif settings_missing:
            # Never saved here, so nothing was lost — the normal state for
            # an install driven purely by environment variables, which is
            # measurably most of them: `save_persistent()` only ever runs
            # from a user action, so such an install never writes one.
            # Worth a log line and nothing more; this used to be a warning
            # about possible data loss, on every single boot.
            print("No settings.json yet — everything is coming from the "
                  "environment. Normal until you save something in the Web "
                  "UI or via the bot.")
        if storage_msg:
            if bot.enabled:
                bot.send_message(storage_msg)
            if notifier.has_channels():
                notifier.send_message(storage_msg)

    # Sent regardless of how we restarted — a self-update and a manual
    # pull both land the user on a new version, and both deserve the note.
    # Said even when a self-update restart suppresses the ordinary startup
    # line: "we were killed" is never the story that message tells.
    if hard_kill or recovery_msg or killed_stopping:
        parts = []
        if hard_kill:
            parts.append(t("startup_hard_kill"))
        elif killed_stopping:
            parts.append(t("startup_killed_stopping"))
        if recovery_msg:
            parts.append(recovery_msg)
        killed_msg = "\n".join(parts)
        if bot.enabled:
            bot.send_message(killed_msg)
        if notifier.has_channels():
            notifier.send_message(killed_msg)

    if whatsnew_msg:
        if bot.enabled:
            bot.send_message(whatsnew_msg)
        if notifier.has_channels():
            notifier.send_message(whatsnew_msg)
        print(whatsnew_msg)

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
