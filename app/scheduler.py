#!/usr/bin/env python3
"""Cron-like scheduler for periodic update checks."""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from errfmt import clip


# Markers older than this are considered stale (a previous self-update
# crashed before the new container could pick up the deferred check) and
# get silently discarded.
_DEFERRED_CHECK_TTL = timedelta(hours=1)


def _dow_matches_seven(pattern):
    """Whether a day-of-week pattern matches Sunday written as `7`.

    Cron accepts both `0` and `7` for Sunday. The main matcher compares
    against 0, so this covers the other spelling — including `1-7`, which
    means Monday-through-Sunday and would otherwise exclude the one day it
    names explicitly.
    """
    pattern = (pattern or "").strip()
    if pattern in ("*", ""):
        return False
    for piece in pattern.split(","):
        piece = piece.strip().split("/")[0]
        if piece == "7":
            return True
        if "-" in piece:
            try:
                start, end = (int(x) for x in piece.split("-", 1))
            except (ValueError, TypeError):
                continue
            if start <= 7 <= end:
                return True
    return False


def cron_matches(expr, now):
    """Return True if the 5-field cron `expr` would fire at the datetime
    `now` (minute-precision). Supports `*`, `*/n`, exact values, ranges
    `a-b`, range+step `a-b/n`, and comma lists `a,b,c`.

    Extracted from Scheduler._matches_cron in v1.20.0 so the Web UI
    settings page can preview "next 3 ticks" for a freshly-typed
    schedule without instantiating a Scheduler.
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    # Cron counts weekdays from SUNDAY (0=Sun … 6=Sat, and 7 is Sunday
    # again); Python's weekday() counts from Monday. Feeding weekday()
    # straight into the matcher shifted every day-of-week schedule one day
    # late — `0 9 * * 1` meant Monday and fired on Tuesday, including the
    # Web UI's own shipped "Weekly (Mondays 9 AM)" preset — and `7` never
    # matched anything at all, so a perfectly valid Sunday schedule simply
    # never ran. Measured across a full week before fixing. (wud#410, found
    # by sweeping comparable tools' bug histories.)
    dow = (now.weekday() + 1) % 7
    fields = [
        (parts[0], now.minute),
        (parts[1], now.hour),
        (parts[2], now.day),
        (parts[3], now.month),
        (parts[4], dow),
    ]
    for i, (pattern, value) in enumerate(fields):
        if pattern == "*":
            continue
        # Sunday answers to both 0 and 7, so a pattern written either way —
        # including inside a range like `1-7` — has to match.
        if i == 4 and dow == 0 and _dow_matches_seven(pattern):
            continue
        try:
            if "/" in pattern and "-" in pattern.split("/")[0]:
                range_part, step_part = pattern.split("/", 1)
                start, end = range_part.split("-")
                step = int(step_part)
                if not (int(start) <= value <= int(end)):
                    return False
                if (value - int(start)) % step != 0:
                    return False
                continue
            if pattern.startswith("*/"):
                step = int(pattern[2:])
                if value % step != 0:
                    return False
                continue
            if "," in pattern:
                if str(value) not in pattern.split(","):
                    return False
                continue
            if "-" in pattern:
                start, end = pattern.split("-")
                if not (int(start) <= value <= int(end)):
                    return False
                continue
            if int(pattern) != value:
                return False
        except (ValueError, IndexError):
            return False
    return True


def cron_next_ticks(expr, count=3, after=None, max_minutes=60 * 24 * 366):
    """Return the next `count` datetime ticks that match the cron
    expression starting from `after` (default: now), capped at
    `max_minutes` of look-ahead. Returns fewer results than `count` if
    the cap is hit. Returns an empty list when the expression is
    malformed.

    Used by the Settings page schedule-editor preview (Bundle 2).
    """
    if after is None:
        after = datetime.now()
    # Round up to the next whole minute and start there
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    out = []
    steps = 0
    while len(out) < count and steps < max_minutes:
        if cron_matches(expr, cursor):
            out.append(cursor)
        cursor += timedelta(minutes=1)
        steps += 1
    return out


class Scheduler:
    def __init__(self, config, checker, bot, hosts=None):
        self.config = config
        # The local host's checker. Disk space, cleanup and the monitor are
        # about the machine Docksentry runs on, so they stay on this one
        # even when several hosts are managed.
        self.checker = checker
        self.bot = bot
        # Every managed host (#7). None → single-host, and the loop below
        # runs exactly once against `checker`, i.e. unchanged behaviour.
        self.hosts = hosts
        self.running = False
        self.thread = None
        # In-memory fallbacks so a failed state-file write (e.g. a full disk —
        # exactly when these alerts matter most) can't cause the same report
        # to fire over and over. They persist for the process lifetime and
        # reset on restart. See _run / _check_disk_space.
        self._weekly_sent_date = None
        self._disk_warned_at = None

    def _host_failed(self, host_name, err):
        """Tell somebody a host dropped out — once, not every night.

        The scheduled check used to `print` this and stop there, while the
        manual `/check` sent a message. Backwards: the unattended run is
        the one nobody is watching. @famewolf added two hosts, the SSH
        from inside the container could not authenticate, and his nightly
        check quietly covered one machine out of three — with the reason
        sitting in a container log he had no reason to open (#2).

        Once per host per outage, and again when it comes back. A message
        every night about the same dead box is a message people learn to
        ignore, which is the same defect in a different coat.
        """
        state = getattr(self, "_host_failures", None)
        if state is None:
            state = self._host_failures = {}
        if host_name in state:
            return
        state[host_name] = clip(err)
        # If we can say *why* the connection was refused, say it here —
        # this message is the one somebody reads at 18:00 when nothing
        # updated, and "Permission denied (publickey)" is right and
        # useless on its own (#2).
        detail = clip(err)
        try:
            import hostdiag
            endpoint = ""
            for h in (self.hosts or ()):
                if getattr(h, "name", None) == host_name:
                    endpoint = getattr(h, "endpoint", "") or ""
                    break
            extra = hostdiag.hint(endpoint, err)
            if extra:
                detail += "\n\n" + extra
        except Exception:
            pass
        self._tell(self.bot.t("host_check_failed", host=host_name or "local",
                              error=detail))

    def _host_recovered(self, host_name):
        state = getattr(self, "_host_failures", None) or {}
        if host_name not in state:
            return
        state.pop(host_name, None)
        self._tell(self.bot.t("host_check_recovered",
                              host=host_name or "local"))

    def _tell(self, text):
        """Every channel, not just Telegram — a host dropping out is not
        a Telegram-only fact."""
        try:
            if getattr(self.bot, "enabled", False):
                self.bot.send_message(text)
            notifier = getattr(self.bot, "notifier", None)
            if notifier is not None and notifier.has_channels():
                notifier.send_message(text)
        except Exception as e:
            print(f"Could not report the host state change: {e}")

    def _checkers(self):
        """(checker, host_name) for every managed host, local first.

        The body of this moved to `hosts.host_checkers` once it turned out
        that this class was the only thing in the process that knew how to
        walk every host — so the scheduled check covered a whole estate
        and the two manual ones covered the local machine and said
        nothing. Kept as a method because the call sites below read better
        for it, and because it is what the scheduler tests reach for.
        """
        from hosts import host_checkers
        return host_checkers(self.hosts, self.checker)

    def start(self):
        self.running = True
        # Pick up any pending deferred-check marker from a self-update we
        # were in the middle of when the process restarted. Triggers a
        # one-shot check in the background so the regular scheduler loop
        # starts unblocked. Belt-and-suspenders: even though the method
        # itself is wrapped in try/except, an unexpected failure here must
        # not block the main scheduler thread from starting.
        try:
            self._resume_deferred_check()
        except Exception as e:
            print(f"Deferred-check resume failed (non-fatal): {e}")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _resume_deferred_check(self):
        """If a deferred-check marker exists and is fresh, run check_all
        immediately on a background thread. Always remove the marker
        afterwards so a failed run doesn't loop forever.

        ALSO claims the current minute via `self._resumed_minute` so the
        main scheduler loop in _run() skips its cron-tick for the same
        minute. Without this, after a self-update restart the deferred
        check AND the regular cron tick would both fire for e.g. 18:00
        and the user gets "Updates Available" twice (one minute apart
        in Telegram because of timing offsets). Reported by the user
        from a real-world v1.17.0 deployment.

        Wrapped in a broad try/except — a malformed marker must never
        prevent the bot from starting up. Worst case the user waits for
        the next cron tick."""
        path = self.config.deferred_check_file
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            os.remove(path)
            ts = datetime.fromisoformat(data.get("trigger_time", ""))
            # Strip tz so we can subtract — datetime.now() is naive.
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            age = datetime.now() - ts
            if age > _DEFERRED_CHECK_TTL:
                print(f"Deferred check marker is stale ({age}), discarding")
                return
            print(f"Resuming deferred check after self-update (marker age: {age})")
        except Exception as e:
            print(f"Deferred-check marker unreadable, ignoring: {e}")
            try:
                os.remove(path)
            except OSError:
                pass
            return

        # Claim the current minute so the main loop doesn't double-fire.
        # The deferred check IS this minute's cron tick — just resumed
        # across a process boundary.
        self._resumed_minute = datetime.now().strftime("%Y-%m-%d %H:%M")

        def _run_deferred():
            # Tiny delay so the bot listener / Web UI are fully up before
            # we send the "now checking your containers" message.
            time.sleep(3)
            try:
                from version import VERSION
                resumed_msg = self.bot.t("selfupdate_resumed_check", version=VERSION)
                if self.bot.enabled:
                    self.bot.send_message(resumed_msg, auto=True)
                # Also fan out to Discord / webhook so non-Telegram users
                # see the post-restart confirmation too (#19).
                notifier = getattr(self.bot, "notifier", None)
                if notifier and notifier.has_channels():
                    notifier.send_message(resumed_msg)
                for host_checker, host_name in self._checkers():
                    try:
                        updates = host_checker.check_all()
                        if updates:
                            self.bot.handle_autoupdates(updates, host_checker)
                    except Exception as e:
                        where = f" on {host_name}" if host_name else ""
                        print(f"Deferred check error{where}: {e}")
            except Exception as e:
                print(f"Deferred check error: {e}")

        threading.Thread(target=_run_deferred, daemon=True).start()

    def stop(self):
        self.running = False

    def _matches_cron(self, now):
        """Simple cron matching: minute hour day month weekday."""
        return cron_matches(self.config.cron_schedule, now)

    def _run(self):
        # Pre-claim the resumed minute if we just came back from a
        # self-update. Prevents the cron tick from firing a second time
        # for the same minute the deferred-check is already handling.
        last_check = getattr(self, "_resumed_minute", None)
        last_weekly = None
        last_disk_ts = 0.0  # monotonic; 0 → first disk check fires on startup
        last_mon_ts = 0.0
        # Container state monitor (#2, @NotRetarded) — same own-cadence
        # pattern as the disk check. Constructed lazily so a disabled
        # monitor costs nothing.
        # One monitor per host, each with its own baseline and its own event
        # stream. Update checking has been multi-host since v1.62.0, but the
        # crash and health monitor was only ever built for the local box —
        # measured on a live two-host install: a Podman container died with
        # exit 42 and nothing was reported, while the same instance
        # cheerfully logged "Managing 2 hosts". A shared monitor would not
        # do: the baseline diff is per-host state, and one host's sweep
        # would read the other's containers as vanished.
        monitors = []
        if getattr(self.config, "monitor_enabled", True):
            from monitor import ContainerMonitor
            for checker, host_name in self._checkers():
                monitors.append(ContainerMonitor(
                    self.config, checker, self.bot,
                    backend=getattr(checker, "backend", None),
                    host_name=host_name))
        print(f"Scheduler started with schedule: {self.config.cron_schedule}")
        if last_check:
            print(f"Initial cron tick for {last_check} claimed by deferred-check (post-selfupdate)")

        while self.running:
            now = datetime.now()
            current_minute = now.strftime("%Y-%m-%d %H:%M")

            # Weekly report: independent of the cron schedule. Fires at the
            # configured weekday + hour, at most once per day.
            current_hour = now.strftime("%Y-%m-%d %H")
            if current_hour != last_weekly:
                last_weekly = current_hour
                try:
                    from weekly_report import maybe_send_weekly_report
                    from i18n import get_translator
                    today = now.date().isoformat()
                    # In-memory guard: if the state file couldn't be written
                    # last time (e.g. full disk), should_send_now would re-fire
                    # the report every hour. Skip if we already sent it today.
                    if self._weekly_sent_date != today:
                        t = get_translator(self.config.language)
                        if maybe_send_weekly_report(self.config, self.bot, t):
                            self._weekly_sent_date = today
                except Exception as e:
                    print(f"Weekly report check error: {e}")

            if current_minute != last_check and self._matches_cron(now):
                last_check = current_minute
                # Maintenance-mode short-circuit: skip the entire tick.
                # Disk-warning, weekly-report etc. all get skipped too —
                # the whole point of maintenance is "nothing autonomous".
                from maintenance import is_active as _maint_active
                if _maint_active(self.config):
                    print(f"Scheduled check at {current_minute} skipped — maintenance mode active")
                    time.sleep(30)
                    continue
                print(f"Scheduled check triggered at {current_minute}")

                # Auto self-update FIRST. If a new image is available we
                # restart this process before running the container-update
                # check — that way the check itself runs on the new code
                # and the user gets one linear notification story rather
                # than "5 updates found" → bot dies mid-conversation →
                # silent restart. The deferred-check marker (handled in
                # _resume_deferred_check on next boot) carries the intent
                # across the restart.
                if self.config.auto_selfupdate:
                    try:
                        applied = self.bot.check_selfupdate_auto(defer_check=True)
                        if applied:
                            # Helper container is about to stop us.
                            # check_selfupdate_auto already wrote the
                            # deferred-check marker, so the freshly-booted
                            # process will run check_all() in our place.
                            print("Self-update applied, exiting tick — check will resume after restart")
                            return
                    except Exception as e:
                        print(f"Auto selfupdate error: {e}")

                auto_updated = 0
                for host_checker, host_name in self._checkers():
                    try:
                        updates = host_checker.check_all()
                        if updates:
                            auto_updated += self.bot.handle_autoupdates(
                                updates, host_checker) or 0
                        # If no updates, stay quiet (--quiet behavior)
                        self._host_recovered(host_name)
                    except Exception as e:
                        # One unreachable or misbehaving host must not stop
                        # the others from being checked (#7) — report it and
                        # carry on down the list.
                        where = f" on {host_name}" if host_name else ""
                        print(f"Scheduled check error{where}: {e}")
                        self._host_failed(host_name, e)

                # Auto cleanup after successful auto-updates. The grace-hours
                # filter (default 24h) in cleanup_images() prevents removing
                # images we just pulled — the rollback safety-net stays
                # intact. With cleanup_backup_local_only enabled, locally-
                # built images are saved as tarballs first.
                if self.config.auto_cleanup and auto_updated > 0:
                    try:
                        # Guarded: skips silently if another update flow
                        # (e.g. a manual batch in the bot thread) holds the
                        # mutex right now — the next trigger cleans instead.
                        ok, msg = self.bot.cleanup_guarded(self.checker)
                        if ok is None:
                            print(f"Auto cleanup skipped: {msg}")
                        else:
                            print(f"Auto cleanup: {msg}")
                            notifier = getattr(self.bot, "notifier", None)
                            if ok and notifier and notifier.has_channels():
                                notifier.send_message(self.bot.t("cleanup_auto_prefix", message=msg))
                            if ok and self.bot.enabled:
                                # No 🧹 in front: `cleanup_images` already
                                # opens with ✅ or ❌, and two status icons
                                # back to back read as a rendering fault.
                                self.bot.send_message(msg, auto=True)
                    except Exception as e:
                        print(f"Auto cleanup error: {e}")

            # Disk monitoring on its OWN cadence — decoupled from the update
            # cron. A disk can fill in minutes; tying the check to a once-a-day
            # cron meant a crash-looping container filled the disk between two
            # daily ticks with no warning at all (2026-06-18). Runs every
            # disk_check_interval_seconds (default 300). Skipped in maintenance.
            try:
                from maintenance import is_active as _maint_active
                disk_interval = int(getattr(self.config, "disk_check_interval_seconds", 300) or 300)
                if (time.monotonic() - last_disk_ts >= disk_interval
                        and not _maint_active(self.config)):
                    last_disk_ts = time.monotonic()
                    self._check_disk_space()
            except Exception as e:
                print(f"Disk space check error: {e}")

            # Container state monitor — transitions only, quiet during
            # updates (the tick itself checks the update mutex) and in
            # maintenance mode.
            try:
                if monitors:
                    from maintenance import is_active as _maint_active2
                    mon_interval = int(getattr(self.config, "monitor_interval_seconds", 60) or 60)
                    if (time.monotonic() - last_mon_ts >= mon_interval
                            and not _maint_active2(self.config)):
                        last_mon_ts = time.monotonic()
                        for mon in monitors:
                            try:
                                mon.tick()
                            except Exception as e:
                                # One unreachable host must not silence the
                                # others — that would turn a network blip on
                                # the NAS into a blind spot at home.
                                print(f"Monitor error ({mon.host_name or 'local'}): {e}")
            except Exception as e:
                print(f"Monitor error: {e}")

            # Anything a dead network swallowed is still waiting. This loop
            # is the only thing that runs on its own no matter what else is
            # configured, so it is where a held notification gets its next
            # go — within 30s of the network coming back (#66,
            # @NotRetarded, whose crash alert went out over Discord and
            # never over Telegram). Never blocks: the queue skips a channel
            # the moment one of its resends fails.
            try:
                import notify_retry
                notify_retry.flush()
            except Exception as e:
                print(f"Notify retry error: {e}")

            time.sleep(30)

    def _check_disk_space(self):
        action, percent, free_gb = self.checker.check_disk_usage()
        if action == "ok":
            self._disk_warned_at = None  # back below threshold — re-arm
            return
        if action == "silent":
            return  # already warned today (persisted throttle)
        # action == "warn". The file-based throttle in check_disk_usage can't
        # persist its state when the disk is full — exactly the situation here —
        # so it would return "warn" on every pass. The in-memory guard keeps
        # the notification to ~once/day even when the state file can't be written.
        now_ts = time.time()
        if self._disk_warned_at and now_ts - self._disk_warned_at < 23 * 3600:
            return
        self._disk_warned_at = now_ts
        # Extended warning body (#2, @famewolf): a bare "disk at X%" gets lost
        # in the noise. Tell the user how much can be reclaimed with
        # `/cleanup` and whether auto-cleanup would have already done it —
        # then the warning is actionable, not just informational.
        msg = f"⚠️ Disk usage at {percent}% — {free_gb:.1f} GB free."
        reclaim = self.checker.reclaimable_bytes()
        if reclaim > 0:
            gib = reclaim / (1024 ** 3)
            unit = f"{gib:.1f} GB" if gib >= 0.1 else f"{reclaim / (1024 ** 2):.0f} MB"
            msg += f"\n🧹 {unit} reclaimable via `/cleanup` (unused images / build cache)."
        if not self.config.disk_warn_auto_cleanup:
            msg += "\n💡 Auto-cleanup is OFF — set `DISK_WARN_AUTO_CLEANUP=true` (or enable it in Web UI → Settings) to reclaim automatically next time."
        notifier = getattr(self.bot, "notifier", None)
        if notifier and notifier.has_channels():
            notifier.send_message(msg)
        if self.bot.enabled:
            self.bot.send_message(msg, auto=True)
        print(msg)

        if self.config.disk_warn_auto_cleanup:
            print("Disk warning + auto-cleanup enabled — running cleanup")
            # Guarded: if an update is mid-flight, pruning now could delete
            # the image it just pulled. Skip — the disk monitor re-fires on
            # its own cadence, so a genuinely full disk retries in minutes.
            ok, cmsg = self.bot.cleanup_guarded(self.checker)
            if ok is None:
                print(f"Disk auto cleanup skipped: {cmsg}")
                return
            full = self.bot.t("cleanup_disk_prefix", message=cmsg)
            if notifier and notifier.has_channels():
                notifier.send_message(full)
            if self.bot.enabled:
                self.bot.send_message(full, auto=True)
