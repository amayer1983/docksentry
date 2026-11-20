#!/usr/bin/env python3
"""Does the suite actually hold the promises, or only run near them?

On 06.10.2026 an audit deleted the one-retry cap out of `notify_retry`
entirely and ran all 181 procedures. Not one went red. The check that
was supposed to guard it asserted that `tries=1` had been *passed*, with
`remember` mocked away, so the enforcement was never executed by
anything. Three greps for `/hosts check` were satisfied by the call
being present as a *comment*, with the feature dead for every user.

Both were invisible to a green suite, and both were found the same way:
break the guard on purpose, and see whether anything notices. That is
what this file does, on a schedule instead of when somebody thinks of
it.

`test_check_quality.py` is the companion one floor down — it catches
checks that are wrong in their own right. This one catches checks that
are fine as code and guard nothing.

Two lists, and the second is the honest half.

`GUARDED` is a promise, the line that delivers it, and the procedure
that must go red when that line is taken away. If the procedure stays
green the promise is decoration, and this says so with the promise's own
words rather than a file name.

`UNGUARDED` is the debt: promises measured to have no guard at all.
Every entry is a thing the README or the docs tell people the program
does, where the enforcement can be removed and the suite notices
nothing. The list may only get shorter — an entry that starts failing
means somebody wrote a real check, and the message says to move it up.
That way the hole is in the suite rather than in a note nobody reads.

It is empty as of 16.10.2026, and that is worth stating carefully:
empty means every promise *this file knows about* can be falsified. It
does not mean every promise can be. The 10.10. sweep found six that
nobody had thought to list, and the one on 11.10. found nine more. The
list being empty is an invitation to sweep again, not a result.

Deliberately NOT here: anything whose mutation needs a live daemon, and
anything where the mutated line is the only caller of itself. Those are
unfalsifiable in a copy of the tree, and a check that cannot fail is
what this file exists to find.
"""
import ast
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: (promise, file, anchor, replacement, procedure that must go red)
#:
#: `anchor` has to occur exactly once, or the entry is stale and this
#: says so instead of quietly skipping it — a mutation that does not
#: apply is a check that passed without running, which is the whole
#: thing being hunted here.
GUARDED = [
    ("queued work is picked up even when nobody drains it",
     "app/telegram_bot.py",
     "                self._sweep_update_queue(checker)",
     "                pass",
     "test_update_queue_sweep.py"),

    # The sweep's rules are the engine's since Q9.
    ("the sweep leaves the queue alone while somebody holds the lock",
     "app/update_engine.py",
     "            if self.update_claimed:\n                return",
     "            if False:\n                return",
     "test_update_queue_sweep.py"),

    ("the sweep does not call the drain when nothing is queued",
     "app/update_engine.py",
     "        try:\n            if not self.has_queued_updates():\n                return",
     "        try:\n            if False:\n                return",
     "test_update_queue_sweep.py"),

    ("…and does call it when something is queued and the lock is free",
     "app/update_engine.py",
     "            drain()\n        except",
     "            pass\n        except",
     "test_update_queue_sweep.py"),

    ("a failing drain does not take the listener loop down with it",
     "app/update_engine.py",
     '            print(f"Queue sweep failed: {e}")',
     "            raise",
     "test_update_queue_sweep.py"),

    ("Telegram's sweep goes through the engine's rules, not its own",
     "app/telegram_bot.py",
     "        self.engine.sweep_queue(lambda: self._drain_update_queue(checker))",
     "        self._drain_update_queue(checker)",
     "test_update_queue_sweep.py"),

    ("a queue entry remembers when it was queued",
     "app/update_engine.py",
     "            self._queued_at[key] = time.time()",
     "            pass",
     "test_update_queue_sweep.py"),

    ("a drained entry forgets its timestamp",
     "app/update_engine.py",
     "            key = self._update_queue.pop(0)\n            self._queued_at.pop(key, None)",
     "            key = self._update_queue.pop(0)",
     "test_update_queue_sweep.py"),

    ("the 'already queued' answer says how long",
     "app/lang/en.json",
     "has been in the queue for {since}",
     "is already in the queue.",
     "test_update_queue_sweep.py"),

    ("a tag that disagrees with version.py does not build",
     ".github/workflows/docker-publish.yml",
     '          if [[ "$TAG_VERSION" != "$FILE_VERSION" ]]; then',
     '          if false; then',
     "test_release_tag_guard.py"),

    ("the outer page frame filters the language attribute too",
     "app/web_ui.py",
     '<html lang="{_e(safe_language(config.language))}"><head>',
     '<html lang="{_e(config.language)}"><head>',
     "test_v2_output_safety.py"),

    ("a backup bundle is written only its owner can read",
     "app/backup.py",
     '        fd, tmp = tempfile.mkstemp(dir=directory,\n'
     '                                   prefix=os.path.basename(path) + ".",\n'
     '                                   suffix=".tmp")',
     '        tmp = path + ".tmp"\n'
     "        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)",
     "test_backup_write_safety.py"),

    ("a damaged settings file does not take the start down",
     "app/config.py",
     "        saved = {}\n        try:",
     "        try:",
     "test_config_damage.py"),

    ("…including one that is damaged below the JSON layer",
     "app/config.py",
     "        except (json.JSONDecodeError, UnicodeDecodeError,\n"
     "                _DamagedSettings) as e:",
     "        except (json.JSONDecodeError,\n"
     "                _DamagedSettings) as e:",
     "test_config_damage.py"),

    ("…and the rest of the process is told it happened",
     "app/config.py",
     "        self.settings_quarantined = True\n",
     "        self.settings_quarantined = False\n",
     "test_config_damage.py"),

    ("losing the settings file does not open the Web UI",
     "app/web_ui.py",
     "                return not (getattr(config, \"settings_quarantined\", False)\n                            or configured_before)",
     "                return True",
     "test_web_auth_gate.py"),

    ("a settings file of the wrong shape is damage, not a crash",
     "app/config.py",
     "            if not isinstance(saved, dict):\n"
     "                raise _DamagedSettings(",
     "            if False:\n                raise _DamagedSettings(",
     "test_config_damage.py"),

    ("clicking Update in the Web UI is answered, not swallowed",
     "app/web_ui.py",
     "                    self._send_redirect(\"/?update=started\")",
     "                    self._send_redirect(\"/\")",
     "test_web_update_feedback.py"),

    ("the image's own start_period extends the health wait",
     "app/update_checker.py",
     "        effective = max(int(max_starting), int(image_start * 1.5))",
     "        effective = int(max_starting)",
     "test_health_outcomes.py"),

    ("a refused image lookup is a failed check, not a phantom update",
     "app/update_checker.py",
     "                    if run_id and self._digests_unreadable(run_id):",
     "                    if False and self._digests_unreadable(run_id):",
     "test_running_image_check.py"),

    ("an unreadable pin list stops updates instead of waving them through",
     "app/update_checker.py",
     "                self._pin_file_unreadable(e)\n                return EVERYTHING_PINNED",
     "                raw = []",
     "test_pin_fail_closed.py"),

    ("…and does not take this host's pending list down with it",
     "app/update_checker.py",
     '            if pin_unreadable:',
     "            if False:",
     "test_pin_fail_closed.py"),

    ("…and says so, instead of stopping them in silence",
     "app/update_checker.py",
     '        print(f"ERROR: {self.config.pinned_file} could not be read "',
     '        print(f"ERROR: {self.config.pinned_file} "',
     "test_pin_fail_closed.py"),

    ("a quarantined instance is still locked after a restart",
     "app/web_ui.py",
     "                return not (getattr(config, \"settings_quarantined\", False)\n                            or configured_before)",
     "                return not getattr(config, \"settings_quarantined\", False)",
     "test_web_auth_gate.py"),

    ("a rate limit answers in the user's language, not in German",
     "app/update_checker.py",
     '                msg = self._t("rate_limit")',
     '                msg = "Rate limit erreicht"',
     "test_pull_failure_report.py"),

    ("a key left on the English string is caught, not just a missing one",
     "app/lang/ja.json",
     '"web_update_busy": "別の更新がすでに実行中のため、これは開始されませんでした。完了してからもう一度お試しください。"',
     '"web_update_busy": "Another update is already running, so this one was not started. Try again once it has finished."',
     "test_lang_parity.py"),

    ("the one writer of every JSON file names its encoding",
     "app/container_store.py",
     "        with os.fdopen(fd, \"w\", encoding=\"utf-8\") as f:",
     "        with os.fdopen(fd, \"w\") as f:",
     "test_file_encoding.py"),

    ("an API token reaches the two read endpoints and nothing else",
     "app/web_ui.py",
     "            if path.split(\"?\")[0] in (\"/metrics\", \"/api/status\"):",
     "            if path.split(\"?\")[0] in (\"/metrics\", \"/api/status\", \"/logs\"):",
     "test_metrics_api.py"),

    ("an advisory never becomes a pending update",
     "app/update_checker.py",
     "                        advisories[c[\"name\"]] = {\"current\": tag, \"newer\": newer}",
     "                        advisories[c[\"name\"]] = {\"current\": tag, \"newer\": newer}\n"
     "                        updates.extend([c])",
     "test_newer_version.py"),

    ("docker wins over podman while it is on PATH",
     "app/container_backend.py",
     "    if shutil.which(\"docker\"):\n"
     "        return \"docker\"\n"
     "    if shutil.which(\"podman\"):\n"
     "        return \"podman\"",
     "    if shutil.which(\"podman\"):\n"
     "        return \"podman\"\n"
     "    if shutil.which(\"docker\"):\n"
     "        return \"docker\"",
     "test_container_backend.py"),

    ("every Discord command is admin-only and unusable in a DM",
     "app/discord_bot.py",
     "        out.append(c)",
     "        pass  # out.append(c)",
     "test_discord_bot.py"),

    ("every language file carries the same keys as English",
     "app/lang/de.json",
     '"status_queued"',
     '"status_queued_BROKEN"',
     "test_lang_parity.py"),

    ("a cleanup grace of zero means none, not the default",
     "app/update_checker.py",
     "        grace = grace_hours(self.config.cleanup_grace_hours)",
     "        try:\n"
     "            grace = int(self.config.cleanup_grace_hours or 24)\n"
     "        except (ValueError, TypeError):\n"
     "            grace = 24",
     "test_cleanup_rules.py"),

    ("the cleanup preview counts against the configured grace",
     "app/update_checker.py",
     "        cutoff = datetime.now(timezone.utc) - timedelta(hours=grace)",
     "        cutoff = datetime.now(timezone.utc) - timedelta(hours=999)",
     "test_cleanup_rules.py"),

    ("a maintenance file of the wrong shape does not stop the updates",
     "app/maintenance.py",
     "    return data if isinstance(data, dict) else {}",
     "    return data",
     "test_maintenance_damage.py"),

    ("a typo in an environment variable does not kill the boot",
     "app/config.py",
     "            pull_timeout=_clamp_int(\n"
     "                _env(\"PULL_TIMEOUT\", \"1800\"),\n"
     "                *INT_RANGES[\"pull_timeout\"], 1800,\n"
     "                name=\"PULL_TIMEOUT\")",
     "            pull_timeout=int(_env(\"PULL_TIMEOUT\", \"1800\"))",
     "test_env_bounds.py"),

    ("…and the ranges the Settings form enforces hold for the environment too",
     "app/config.py",
     "            disk_warn_percent=_clamp_int(\n"
     "                _env(\"DISK_WARN_PERCENT\", \"85\"),\n"
     "                *INT_RANGES[\"disk_warn_percent\"], 85,\n"
     "                name=\"DISK_WARN_PERCENT\")",
     "            disk_warn_percent=int(_env(\"DISK_WARN_PERCENT\", \"85\"))",
     "test_env_bounds.py"),

    ("a damaged settings file is kept, not overwritten",
     "app/config.py",
     "            os.replace(self.settings_file, kept)",
     "            pass",
     "test_config_damage.py"),

    ("two quarantines in one second do not overwrite each other",
     "app/config.py",
     "        n = 1\n        while os.path.exists(kept):",
     "        n = 1\n        while False:",
     "test_config_damage.py"),

    ("a value cannot close the page's script block",
     "app/web_v2.py",
     '.replace("<", "\\\\u003c")',
     '.replace("<", "<")',
     "test_v2_output_safety.py"),

    ("an unknown language code is never rendered",
     "app/web_v2.py",
     "    lang = safe_language(lang)",
     "    pass",
     "test_v2_output_safety.py"),

    ("a stored link is checked before the V2 page gets it",
     "app/web_v2.py",
     '                "link": _link(links.get(name, "")),',
     '                "link": links.get(name, ""),',
     "test_v2_output_safety.py"),

    ("two writers cannot publish a mixture of their writes",
     "app/container_store.py",
     '    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".",\n'
     '                               suffix=".tmp")',
     '    tmp = path + ".tmp"\n'
     "    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)",
     "test_store_concurrency.py"),

    ("a failed write removes its own temp file",
     "app/container_store.py",
     "        try:\n            os.unlink(tmp)\n        except OSError:\n            pass\n        raise",
     "        raise",
     "test_store_concurrency.py"),

    ("an unreadable pin list means protected, not unprotected",
     "app/container_store.py",
     "        readable, pinned = self._load_checked(self.pinned_file)\n"
     "        if not readable:\n            return True",
     "        readable, pinned = self._load_checked(self.pinned_file)\n"
     "        if not readable:\n            return False",
     "test_store_concurrency.py"),

    ("an unreadable ask-before-major list means ask",
     "app/container_store.py",
     "        readable, names = self._load_checked(self.ask_before_major_file)\n"
     "        if not readable:\n            return True",
     "        readable, names = self._load_checked(self.ask_before_major_file)\n"
     "        if not readable:\n            return False",
     "test_store_concurrency.py"),

    ("an unreadable protect-stop list means protected",
     "app/container_store.py",
     "        readable, names = self._load_checked(self.protect_stop_file)\n"
     "        if not readable:\n            return True",
     "        readable, names = self._load_checked(self.protect_stop_file)\n"
     "        if not readable:\n            return False",
     "test_store_concurrency.py"),

    ("a file that exists but will not parse is reported as unreadable",
     "app/container_store.py",
     '                print(f"Failed to read {path}: {e}")\n                return False, []',
     "                return True, []",
     "test_store_concurrency.py"),

    ("concurrent pins are serialised and none is lost",
     "app/container_store.py",
     "            with path_lock(target):\n                return fn(self, *args, **kwargs)",
     "            return fn(self, *args, **kwargs)",
     "test_store_concurrency.py"),

    ("the alert is logged before the channels are tried",
     "app/monitor.py",
     '        print(f"Monitor: {msg}")\n        try:\n'
     "            sent = self.bot.send_message(msg, auto=True)",
     "        try:\n            sent = self.bot.send_message(msg, auto=True)",
     "test_monitor.py"),

    ("the mail subject says which container and what happened",
     "app/notifiers/smtp.py",
     "        ok, why, msgid = self._attempt(_subject_for(text),",
     '        ok, why, msgid = self._attempt("Docksentry",',
     "test_smtp.py"),

    ("a mail the server took is not delivered a third time",
     "app/notify_retry.py",
     "            elif result is STOP:",
     "            elif False:",
     "test_smtp_resilience.py"),

    ("a connection that dies before the server says 'go ahead' is retried free",
     "app/notifiers/smtp.py",
     "                _data = getattr(server, \"data\", None)",
     '                handed["over"] = True\n                _data = getattr(server, "data", None)',
     "test_smtp_resilience.py"),

    ("a greylisting relay's 'come back later' is come back to",
     "app/notifiers/smtp.py",
     "        return TEMPORARY if 400 <= code <= 499 else PERMANENT",
     "        return PERMANENT",
     "test_smtp_resilience.py"),

    ("an outage during the TLS handshake holds the alert",
     "app/notifiers/smtp.py",
     "    if isinstance(err, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):",
     "    if False:",
     "test_smtp_resilience.py"),

    ("a server that refuses outright is not asked again every 30s",
     "app/notifiers/smtp.py",
     "        if ok or why == PERMANENT:",
     "        if ok:",
     "test_smtp_resilience.py"),

    ("a stopped daemon is not reported as a permission problem",
     "app/hostcheck.py",
     '_REFUSED = ("permission denied", "access denied", "not permitted",',
     '_REFUSED = ("cannot connect to the docker daemon at unix",\n            "permission denied", "access denied", "not permitted",',
     "test_hostcheck.py"),

    ("an ssh key refusal is not reported as a socket permission",
     "app/hostcheck.py",
     "        elif _ssh_refused(why):",
     '        elif ep.startswith("ssh://"):',
     "test_hostcheck.py"),

    ("containers that finished mid-check do not condemn the host",
     "app/hostcheck.py",
     "        if every_one_gone:",
     "        if False:",
     "test_hostcheck.py"),

    ("a socket that refuses us blocks the host",
     "app/hostcheck.py",
     '    bad = {"scheme_bad", "unreachable", "needs_tls", "denied", '
     '"denied_ssh",',
     '    bad = {"scheme_bad", "unreachable", "needs_tls",',
     "test_hostcheck.py"),

    ("the phrase for 'not TLS' does not produce a TLS finding",
     "app/hostcheck.py",
     '_TLS_WORDS = ("client sent an http request to an https server",)',
     '_TLS_WORDS = ("client sent an http request to an https server",\n'
     '              "tls handshake")',
     "test_hostcheck.py"),

    ("a TLS port is named as one, not called unreachable",
     "app/hostcheck.py",
     '            out.append(("needs_tls", params))',
     '            out.append(("unreachable", params))',
     "test_hostcheck.py"),

    ("…and the port is asked rather than guessed from the words",
     "app/hostcheck.py",
     "                and (_tls_said or (probe_tls or speaks_tls)(ep))):",
     "                and (_tls_said)):",
     "test_hostcheck.py"),

    ("…and a refusal is not made to wait for a handshake",
     "app/hostcheck.py",
     "        if (ep.startswith(\"tcp://\") and not _refused(why)",
     "        if (ep.startswith(\"tcp://\")",
     "test_hostcheck.py"),

    ("/hosts check reaches the pre-flight in Telegram",
     "app/telegram_bot.py",
     "                    self.send_message(self._hostcheck_text(_ep))",
     '                    self.send_message("x")',
     "test_hostcheck.py"),

    ("…and the Discord option reaches it too",
     "app/discord_bot.py",
     "        if _ep:\n            return self._hostcheck_text(_ep)\n",
     '        if _ep:\n            return "x"\n',
     "test_hostcheck.py"),
    ("EXCLUDE_CONTAINERS keeps a container out of every run",
     "app/update_checker.py",
     '            if name_matches(name, self.config.exclude_containers):',
     '            if False and name_matches(name, self.config.exclude_containers):',
     "test_container_scope.py"),
    ("a pinned container is never listed and never updated",
     "app/update_checker.py",
     '            if name in pinned:',
     '            if False and name in pinned:',
     "test_container_scope.py"),
    ("docksentry.pin on the container does the same",
     "app/update_checker.py",
     '            if self.label_bool(labels, "pin") is True:',
     '            if False and self.label_bool(labels, "pin") is True:',
     "test_container_scope.py"),
    ("docksentry.enable=false takes a container out of scope",
     "app/update_checker.py",
     '            if self.label_bool(labels, "enable") is False',
     '            if False and self.label_bool(labels, "enable") is False',
     "test_container_scope.py"),
    ("the update path never touches Docksentry's own container",
     "app/update_checker.py",
     "            if own_name and name == own_name and not include_self:",
     "            if False and own_name and name == own_name:",
     "test_container_scope.py"),
    ("quiet hours hold routine notifications back",
     "app/notifier.py",
     "        if is_quiet_now(self.config):\n            return True",
     "        if False:\n            return True",
     "test_quiet_enforcement.py"),

    ("…and the gate really asks about THIS config, not some other",
     "app/notifier.py",
     "        if is_quiet_now(self.config):",
     "        if is_quiet_now(None):",
     "test_quiet_enforcement.py"),

    ("…and so does the maintenance half of the same gate",
     "app/notifier.py",
     "            if _maint_active(self.config):",
     "            if _maint_active(None):",
     "test_quiet_enforcement.py"),

    ("podman is asked for its cgroup version in podman's own words",
     "app/update_checker.py",
     '            for fmt in ("{{.CgroupVersion}}", "{{.Host.CgroupsVersion}}"):',
     '            for fmt in ("{{.CgroupVersion}}",):',
     "test_cgroup_probe.py"),

    ("…and its leading v is not mistaken for a nonsense answer",
     "app/update_checker.py",
     '                out = (r.stdout or "").strip().lstrip("vV")',
     '                out = (r.stdout or "").strip()',
     "test_cgroup_probe.py"),

    ("a host that just booted still gets its first alert",
     "app/monitor.py",
     "            _last = self._last_sent.get((n, k))",
     "            _last = self._last_sent.get((n, k), 0)",
     "test_mass_stop.py"),

    ("…and so does a container that turns unhealthy right after boot",
     "app/monitor.py",
     "            last = self._last_sent.get(key)",
     "            last = self._last_sent.get(key, 0)",
     "test_boot_clock.py"),

    ("the first disk check runs on startup, however long the host has been up",
     "app/scheduler.py",
     "        last_disk_ts = None",
     "        last_disk_ts = 0.0",
     "test_boot_clock.py"),

    ("…and so does the first monitor pass",
     "app/scheduler.py",
     "        last_mon_ts = None",
     "        last_mon_ts = 0.0",
     "test_boot_clock.py"),

    ("a death right after boot still gets its memory picture taken",
     "app/event_watcher.py",
     '        self._recent_snap = (None, "")',
     '        self._recent_snap = (0.0, "")',
     "test_boot_clock.py"),

    ("a setting the range moves is reported, not silently rewritten",
     "app/config.py",
     "    got = max(lo, min(want, hi))\n    if got != want:",
     "    got = max(lo, min(want, hi))\n    if False:",
     "test_env_bounds.py"),

    ("…and the range the code enforces is the one the docs print",
     "docs/configuration.md",
     "before falling back to `docker kill` (1\u201386400).",
     "before falling back to `docker kill` (1\u20133600).",
     "test_env_bounds.py"),

    # A floor is a number too. `str(lo) in row` found the "1" of a new
    # floor inside the "100" of the old range, so this stayed green.
    ("\u2026including a floor, not only a ceiling",
     "docs/configuration.md",
     "exceeds this percentage (1\u2013100).",
     "exceeds this percentage (50\u2013100).",
     "test_env_bounds.py"),

    ("a missing container CLI is a skip, not a crash that hides every later check",
     "scripts/livefixture.py",
     "    try:\n        return subprocess.run([cli, \"info\"],\n                              capture_output=True).returncode == 0\n    except OSError:\n        return False",
     "    return subprocess.run([cli, \"info\"],\n                          capture_output=True).returncode == 0",
     "test_live_fixtures.py"),

    ("…and a window across midnight really is two intervals",
     "app/quiet_hours.py",
     "    # Wrap around midnight\n    return current >= start or current < end",
     "    # Wrap around midnight\n    return start <= current < end",
     "test_quiet_enforcement.py"),

    ("…but an urgent alert gets out of them anyway",
     "app/notifier.py",
     "        if not urgent and self._suppressed():",
     "        if self._suppressed():",
     "test_quiet_enforcement.py"),

    ("maintenance mode holds routine notifications back too",
     "app/notifier.py",
     "            if _maint_active(self.config):\n                return True",
     "            if False:\n                return True",
     "test_quiet_enforcement.py"),

    ("the SMTP password is never sent to an unverified server",
     "app/notifiers/smtp.py",
     "            ctx = ssl.create_default_context()",
     "            ctx = ssl._create_unverified_context()",
     "test_cron_dow.py"),

    ("a rate-limited alert is held, not thrown away",
     "app/notifiers/base.py",
     "TEMPORARY_STATUS = (408, 429, 502, 503, 504)",
     "TEMPORARY_STATUS = ()",
     "test_channel_backoff.py"),

    ("…and a refused one is not asked again every 30 seconds",
     "app/notifiers/base.py",
     "        return int(code) in TEMPORARY_STATUS",
     "        return True",
     "test_channel_backoff.py"),

    ("…and Telegram reads the same table, not only 429",
     "app/telegram_bot.py",
     "                temporary = retryable_status(e.code)",
     "                temporary = e.code == 429",
     "test_telegram_transient.py"),

    ("…while a burst of them does not fill the log one line per poll",
     "app/telegram_bot.py",
     "                if quiet_timeout and (self._is_timeout(e) or temporary):",
     "                if quiet_timeout and self._is_timeout(e):",
     "test_telegram_transient.py"),

    ("a held Telegram redelivery is one attempt, with no sleep on the scheduler thread",
     "app/telegram_bot.py",
     "        attempts = 1 if (quiet_timeout or once) else 3",
     "        attempts = 1 if quiet_timeout else 3",
     "test_telegram_transient.py"),

    ("…and the redelivery asks for exactly that",
     "app/telegram_bot.py",
     "            result = self.api_call(\"sendMessage\", data, timeout=timeout,\n"
     "                                   once=_held, answer=answer)",
     "            result = self.api_call(\"sendMessage\", data, timeout=timeout,\n"
     "                                   once=False, answer=answer)",
     "test_telegram_transient.py"),

    ("a 504 from Telegram is not retried inside the call",
     "app/telegram_bot.py",
     "                if temporary and e.code != 504 and attempt < attempts - 1:",
     "                if temporary and attempt < attempts - 1:",
     "test_telegram_transient.py"),

    ("…and the caller learns which status it was",
     "app/telegram_bot.py",
     "                        answer[\"code\"] = e.code",
     "                        pass",
     "test_telegram_transient.py"),

    ("…and a held message answered 504 again is not sent a third time",
     "app/telegram_bot.py",
     "        taken = _held and any(code == 504 for _p, code in unsent)",
     "        taken = False",
     "test_telegram_transient.py"),

    ("…which the retry queue is told as STOP, not as 'still down'",
     "app/telegram_bot.py",
     "            return notify_retry.STOP",
     "            return False",
     "test_telegram_transient.py"),

    ("a gateway's HTML error page is classified by its status",
     "app/telegram_bot.py",
     "                    if not temporary:",
     "                    if True:",
     "test_telegram_transient.py"),

    ("the poll summary does not call a status answer 'no answer'",
     "app/telegram_bot.py",
     "            if isinstance(exc, urllib.error.HTTPError):",
     "            if False:",
     "test_telegram_transient.py"),

    ("the send log does not call a status answer 'no answer from Telegram'",
     "app/telegram_bot.py",
     "                elif code:",
     "                elif False:",
     "test_telegram_transient.py"),

    ("a redelivered message does not blame the network",
     "app/notify_retry.py",
     "        return (f\"⏳ Delayed {_ago(now - held['at'])} — could not be \"",
     "        return (f\"⏳ Delayed {_ago(now - held['at'])} — no network, could not be \"",
     "test_telegram_transient.py"),

    ("the ssh hint is spoken in the reader's language",
     "app/hostdiag.py",
     "                return t(key, dir=ssh_dir)",
     "                return _EN[key].format(dir=ssh_dir)",
     "test_ssh_hint.py"),

    ("…and the scheduled check asks for it that way too",
     "app/scheduler.py",
     "            extra = hostdiag.hint(endpoint, err, t=self.bot.t)",
     "            extra = hostdiag.hint(endpoint, err)",
     "test_ssh_hint.py"),

    ("a container that is merely slow is not rolled back",
     "app/update_checker.py",
     '        return "starting", state, health',
     '        return "unhealthy", state, health',
     "test_health_outcomes.py"),

    ("…and a crash loop is not mistaken for a slow start",
     "app/update_checker.py",
     "            if restarts > baseline_restarts:",
     "            if False:",
     "test_health_outcomes.py"),

    ("docksentry.auto=false keeps a container manual under AUTO_UPDATE_ALL",
     "app/telegram_bot.py",
     "            if lab is not None:\n                return lab",
     "            if False:\n                return lab",
     "test_auto_label_rule.py"),

    ("a major version bump waits for confirmation",
     "app/update_engine.py",
     "            if auto and ask_this:",
     "            if False:",
     "test_debt_closers.py"),

    ("an image too fresh to trust is held back",
     "app/telegram_bot.py",
     "            if age is not None:",
     "            if False:",
     "test_debt_closers.py"),

    ("secrets are redacted before anything is written to the audit log",
     "app/audit.py",
     "        clean = redact(detail)",
     "        clean = detail",
     "test_audit.py"),

    ("an OOM kill is recognised as one",
     "app/monitor.py",
     '"oom": bool(state.get("OOMKilled"))',
     '"oom": False',
     "test_debt_closers.py"),

    ("a held notification is not kept past its usefulness",
     "app/notify_retry.py",
     "MAX_AGE_SECONDS = 900",
     "MAX_AGE_SECONDS = 999999",
     "test_notifier_retry.py"),

    ("only the registries named in the setting are treated as insecure",
     "app/update_checker.py",
     '            host, getattr(self.config, "insecure_registries", []))',
     "            host, [])",
     "test_debt_closers.py"),

    ("a failed pull stops the self-update before the swap",
     "app/selfupdate.py",
     "        ctx.send_message(msg)\n        return\n\n    # Check if image actually changed",
     "        ctx.send_message(msg)\n\n    # Check if image actually changed",
     "test_selfupdate_chain.py"),

    ("…and an identical image is not swapped onto itself",
     "app/selfupdate.py",
     "    if new_id == old_id:",
     "    if False:",
     "test_selfupdate_chain.py"),

    ("a hostile container name cannot reach the self-update shell",
     "app/selfupdate.py",
     "    qname = shlex.quote(name)",
     "    qname = name",
     "test_selfupdate_chain.py"),

    ("…and the boot report that names a failed recreate stays live",
     "app/main.py",
     '            if "rolling back" in _hcontent:',
     "            if False:",
     "test_selfupdate_chain.py"),

    ("…and `/selfupdate previous` finds the release below this one",
     "app/selfupdate.py",
     '    if target.lower() == "previous":',
     "    if False:",
     "test_selfupdate_chain.py"),

    ("the disk warning waits the documented twenty-three hours",
     "app/update_checker.py",
     "        if last_warn and (datetime.now() - last_warn) < timedelta(hours=23):",
     "        if last_warn and (datetime.now() - last_warn) < timedelta(hours=24):",
     "test_disk_warn_rules.py"),

    ("the batch really pauses between a group's containers",
     "app/update_engine.py",
     "                    _wait(wait_s, self)",
     "                    pass  # _wait(wait_s, self)",
     "test_group_rules.py"),

    ("a group member whose update raised still aborts the group",
     "app/update_engine.py",
     "                if cur_group:\n                    group_aborted.add(abort_key)",
     "                if False:\n                    group_aborted.add(abort_key)",
     "test_manual_update_gates.py"),

    ("a new password ends the old password's sessions",
     "app/web_ui.py",
     "                        sessions = self._sessions()\n"
     "                        if sessions is not None:\n"
     "                            sessions.clear()",
     "                        sessions = self._sessions()\n"
     "                        if False:\n"
     "                            sessions.clear()",
     "test_secret_handling.py"),

    ("settings.json is not left world-readable",
     "app/config.py",
     "            os.chmod(self.settings_file, 0o600)",
     "            pass  # os.chmod(self.settings_file, 0o600)",
     "test_secret_handling.py"),

    ("the settings page does not print the bot token",
     "app/web_ui.py",
     '            token_masked = f"{config.bot_token[:4]}...{config.bot_token[-4:]}" if len(config.bot_token) > 8 else "***"',
     "            token_masked = config.bot_token",
     "test_secret_handling.py"),

    ("cleanup only prunes images idle for the grace period",
     "app/update_checker.py",
     '                all=True, force=True, until=f"{grace}h", timeout=180)',
     "                all=True, force=True, timeout=180)",
     "test_cleanup_rules.py"),

    ("…and only deletes backups older than the retention window",
     "app/update_checker.py",
     "                if os.path.isdir(path) and os.path.getmtime(path) < cutoff:",
     "                if os.path.isdir(path):",
     "test_cleanup_rules.py"),

    ("…and a locally-built image is copied before it is pruned",
     "app/update_checker.py",
     "        if self.config.cleanup_backup_local_only:",
     "        if False:",
     "test_cleanup_rules.py"),

    ("…while an image that can be pulled again is not copied",
     "app/update_checker.py",
     "            if repo_digests:\n                continue",
     "            if False:\n                continue",
     "test_cleanup_rules.py"),

    ("a Web UI POST leaves a trace",
     "app/web_ui.py",
     "            self._audit_post(path)",
     "            pass  # self._audit_post(path)",
     "test_audit.py"),

    ("…and so does a Discord command",
     "app/discord_bot.py",
     '                audit.record("discord",',
     '                (lambda *a, **kw: None)(',
     "test_audit.py"),

    ("…and so does a Telegram button press",
     "app/telegram_bot.py",
     '                audit.record("telegram", user_id, f"button:{_btn}")',
     "                pass",
     "test_audit.py"),

    ("a Telegram command leaves a trace",
     "app/telegram_bot.py",
     "                    audit.record(\"telegram\", user_id, cmd.strip(),",
     "                    (lambda *a, **kw: None)(",
     "test_audit.py"),

    ("a token is recorded as a token, not as a person",
     "app/web_ui.py",
     '                return f"token:{token}", "api"',
     '                return "?", "web"',
     "test_audit.py"),

    ("the history page shows the trail",
     "app/web_ui.py",
     "            rows = audit.entries(100) if audit is not None else []",
     "            rows = []",
     "test_audit.py"),

    ("a logged value is a pointer, not a copy",
     "app/audit.py",
     "        elif len(text) > MAX_VALUE:",
     "        elif False:",
     "test_audit.py"),

    ("a server asking for an hour does not get it",
     "app/notifiers/base.py",
     "                wait = _ra.usable_wait(_retry_after(e), slept)",
     "                wait = _retry_after(e)",
     "test_channel_backoff.py"),

    ("…and the wait budget is for the send, not for each attempt",
     "app/retry_after.py",
     "    remaining = budget - float(already_slept or 0.0)",
     "    remaining = budget",
     "test_channel_backoff.py"),

    ("…and a negative Retry-After never reaches sleep()",
     "app/retry_after.py",
     "    if wait < 0:\n        return None",
     "    if False:\n        return None",
     "test_channel_backoff.py"),

    ("…and reads at most the cap, not the length the caller claims",
     "app/web_ui.py",
     "                raw = self.rfile.read(min(length, _cap + _envelope))",
     "                raw = self.rfile.read()",
     "test_backup_channels.py"),

    ("the Web door asks whether the file is a backup at all",
     "app/web_ui.py",
     '                if not isinstance(bundle, dict) or "schema_version" not in bundle:',
     "                if False:",
     "test_restore_parity.py"),

    ("only the person who dropped a bundle may restore it",
     "app/telegram_bot.py",
     '            if _rrec is not None and (not _rrec.get("user")\n'
     '                                      or _rrec["user"] != user_id):',
     "            if False:",
     "test_restore_parity.py"),

    # The deadline has no entry of its own, and that is deliberate.
    # `_sweep_restores()` (below) drops anything past the TTL before a
    # button is handled, and `_do_restore` keeps a second check for a
    # direct caller. Two guards covering each other means neither can
    # be removed on its own — good code, and an unfalsifiable ratchet
    # entry. The sweep is the one mutated.

    ("a refused press leaves the owner's buttons alone",
     "app/telegram_bot.py",
     '            _rtok = data.split(":", 1)[1]\n'
     '            _rrec = (getattr(self, "_pending_restores", {}) or {}).get(_rtok)',
     '            if msg_id and chat_id:\n'
     '                self.remove_buttons(chat_id, msg_id)\n'
     '            _rtok = data.split(":", 1)[1]\n'
     '            _rrec = (getattr(self, "_pending_restores", {}) or {}).get(_rtok)',
     "test_restore_parity.py"),

    ("…and cancelling one offer does not forget another",
     "app/telegram_bot.py",
     "            if _ctok:\n                _pend.pop(_ctok, None)",
     "            _pend.clear()",
     "test_restore_parity.py"),

    ("…and an expired offer is swept out of memory",
     "app/telegram_bot.py",
     '        self._sweep_restores()\n        data = callback.get("data", "")',
     '        data = callback.get("data", "")',
     "test_restore_parity.py"),

    ("the backup cap weighs the file, not the envelope around it",
     "app/web_ui.py",
     "                if len(body) > _cap:",
     "                if False and len(body) > _cap:",
     "test_backup_channels.py"),

    ("a body that is not a form is not read as one",
     "app/web_ui.py",
     '                if "application/x-www-form-urlencoded" in _ctype:',
     "                if True:",
     "test_audit.py"),

    ("apprise URLs are a secret like their four neighbours",
     "app/audit.py",
     '                "apprise")',
     "                )",
     "test_audit.py"),

    ("…and ssh refusing its own config is a refusal, not silence",
     "app/hostcheck.py",
     '            "permission or group", "insufficient privileges",',
     '            "permission or group", "insufficient privileges")\nX_UNUSED = (',
     "test_ssh_hint.py"),

    ("…and one that looked healthy before the wait ran out is accepted",
     "app/update_checker.py",
     "        if healthy_since is not None:\n"
     '            return "healthy", state, health',
     "        if False:\n"
     '            return "healthy", state, health',
     "test_health_outcomes.py"),

    ("a locally-built image is copied BEFORE the prune, not after",
     "app/update_checker.py",
     '        backup_msg = ""\n        if self.config.cleanup_backup_local_only:',
     '        backup_msg = ""\n        if False:',
     "test_cleanup_rules.py"),

    ("a major version bump is detected, not just acted on",
     "app/update_engine.py",
     "        return best_parsed[0] > cur[0], tag, best_tag",
     "        return False, tag, best_tag",
     "test_debt_closers.py"),

    ("a backup dropped on Discord has the same size ceiling",
     "app/discord_bot.py",
     "        if size > self.RESTORE_MAX_BYTES:",
     "        if False:",
     "test_restore_parity.py"),

    ("…and the same question of whether it is a backup at all",
     "app/discord_bot.py",
     '        if not isinstance(bundle, dict) or "schema_version" not in bundle:',
     "        if False:",
     "test_restore_parity.py"),

    ("a STOPped message is never logged as delivered",
     "app/notify_retry.py",
     "    def __bool__(self):\n        return False",
     "    def __bool__(self):\n        return True",
     "test_notifier_retry.py"),

    ("a host nobody measured is not called usable",
     "app/hostcheck.py",
     "    return bool(findings) and not any(kind in bad for kind, _p in findings)",
     "    return not any(kind in bad for kind, _p in findings)",
     "test_hostcheck.py"),

    ("a docker cli talking to podman is not reported as docker",
     "app/hostcheck.py",
     '        out.append(("engine", {"engine": "podman" if "podman" in low',
     '        out.append(("engine", {"engine": "docker" if "docker" in low',
     "test_hostcheck.py"),

    ("the pre-flight does not promise what it never tested",
     "app/lang/en.json",
     '"hostcheck_ready": "\u2022 Ready: `{endpoint}` can be read in full.',
     '"hostcheck_ready": "\u2022 Ready: `{endpoint}` is fully writable.',
     "test_hostcheck.py"),

    ("a capitalised or unknown SMTP_TLS never means plaintext",
     "app/notifiers/smtp.py",
     '        if _tls not in ("ssl", "starttls", "none"):',
     "        if False:",
     "test_cron_dow.py"),

    ("one message cannot make the retry queue grow past its cap",
     "app/notify_retry.py",
     "            while len(self._items) > self.max_items:\n                gone = self._items.pop(0)\n                dropped += 1",
     "            while False:\n                gone = self._items.pop(0)\n                dropped += 1",
     "test_notifier_retry.py"),

    ("a slow pre-flight does not stop the Telegram bot answering",
     "app/telegram_bot.py",
     "                threading.Thread(target=_probe, daemon=True).start()",
     "                _probe()",
     "test_hostcheck.py"),

    ("a failed update rolls back to the old container",
     "app/update_checker.py",
     "    def _rollback_to_old(self, name, old_name):\n",
     "    def _rollback_to_old(self, name, old_name):\n        return False  # MUTATION\n",
     "test_rollback.py"),

    ("…and never destroys the container when there is no backup",
     "app/update_checker.py",
     "        if not self._container_exists(old_name):",
     "        if False:",
     "test_rollback.py"),

    ("…removing the broken one forcibly, so a wedged container cannot block it",
     "app/update_checker.py",
     "            self.backend.rm(name, force=True,",
     "            self.backend.rm(name, force=False,",
     "test_rollback.py"),

    ("…and a restored-but-not-started container is not called a success",
     "app/update_checker.py",
     "            ok = start.returncode == 0",
     "            ok = True",
     "test_rollback.py"),

    ("the Web UI rejects a POST that did not come from its own page",
     "app/web_ui.py",
     "        def _check_csrf(self):\n",
     "        def _check_csrf(self):\n            return True  # MUTATION\n",
     "test_web_csrf.py"),

    ("…including one with no Origin and no Referer at all",
     "app/web_ui.py",
     "            source = origin or referer\n            if not source:\n                return False",
     "            source = origin or referer\n            if not source:\n                return True",
     "test_web_csrf.py"),

    ("…and the login form is checked too",
     "app/web_ui.py",
     '                if not self._check_csrf():\n                    return self._send_forbidden("CSRF check failed")',
     "                if False:\n                    return self._send_forbidden(\"CSRF check failed\")",
     "test_web_csrf.py"),

    ("a maintenance window holds an auto-update back",
     "app/telegram_bot.py",
     "        skipped_window = [u for u in auto_candidates\n                          if not is_window_open(_state_of(u)[1].get(u[\"name\"]))]",
     "        skipped_window = []",
     "test_update_window_enforcement.py"),

    ("…and its weekday list is not ignored",
     "app/update_window.py",
     "        if weekdays and current_wd not in weekdays:\n            return False",
     "        if False:\n            return False",
     "test_update_window_enforcement.py"),

    ("…and the small hours belong to the day the window opened on",
     "app/update_window.py",
     "            return not weekdays or yesterday_wd in weekdays",
     "            return not weekdays or current_wd in weekdays",
     "test_update_window_enforcement.py"),

    ("the Web UI asks for a password when one is set",
     "app/web_ui.py",
     "        def _check_auth(self):\n",
     "        def _check_auth(self):\n            return True  # MUTATION\n",
     "test_web_auth_gate.py"),

    ("…and reads it fresh, so setting one takes effect at once",
     "app/web_ui.py",
     '            current = getattr(config, "web_password", "") or ""\n            if not current:',
     '            current = ""\n            if not current:',
     "test_web_auth_gate.py"),

    ("the weekly report fires on its day and hour, not every tick",
     "app/weekly_report.py",
     "    if now.weekday() != target_wd:\n        return False",
     "    if False:\n        return False",
     "test_weekly_schedule.py"),

    ("…and only once a day",
     "app/weekly_report.py",
     "    if last == now.date().isoformat():\n        return False",
     "    if False:\n        return False",
     "test_weekly_schedule.py"),

    ("…and not at all when it is switched off",
     "app/weekly_report.py",
     '    if not getattr(config, "weekly_report_enabled", False):\n        return False',
     "    if False:\n        return False",
     "test_weekly_schedule.py"),

    ("a Web UI that stopped listening reports the container unhealthy",
     "app/healthcheck.py",
     "        if _check_web_ui(web_port):\n            sys.exit(0)",
     "        if True:\n            sys.exit(0)",
     "test_healthcheck_exit.py"),

    ("…and a webhook-only install is not reported unhealthy forever",
     "app/healthcheck.py",
     "    if headless_only:",
     "    if False:",
     "test_healthcheck_exit.py"),

    ("a --rm sidecar's config is read before it is stopped, not after",
     "app/update_checker.py",
     "        insp = self.backend.run([\"inspect\", name])\n        if insp.returncode != 0:\n            return False, \"inspect failed\"",
     "        self._stop_container(name)\n        insp = self.backend.run([\"inspect\", name])\n        if insp.returncode != 0:\n            return False, \"inspect failed\"",
     "test_netns_dependent_recreate.py"),

    ("the session cookie cannot be read by a script",
     "app/web_ui.py",
     'f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "',
     'f"{SESSION_COOKIE}={token}; Path=/; SameSite=Lax; "',
     "test_web_login.py"),

    ("…and is not sent on a cross-site request",
     "app/web_ui.py",
     'f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "',
     'f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; "',
     "test_web_login.py"),

    ("an API token is compared in constant time",
     "app/web_ui.py",
     "                if _hmac.compare_digest(token.strip(), supplied):",
     "                if token.strip() == supplied:",
     "test_metrics_api.py"),

    ("…and not by its prefix",
     "app/web_ui.py",
     "                if _hmac.compare_digest(token.strip(), supplied):",
     "                if supplied.startswith(token.strip()[:6]):",
     "test_metrics_api.py"),

    ("a compose recreate waits longer than the grace it grants",
     "app/update_checker.py",
     "        result = self.backend.run(up_cmd[1:], timeout=stop_grace + 120)",
     "        result = self.backend.run(up_cmd[1:], timeout=120)",
     "test_compose_recreate_budget.py"),

    ("a failed pull reports the end of what the tool said",
     "app/update_checker.py",
     '            msg = f"Compose pull failed: {clip(result.stderr)}"',
     '            msg = f"Compose pull failed: {result.stderr[:200]}"',
     "test_pull_failure_report.py"),

    ("…and the time a pull may take comes from the configuration",
     "app/update_checker.py",
     "        result = self.backend.run(pull_cmd[1:], timeout=pull_grace)",
     "        result = self.backend.run(pull_cmd[1:], timeout=1800)",
     "test_pull_failure_report.py"),

    ("…and still grants it",
     "app/update_checker.py",
     '                                 "--timeout", str(stop_grace), service]',
     "                                 service]",
     "test_compose_recreate_budget.py"),

    ("the bot forwards the policy question to the engine",
     "app/telegram_bot.py",
     "    def _policy_decision(self, *a, **k):\n        return self.engine._policy_decision(*a, **k)",
     "    def _policy_decision(self, *a, **k):\n        return None",
     "test_update_policy.py"),

    ("tapping the same update twice does not produce a bogus warning",
     "app/update_engine.py",
     "        pos = (None if container_key in self.updating\n",
     "        pos = (None if False\n",
     "test_update_queue_duplicate.py"),

    # Q7: the engine claims or queues; a connection only translates.
    ("a tap on a free lock claims it in the engine",
     "app/update_engine.py",
     "        if self.claim_update():\n            return True, None, {}\n",
     "        if False:\n            return True, None, {}\n",
     "test_update_queue.py"),

    ("a request for several containers takes one claim, and queues nothing",
     "app/update_engine.py",
     "        if self.claim_update():\n            return True, []\n",
     "        if False:\n            return True, []\n",
     "test_update_queue.py"),

    ("a batch goes on the queue whole or not at all",
     "app/update_engine.py",
     "            if len(self._update_queue) + len(new) > self.UPDATE_QUEUE_MAX:\n",
     "            if False:\n",
     "test_update_queue.py"),

    ("…and one that fits exactly goes in",
     "app/update_engine.py",
     "            if len(self._update_queue) + len(new) > self.UPDATE_QUEUE_MAX:\n",
     "            if len(self._update_queue) + len(new) >= self.UPDATE_QUEUE_MAX:\n",
     "test_update_queue.py"),

    ("…in the order it was asked for",
     "app/update_engine.py",
     "                self._update_queue.append(k)\n",
     "                self._update_queue.insert(0, k)\n",
     "test_update_queue.py"),

    ("…and each key of it knows since when it waits",
     "app/update_engine.py",
     "                self._queued_at[k] = now\n",
     "                pass\n",
     "test_update_queue.py"),

    ("…and the container being updated is not queued behind itself",
     "app/update_engine.py",
     "        running = self.updating\n",
     "        running = {}\n",
     "test_update_queue.py"),

    ("…and a batch that does not fit is answered as a whole",
     "app/update_engine.py",
     "        if -1 in positions.values():\n",
     "        if False:\n",
     "test_update_queue.py"),

    ("'already queued' hands over raw seconds, not a rendered time",
     "app/update_engine.py",
     '                "since_seconds": waited if waited is not None else 0}\n',
     '                "since_seconds": 0}\n',
     "test_update_queue.py"),

    ("every busy answer carries what its sentence needs",
     "app/update_engine.py",
     '            return "update_queue_full", {"max": self.UPDATE_QUEUE_MAX}\n',
     '            return "update_queue_full", {}\n',
     "test_reply_params.py"),

    ("Telegram's tap asks the engine instead of deciding itself",
     "app/telegram_bot.py",
     "            claimed, busy, params = self.engine.claim_or_queue(container_key)\n",
     "            claimed, busy, params = (self.engine.claim_update(), \"update_queued\", {\"pos\": 1})\n",
     "test_update_queue.py"),

    ("…renders how long an entry has waited",
     "app/telegram_bot.py",
     '            kw["since"] = _ago(kw.pop("since_seconds"))\n',
     '            kw["since"] = kw.pop("since_seconds")\n',
     "test_update_queue.py"),

    ("…and names the container, host included",
     "app/telegram_bot.py",
     "        return self.t(key, name=self._key_label(container_key), **kw)\n",
     "        return self.t(key, **kw)\n",
     "test_update_queue.py"),

    ("the lock-site list sees a claim through claim_or_queue",
     "scripts/test_update_lock_sites.py",
     "                    elif c in DOOR_QUEUE:\n",
     "                    elif False:\n",
     "test_update_lock_sites.py"),

    ("…and a batch feeding the queue",
     "scripts/test_update_lock_sites.py",
     'QUEUE_FEEDERS = ("enqueue_update", "enqueue_updates")\n',
     'QUEUE_FEEDERS = ("enqueue_update",)\n',
     "test_update_lock_sites.py"),

    # The single update's body is the engine's run_keys since Q11a;
    # Telegram keeps the sentences.
    ("…which needs the single-update path to say what it is doing",
     "app/update_engine.py",
     "                self.mark_updating(key, target.get(\"new_version\") or \"\")\n",
     "                pass  # MUTATION: nothing says what is running\n",
     "test_update_queue_duplicate.py"),

    ("…and finished work is not called a mistake",
     "app/telegram_bot.py",
     '            "update_already_done" if from_queue else "container_not_in_list",',
     '            "container_not_in_list",',
     "test_update_queue_duplicate.py"),

    # Q11a: run_keys runs the single update; Telegram says how it went.
    # Each of these goes red in test_single_update_report, which drives
    # the real tap and drain with a recording notifier and Broadcast.
    ("a single update is reported to the other channels",
     "app/telegram_bot.py",
     "        if self.notifier:\n            self.notifier.send_update_result(\n                split_host_key(container_key)[1],",
     "        if False:\n            self.notifier.send_update_result(\n                split_host_key(container_key)[1],",
     "test_single_update_report.py"),

    ("…and said on Telegram",
     "app/telegram_bot.py",
     '        self.send_message(f"{status} {self._display_name(target)}: {msg}")\n',
     "        pass  # MUTATION: no result line\n",
     "test_single_update_report.py"),

    ("…each of them once, not twice",
     "app/update_engine.py",
     "                    if report is not None:\n                        report(key, target, success, msg)\n",
     "                    if report is not None:\n                        report(key, target, success, msg)\n                        report(key, target, success, msg)\n",
     "test_single_update_report.py"),

    ("…an update that raises is reported, as a failure",
     "app/update_engine.py",
     "                    if report is not None:\n                        report(key, target, False, msg)\n",
     "                    pass  # MUTATION: a crash goes unreported\n",
     "test_single_update_report.py"),

    ("…and the report does not wait for the pending file to be rewritten",
     "app/update_engine.py",
     "                    last_group = None if success else group_of(key)\n                    if report is not None:\n                        report(key, target, success, msg)\n",
     "                    last_group = None if success else group_of(key)\n                    _pending.write(pending_file, [u for u in updates if _pending.key_of(u) != (host_name, name)])\n                    if report is not None:\n                        report(key, target, success, msg)\n",
     "test_single_update_report.py"),

    ("'Updating …' is said before the single update runs, not after it",
     "app/update_engine.py",
     "                if starting is not None:\n                    starting(key, target)\n                try:\n                    compose_kwargs = {k: target[k] for k in target\n                                      if k.startswith(\"compose_\")}\n                    success, msg = checker_for(key).update_container(\n                        target[\"name\"], target[\"image\"], **compose_kwargs)\n",
     "                try:\n                    compose_kwargs = {k: target[k] for k in target\n                                      if k.startswith(\"compose_\")}\n                    success, msg = checker_for(key).update_container(\n                        target[\"name\"], target[\"image\"], **compose_kwargs)\n                    if starting is not None:\n                        starting(key, target)\n",
     "test_single_update_report.py"),

    ("a failed single update names the group it takes out of the queue",
     "app/update_engine.py",
     "                    last_group = None if success else group_of(key)\n",
     "                    last_group = None\n",
     "test_single_update_report.py"),

    ("the single update stops saying a container is in flight once done",
     "app/update_engine.py",
     "            finally:\n                self.clear_updating(key)\n",
     "            finally:\n                pass\n",
     "test_update_queue_duplicate.py"),

    ("'all done' after the single update that took the last entry",
     "app/update_engine.py",
     "        return results, (written and not updates)\n",
     "        return results, False\n",
     "test_single_update_report.py"),

    ("…and not while another entry is still pending",
     "app/update_engine.py",
     "        return results, (written and not updates)\n",
     "        return results, written\n",
     "test_single_update_report.py"),

    ("Telegram hands the engine the tapped key's own checker",
     "app/telegram_bot.py",
     "                checker_for=lambda key: checker,\n",
     "                checker_for=lambda key: base_checker,\n",
     "test_telegram_callback_matrix.py"),

    ("…and its own link lookup, so the result line carries the link",
     "app/telegram_bot.py",
     "                enrich=self._enrich_with_source_url,\n",
     "                enrich=lambda updates: None,\n",
     "test_telegram_callback_matrix.py"),

    ("a server that answers after the body is believed over a guess",
     "app/notifiers/smtp.py",
     "    code = _smtp_code(err)\n    if code is not None:\n        return TEMPORARY if 400 <= code <= 499 else PERMANENT\n    if taken:",
     "    if taken:\n        return TAKEN\n    code = _smtp_code(err)\n    if code is not None:\n        return TEMPORARY if 400 <= code <= 499 else PERMANENT\n    if taken:",
     "test_smtp_resilience.py"),

    ("…and mixed recipient codes follow the recoverable one",
     "app/notifiers/smtp.py",
     "            return min(codes)",
     "            return max(codes)",
     "test_smtp_resilience.py"),

    ("who refused decides, not which scheme was typed",
     "app/hostcheck.py",
     "        elif _ssh_refused(why):",
     '        elif ep.startswith("ssh://"):',
     "test_hostcheck.py"),

    ("…and a daemon refusal over ssh is still the socket's",
     "app/hostcheck.py",
     '    if "docker daemon socket" in low or "unix://" in low:\n        return False            # the far side\'s socket, over ssh\n    return any(k in low for k in _SSH_REFUSED)',
     "    return True",
     "test_hostcheck.py"),

    ("a very long endpoint does not split the two chats",
     "app/telegram_bot.py",
     "        endpoint = hostcheck.short_endpoint(endpoint)",
     "        endpoint = endpoint",
     "test_hostcheck.py"),

    ("an empty SMTP_TLS means STARTTLS, as it does everywhere else",
     "app/notifiers/smtp.py",
     '        _tls = (c.smtp_tls or "").strip().lower() or "starttls"',
     '        _tls = (c.smtp_tls or "none").strip().lower()',
     "test_cron_dow.py"),

    ("a weekday list that cannot be read does not mean every day",
     "app/update_window.py",
     "    if _raw and not weekdays:\n        return False",
     "    if False:\n        return False",
     "test_update_window_enforcement.py"),

    ("a revoked API token is rejected, not passed to the password check",
     "app/web_ui.py",
     "                if self._api_token_supplied():",
     "                if False:",
     "test_metrics_api.py"),

    ("the Web UI's session cookie keeps its path and its lifetime",
     "app/web_ui.py",
     '            max_age = int(getattr(config, "web_session_hours", 8)) * 3600',
     "            max_age = 0",
     "test_web_login.py"),

    ("a scheme that is not Basic does not authenticate",
     "app/web_ui.py",
     '            if not auth.startswith("Basic "):',
     "            if not auth:",
     "test_web_auth_gate.py"),

    ("a sidecar rejoins the head's current container, not its dead one",
     "app/update_checker.py",
     "                                   netns_name=netns_name,\n                                   inherited=self._image_config(config.get(\"Image\")),",
     "                                   netns_name=None,\n                                   inherited=self._image_config(config.get(\"Image\")),",
     "test_netns_dependent_recreate.py"),

    ("the interface does not claim STARTTLS for a value that is not valid",
     "app/web_ui.py",
     '            if _tls not in ("starttls", "ssl", "none"):\n                smtp_tls_options = (',
     "            if False:\n                smtp_tls_options = (",
     "test_cron_dow.py"),

    ("a test send leaves nothing in the retry queue",
     "app/web_ui.py",
     "                        _held = _nr.queue.drop_since(_mark)",
     "                        _held = 0",
     "test_channel_test_button.py"),

    ("…and says a real alert would have been held, not just 'failed'",
     "app/web_ui.py",
     "                        if _held:",
     "                        if False:",
     "test_channel_test_button.py"),

    ("the certificate warning names what is actually at risk",
     "app/notifiers/smtp.py",
     '        _has_password = bool(c.smtp_user and c.smtp_password)',
     "        _has_password = True",
     "test_cron_dow.py"),

    ("/metrics reports that the program is up",
     "app/web_ui.py",
     '                "docksentry_up 1",',
     "",
     "test_machine_endpoints.py"),

    ("…and counts the containers it is watching",
     "app/web_ui.py",
     "                f\"docksentry_containers {st['containers']}\",",
     '                "docksentry_containers 0",',
     "test_machine_endpoints.py"),

    ("…and the updates that are pending",
     "app/web_ui.py",
     "docksentry_updates_pending {st['pending']}",
     "docksentry_x_pending {st['pending']}",
     "test_machine_endpoints.py"),

    ("…and says which hosts answered",
     "app/web_ui.py",
     "                lines.append(f'docksentry_host_up{{host=\"{_metric_label(host)}\"}} {up}')",
     "                pass",
     "test_machine_endpoints.py"),

    ("…as Prometheus text, not as a web page",
     "app/web_ui.py",
     '"text/plain; version=0.0.4; charset=utf-8"',
     '"text/html"',
     "test_machine_endpoints.py"),

    ("the JSON endpoint reports the same numbers",
     "app/web_ui.py",
     '                "containers": st["containers"],',
     '                "containers": 0,',
     "test_machine_endpoints.py"),

    ("…under the names it documents",
     "app/web_ui.py",
     '                "updates_pending": st["pending"],',
     '                "pending_updates": st["pending"],',
     "test_machine_endpoints.py"),

    ("a container name cannot break the metrics format",
     "app/web_ui.py",
     """            .replace('"', '\\\\"')\n""",
     "",
     "test_machine_endpoints.py"),

    ("a self-update clears the old backup before it starts",
     "app/selfupdate.py",
     'f"docker rm -f {qname}_old >/dev/null 2>&1; "',
     '""',
     "test_selfupdate_chain.py"),

    ("…stops itself with the configured grace, not Docker's ten seconds",
     "app/selfupdate.py",
     "docker stop -t {stop_timeout} {qname}",
     "docker stop {qname}",
     "test_selfupdate_chain.py"),

    ("…restarts the container it still has if the rename fails",
     "app/selfupdate.py",
     "docker start {qname}; exit 1",
     "exit 1",
     "test_selfupdate_chain.py"),

    ("…rolls back if the new container will not run",
     "app/selfupdate.py",
     "'Selfupdate recreate failed — rolling back'",
     "'Selfupdate recreate failed'",
     "test_selfupdate_chain.py"),

    ("…and removes the backup only once the new one is up",
     "app/selfupdate.py",
     'f"docker rm {qname}_old"',
     'f"true"',
     "test_selfupdate_chain.py"),

    ("the next boot reports a self-update that rolled back",
     "app/main.py",
     '            if "rolling back" in _hcontent:',
     '            if "xx-never-matches" in _hcontent:',
     "test_selfupdate_chain.py"),

    ("an unattended update leaves a record too",
     "app/telegram_bot.py",
     '                        _audit.record(\n                            "schedule", "system", "auto-update",',
     '                        pass  # MUTATION\n                        _noop = (\n                            "schedule", "system", "auto-update",',
     "test_audit.py"),

    ("a declared gigabyte is refused before the body is read",
     "app/web_ui.py",
     "                if length > _cap + _envelope:",
     "                if False:",
     "test_backup_channels.py"),

    ("the disk warning fires at the configured threshold",
     "app/update_checker.py",
     "        if percent < threshold:",
     "        if False:",
     "test_disk_warn_rules.py"),

    ("…and says it once a day, not once a tick",
     "app/update_checker.py",
     "        if last_warn and (datetime.now() - last_warn) < timedelta(hours=23):",
     "        if False:",
     "test_disk_warn_rules.py"),

    ("…and the settings page cannot store a threshold that never fires",
     "app/web_ui.py",
     "                        config.disk_warn_percent = _in_range(\"disk_warn_percent\", v)",
     "                        config.disk_warn_percent = v",
     "test_disk_warn_rules.py"),

    # The Settings form against the environment (18.11.2026). from_env()
    # had been widened, the form kept 3600 and 50, and a value the
    # environment accepts made the whole form refuse to submit.
    ("the Settings form admits every stop timeout the environment admits",
     "app/web_ui.py",
     'name="docker_stop_timeout" value="{_e(config.docker_stop_timeout)}" {_range_attrs("docker_stop_timeout")}',
     'name="docker_stop_timeout" value="{_e(config.docker_stop_timeout)}" min="1" max="3600"',
     "test_settings_ranges.py"),

    ("…and every healthcheck grace",
     "app/web_ui.py",
     'name="healthcheck_max_starting" value="{_e(config.healthcheck_max_starting)}" {_range_attrs("healthcheck_max_starting")}',
     'name="healthcheck_max_starting" value="{_e(config.healthcheck_max_starting)}" min="30" max="3600"',
     "test_settings_ranges.py"),

    ("…and every disk-warning threshold",
     "app/web_ui.py",
     'name="disk_warn_percent" value="{_e(config.disk_warn_percent)}" {_range_attrs("disk_warn_percent")}',
     'name="disk_warn_percent" value="{_e(config.disk_warn_percent)}" min="50" max="100"',
     "test_settings_ranges.py"),

    ("saving the Settings page keeps a long stop timeout as it is",
     "app/web_ui.py",
     '                        config.docker_stop_timeout = _in_range("docker_stop_timeout", v)',
     "                        config.docker_stop_timeout = max(1, min(v, 3600))",
     "test_settings_ranges.py"),

    ("…and a long healthcheck grace",
     "app/web_ui.py",
     '                        config.healthcheck_max_starting = _in_range("healthcheck_max_starting", v)',
     "                        config.healthcheck_max_starting = max(30, min(v, 3600))",
     "test_settings_ranges.py"),

    ("…and a disk-warning threshold below 50",
     "app/web_ui.py",
     '                        config.disk_warn_percent = _in_range("disk_warn_percent", v)',
     "                        config.disk_warn_percent = max(50, min(v, 100))",
     "test_settings_ranges.py"),

    ("every number field on the settings pages has the environment's bounds",
     "app/web_ui.py",
     "    return f'min=\"{lo}\" max=\"{hi}\"'",
     "    return f'min=\"{lo}\" max=\"{min(hi, 3600)}\"'",
     "test_settings_ranges.py"),

    ("…and every save of one is clamped to them, not tighter",
     "app/web_ui.py",
     "    return max(lo, min(v, hi))",
     "    return max(lo, min(v, hi, 3600))",
     "test_settings_ranges.py"),

    ("the environment keeps the long stop timeout it documents",
     "app/config.py",
     '    "docker_stop_timeout": (1, 86400),',
     '    "docker_stop_timeout": (1, 3600),',
     "test_env_bounds.py"),

    ("…and the long healthcheck grace",
     "app/config.py",
     '    "healthcheck_max_starting": (30, 86400),',
     '    "healthcheck_max_starting": (30, 3600),',
     "test_env_bounds.py"),

    ("…and the disk-warning floor of 1",
     "app/config.py",
     '    "disk_warn_percent": (1, 100),',
     '    "disk_warn_percent": (50, 100),',
     "test_env_bounds.py"),

    ("a group pauses between its containers",
     "app/update_engine.py",
     "                if wait_s > 0:\n                    _wait(wait_s, self)",
     "                pass",
     "test_group_rules.py"),

    ("…and a dependent restart never waits less than thirty seconds",
     "app/update_engine.py",
     '                            wait_s = max(int(grp.get("wait_seconds", 30) or 30), 30)',
     '                            wait_s = int(grp.get("wait_seconds", 30) or 30)',
     "test_group_rules.py"),

    ("a container belongs to one group, not two",
     "app/container_store.py",
     '            other["containers"] = [c for c in (other.get("containers") or []) if c not in cleaned]',
     "            pass",
     "test_group_rules.py"),

    ("…and a group's wait cannot be absurd",
     "app/container_store.py",
     "            wait = max(0, min(int(wait_seconds), 600))",
     "            wait = int(wait_seconds)",
     "test_group_rules.py"),

    ("the Discord bot channel holds a 'not now' like every other channel",
     "app/notifiers/discordbot.py",
     "            temporary = e.status == 0 or retryable_status(e.status)",
     "            temporary = e.status == 0",
     "test_channel_backoff.py"),

    ("…and still holds when nothing answered at all",
     "app/notifiers/discordbot.py",
     "            temporary = e.status == 0 or retryable_status(e.status)",
     "            temporary = retryable_status(e.status)",
     "test_channel_backoff.py"),

    ("…and does not hold a refusal",
     "app/notifiers/discordbot.py",
     "            temporary = e.status == 0 or retryable_status(e.status)",
     "            temporary = True",
     "test_channel_backoff.py"),

    ("Apprise does not send a temporary answer to the settings",
     "app/notifiers/apprise.py",
     "                     if retryable_status(e.code)\n",
     "                     if False\n",
     "test_channel_backoff.py"),

    ("…while a refusal still points at APPRISE_URL",
     "app/notifiers/apprise.py",
     "                     if e.code < 500 else \"\")",
     "                     if False else \"\")",
     "test_channel_backoff.py"),

    ("a new channel cannot be added without its hold check",
     "app/notifiers/ntfy.py",
     "class NtfyNotifier(BaseNotifier):",
     'class PagerNotifier(BaseNotifier):\n    name = "pager"\n\n\n'
     "class NtfyNotifier(BaseNotifier):",
     "test_channel_backoff.py"),

    # A failed cgroup probe is not remembered as an answer (18.11.2026).
    ('a cgroup probe that failed is asked again on the next recreate',
     'app/update_checker.py',
     '            if version is None:\n                return "1"',
     '            if version is None:\n                version = "1"',
     'test_cgroup_probe.py'),

    # An unreadable pin list means every container is pinned, on every
    # update path and not only in `check_all`; and a check that could not
    # look says so instead of "up to date" (18.11.2026).

    ('the container listing says when the pin list could not be read',
     'app/update_checker.py',
     '        containers.pin_list_unreadable = pinned is EVERYTHING_PINNED',
     '        containers.pin_list_unreadable = False',
     'test_pin_fail_closed.py'),

    ('every manual update path reads the pin list the way the check does',
     'app/update_checker.py',
     '    return read() is EVERYTHING_PINNED',
     '    return False',
     'test_pin_unreadable_paths.py'),

    ('the Telegram update button does nothing while the pin list is unreadable',
     'app/telegram_bot.py',
     '        # entry where it is (see `pin_list_unreadable`).\n        from update_checker import pin_list_unreadable\n        if pin_list_unreadable(checker):',
     '        # entry where it is (see `pin_list_unreadable`).\n        from update_checker import pin_list_unreadable\n        if False:',
     'test_pin_unreadable_paths.py'),

    ('…and neither does the Telegram major confirmation',
     'app/telegram_bot.py',
     '            self.send_message(self.t("major_none_pending", name=name))\n            return\n        from update_checker import pin_list_unreadable\n        if pin_list_unreadable(checker):',
     '            self.send_message(self.t("major_none_pending", name=name))\n            return\n        from update_checker import pin_list_unreadable\n        if False:',
     'test_pin_unreadable_paths.py'),

    ('…nor Telegram "Update all"',
     'app/telegram_bot.py',
     '        if pin_list_unreadable(updater):',
     '        if False:',
     'test_pin_unreadable_paths.py'),

    ('…nor Telegram /update <name>, which says why instead of "no match"',
     'app/telegram_bot.py',
     '                if getattr(updates, "pin_list_unreadable", False):\n                    pins_unreadable = True',
     '                if False:\n                    pins_unreadable = True',
     'test_pin_unreadable_paths.py'),

    ('/check says nothing was checked when the pin list is unreadable',
     'app/telegram_bot.py',
     '            if getattr(updates, "pin_list_unreadable", False):\n                if not pins_said:\n                    self.send_message(self.t("check_pin_list_unreadable"))',
     '            if False:\n                if not pins_said:\n                    self.send_message(self.t("check_pin_list_unreadable"))',
     'test_pin_unreadable_paths.py'),

    ('…and so does /check --dry-run',
     'app/telegram_bot.py',
     '                if getattr(updates, "pin_list_unreadable", False):\n                    if not pins_said:\n                        self.send_message(\n                            self.t("check_pin_list_unreadable"))\n                        pins_said = True\n                    return\n                if updates:\n                    found = True\n                    self.send_message(self._build_dry_run(updates,',
     '                if False:\n                    if not pins_said:\n                        self.send_message(\n                            self.t("check_pin_list_unreadable"))\n                        pins_said = True\n                    return\n                if updates:\n                    found = True\n                    self.send_message(self._build_dry_run(updates,',
     'test_pin_unreadable_paths.py'),

    ('…and /check <name>',
     'app/telegram_bot.py',
     '                if getattr(updates, "pin_list_unreadable", False):\n                    if not pins_said:\n                        self.send_message(\n                            self.t("check_pin_list_unreadable"))\n                        pins_said = True\n                    return\n                if updates:\n                    found = True\n                    self.notify_updates(updates)\n            container_flags.check_on_hosts(',
     '                if False:\n                    if not pins_said:\n                        self.send_message(\n                            self.t("check_pin_list_unreadable"))\n                        pins_said = True\n                    return\n                if updates:\n                    found = True\n                    self.notify_updates(updates)\n            container_flags.check_on_hosts(',
     'test_pin_unreadable_paths.py'),

    ('Discord /check says nothing was checked when the pin list is unreadable',
     'app/discord_bot.py',
     '            if getattr(hc.updates, "pin_list_unreadable", False):\n                pins_unreadable = True',
     '            if False:\n                pins_unreadable = True',
     'test_pin_unreadable_paths.py'),

    ('Discord /update does nothing while the pin list is unreadable',
     'app/discord_bot.py',
     '        if any(pin_list_unreadable(self._checker_for(h)) for h, _t in jobs):',
     '        if False:',
     'test_pin_unreadable_paths.py'),

    ('…Discord /updateall does not even ask',
     'app/discord_bot.py',
     '        if any(pin_list_unreadable(self._checker_for(h)) for h in targets):',
     '        if False:',
     'test_pin_unreadable_paths.py'),

    ('…and its press is refused if the list broke while it waited',
     'app/discord_bot.py',
     '        if any(pin_list_unreadable(self._checker_for(h)) for h, _e in batches):',
     '        if False:',
     'test_pin_unreadable_paths.py'),

    ('the Web UI update button starts nothing while the pin list is unreadable',
     'app/web_ui.py',
     '                elif _pins_unreadable_for([key]):',
     '                elif False:',
     'test_pin_unreadable_paths.py'),

    ('…nor does the Web UI major confirmation',
     'app/web_ui.py',
     '                        and _pins_unreadable_for([raw])):',
     '                        and False):',
     'test_pin_unreadable_paths.py'),

    ('…nor the Web UI bulk update',
     'app/web_ui.py',
     '                if action == "update" and keys and _pins_unreadable_for(keys):',
     '                if False:',
     'test_pin_unreadable_paths.py'),

    ('…nor the Web UI update worker behind them',
     'app/web_ui.py',
     '            if any(pin_list_unreadable(hc) for _h, hc, _e in batches):',
     '            if False:',
     'test_pin_unreadable_paths.py'),

    ('…and the status page shows why',
     'app/web_ui.py',
     '            if _upd in ("started", "busy", "gone", "pinlist"):',
     '            if _upd in ("started", "busy", "gone"):',
     'test_pin_unreadable_paths.py'),

    ('the Web UI row check is not "up to date" when nothing was checked',
     'app/web_ui.py',
     '                    if getattr(updates, "pin_list_unreadable", False):\n                        # Not "up to date": nothing was checked.',
     '                    if False:\n                        # Not "up to date": nothing was checked.',
     'test_pin_unreadable_paths.py'),

    # The three recreate paths hand the probe's answer to
    # `_build_run_args`. Each of these went green with `None` in its
    # place until test_cgroup_probe drove the paths themselves.
    ('a dependent recreate is built for the host\'s cgroup version',
     'app/update_checker.py',
     '                                   inherited=self._image_config(config.get("Image")),\n'
     '                                   cgroup_version=self._cgroup_version(self.backend))',
     '                                   inherited=self._image_config(config.get("Image")),\n'
     '                                   cgroup_version=None)',
     'test_cgroup_probe.py'),

    ('…and so is a standalone recreate',
     'app/update_checker.py',
     '                                       inherited=self._image_config(config.get("Image")),\n'
     '                                       cgroup_version=self._cgroup_version(self.backend))',
     '                                       inherited=self._image_config(config.get("Image")),\n'
     '                                       cgroup_version=None)',
     'test_cgroup_probe.py'),

    ('…and one whose container vanished when it stopped',
     'app/update_checker.py',
     '                    cgroup_version=self._cgroup_version(self.backend),\n',
     '                    cgroup_version=None,\n',
     'test_cgroup_probe.py'),

    ('a backend that raises during the cgroup probe does not take the '
     'recreate down',
     'app/update_checker.py',
     '                except (subprocess.SubprocessError, OSError):\n'
     '                    break\n'
     '                if r.returncode != 0:',
     '                except ():\n'
     '                    break\n'
     '                if r.returncode != 0:',
     'test_cgroup_probe.py'),

    ('another host\'s pending updates survive this host\'s scan',
     'app/update_checker.py',
     '    others = [u for u in prev if host_of(u) != host_name]',
     '    others = []',
     'test_pin_fail_closed.py'),

    ('/stop on Telegram asks before it stops anything',
     'app/telegram_bot.py',
     '            if action != "stop":\n'
     '                self._emit(lifecycle.act(action, targets, partial=arg,',
     '            if True:\n'
     '                self._emit(lifecycle.act(action, targets, partial=arg,',
     'test_stop_confirms.py'),

    ('a Discord 404 that is not the webhook being gone keeps a private '
     'answer private',
     'app/discord_bot.py',
     '        return code in (10015, 50027)',
     '        return True',
     'test_discord_bot.py'),

    ('…and so does a 429 or a 500',
     'app/discord_bot.py',
     '        if status != 404:\n            return False',
     '        if status != 404:\n            return True',
     'test_discord_bot.py'),

    ("maintenance mode stops the scheduled run: no self-update, no "
     "auto-update",
     "app/scheduler.py",
     "                if _maint_active(self.config):",
     "                if False:",
     "test_scheduler_maintenance.py"),

    ("…and keeps the monitor quiet while it lasts",
     "app/scheduler.py",
     "                            and not _maint_active2(self.config)):",
     "                            ):",
     "test_scheduler_maintenance.py"),

    ("a scheduled auto-update touches nothing while a manual update holds "
     "the lock",
     "app/telegram_bot.py",
     '                      "(will retry next tick)")\n'
     '                auto_updates = []',
     '                      "(will retry next tick)")',
     "test_auto_update_lock.py"),

    ("…because it claims the lock instead of assuming it is free",
     "app/telegram_bot.py",
     "            if self.engine.claim_update():\n"
     "                auto_lock_held = True",
     "            if True:\n"
     "                auto_lock_held = False",
     "test_auto_update_lock.py"),

    ("an update aimed at Docksentry's own container stops nothing, "
     "whoever asks",
     "app/update_checker.py",
     "        if self._would_kill_self(name):",
     "        if False:",
     "test_self_kill_backstop.py"),

    ("the weekly report reaches Telegram during quiet hours",
     "app/weekly_report.py",
     "            bot.send_message(text, auto=False)",
     "            bot.send_message(text, auto=True)",
     "test_weekly_report_channels.py"),

    ("leftover _old containers are named on the Status page, with the "
     "command to remove them",
     "app/web_ui.py",
     '<pre>docker rm {_e(" ".join(leftovers))}</pre>\n',
     "",
     "test_leftover_not_during_update.py"),

    ("…and rendering that page removes none of them",
     "app/web_ui.py",
     "            leftovers = sorted(set(leftovers))",
     "            leftovers = sorted(set(leftovers))\n"
     "            for _v in views:\n"
     '                if _v.get("backend") is not None and leftovers:\n'
     '                    _v["backend"].rm(leftovers, force=True, timeout=10)',
     "test_leftover_not_during_update.py"),

    ("…and neither does a cleanup run",
     "app/update_checker.py",
     "            result = self.backend.image_prune(",
     '            _ps = self.backend.ps(all=True, fmt="{{.Names}}", timeout=10)\n'
     '            _stale = [n for n in (_ps.stdout or "").split()\n'
     '                      if n.endswith("_old")]\n'
     "            if _stale:\n"
     "                self.backend.rm(_stale, force=True, timeout=30)\n"
     "            result = self.backend.image_prune(",
     "test_leftover_not_during_update.py"),

    # ── the run log, the release gate, the ratchet's own copy ─────────
    ("a procedure that fails without a word still shows its exit code",
     "scripts/verify.py",
     '            tail = "\\n".join(lines[-12:] + [_ended(r.returncode, lines)])',
     '            tail = "\\n".join(lines[-12:])',
     "test_suite_runner.py"),

    ("the report does not say the suite stays out of CI",
     "scripts/verify.py",
     '    L.append("> CI runs the suite on every push to main and before an "',
     '    L.append("> The suite runs locally, not in CI. "\n'
     '             "CI runs the suite on every push to main and before an "',
     "test_suite_runner.py"),

    ("a tag on a red tree is not published",
     ".github/workflows/docker-publish.yml",
     "    needs: suite\n",
     "",
     "test_release_tag_guard.py"),

    ("…and the job the build waits for really runs the suite",
     ".github/workflows/docker-publish.yml",
     "        run: python3 scripts/verify.py\n",
     '        run: "true"\n',
     "test_release_tag_guard.py"),

    ("a hand-edited session length is named in the warning",
     "app/config.py",
     '            name="WEB_SESSION_HOURS")',
     '            )',
     "test_env_bounds.py"),

    ("…and so is the session maximum",
     "app/config.py",
     '            name="WEB_SESSION_MAX_DAYS")',
     '            )',
     "test_env_bounds.py"),

    # The anchors below are split into two literals on purpose: written
    # in one piece they would occur here as well as at the code they
    # point at, and every anchor has to occur exactly once in this file.
    ("the ratchet's copy leaves out what the ignore rules leave out",
     "scripts/test_guard_mutations.py",
     '         "--exclude-' 'standard"],',
     '         ],',
     "test_ratchet_copy.py"),

    ("…and still takes a new file nobody has committed yet",
     "scripts/test_guard_mutations.py",
     '        ["git", "-C", root, "ls-files", "-z", "--cached", ' '"--others",',
     '        ["git", "-C", root, "ls-files", "-z", "--cached",',
     "test_ratchet_copy.py"),

    ("…and no stale bytecode rides along into the copy",
     "scripts/test_guard_mutations.py",
     "        if not os.path.isfile(src) or " "_cache(rel):\n",
     "        if not os.path.isfile(src):\n",
     "test_ratchet_copy.py"),

    ("…and removes the directory it made, not only the copy in it",
     "scripts/test_guard_mutations.py",
     "    shutil.rmtree(os.path.dirname(tree), " "ignore_errors=True)\n",
     "    shutil.rmtree(tree, ignore_errors=True)\n",
     "test_ratchet_copy.py"),

    # The source-reading procedures cut app/ code through srcslice so a
    # moved anchor reads as a red check, not as a traceback this file
    # can only score as [cannot run]. On an untouched tree no procedure
    # takes the missing-anchor path, so test_srcslice drives it.
    ("a source slice whose anchor is gone is None, not a ValueError",
     "scripts/srcslice.py",
     "    i = -1 if src is None else src.find(needle, start)",
     "    i = src.index(needle, start)",
     "test_srcslice.py"),

    ("…and says which anchor it was, in a FAIL line",
     "scripts/srcslice.py",
     '    print(f"  FAIL  {label}: anchor not found")',
     "    pass",
     "test_srcslice.py"),

    ("…and a missing end anchor is not read as 'to the end of the file'",
     "scripts/srcslice.py",
     "    j = position(src, end, i + len(start), label=label)\n"
     "    if j is None:\n        return None\n",
     "    j = src.find(end, i + len(start))\n",
     "test_srcslice.py"),

    ("…and the end anchor is looked for behind the start, not inside it",
     "scripts/srcslice.py",
     "i + len(start), label=label)",
     "i, label=label)",
     "test_srcslice.py"),

    ("a method that has moved is None, not an AttributeError",
     "scripts/srcslice.py",
     "    except AttributeError:\n        _fail(label or",
     "    except KeyError:\n        _fail(label or",
     "test_srcslice.py"),

    ("an order check over a missing needle is False, not a pass",
     "scripts/srcslice.py",
     "    return all(i >= 0 for i in at) and all(",
     "    return all(",
     "test_srcslice.py"),

    ("walking back to a marker that is gone is None, not a ValueError",
     "scripts/srcslice.py",
     "    i = -1 if src is None else src.rfind(needle, 0, stop)",
     "    i = src.rindex(needle, 0, stop)",
     "test_srcslice.py"),

    ("…and it only walks back from where it was told to start",
     "scripts/srcslice.py",
     "src.rfind(needle, 0, stop)",
     "src.rfind(needle)",
     "test_srcslice.py"),

    # The Telegram command matrix (core split, T2). Bare `/restart` is
    # the one branch whose position in the elif chain is its meaning:
    # below the lifecycle prefix it answers with a usage line. D7 and D9
    # move this condition; re-point the anchor when they do.
    ("a typed command is answered from the branch its word names",
     "app/telegram_bot.py",
     '        elif text == "/restart":\n',
     "        elif False:\n",
     "test_telegram_dispatch_matrix.py"),

    ("…and a call that gets past the matrix's stand-ins turns it red",
     "scripts/test_telegram_dispatch_matrix.py",
     '        ESCAPES.append(f"{kind}({what!r})"[:200])',
     "        pass",
     "test_telegram_dispatch_matrix.py"),

    # The trap covers raw sockets too (follow-up to T2): hostcheck's TLS
    # probe, the websocket client and smtplib dial past urlopen.
    ("…and so does a raw socket.create_connection",
     "scripts/test_telegram_dispatch_matrix.py",
     '    socket.create_connection = _escape("socket.create_connection")\n',
     "    pass\n",
     "test_telegram_dispatch_matrix.py"),

    ("…and a bare socket.connect",
     "scripts/test_telegram_dispatch_matrix.py",
     '    socket.socket.connect = _escape_method("socket.connect")\n',
     "    pass\n",
     "test_telegram_dispatch_matrix.py"),

    # The Telegram callback matrix (core split, T3). With `stop_cancel`
    # switched off the press falls through every branch and nothing
    # answers — only that family's rows go red, measured.
    ("a button press is answered from the branch its data names",
     "app/telegram_bot.py",
     '        if data == "stop_cancel":\n',
     "        if False:\n",
     "test_telegram_callback_matrix.py"),

    ("…and an exception inside the thread a press starts turns it red",
     "scripts/test_telegram_dispatch_matrix.py",
     '    THREAD_ERRORS.append(f"{args.exc_type.__name__} in thread")',
     "    pass",
     "test_telegram_callback_matrix.py"),

    # The Discord matrix (core split, T4). Without the action check a
    # stop token pressed as "updateall" runs an update with the stop's
    # parameters — exactly the two "pressed as updateall" rows go red,
    # measured. Inside `_on_component`, which D11 does not move.
    ("a Discord confirmation only answers the action it was issued for",
     "app/discord_bot.py",
     '        if rec is None or rec["action"] != action:\n',
     "        if rec is None:\n",
     "test_discord_dispatch_matrix.py"),

    ("…the restore fetch's stand-in still hands everything else to the trap",
     "scripts/test_discord_dispatch_matrix.py",
     "        return trap(req, *a, **kw)",
     '        return _Body(b"")',
     "test_discord_dispatch_matrix.py"),

    ("…and what the bot sends to Discord is what the matrix compares",
     "scripts/test_discord_dispatch_matrix.py",
     '            LOG.append(("rest", (self.who, method, path,\n',
     '            (("rest", (self.who, method, path,\n',
     "test_discord_dispatch_matrix.py"),

    # The Web action matrix (core split, T5). /api/unpin calling the
    # store's `pin` instead turns exactly the unpin rows red (7 of 612,
    # measured). A flag branch, so none of the queue, walk or cleanup
    # slices moves it.
    ("a Web request acts through the store method its path names",
     "app/web_ui.py",
     "                    target[4].unpin(target[1])\n",
     "                    target[4].pin(target[1])\n",
     "test_web_action_matrix.py"),

    ("…the webhook stand-in still hands every other URL to the trap",
     "scripts/test_web_action_matrix.py",
     "        return trap(req, *a, **kw)",
     "        return _Resp()",
     "test_web_action_matrix.py"),

    ("…the docker-pull stand-in still hands every other command to the trap",
     "scripts/test_web_action_matrix.py",
     "        return trap(args, *a, **kw)",
     '        return subprocess.CompletedProcess(args, 0, "", "")',
     "test_web_action_matrix.py"),

    ("…and what reaches Telegram is what the Web matrix compares",
     "scripts/test_web_action_matrix.py",
     '                LOG.append(("telegram", (data.get("text", ""), n)))',
     "                pass",
     "test_web_action_matrix.py"),

    # Follow-up to T5: a self-update queued behind the lock is handed
    # over by the Web worker once it releases. Dropping the hand-over or
    # running it before the release both turn the two "hands over" rows
    # red, measured. Q14 rewrites this claim and release — re-point the
    # anchor there. Since Q3 the hand-over is the door's `after`.
    ("…and a Web update hands a queued self-update over after its release",
     "app/web_ui.py",
     "                engine.release_update(after=bot._run_queued_selfupdate)\n",
     "                engine.release_update()\n",
     "test_web_action_matrix.py"),

    # Q3: the Web worker and the banner's question go through the
    # engine's door. Each one applied by hand in a copy, red without a
    # traceback.
    ("the engine's update_claimed answers what the lock says",
     "app/update_engine.py",
     "        returned; the claim that decides is still `claim_update`.\n"
     "        \"\"\"\n"
     "        return self._update_lock.locked()\n",
     "        returned; the claim that decides is still `claim_update`.\n"
     "        \"\"\"\n"
     "        return False\n",
     "test_update_lock_door.py"),

    ("the Web banner asks the engine whether an update holds the lock",
     "app/web_ui.py",
     "                return bool(getattr(eng, \"update_claimed\", False))\n",
     "                return False\n",
     "test_web_update_feedback.py"),
    ("the Web worker does no work while somebody else holds the lock",
     "app/web_ui.py",
     "            if not engine.claim_update():\n",
     "            if False:\n",
     "test_update_lock_sites.py"),
    ("the Web worker claims through the engine's door, not the raw lock",
     "app/web_ui.py",
     "            if not engine.claim_update():\n",
     "            if not engine._update_lock.acquire(blocking=False):\n",
     "test_update_lock_sites.py"),
    ("a read of the door's update_claimed is noticed",
     "scripts/test_update_lock_sites.py",
     "                    ops[(rel, qual)].append(DOOR_READ)\n",
     "                    pass\n",
     "test_update_lock_sites.py"),

    # Q4: update_running only looks. Applied by hand in a copy, red
    # without a traceback.
    ("update_running only looks at the lock, so a claim landing while "
     "it asks is not refused",
     "app/update_engine.py",
     "        although nobody was updating.\"\"\"\n"
     "        return self._update_lock.locked()\n",
     "        although nobody was updating.\"\"\"\n"
     "        locked = self._update_lock.acquire(blocking=False)\n"
     "        if locked:\n"
     "            self._update_lock.release()\n"
     "            return False\n"
     "        return True\n",
     "test_update_lock_door.py"),
    ("…and T6 lists update_running as a reader, not a probe",
     "app/update_engine.py",
     "        although nobody was updating.\"\"\"\n"
     "        return self._update_lock.locked()\n",
     "        although nobody was updating.\"\"\"\n"
     "        if self._update_lock.acquire(blocking=False):\n"
     "            self._update_lock.release()\n"
     "        return self._update_lock.locked()\n",
     "test_update_lock_sites.py"),

    # The update-lock sites (core split, T6). A claim added anywhere in
    # app/ shows up as an unlisted function in the AST enumeration; the
    # construction line is the anchor because the claim/release slices
    # replace every site but keep the one lock.
    ("a new place that takes the update lock is noticed",
     "app/update_engine.py",
     "        self._update_lock = threading.Lock()\n",
     "        self._update_lock = threading.Lock()\n"
     "        self._update_lock.acquire(blocking=False)\n"
     "        self._update_lock.release()\n",
     "test_update_lock_sites.py"),

    # Follow-up to T6: asking the lock through the Web helper is counted
    # per caller, so a new `_update_lock_held(bot)` is a new entry. The
    # counter is the anchor; the one caller today sits in a pre-gate
    # that Q14 rewrites.
    ("…and so is a new place that asks it through a reader helper",
     "scripts/test_update_lock_sites.py",
     "                        helpers[(rel, qual, c)] += 1\n",
     "                        pass\n",
     "test_update_lock_sites.py"),

    # selfupdate.start stays a raw claim by design (Q3), so this anchor
    # survives the split. Dropping the queue turns QUEUE_SELF into REJECT.
    ("a self-update asked for while the lock is held is remembered",
     "app/selfupdate.py",
     "        ctx._queued_selfupdate = (target, reply_to) if reply_to else (target,)\n",
     "        ctx._queued_selfupdate = None\n",
     "test_update_lock_sites.py"),

    # Discord's worker-pool refusal: not the update lock, and nothing in
    # the split moves it. Swapping the key is a busy sentence the list
    # does not know.
    ("a new busy sentence anywhere in app/ is noticed",
     "app/discord_bot.py",
     'self.t("chan_busy_capacity")',
     'self.t("chan_update_busy")',
     "test_update_lock_sites.py"),

    ("…the lock-site drive still sees the work a claim does",
     "scripts/test_update_lock_sites.py",
     '            WORK.append((f"update_container@{host}:{name}",\n'
     "                         self.lock.locked()))\n",
     "            pass\n",
     "test_update_lock_sites.py"),

    # Q1: the engine's door to the update lock. Telegram's six claims go
    # through it; the promises are the door's own.
    ("the update lock stays held while the self-update swaps",
     "app/update_engine.py",
     "        if not swap:\n"
     "            try:\n"
     "                self._update_lock.release()\n",
     "        if True:\n"
     "            try:\n"
     "                self._update_lock.release()\n",
     "test_update_lock_door.py"),

    ("…and a swap flag that raises does not keep it held instead",
     "app/update_engine.py",
     "        try:\n"
     '            swap = bool(getattr(self, "_swap_in_flight", False))\n'
     "        except Exception:\n"
     "            swap = False\n",
     "        swap = bool(self._swap_in_flight)\n",
     "test_update_lock_door.py"),

    ("giving back a lock nobody holds never raises out of a finally",
     "app/update_engine.py",
     "            except RuntimeError:\n"
     '                print("release_update: the update lock was not held "\n',
     "            except ZeroDivisionError:\n"
     '                print("release_update: the update lock was not held "\n',
     "test_update_lock_door.py"),

    ("…what comes after a flow runs once it has given the lock back",
     "app/update_engine.py",
     "        if after is not None:\n"
     "            after()\n",
     "        pass\n",
     "test_update_lock_door.py"),

    ("a claim on the update lock never waits",
     "app/update_engine.py",
     "        return self._update_lock.acquire(blocking=False)\n",
     "        return self._update_lock.acquire(timeout=1)\n",
     "test_update_lock_door.py"),

    ("giving back a lock nobody holds leaves it free for the next claim",
     "app/update_engine.py",
     "                print(\"release_update: the update lock was not held \"\n"
     "                      \"— nothing to give back\")\n",
     "                print(\"release_update: the update lock was not held \"\n"
     "                      \"— nothing to give back\")\n"
     "                self._update_lock.acquire()\n",
     "test_update_lock_door.py"),

    ("the door keeps no state of its own next to the lock",
     "app/update_engine.py",
     "        return self._update_lock.acquire(blocking=False)\n",
     "        self._door_claims = 1\n"
     "        return self._update_lock.acquire(blocking=False)\n",
     "test_update_lock_door.py"),

    ("…and a `with update_claim()` that lost gives back nobody's lock",
     "app/update_engine.py",
     "            if claimed:\n"
     "                self.release_update(after=after)\n",
     "            if True:\n"
     "                self.release_update(after=after)\n",
     "test_update_lock_door.py"),

    ("the bot's update lock is the engine's, not one of its own",
     "app/telegram_bot.py",
     "        return self.engine._update_lock\n",
     "        return threading.Lock()\n",
     "test_update_lock_door.py"),

    ("…and the bot's update_running reads the engine's",
     "app/telegram_bot.py",
     "        return self.engine.update_running\n",
     "        return False\n",
     "test_update_lock_door.py"),

    ("a new caller of the update-lock door is noticed",
     "scripts/test_update_lock_sites.py",
     "                    elif c in (DOOR_CLAIM, DOOR_RELEASE) or (\n",
     "                    elif False and (\n",
     "test_update_lock_sites.py"),

    ("…including one that spells it as a with block",
     "scripts/test_update_lock_sites.py",
     "                        if isinstance(e, ast.Call) \\\n"
     "                                and _callee(e) == DOOR_WITH:\n",
     "                        if False:\n",
     "test_update_lock_sites.py"),

    ("…and which sentence a busy site picked",
     "scripts/test_update_lock_sites.py",
     "            KEYS.append(key)\n",
     "            pass\n",
     "test_update_lock_sites.py"),

    # A1: which hosts a command reaches is decided once, in the core, and
    # Telegram only parses and renders. Both halves of the no-host rule
    # sit on one line of targets_for, so each gets its own mutation.
    ("a read with no host named reaches every host",
     "app/container_flags.py",
     "    if host_token is None:\n"
     "        return (list(registry) if mode == \"read\" else [registry.local]), None\n",
     "    if host_token is None:\n"
     "        return ([registry.local] if mode == \"read\" else [registry.local]), None\n",
     "test_target_resolution_shared.py"),

    ("…and a write with no host named stays on the local one",
     "app/container_flags.py",
     "    if host_token is None:\n"
     "        return (list(registry) if mode == \"read\" else [registry.local]), None\n",
     "    if host_token is None:\n"
     "        return (list(registry) if mode == \"read\" else list(registry)), None\n",
     "test_target_resolution_shared.py"),

    ("a target mode the core does not know is refused, not read as a write",
     "app/container_flags.py",
     "    if mode not in TARGET_MODES:\n",
     "    if False:\n",
     "test_target_resolution_shared.py"),

    ("a one-host install's @-token stays in the text, unparsed",
     "app/telegram_bot.py",
     "        if not self._multi():\n"
     "            return arg_text, None, None\n"
     "        import container_flags\n",
     "        import container_flags\n",
     "test_target_resolution_shared.py"),

    # A2: Discord reads go through the same core on multi, and `all` is
    # the every-host sentinel there. Handing the core the raw name makes
    # `host: all` an unknown host again.
    ("Discord `host: all` on a read reaches every host (multi-host)",
     "app/discord_bot.py",
     "        targets, fatal = container_flags.targets_for_read(\n"
     "            self.hosts, self._host_token(name))\n",
     "        targets, fatal = container_flags.targets_for_read(\n"
     "            self.hosts, name)\n",
     "test_target_resolution_shared.py"),

    ("…and the Discord matrix sees it: `host: all` rows equal the no-host rows",
     "app/discord_bot.py",
     "        targets, fatal = container_flags.targets_for_read(\n"
     "            self.hosts, self._host_token(name))\n",
     "        targets, fatal = container_flags.targets_for_read(\n"
     "            self.hosts, name)\n",
     "test_discord_dispatch_matrix.py"),

    ("Discord's `all` is `all` in any case, for reads and writes",
     "app/discord_bot.py",
     '        return ALL_HOSTS if name.strip().lower() == "all" else name\n',
     '        return ALL_HOSTS if name == "all" else name\n',
     "test_target_resolution_shared.py"),

    ("a one-host Discord read keeps its own path; `all` stays unknown there",
     "app/discord_bot.py",
     '        if not getattr(self.hosts, "is_multi", False):\n',
     "        if False:\n",
     "test_target_resolution_shared.py"),

    ("Discord autocomplete with `host: all` asks the local host only",
     "app/discord_bot.py",
     "        named = self._host_token(host_name) not in (None, hostslib.ALL_HOSTS)\n",
     "        named = bool(host_name)\n",
     "test_target_resolution_shared.py"),

    # …which only holds while no host can be called `all`. The host list
    # drops one at load and says so in the log.
    ("the log says why a host named `all` was dropped at load",
     "app/config.py",
     '            print(f"DOCKER_HOSTS: skipping {entry!r} — {name!r} is reserved: "\n',
     '            (f"DOCKER_HOSTS: skipping {entry!r} — {name!r} is reserved: "\n',
     "test_docker_hosts.py"),

    # The host walk lives once, in hosts.walk (W1); the Web UI's
    # _host_views is its first caller.
    ("a host found dead is not waited for again within its cooldown",
     "app/hosts.py",
     "            if left > 0:\n                skip[i] = (cached, int(left))\n",
     "            if False:\n                skip[i] = (cached, int(left))\n",
     "test_dead_host_memory.py"),

    ("the status page skips a host still in its cooldown, via the walk",
     "app/hosts.py",
     "            if left > 0:\n                skip[i] = (cached, int(left))\n",
     "            if False:\n                skip[i] = (cached, int(left))\n",
     "test_host_views_parallel.py"),

    ("the walk remembers a host that did not answer",
     "app/hosts.py",
     "            mark_unreachable(host.name, why)\n",
     "            pass\n",
     "test_dead_host_memory.py"),

    ("the walk forgets a host's failures once it answers",
     "app/hosts.py",
     "        mark_reachable(host.name)\n        walked.append",
     "        walked.append",
     "test_dead_host_memory.py"),

    ("the walk asks at most eight hosts at once",
     "app/hosts.py",
     "        workers = min(max_workers, MAX_WALK_WORKERS, len(todo))\n",
     "        workers = len(todo)\n",
     "test_dead_host_memory.py"),

    ("a walk with use_cache=False asks a host inside its cooldown",
     "app/hosts.py",
     "        if use_cache:\n",
     "        if True:\n",
     "test_dead_host_memory.py"),

    ("a skipped host's retry_in is whole seconds, rounded down",
     "app/hosts.py",
     "                skip[i] = (cached, int(left))\n",
     "                skip[i] = (cached, int(left) + 1)\n",
     "test_dead_host_memory.py"),

    ("a caller's max_workers cannot lift the walk above eight",
     "app/hosts.py",
     "        workers = min(max_workers, MAX_WALK_WORKERS, len(todo))\n",
     "        workers = min(max_workers, len(todo))\n",
     "test_dead_host_memory.py"),

    ("a skipped host keeps its place in the walk's order",
     "app/hosts.py",
     "    return walked\n",
     "    walked.sort(key=lambda w: not w.skipped)\n    return walked\n",
     "test_dead_host_memory.py"),

    ("use_cache=False still remembers a host that fails",
     "app/hosts.py",
     "            mark_unreachable(host.name, why)\n",
     "            if use_cache:\n                mark_unreachable(host.name, why)\n",
     "test_dead_host_memory.py"),

    ("use_cache=False still forgets a host that answers",
     "app/hosts.py",
     "        mark_reachable(host.name)\n        walked.append",
     "        if use_cache:\n            mark_reachable(host.name)\n        walked.append",
     "test_dead_host_memory.py"),

    ("the status page hands the walk its own words for a dead host",
     "app/web_ui.py",
     "                    describe=self._why):\n",
     "                    describe=str):\n",
     "test_dead_host_memory.py"),

    # Finding a container across hosts (W2): unreachable is named, never
    # folded into "not found". test_find_on_hosts drives the core; since
    # W3 Telegram's /status, /check and /update <name> call it too.
    ('a host that does not answer is named, not called empty',
     'app/container_flags.py',
     '    if rc != 0:\n        return None, None, (rc, getattr(result, "stderr", "") or "")\n',
     '    if False:\n        return None, None, (rc, getattr(result, "stderr", "") or "")\n',
     'test_find_on_hosts.py'),

    ("a glob on a host that does not answer is unreachable, not 'no match'",
     'app/lifecycle.py',
     '    if rc != 0:\n        return [], (rc, getattr(result, "stderr", "") or "")\n',
     '    if False:\n        return [], (rc, getattr(result, "stderr", "") or "")\n',
     'test_find_on_hosts.py'),

    ("a backend that raises is the checked resolver's failure, not a crash",
     'app/container_flags.py',
     '    except Exception as e:                                  # noqa: BLE001\n        return None, None, e\n',
     '    except ZeroDivisionError as e:                          # noqa: BLE001\n        return None, None, e\n',
     'test_find_on_hosts.py'),

    ('find_on_hosts never raises for a host, whatever it throws',
     'app/container_flags.py',
     '        except Exception as e:                              # noqa: BLE001\n            backend, failure = None, e\n',
     '        except ZeroDivisionError as e:                      # noqa: BLE001\n            backend, failure = None, e\n',
     'test_find_on_hosts.py'),

    ("a host with no backend is chan_no_backend, not unreachable",
     'app/container_flags.py',
     '        elif backend is None:\n            reply = Reply("chan_no_backend", ok=False)\n',
     '        elif False:\n            reply = Reply("chan_no_backend", ok=False)\n',
     'test_find_on_hosts.py'),

    ("a caller's mistake raises from find_on_hosts, it is no dead host",
     'app/container_flags.py',
     '            name, reply, failure = resolve_container_checked(\n                arg, backend=backend, timeout=probe)\n',
     '            try:\n                name, reply, failure = resolve_container_checked(\n                    arg, backend=backend, timeout=probe)\n            except Exception as e:                          # noqa: BLE001\n                name, reply, failure = None, None, e\n',
     'test_find_on_hosts.py'),

    ("a bare non-zero exit is named in the reader's language",
     'app/container_flags.py',
     '                        else _exit_text(rc, t))\n',
     '                        else _exit_text(rc, None))\n',
     'test_find_on_hosts.py'),

    ('find_on_hosts hands every host to on_host',
     'app/container_flags.py',
     '        if on_host is not None:\n            on_host(HostFind(host, tuple(names), reply, gone))\n',
     '        pass\n',
     'test_find_on_hosts.py'),

    ("an unreachable host's SSH hint is in the reader's language",
     'app/container_flags.py',
     '                hint = hostdiag.hint(getattr(host, "endpoint", ""), raw,\n                                     t=t) or ""\n',
     '                hint = hostdiag.hint(getattr(host, "endpoint", ""), raw,\n                                     t=None) or ""\n',
     'test_find_on_hosts.py'),

    ("a dead host's error says how long we waited, not the argv",
     'app/container_flags.py',
     '                text = _human(failure, t)\n',
     '                text = str(failure)\n',
     'test_find_on_hosts.py'),

    ("nothing found anywhere answers with the first host's Reply",
     'app/container_flags.py',
     '        return self.errors[0] if self.errors else None\n',
     '        return None\n',
     'test_find_on_hosts.py'),

    ("a per-host 'not found' says which host it is about",
     'app/container_flags.py',
     '            reply = replace(reply, host=host, host_is_local=_is_local(host))\n',
     '            reply = reply\n',
     'test_find_on_hosts.py'),

    ("a plain name on two hosts is found on both, not just the first",
     'app/container_flags.py',
     '            found.append((host, tuple(names)))\n',
     '            if is_glob or not found:\n                found.append((host, tuple(names)))\n',
     'test_find_on_hosts.py'),

    # W3: Telegram's plain-name /status, /check and /update go through
    # find_on_hosts, so the W2 promise now guards a production path and
    # the Telegram matrix sees it break.
    ("…and Telegram says a refused host is unreachable, not 'not found'",
     'app/container_flags.py',
     '    if rc != 0:\n        return None, None, (rc, getattr(result, "stderr", "") or "")\n',
     '    if False:\n        return None, None, (rc, getattr(result, "stderr", "") or "")\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /check and /update <name> keep an unreachable host apart from 'not found'",
     'app/telegram_bot.py',
     '        if found.unreachable:\n            return [], None, found.unreachable[0]\n',
     '        if found.unreachable:\n            return [], self.t("resolve_not_found", name=arg), None\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /status <name> names the hosts that did not answer",
     'app/telegram_bot.py',
     '                    on_host=_show, t=self.t).unreachable\n',
     '                    on_host=_show, t=self.t).unreachable[:0]\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /check <name> names the hosts that did not answer",
     'app/telegram_bot.py',
     '            # before the check starts on the ones that did.\n'
     '            tail = self._lookup_tail(bool(selections), first_err, unreachable)\n',
     '            # before the check starts on the ones that did.\n'
     '            tail = self._lookup_tail(bool(selections), first_err, [])\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /update <name> names the hosts that did not answer",
     'app/telegram_bot.py',
     '            # the lookup ends, before anything is checked or updated.\n'
     '            tail = self._lookup_tail(bool(selections), first_err, unreachable)\n',
     '            # the lookup ends, before anything is checked or updated.\n'
     '            tail = self._lookup_tail(bool(selections), first_err, [])\n',
     'test_telegram_dispatch_matrix.py'),

    ("found nowhere that answered: Telegram's 'not found' leads the unreachable lines",
     'app/telegram_bot.py',
     '        lines = [] if found_any or not first_err else [first_err]\n',
     '        lines = []\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram shows an unreachable host's SSH hint under its line",
     'app/telegram_bot.py',
     '                         + ("\\n" + gone.hint if gone.hint else ""))\n',
     '                         + "")\n',
     'test_telegram_dispatch_matrix.py'),

    # The behavioural half of command_host_coverage drives /logs and
    # /audit through _handle_message now. Both mutations keep the text
    # the static half looks for (`backend_for=self._backend_for`), so
    # only a real dispatch to a remote-only container sees them; the old
    # hand-built sweep stayed green under both (measured on 1403ec1).
    ("Telegram /logs <name> looks on every host the token names",
     "app/telegram_bot.py",
     "                log_targets or [None], backend_for=self._backend_for,",
     "                [None], backend_for=self._backend_for,",
     "test_command_host_coverage.py"),

    ("Telegram /audit <name> looks on every host the token names",
     "app/telegram_bot.py",
     "                audit_targets or [None], backend_for=self._backend_for,",
     "                [None], backend_for=self._backend_for,",
     "test_command_host_coverage.py"),

    # W3d: Discord /status <container> names a dead or refused host as
    # unreachable and never answers "not found" for it. Two lines carry
    # that: the lookup error is not the answer, and the overview it
    # falls into writes the unreachable line. Measured on f713111, the
    # plan expected "not found" there and it was not.
    ("Discord /status <container>: a lookup error falls into the overview, "
     "never the answer",
     "app/discord_bot.py",
     "                name, err = self._resolve_container(wanted, backend, host)\n"
     "                if err:\n",
     "                name, err = self._resolve_container(wanted, backend, host)\n"
     "                if err:\n"
     "                    return err\n",
     "test_discord_dispatch_matrix.py"),

    # The handler raising there is caught by the dispatcher and still
    # makes one edit ("Something went wrong"), so the W3d "nothing
    # raised" check has to look at the log line, not only at `raised`.
    ("Discord /status <container>: a lookup error never ends in "
     "\"Something went wrong\"",
     "app/discord_bot.py",
     "                name, err = self._resolve_container(wanted, backend, host)\n"
     "                if err:\n",
     "                name, err = self._resolve_container(wanted, backend, host)\n"
     "                if err:\n"
     "                    raise RuntimeError(err)\n",
     "test_discord_dispatch_matrix.py"),

    ("Discord /status: a host that does not answer the overview is named "
     "unreachable",
     "app/discord_bot.py",
     "            if host_name in gone:\n"
     "                lines.append(self.t(\"host_unreachable_short\",\n"
     "                                    host=host_name, error=gone[host_name]))\n"
     "                continue\n",
     "            if host_name in gone:\n"
     "                continue\n",
     "test_discord_dispatch_matrix.py"),

    # W4: Telegram's glob /status, /check and /update go through
    # find_on_hosts too, so a dead or refused host is named instead of
    # reading as "no containers match". The two failure lines of
    # match_glob_checked now guard a chat path, and the two glob call
    # sites each get a mutation that puts the old shortcut back.
    ("…and Telegram says a refused host is unreachable for a glob, not 'no match'",
     'app/lifecycle.py',
     '    if rc != 0:\n        return [], (rc, getattr(result, "stderr", "") or "")\n',
     '    if False:\n        return [], (rc, getattr(result, "stderr", "") or "")\n',
     'test_telegram_dispatch_matrix.py'),

    ("…and Telegram says a dead host is unreachable for a glob, not 'no match'",
     'app/lifecycle.py',
     '    except Exception as e:                                  # noqa: BLE001\n'
     '        return [], e\n',
     '    except Exception as e:                                  # noqa: BLE001\n'
     '        return [], None\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /check and /update <glob> keep an unreachable host apart from 'no match'",
     'app/telegram_bot.py',
     '        /update leaves it False: a write always asks."""\n'
     '        import container_flags\n',
     '        /update leaves it False: a write always asks."""\n'
     '        if self._is_glob(arg):\n'
     '            import lifecycle\n'
     '            names = lifecycle.match_glob(arg, backend=backend or self.backend)\n'
     '            if not names:\n'
     '                return [], self.t("glob_no_match", pattern=arg), None\n'
     '            return names, None, None\n'
     '        import container_flags\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /status <glob> names the hosts that did not answer",
     'app/telegram_bot.py',
     '                    on_host=_show, t=self.t).unreachable\n',
     '                    on_host=_show, t=self.t).unreachable[:0 if glob else None]\n',
     'test_telegram_dispatch_matrix.py'),

    # W5: the /status overview's host walk moved out of Telegram into
    # status_render.collect_overview, unchanged. Each line of the walk
    # that decides what the overview says about a host gets broken here,
    # and test_status_overview_core drives the walk on T2's hosts.
    ("the /status overview lists a host whose ps raises (dead host) as unreachable",
     'app/status_render.py',
     '            ids_p = _b.run(["ps", "-q"], timeout=10)\n'
     '        except Exception as _e:\n'
     '            _mark_unreachable(_host, clip(str(_e)))\n',
     '            ids_p = _b.run(["ps", "-q"], timeout=10)\n'
     '        except Exception as _e:\n'
     '            pass\n',
     'test_status_overview_core.py'),

    ("…and a host whose ps exits non-zero (refused SSH) too",
     'app/status_render.py',
     '        if getattr(ids_p, "returncode", 0) != 0:\n',
     '        if False:\n',
     'test_status_overview_core.py'),

    ("…and a host whose inspect raises",
     'app/status_render.py',
     '            ins_p = _b.run(["inspect", *ids], timeout=10)\n'
     '        except Exception as _e:\n'
     '            _mark_unreachable(_host, clip(str(_e)))\n',
     '            ins_p = _b.run(["inspect", *ids], timeout=10)\n'
     '        except Exception as _e:\n'
     '            pass\n',
     'test_status_overview_core.py'),

    ("the /status overview asks each host's ps for ten seconds, not the backend's 30",
     'app/status_render.py',
     '_b.run(["ps", "-q"], timeout=10)',
     '_b.run(["ps", "-q"], timeout=30)',
     'test_status_overview_core.py'),

    ("…and its inspect for ten seconds",
     'app/status_render.py',
     '_b.run(["inspect", *ids], timeout=10)',
     '_b.run(["inspect", *ids], timeout=30)',
     'test_status_overview_core.py'),

    ("an unreachable host in the /status overview gets its SSH hint in the reader's language",
     'app/status_render.py',
     '_h = hostdiag.hint(getattr(_host, "endpoint", ""), _why, t=t)',
     '_h = hostdiag.hint(getattr(_host, "endpoint", ""), _why, t=None)',
     'test_status_overview_core.py'),

    ("…and a hint that raises costs the hint, not the overview",
     'app/status_render.py',
     '        try:\n'
     '            import hostdiag\n'
     '            _h = hostdiag.hint(getattr(_host, "endpoint", ""), _why, t=t)\n'
     '            if _h:\n'
     '                _why += "\\n" + _h\n'
     '        except Exception:\n'
     '            pass\n',
     '        if True:\n'
     '            import hostdiag\n'
     '            _h = hostdiag.hint(getattr(_host, "endpoint", ""), _why, t=t)\n'
     '            if _h:\n'
     '                _why += "\\n" + _h\n',
     'test_status_overview_core.py'),

    ("a single-host install's unreachable daemon is called local in the overview",
     'app/status_render.py',
     '(getattr(_host, "name", "") or LOCAL_HOST, _why))',
     '(getattr(_host, "name", ""), _why))',
     'test_status_overview_core.py'),

    ("a refused host with nothing on stderr says its exit code",
     'app/status_render.py',
     '                or f"exit {ids_p.returncode}")\n',
     '                or "")\n',
     'test_status_overview_core.py'),

    ("a refused host's words are clipped like every other host line",
     'app/status_render.py',
     '                clip(getattr(ids_p, "stderr", "") or "")\n',
     '                (getattr(ids_p, "stderr", "") or "")\n',
     'test_status_overview_core.py'),

    ("…and so are a raising ps's",
     'app/status_render.py',
     '            ids_p = _b.run(["ps", "-q"], timeout=10)\n'
     '        except Exception as _e:\n'
     '            _mark_unreachable(_host, clip(str(_e)))\n',
     '            ids_p = _b.run(["ps", "-q"], timeout=10)\n'
     '        except Exception as _e:\n'
     '            _mark_unreachable(_host, str(_e))\n',
     'test_status_overview_core.py'),

    ("…and a raising inspect's",
     'app/status_render.py',
     '            ins_p = _b.run(["inspect", *ids], timeout=10)\n'
     '        except Exception as _e:\n'
     '            _mark_unreachable(_host, clip(str(_e)))\n',
     '            ins_p = _b.run(["inspect", *ids], timeout=10)\n'
     '        except Exception as _e:\n'
     '            _mark_unreachable(_host, str(_e))\n',
     'test_status_overview_core.py'),

    ("an inspect that answers null leaves the overview standing",
     'app/status_render.py',
     '_cfgs = json.loads(ins_p.stdout) or []',
     '_cfgs = json.loads(ins_p.stdout)',
     'test_status_overview_core.py'),

    ("the /status overview walks the hosts in registry order",
     'app/status_render.py',
     '    for _host in (targets or [None]):\n',
     '    for _host in reversed(targets or [None]):\n',
     'test_status_overview_core.py'),

    ("Telegram /status renders what the core walk found",
     'app/telegram_bot.py',
     '            inspected, unreachable = status_render.collect_overview(\n'
     '                status_targets, backend_for=self._backend_for, t=self.t,\n'
     '                use_cache=True)\n',
     '            inspected, unreachable = [], []\n',
     'test_status_overview_core.py'),

    ("…and so do T2's /status rows",
     'app/telegram_bot.py',
     '            inspected, unreachable = status_render.collect_overview(\n'
     '                status_targets, backend_for=self._backend_for, t=self.t,\n'
     '                use_cache=True)\n',
     '            inspected, unreachable = [], []\n',
     'test_telegram_dispatch_matrix.py'),

    # W5d: Discord's /status overview walks collect_overview too, so both
    # chats list the same containers and a dead or refused host carries
    # its SSH hint. T4's W5d checks compare Discord's answer with
    # Telegram's own /status in the same world.
    ("Discord /status lists pinned and excluded containers, like Telegram",
     'app/discord_bot.py',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n'
     '                if nm not in {c["name"] for c in self._checker_for(host)\n'
     '                              .get_running_containers(include_self=True)}:\n'
     '                    continue\n',
     'test_discord_dispatch_matrix.py'),

    ("…and test_status_parity sees a Discord back on the update path's list",
     'app/discord_bot.py',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n'
     '                if nm not in {c["name"] for c in self._checker_for(host)\n'
     '                              .get_running_containers(include_self=True)}:\n'
     '                    continue\n',
     'test_status_parity.py'),

    ("Discord /status lists containers opted out by docksentry label",
     'app/discord_bot.py',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n'
     '                lab = (cfg.get("Config") or {}).get("Labels") or {}\n'
     '                if lab.get("docksentry.enable") == "false" or lab.get("docksentry.exclude") == "true":\n'
     '                    continue\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /status lists EXCLUDE_CONTAINERS, on one host and on many",
     'app/discord_bot.py',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n'
     '                if nm in (self.config.exclude_containers or ()):\n'
     '                    continue\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /status lists the container that answers it",
     'app/discord_bot.py',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n',
     '                nm = (cfg.get("Name") or "?").lstrip("/")\n'
     '                if cfg.get("Id", "")[:12] == __import__("os").environ.get("HOSTNAME"):\n'
     '                    continue\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /status puts the SSH hint under an unreachable host's line",
     'app/discord_bot.py',
     'host=host_name, error=gone[host_name]))',
     'host=host_name, error=gone[host_name].split("\\n")[0]))',
     'test_discord_dispatch_matrix.py'),

    ("Discord /status <name> that does not resolve still narrows the overview",
     'app/discord_bot.py',
     '                if wanted and wanted.lower() not in nm.lower():\n'
     '                    continue\n'
     '                lines.append(status_render.overview_line(\n',
     '                lines.append(status_render.overview_line(\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /status overview keeps the image's version label",
     'app/discord_bot.py',
     'nm, container_info.state_of(self._backend_for(host), cfg),',
     'nm, dict(container_info.state_of(self._backend_for(host), cfg), version=""),',
     'test_discord_dispatch_matrix.py'),

    ("…and test_status_parity drives that label through Discord too",
     'app/discord_bot.py',
     'nm, container_info.state_of(self._backend_for(host), cfg),',
     'nm, dict(container_info.state_of(self._backend_for(host), cfg), version=""),',
     'test_status_parity.py'),

    ("…and each line is the shared overview line",
     'app/discord_bot.py',
     '                lines.append(status_render.overview_line(\n'
     '                    nm, container_info.state_of(self._backend_for(host), cfg),\n'
     '                    host_tag=tag))\n',
     '                lines.append(f"{nm}{tag}")\n',
     'test_status_parity.py'),

    # W6: Telegram's four check walks are container_flags.check_on_hosts.
    ("a single-host install's check still raises as it always did",
     'app/container_flags.py',
     '            if alone and raise_alone:\n                raise\n',
     '            if False:\n                raise\n',
     'test_check_on_hosts.py'),

    ("one host failing its check does not stop the others (Telegram)",
     'app/container_flags.py',
     '            updates, error = None, e\n',
     '            raise\n',
     'test_telegram_dispatch_matrix.py'),

    ("the check walk asks for a hint only when the caller wants one",
     'app/container_flags.py',
     '        if error is not None and hint:\n',
     '        if error is not None:\n',
     'test_check_on_hosts.py'),

    ("…in the reader's language",
     'app/container_flags.py',
     '                why = hostdiag.hint(getattr(host, "endpoint", ""), error,\n'
     '                                    t=t) or ""\n',
     '                why = hostdiag.hint(getattr(host, "endpoint", ""), error,\n'
     '                                    t=None) or ""\n',
     'test_check_on_hosts.py'),

    ("…and a hint that throws costs the hint, not the check",
     'app/container_flags.py',
     '            try:\n'
     '                import hostdiag\n'
     '                why = hostdiag.hint(getattr(host, "endpoint", ""), error,\n'
     '                                    t=t) or ""\n'
     '            except Exception:                               # noqa: BLE001\n'
     '                why = ""\n',
     '            import hostdiag\n'
     '            why = hostdiag.hint(getattr(host, "endpoint", ""), error,\n'
     '                                t=t) or ""\n',
     'test_check_on_hosts.py'),

    ("a host's check result is handed on before the next host is checked",
     'app/container_flags.py',
     '        out.append(answer)\n'
     '        if on_host is not None:\n'
     '            on_host(answer)\n'
     '    return out\n',
     '        out.append(answer)\n'
     '    for answer in out:\n'
     '        if on_host is not None:\n'
     '            on_host(answer)\n'
     '    return out\n',
     'test_check_on_hosts.py'),

    ("a scoped check asks each host only about its own matches",
     'app/container_flags.py',
     '            kw["only"] = only_for(host)\n',
     '            pass\n',
     'test_telegram_dispatch_matrix.py'),

    ("the check walk hands the debug sink to check_all",
     'app/container_flags.py',
     '            kw["bot"] = debug_sink\n',
     '            pass\n',
     'test_check_on_hosts.py'),

    ("a host without a checker is that host's failure, not the walk's",
     'app/container_flags.py',
     '        checker = None\n        try:\n            checker = checker_for(host)\n',
     '        checker = checker_for(host)\n        try:\n',
     'test_check_on_hosts.py'),

    ("Telegram /check asks for the SSH hint under a failed host",
     'app/telegram_bot.py',
     '            debug_sink=self, hint=True, on_host=_report, t=self.t)\n',
     '            debug_sink=self, hint=False, on_host=_report, t=self.t)\n',
     'test_check_on_hosts.py'),

    ("…and puts it under the failure",
     'app/telegram_bot.py',
     '                if hc.hint:\n                    _detail += "\\n\\n" + hc.hint\n',
     '                if False:\n                    _detail += "\\n\\n" + hc.hint\n',
     'test_check_on_hosts.py'),

    ("…and sends the debug trace through the bot",
     'app/telegram_bot.py',
     '            debug_sink=self, hint=True, on_host=_report, t=self.t)\n',
     '            debug_sink=None, hint=True, on_host=_report, t=self.t)\n',
     'test_check_per_host.py'),

    ("the dry run says a failed host without a hint, as it always did",
     'app/telegram_bot.py',
     '                debug_sink=self, on_host=_dry, t=self.t)\n',
     '                debug_sink=self, hint=True, on_host=_dry, t=self.t)\n',
     'test_check_on_hosts.py'),

    ("…and so does /check <name>",
     'app/telegram_bot.py',
     '                only_for=scoped.__getitem__, debug_sink=self,\n'
     '                on_host=_scoped_check, t=self.t)\n',
     '                only_for=scoped.__getitem__, debug_sink=self, hint=True,\n'
     '                on_host=_scoped_check, t=self.t)\n',
     'test_check_on_hosts.py'),

    ("Telegram /check <name> scopes each host's check to its matches",
     'app/telegram_bot.py',
     '                only_for=scoped.__getitem__, debug_sink=self,\n'
     '                on_host=_scoped_check, t=self.t)\n',
     '                only_for=None, debug_sink=self,\n'
     '                on_host=_scoped_check, t=self.t)\n',
     'test_telegram_dispatch_matrix.py'),

    ("…and so does /update <name>",
     'app/telegram_bot.py',
     '                only_for=scoped.__getitem__, debug_sink=self,\n'
     '                on_host=_batch, t=self.t)\n',
     '                only_for=None, debug_sink=self,\n'
     '                on_host=_batch, t=self.t)\n',
     'test_telegram_dispatch_matrix.py'),

    # W6d: Discord's /check walk is check_on_hosts too.
    ("raise_alone=False reports a single-host install's failed check",
     'app/container_flags.py',
     '            if alone and raise_alone:\n                raise\n',
     '            if alone:\n                raise\n',
     'test_check_on_hosts.py'),

    ("Discord /check never raises out of the handler, not even alone",
     'app/discord_bot.py',
     '                raise_alone=False, use_cache=True,\n',
     '                raise_alone=True, use_cache=True,\n',
     'test_check_on_hosts.py'),

    ("Discord /check asks for no SSH hint",
     'app/discord_bot.py',
     '                walk, checker_for=self._checker_for, hint=False,\n',
     '                walk, checker_for=self._checker_for, hint=True,\n',
     'test_check_on_hosts.py'),

    ("Discord /check lists the hosts in the order they were asked",
     'app/discord_bot.py',
     '                walk, checker_for=self._checker_for, hint=False,\n',
     '                walk[::-1], checker_for=self._checker_for, hint=False,\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /check skips a host without a checker, as it always did",
     'app/discord_bot.py',
     '        walk = [h for h in targets if self._checker_for(h) is not None]\n',
     '        walk = list(targets)\n',
     'test_check_on_hosts.py'),

    ("…and with nothing left starts no walk at all",
     'app/discord_bot.py',
     '                t=self.t) if walk else ():\n',
     '                t=self.t):\n',
     'test_check_on_hosts.py'),

    ("…not even for the pseudo-host check_on_hosts makes of []",
     'app/discord_bot.py',
     '        walk = [h for h in targets if self._checker_for(h) is not None]\n',
     '        walk = [h for h in targets if self._checker_for(h) is not None]'
     ' or [None]\n',
     'test_check_on_hosts.py'),

    ("Discord /check cuts a host's error to 80 characters",
     'app/discord_bot.py',
     '        return str(error)[:80]\n',
     '        return str(error)\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /check names the host that could not be checked",
     'app/discord_bot.py',
     '                                    host=getattr(hc.host, "name", "local"),\n',
     '                                    host="local",\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /check tags an update with its host",
     'app/discord_bot.py',
     '{self._label(hc.host)} — ',
     '{self._label(None)} — ',
     'test_discord_dispatch_matrix.py'),

    # W7d: Discord's reads and its container autocomplete take the
    # dead-host memory and a short probe.
    ("Discord /status skips a remote host found dead a moment ago",
     'app/discord_bot.py',
     '            targets, backend_for=self._backend_for, t=self.t,\n'
     '            use_cache=True)\n',
     '            targets, backend_for=self._backend_for, t=self.t,\n'
     '            use_cache=False)\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /check skips a remote host found dead a moment ago",
     'app/discord_bot.py',
     '                raise_alone=False, use_cache=True,\n',
     '                raise_alone=False, use_cache=False,\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /check keeps a skipped host's clause whole after the cut",
     'app/discord_bot.py',
     '            return hostslib.skipped_words(words, error.retry_in, t=self.t)\n',
     '            return str(error)[:80]\n',
     'test_discord_dispatch_matrix.py'),

    ("…and cuts the skipped host's words to 80, as a fresh failure's",
     'app/discord_bot.py',
     '            if len(words) > 80:\n'
     '                words = words[:79].rstrip() + "…"\n',
     '            if False:\n'
     '                words = words[:79].rstrip() + "…"\n',
     'test_discord_dispatch_matrix.py'),

    ("…with an ellipsis at the cut, so the stub before the clause is not "
     "read as a word",
     'app/discord_bot.py',
     '                words = words[:79].rstrip() + "…"\n',
     '                words = words[:80]\n',
     'test_discord_dispatch_matrix.py'),

    ("…after making them a person's, so the cut cannot leave the argv",
     'app/discord_bot.py',
     '            words = errfmt.human(error.reason or "", self.t)\n'
     '            if len(words) > 80:\n',
     '            words = error.reason or ""\n'
     '            if len(words) > 80:\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /status <container> does not ask a remembered host for the name",
     'app/discord_bot.py',
     '                if hostslib.remembered_dead(host) is not None:\n'
     '                    continue\n',
     '                if False:\n'
     '                    continue\n',
     'test_discord_dispatch_matrix.py'),

    ("Discord /status <container> gives a remote host ten seconds",
     'app/discord_bot.py',
     '            result = (backend.run(cmd, timeout=10) if remote\n',
     '            result = (backend.run(cmd) if remote\n',
     'test_discord_dispatch_matrix.py'),

    ("…and the local host its backend's default",
     'app/discord_bot.py',
     '        remote = host is not None and not getattr(host, "is_local", False)\n',
     '        remote = host is not None\n',
     'test_discord_dispatch_matrix.py'),

    ("…remembers a remote host whose listing raised",
     'app/discord_bot.py',
     '            hostslib.remember(host, clip(str(e)))\n',
     '            pass\n',
     'test_discord_dispatch_matrix.py'),

    ("…remembers a remote host whose listing exited non-zero",
     'app/discord_bot.py',
     '            hostslib.remember(host, words)\n',
     '            pass\n',
     'test_discord_dispatch_matrix.py'),

    ("…and clears the record of a host that answered",
     'app/discord_bot.py',
     '        hostslib.remember(host, None)\n        names = [',
     '        names = [',
     'test_discord_dispatch_matrix.py'),

    ("Discord autocomplete gives `ps -a` three seconds",
     'app/discord_bot.py',
     '        result = backend.run(["ps", "-a", "--format", "{{.Names}}"],\n'
     '                             timeout=3)\n',
     '        result = backend.run(["ps", "-a", "--format", "{{.Names}}"])\n',
     'test_discord_dispatch_matrix.py'),

    ("…does not ask a host found dead a moment ago",
     'app/discord_bot.py',
     '        if backend is None or hostslib.remembered_dead(host) is not None:\n',
     '        if backend is None:\n',
     'test_discord_dispatch_matrix.py'),

    ("…and marks no host dead itself",
     'app/discord_bot.py',
     '        result = backend.run(["ps", "-a", "--format", "{{.Names}}"],\n'
     '                             timeout=3)\n',
     '        try:\n'
     '            result = backend.run(["ps", "-a", "--format", "{{.Names}}"],\n'
     '                                 timeout=3)\n'
     '        except Exception as e:\n'
     '            hostslib.remember(host, str(e))\n'
     '            raise\n',
     'test_discord_dispatch_matrix.py'),

    ("Web Check Updates walks every host, not only the first",
     'app/web_ui.py',
     '                    list(hosts) if hosts else None,\n',
     '                    list(hosts)[:1] if hosts else None,\n',
     'test_web_action_matrix.py'),

    ("…each host with its own checker",
     'app/web_ui.py',
     '                    checker_for=lambda h: checker if h is None else h.checker,\n',
     '                    checker_for=lambda h: checker,\n',
     'test_manual_check_hosts.py'),

    ("Web Check Updates asks for no SSH hint",
     'app/web_ui.py',
     '                    hint=False, raise_alone=False, use_cache=True,\n',
     '                    hint=True, raise_alone=False, use_cache=True,\n',
     'test_check_on_hosts.py'),

    ("…hands check_all no bot, so DEBUG does not send a click's trace",
     'app/web_ui.py',
     '                    on_host=_report, t=None)\n',
     '                    on_host=_report, t=None, debug_sink=bot)\n',
     'test_check_on_hosts.py'),

    ("…and never raises, not even without a registry",
     'app/web_ui.py',
     '                    hint=False, raise_alone=False, use_cache=True,\n',
     '                    hint=False, raise_alone=True, use_cache=True,\n',
     'test_check_on_hosts.py'),

    ("…and skips a remote host found dead a moment ago (W7w)",
     'app/web_ui.py',
     '                    hint=False, raise_alone=False, use_cache=True,\n',
     '                    hint=False, raise_alone=False, use_cache=False,\n',
     'test_web_action_matrix.py'),

    ("Web Check Updates logs a host's failure on one host too",
     'app/web_ui.py',
     '                    if hc.error is not None:\n'
     '                        where = f" on {hc.host.name}" if named else ""\n',
     '                    if hc.error is not None and named:\n'
     '                        where = f" on {hc.host.name}" if named else ""\n',
     'test_check_on_hosts.py'),

    ("…naming the host only on a multi-host install",
     'app/web_ui.py',
     '                named = bool(hosts) and hosts.is_multi\n',
     '                named = bool(hosts)\n',
     'test_check_on_hosts.py'),

    ("…and naming the host that failed",
     'app/web_ui.py',
     '                        where = f" on {hc.host.name}" if named else ""\n',
     '                        where = f" on {hosts.local.name}" if named else ""\n',
     'test_web_action_matrix.py'),

    ("Web Check Updates notifies what the hosts found",
     'app/web_ui.py',
     '                        found.extend(hc.updates)\n',
     '                        found.extend(hc.updates[:0])\n',
     'test_web_action_matrix.py'),

    # W7: Telegram's reads (/status, /status <name>, /check, /check
    # --dry-run, /check <name>) take the dead-host memory and give a
    # remote lookup ten seconds; /update never does. Every mutation was
    # applied by hand in a scratch copy and turned its procedure red
    # without a traceback.
    ("a chat command does not wait 30 s on a host found dead a moment ago",
     'app/container_flags.py',
     '    for host in walk:\n'
     '        dead = _hosts.remembered_dead(host) if use_cache else None\n',
     '    for host in walk:\n'
     '        dead = None\n',
     'test_check_on_hosts.py'),

    ("…and /status then /check on Telegram does not ask the dead host again",
     'app/container_flags.py',
     '    for host in walk:\n'
     '        dead = _hosts.remembered_dead(host) if use_cache else None\n',
     '    for host in walk:\n'
     '        dead = None\n',
     'test_telegram_dispatch_matrix.py'),

    # Review of W7w: the memory is shared by readers in different
    # languages (the Web's check passes t=None, the chats their bot's), so
    # every walk keeps the CLI's words and `skipped_words` translates.
    ("a check remembers the CLI's words, not a reader's (Web, then German chat)",
     'app/container_flags.py',
     '            _hosts.remember(host, errfmt.clip(error))\n',
     '            _hosts.remember(host, _human(error, t))\n',
     'test_check_on_hosts.py'),

    ("a lookup remembers the CLI's words, not a reader's",
     'app/container_flags.py',
     '                _hosts.remember(host, errfmt.clip(kept))\n',
     '                _hosts.remember(host, gone.error)\n',
     'test_find_on_hosts.py'),

    ("a lookup's bare exit is remembered as \"exit code N\", untranslated",
     'app/container_flags.py',
     '                kept = stderr if stderr.strip() else _exit_text(rc, None)\n',
     '                kept = stderr if stderr.strip() else _exit_text(rc, t)\n',
     'test_find_on_hosts.py'),

    ("a skipped host's remembered \"exit code N\" is said in the reader's language",
     'app/hosts.py',
     '    if code and t is not None:\n',
     '    if False:\n',
     'test_find_on_hosts.py'),

    ("a check that failed with its daemon answering is not remembered as dead",
     'app/container_flags.py',
     '        elif use_cache and _lost_host(error):\n',
     '        elif use_cache:\n',
     'test_check_on_hosts.py'),

    ("…so /status after /check still asks that host (T2)",
     'app/container_flags.py',
     '        elif use_cache and _lost_host(error):\n',
     '        elif use_cache:\n',
     'test_telegram_dispatch_matrix.py'),

    ("a host that could not list its containers is remembered after a check",
     'app/container_flags.py',
     '    return isinstance(error, (subprocess.TimeoutExpired,\n'
     '                              ContainerListUnavailable))\n',
     '    return isinstance(error, (subprocess.TimeoutExpired,))\n',
     'test_check_on_hosts.py'),

    ("a host that checked fine is forgotten",
     'app/container_flags.py',
     '        if error is None:\n'
     '            _hosts.remember(host, None)\n',
     '        if False:\n'
     '            _hosts.remember(host, None)\n',
     'test_check_on_hosts.py'),

    # Review of W7: a host that answers a walk without the cache (every
    # write, Discord, the Web's check) is forgotten too — /update reached
    # nas and the next /status still skipped it as dead.
    ("a host that checked fine without the cache is forgotten too",
     'app/container_flags.py',
     '        if error is None:\n'
     '            _hosts.remember(host, None)\n',
     '        if use_cache and error is None:\n'
     '            _hosts.remember(host, None)\n',
     'test_check_on_hosts.py'),

    ("a skipped check says the remembered words and the seconds left",
     'app/container_flags.py',
     '            answer = HostCheck(host, None, _hosts.StillUnreachable(\n'
     '                _hosts.skipped_words(*dead, t=t), *dead),\n',
     '            answer = HostCheck(host, None, _hosts.StillUnreachable(\n'
     '                dead[0], *dead),\n',
     'test_check_on_hosts.py'),

    # Review of W7: hostdiag's hint is a check of the remembered words
    # and of this container's ~/.ssh, so a skipped host keeps it.
    ("a skipped check keeps hostdiag's hint when hints are asked for",
     'app/container_flags.py',
     '                _hosts.skipped_hint(host, dead[0], t=t) if hint else "",\n',
     '                "",\n',
     'test_check_on_hosts.py'),

    ("…and /status then /check on Telegram shows it under the refused host",
     'app/container_flags.py',
     '                _hosts.skipped_hint(host, dead[0], t=t) if hint else "",\n',
     '                "",\n',
     'test_telegram_dispatch_matrix.py'),

    ("…and only when hints are asked for",
     'app/container_flags.py',
     '                _hosts.skipped_hint(host, dead[0], t=t) if hint else "",\n',
     '                _hosts.skipped_hint(host, dead[0], t=t),\n',
     'test_check_on_hosts.py'),

    ("a skipped lookup keeps hostdiag's hint",
     'app/container_flags.py',
     '                               _hosts.skipped_hint(host, dead[0], t=t))\n',
     '                               "")\n',
     'test_find_on_hosts.py'),

    ("…and /status then /status web on Telegram shows it",
     'app/container_flags.py',
     '                               _hosts.skipped_hint(host, dead[0], t=t))\n',
     '                               "")\n',
     'test_telegram_dispatch_matrix.py'),

    ("a skipped check still reaches on_host, in its place",
     'app/container_flags.py',
     '            out.append(answer)\n'
     '            if on_host is not None:\n'
     '                on_host(answer)\n'
     '            continue\n',
     '            out.append(answer)\n'
     '            continue\n',
     'test_check_on_hosts.py'),

    ("a lookup skips a remote host found dead a moment ago",
     'app/container_flags.py',
     '    for host in targets:\n'
     '        names, reply, failure = [], None, None\n'
     '        dead = _hosts.remembered_dead(host) if use_cache else None\n',
     '    for host in targets:\n'
     '        names, reply, failure = [], None, None\n'
     '        dead = None\n',
     'test_find_on_hosts.py'),

    ("…and /status then /status web on Telegram names it without asking",
     'app/container_flags.py',
     '    for host in targets:\n'
     '        names, reply, failure = [], None, None\n'
     '        dead = _hosts.remembered_dead(host) if use_cache else None\n',
     '    for host in targets:\n'
     '        names, reply, failure = [], None, None\n'
     '        dead = None\n',
     'test_telegram_dispatch_matrix.py'),

    ("a lookup without the cache (every write) asks a remembered host",
     'app/container_flags.py',
     '    for host in targets:\n'
     '        names, reply, failure = [], None, None\n'
     '        dead = _hosts.remembered_dead(host) if use_cache else None\n',
     '    for host in targets:\n'
     '        names, reply, failure = [], None, None\n'
     '        dead = _hosts.remembered_dead(host)\n',
     'test_find_on_hosts.py'),

    ("…so /update right after /status asks the dead host again (T2)",
     'app/container_flags.py',
     '    for host in targets:\n'
     '        names, reply, failure = [], None, None\n'
     '        dead = _hosts.remembered_dead(host) if use_cache else None\n',
     '    for host in targets:\n'
     '        names, reply, failure = [], None, None\n'
     '        dead = _hosts.remembered_dead(host)\n',
     'test_telegram_dispatch_matrix.py'),

    ("a skipped lookup still reaches on_host, in its place",
     'app/container_flags.py',
     '            unreachable.append(gone)\n'
     '            if on_host is not None:\n'
     '                on_host(HostFind(host, (), None, gone))\n'
     '            continue\n',
     '            unreachable.append(gone)\n'
     '            continue\n',
     'test_find_on_hosts.py'),

    ("a lookup leaves what it learnt for the next read",
     'app/container_flags.py',
     '            if use_cache:\n'
     '                _hosts.remember(host, errfmt.clip(kept))\n',
     '            if False:\n'
     '                _hosts.remember(host, errfmt.clip(kept))\n',
     'test_find_on_hosts.py'),

    ("…so /status after /status web skips the dead host (T2)",
     'app/container_flags.py',
     '            if use_cache:\n'
     '                _hosts.remember(host, errfmt.clip(kept))\n',
     '            if False:\n'
     '                _hosts.remember(host, errfmt.clip(kept))\n',
     'test_telegram_dispatch_matrix.py'),

    ("a lookup without the cache marks no host dead",
     'app/container_flags.py',
     '            if use_cache:\n'
     '                _hosts.remember(host, errfmt.clip(kept))\n',
     '            if True:\n'
     '                _hosts.remember(host, errfmt.clip(kept))\n',
     'test_find_on_hosts.py'),

    ("a host without a backend is not remembered as answered or dead",
     'app/container_flags.py',
     '        elif backend is not None:\n'
     '            _hosts.remember(host, None)\n',
     '        else:\n'
     '            _hosts.remember(host, None)\n',
     'test_find_on_hosts.py'),

    ("a host that answers a lookup without the cache (/update's) is "
     "forgotten",
     'app/container_flags.py',
     '        elif backend is not None:\n'
     '            _hosts.remember(host, None)\n',
     '        elif use_cache and backend is not None:\n'
     '            _hosts.remember(host, None)\n',
     'test_find_on_hosts.py'),

    ("a remote lookup gets the short probe, the local one does not",
     'app/container_flags.py',
     '        probe = (remote_timeout if remote_timeout is not None\n'
     '                 and not _is_local(host) else timeout)\n',
     '        probe = (remote_timeout if remote_timeout is not None\n'
     '                 else timeout)\n',
     'test_find_on_hosts.py'),

    ("Telegram's read lookups ask a remote host with ten seconds (T2 rows)",
     'app/container_flags.py',
     '        probe = (remote_timeout if remote_timeout is not None\n'
     '                 and not _is_local(host) else timeout)\n',
     '        probe = timeout\n',
     'test_telegram_dispatch_matrix.py'),

    ("the /status overview skips a remote host found dead a moment ago",
     'app/status_render.py',
     '        _dead = _hosts.remembered_dead(_host) if use_cache else None\n',
     '        _dead = None\n',
     'test_status_overview_core.py'),

    ("…and /status then /status on Telegram does not ask it again",
     'app/status_render.py',
     '        _dead = _hosts.remembered_dead(_host) if use_cache else None\n',
     '        _dead = None\n',
     'test_telegram_dispatch_matrix.py'),

    ("the overview without the cache skips nobody",
     'app/status_render.py',
     '        _dead = _hosts.remembered_dead(_host) if use_cache else None\n',
     '        _dead = _hosts.remembered_dead(_host)\n',
     'test_status_overview_core.py'),

    ("a skipped overview host is listed with its words and the seconds left",
     'app/status_render.py',
     '            _skip = _hosts.skipped_words(*_dead, t=t)\n',
     '            _skip = _dead[0]\n',
     'test_status_overview_core.py'),

    ("a skipped overview host keeps its SSH hint under the words",
     'app/status_render.py',
     '            _h = _hosts.skipped_hint(_host, _dead[0], t=t)\n',
     '            _h = ""\n',
     'test_status_overview_core.py'),

    ("…and /status then /status on Telegram shows it",
     'app/status_render.py',
     '            _h = _hosts.skipped_hint(_host, _dead[0], t=t)\n',
     '            _h = ""\n',
     'test_telegram_dispatch_matrix.py'),

    ("hostdiag's hint for a skipped host is in the reader's language",
     'app/hosts.py',
     '        return hostdiag.hint(getattr(host, "endpoint", ""), reason or "",\n'
     '                             t=t) or ""\n',
     '        return hostdiag.hint(getattr(host, "endpoint", ""), reason or "",\n'
     '                             t=None) or ""\n',
     'test_find_on_hosts.py'),

    ("a hint that throws costs a skipped host the hint, not the walk",
     'app/hosts.py',
     '                             t=t) or ""\n'
     '    except Exception:                                       # noqa: BLE001\n'
     '        return ""\n',
     '                             t=t) or ""\n'
     '    except LookupError:\n'
     '        return ""\n',
     'test_find_on_hosts.py'),

    ("the overview remembers a host that did not answer, without its hint",
     'app/status_render.py',
     '    def _mark_unreachable(_host, _why):\n'
     '        if use_cache:\n'
     '            _hosts.remember(_host, _why)\n',
     '    def _mark_unreachable(_host, _why):\n'
     '        pass\n',
     'test_status_overview_core.py'),

    ("…the words of its line, not the hint under them",
     'app/status_render.py',
     '        if use_cache:\n'
     '            _hosts.remember(_host, _why)\n'
     '        # The host\'s own diagnostic hint (SSH auth, wrong socket …)\n'
     '        # appended when hostdiag has one.\n'
     '        try:\n'
     '            import hostdiag\n'
     '            _h = hostdiag.hint(getattr(_host, "endpoint", ""), _why, t=t)\n'
     '            if _h:\n'
     '                _why += "\\n" + _h\n'
     '        except Exception:\n'
     '            pass\n',
     '        # The host\'s own diagnostic hint (SSH auth, wrong socket …)\n'
     '        # appended when hostdiag has one.\n'
     '        try:\n'
     '            import hostdiag\n'
     '            _h = hostdiag.hint(getattr(_host, "endpoint", ""), _why, t=t)\n'
     '            if _h:\n'
     '                _why += "\\n" + _h\n'
     '        except Exception:\n'
     '            pass\n'
     '        if use_cache:\n'
     '            _hosts.remember(_host, _why)\n',
     'test_status_overview_core.py'),

    ("the overview forgets a host with nothing running: it answered",
     'app/status_render.py',
     '        if not ids:\n'
     '            _hosts.remember(_host, None)\n'
     '            continue\n',
     '        if not ids:\n'
     '            continue\n',
     'test_status_overview_core.py'),

    ("…without the cache too",
     'app/status_render.py',
     '        if not ids:\n'
     '            _hosts.remember(_host, None)\n'
     '            continue\n',
     '        if not ids:\n'
     '            if use_cache:\n'
     '                _hosts.remember(_host, None)\n'
     '            continue\n',
     'test_status_overview_core.py'),

    ("the overview forgets a host that answered",
     'app/status_render.py',
     '        _hosts.remember(_host, None)\n'
     '        try:\n'
     '            _cfgs = json.loads(ins_p.stdout) or []\n',
     '        try:\n'
     '            _cfgs = json.loads(ins_p.stdout) or []\n',
     'test_status_overview_core.py'),

    ("the overview without the cache forgets a host that answered",
     'app/status_render.py',
     '        _hosts.remember(_host, None)\n'
     '        try:\n'
     '            _cfgs = json.loads(ins_p.stdout) or []\n',
     '        if use_cache:\n'
     '            _hosts.remember(_host, None)\n'
     '        try:\n'
     '            _cfgs = json.loads(ins_p.stdout) or []\n',
     'test_status_overview_core.py'),

    ("the local host is never skipped or marked",
     'app/hosts.py',
     '    if host is None or getattr(host, "is_local", False):\n'
     '        return ""\n',
     '    if host is None:\n'
     '        return ""\n',
     'test_find_on_hosts.py'),

    ("a skipped host's seconds are whole",
     'app/hosts.py',
     '        return reason, math.ceil(left)\n',
     '        return reason, left\n',
     'test_find_on_hosts.py'),

    ("a skipped host's seconds are rounded up: never \"0s\" while skipped",
     'app/hosts.py',
     '        return reason, math.ceil(left)\n',
     '        return reason, int(left)\n',
     'test_status_overview_core.py'),

    ("a skipped host's remembered words are said as errfmt.human says them",
     'app/hosts.py',
     '    try:\n'
     '        reason = errfmt.human(reason or "", t)\n',
     '    try:\n'
     '        reason = reason or ""\n',
     'test_telegram_dispatch_matrix.py'),

    ("a Telegram read's failure moves the backoff the Web page sees",
     'app/hosts.py',
     '    else:\n'
     '        mark_unreachable(name, failed_with)\n',
     '    else:\n'
     '        mark_unreachable(name, failed_with, cooldown=UNREACHABLE_COOLDOWN)\n',
     'test_status_overview_core.py'),

    ("answering clears a host's mark and its backoff",
     'app/hosts.py',
     '    if failed_with is None:\n'
     '        mark_reachable(name)\n',
     '    if failed_with is None:\n'
     '        pass\n',
     'test_find_on_hosts.py'),

    ("failing marks a host with the words it gave",
     'app/hosts.py',
     '    else:\n'
     '        mark_unreachable(name, failed_with)\n',
     '    else:\n'
     '        pass\n',
     'test_find_on_hosts.py'),

    ("a skipped host's line is in the reader's language",
     'app/hosts.py',
     '            return t("host_skipped_retry", error=reason, seconds=retry_in)\n',
     '            raise LookupError("no translation")\n',
     'test_find_on_hosts.py'),

    ("the remembered words come first in the skipped line",
     'app/lang/de.json',
     '"host_skipped_retry": "{error} (vom letzten Versuch, wird {seconds} s lang nicht erneut gefragt)"',
     '"host_skipped_retry": "(vom letzten Versuch, wird {seconds} s lang nicht erneut gefragt) {error}"',
     'test_check_on_hosts.py'),

    # Review of W7: with the backoff a host is skipped up to 900 s, so
    # the clause claims nothing about the words' age.
    ("a skipped host's line does not claim its words are a moment old",
     'app/lang/en.json',
     '"host_skipped_retry": "{error} (from the last try, not asked again for {seconds}s)"',
     '"host_skipped_retry": "{error} (from a moment ago, not asked again for {seconds}s)"',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /status takes the cache",
     'app/telegram_bot.py',
     '                status_targets, backend_for=self._backend_for, t=self.t,\n'
     '                use_cache=True)\n',
     '                status_targets, backend_for=self._backend_for, t=self.t,\n'
     '                use_cache=False)\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /check takes the cache",
     'app/telegram_bot.py',
     '            use_cache=True,\n'
     '            debug_sink=self, hint=True, on_host=_report, t=self.t)\n',
     '            use_cache=False,\n'
     '            debug_sink=self, hint=True, on_host=_report, t=self.t)\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /check --dry-run takes the cache",
     'app/telegram_bot.py',
     '                use_cache=True,\n'
     '                debug_sink=self, on_host=_dry, t=self.t)\n',
     '                use_cache=False,\n'
     '                debug_sink=self, on_host=_dry, t=self.t)\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /check <name>'s check takes the cache",
     'app/telegram_bot.py',
     '                use_cache=True,\n'
     '                only_for=scoped.__getitem__, debug_sink=self,\n'
     '                on_host=_scoped_check, t=self.t)\n',
     '                use_cache=False,\n'
     '                only_for=scoped.__getitem__, debug_sink=self,\n'
     '                on_host=_scoped_check, t=self.t)\n',
     'test_check_on_hosts.py'),

    ("Telegram /update <name>'s check never takes the cache",
     'app/telegram_bot.py',
     '                only_for=scoped.__getitem__, debug_sink=self,\n'
     '                on_host=_batch, t=self.t)\n',
     '                only_for=scoped.__getitem__, debug_sink=self,\n'
     '                use_cache=True, on_host=_batch, t=self.t)\n',
     'test_check_on_hosts.py'),

    ("Telegram /check <name>'s lookup is a read",
     'app/telegram_bot.py',
     '                    arg, backend=self._backend_for(host), host=host,\n'
     '                    read=True)\n',
     '                    arg, backend=self._backend_for(host), host=host)\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /status <name> takes the cache",
     'app/telegram_bot.py',
     '                    use_cache=True, remote_timeout=10,\n',
     '                    use_cache=False, remote_timeout=10,\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /status <name> asks a remote host with ten seconds",
     'app/telegram_bot.py',
     '                    use_cache=True, remote_timeout=10,\n',
     '                    use_cache=True,\n',
     'test_telegram_dispatch_matrix.py'),

    ("Telegram /update <name>'s lookup is a write: no cache, no short probe",
     'app/telegram_bot.py',
     '            arg=arg, use_cache=read, remote_timeout=10 if read else None,\n',
     '            arg=arg, use_cache=True, remote_timeout=10,\n',
     'test_telegram_dispatch_matrix.py'),


    # Q2: Discord's claims and both cleanup guards go through the engine's
    # door; the engine's guard answers busy with the key, each chat says
    # it in its own language and hands on its own queued self-update.
    ('a cleanup never prunes while an update holds the lock',
     'app/update_engine.py',
     '        if not self.claim_update():\n'
     '            return None, "cleanup_busy"\n',
     '        if not self.claim_update():\n'
     '            pass\n',
     'test_op_coordination.py'),

    ('…and the busy answer is the key, each chat says it itself',
     'app/update_engine.py',
     '            return None, "cleanup_busy"\n',
     '            return None, "⏳ Updates in progress — cleanup skipped."\n',
     'test_op_coordination.py'),

    ('a cleanup hands on a queued self-update once the lock is back',
     'app/update_engine.py',
     '        finally:\n'
     '            self.release_update(after=after)\n',
     '        finally:\n'
     '            self.release_update()\n',
     'test_op_coordination.py'),

    ('a crashing prune still gives the update lock back',
     'app/update_engine.py',
     '            return checker.cleanup_images()\n'
     '        finally:\n'
     '            self.release_update(after=after)\n',
     '            res = checker.cleanup_images()\n'
     '            self.release_update(after=after)\n'
     '            return res\n'
     '        finally:\n'
     '            pass\n',
     'test_op_coordination.py'),

    ("Telegram's cleanup says busy in Telegram's language",
     'app/telegram_bot.py',
     '        if ok is None:\n'
     '            return None, self.t(msg)\n'
     '        return ok, msg\n',
     '        if ok is None:\n'
     '            return None, msg\n'
     '        return ok, msg\n',
     'test_op_coordination.py'),

    ("…and hands on Telegram's queued self-update",
     'app/telegram_bot.py',
     '            checker, after=self._run_queued_selfupdate)\n',
     '            checker)\n',
     'test_op_coordination.py'),

    ("Discord's cleanup says busy in Discord's language",
     'app/discord_bot.py',
     '        if ok is None:\n'
     '            return None, self.t(msg)\n'
     '        return ok, msg\n',
     '        if ok is None:\n'
     '            return None, msg\n'
     '        return ok, msg\n',
     'test_op_coordination.py'),

    ("…and hands on Discord's queued self-update",
     'app/discord_bot.py',
     '            checker, after=self._hand_on_selfupdate)\n',
     '            checker)\n',
     'test_op_coordination.py'),

    ('a Discord update hands a queued self-update on through the door',
     'app/discord_bot.py',
     '        self.engine.release_update(after=self._hand_on_selfupdate)\n',
     '        self.engine.release_update()\n',
     'test_discord_bot.py'),

    ('Discord /update refuses while the update lock is held',
     'app/discord_bot.py',
     '        if any(pin_list_unreadable(self._checker_for(h)) for h, _t in jobs):\n'
     '            return self.t("update_refused_pin_list")\n'
     '        if not self.engine.claim_update():\n',
     '        if any(pin_list_unreadable(self._checker_for(h)) for h, _t in jobs):\n'
     '            return self.t("update_refused_pin_list")\n'
     '        if False:\n',
     'test_update_lock_sites.py'),

    ('…and so does a confirmed Discord /updateall',
     'app/discord_bot.py',
     '        if any(pin_list_unreadable(self._checker_for(h)) for h, _e in batches):\n'
     '            return self.t("update_refused_pin_list")\n'
     '        if not self.engine.claim_update():\n',
     '        if any(pin_list_unreadable(self._checker_for(h)) for h, _e in batches):\n'
     '            return self.t("update_refused_pin_list")\n'
     '        if False:\n',
     'test_update_lock_sites.py'),

    ("the lock-site list sees a claim through the engine's cleanup guard",
     'scripts/test_update_lock_sites.py',
     '                    elif c in (DOOR_CLAIM, DOOR_RELEASE) or (\n'
     '                            c == DOOR_GUARDED and _on_engine(f)):\n',
     '                    elif c in (DOOR_CLAIM, DOOR_RELEASE):\n',
     'test_update_lock_sites.py'),

    ("…but not the bot's delegator called by the scheduler and the Web UI",
     'scripts/test_update_lock_sites.py',
     '                            c == DOOR_GUARDED and _on_engine(f)):\n',
     '                            c == DOOR_GUARDED):\n',
     'test_update_lock_sites.py'),

    # Q5: one reader and one writer for pending_updates.json. Each line
    # that decides what a caller gets out of the file, or what a removal
    # leaves in it, is broken here; test_pending_file drives the core and
    # every caller on missing, corrupt, wrongly-shaped and valid files.
    ('a corrupt pending file reads as nothing pending, not a crash',
     'app/pending.py',
     '    except (OSError, ValueError):\n        return None\n',
     '    except OSError:\n        return None\n',
     'test_pending_file.py'),

    ('…and so does a pending file that is not a list',
     'app/pending.py',
     '    return data if isinstance(data, list) else None\n',
     '    return data\n',
     'test_pending_file.py'),

    ('named_only keeps only the pending entries that carry a name',
     'app/pending.py',
     '    if named_only:\n        return [e for e in data if isinstance(e, dict) and e.get("name")]\n',
     '    if named_only:\n        return data\n',
     'test_pending_file.py'),

    ("entries_for hands out only the asked hosts' pending entries",
     'app/pending.py',
     '            if isinstance(e, dict) and entry_host(e) in names]\n',
     '            if isinstance(e, dict)]\n',
     'test_pending_file.py'),

    ('a pending entry is identified by (host, name), not the name',
     'app/pending.py',
     '    return entry_host(entry), entry.get("name")\n',
     '    return LOCAL_HOST, entry.get("name")\n',
     'test_pending_file.py'),

    ('a bare name in a pending removal means the local host',
     'app/pending.py',
     '    wanted = {(LOCAL_HOST, k) if isinstance(k, str) else tuple(k)\n              for k in keys}\n',
     '    wanted = {tuple(k)\n              for k in keys}\n',
     'test_pending_file.py'),

    ('a pending removal keeps entries that are not dicts',
     'app/pending.py',
     '                 if not isinstance(e, dict) or key_of(e) not in wanted]\n',
     '                 if isinstance(e, dict) and key_of(e) not in wanted]\n',
     'test_pending_file.py'),

    ('a pending removal leaves a file it cannot read alone',
     'app/pending.py',
     '    data = load(path)\n    if data is None:\n        return None\n    wanted',
     '    data = load(path)\n    if data is None:\n        data = []\n    wanted',
     'test_pending_file.py'),

    ('a pending removal that empties the file deletes it when asked',
     'app/pending.py',
     '    if remaining or not delete_when_empty:\n',
     '    if True:\n',
     'test_pending_file.py'),

    ('the pending file is written atomically',
     'app/pending.py',
     '    atomic_write_json(path, entries)\n',
     '    with open(path, "w") as fh:\n        json.dump(entries, fh)\n',
     'test_pending_file.py'),

    ("Telegram's pending removal deletes the emptied file",
     'app/telegram_bot.py',
     '        _pending.remove(self.config.pending_file, keys,\n                        delete_when_empty=True)\n',
     '        _pending.remove(self.config.pending_file, keys,\n                        delete_when_empty=False)\n',
     'test_pending_file.py'),

    ("Telegram's read_pending hands back every entry",
     'app/telegram_bot.py',
     '    return _pending.read(path)\n',
     '    return _pending.read(path, named_only=True)\n',
     'test_pending_file.py'),

    ("Discord's pending removal deletes the emptied file",
     'app/discord_bot.py',
     '        _pending.remove(getattr(self.config, "pending_file", ""), keys,\n                        delete_when_empty=True)\n',
     '        _pending.remove(getattr(self.config, "pending_file", ""), keys,\n                        delete_when_empty=False)\n',
     'test_pending_file.py'),

    ('Discord reads only named pending entries',
     'app/discord_bot.py',
     '        return _pending.read(getattr(self.config, "pending_file", ""),\n                             named_only=True)\n',
     '        return _pending.read(getattr(self.config, "pending_file", ""),\n                             named_only=False)\n',
     'test_pending_file.py'),

    ("Discord's pending list for a host is that host's",
     'app/discord_bot.py',
     '        return _pending.entries_for(self._pending(), [host_name_of(host)])\n',
     '        return _pending.entries_for(self._pending(), ["local"])\n',
     'test_pending_file.py'),

    ("the Web UI's pending list for a host is that host's",
     'app/web_ui.py',
     '                                        [host or LOCAL_HOST])\n',
     '                                        [LOCAL_HOST])\n',
     'test_pending_file.py'),

    ("Discord's /updates lists every pending entry, nameless ones too",
     'app/discord_bot.py',
     '        pending = _pending.read(getattr(self.config, "pending_file", ""))\n',
     '        pending = _pending.read(getattr(self.config, "pending_file", ""),\n                                named_only=True)\n',
     'test_pending_file.py'),

    # Q6: Telegram's update flows read and write pending_updates.json
    # through app/pending.py. An unreadable file is answered as nothing
    # pending instead of raising (T2 q6_checks, T3 q6_checks), and the
    # moved writes keep what they wrote before (T3 rows).
    # The single update reads and rewrites through the engine's
    # run_keys since Q11a.
    ('a single update answers an unreadable pending file instead of raising',
     'app/update_engine.py',
     '        updates = _pending.load(pending_file)\n',
     '        updates = (__import__("json").load(open(pending_file, encoding="utf-8"))\n                   if __import__("os").path.exists(pending_file) else None)\n',
     'test_telegram_callback_matrix.py'),

    ('…and tells it from a file that holds [], which is "not in the list"',
     'app/update_engine.py',
     '                if updates is None:\n',
     '                if not updates:\n',
     'test_telegram_callback_matrix.py'),

    ('a single update that takes the last entry leaves [], not no file',
     'app/update_engine.py',
     '                _pending.write(pending_file, updates)\n',
     '                _pending.remove(pending_file,\n                                [(host_name, name)],\n                                delete_when_empty=True)\n',
     'test_telegram_callback_matrix.py'),

    ('the bare "Update all" answers an unreadable pending file instead of raising',
     'app/telegram_bot.py',
     '                updates = _pending.read(pending_file)\n',
     '                with open(pending_file, encoding="utf-8") as f:\n                    updates = json.load(f)\n',
     'test_telegram_dispatch_matrix.py'),

    ('…and deletes the pending file when it is done',
     'app/telegram_bot.py',
     '                _pending.clear(pending_file)\n',
     '                pass\n',
     'test_telegram_callback_matrix.py'),

    ('/updates answers an unreadable pending file instead of raising',
     'app/telegram_bot.py',
     '            pending = _pending.read(self.config.pending_file)\n',
     '            pending = json.load(open(self.config.pending_file, encoding="utf-8"))\n',
     'test_telegram_dispatch_matrix.py'),

    ('/updates @host lists that host\'s pending entries only',
     'app/telegram_bot.py',
     '                                               [h.name for h in targets])\n',
     '                                               ["local"])\n',
     'test_telegram_dispatch_matrix.py'),

    ('"I\'ll do it manually" keeps the other hosts\' pending entries',
     'app/telegram_bot.py',
     '                    pending_store.write(self.config.pending_file, keep)\n',
     '                    pending_store.clear(self.config.pending_file)\n',
     'test_telegram_callback_matrix.py'),

    ('…and deletes the pending file on a single host',
     'app/telegram_bot.py',
     '            else:\n                pending_store.clear(self.config.pending_file)\n',
     '            else:\n                pass\n',
     'test_telegram_callback_matrix.py'),

    ('pending.clear is fine with a file that is already gone',
     'app/pending.py',
     '    try:\n        os.remove(path)\n    except OSError:\n        pass\n',
     '    os.remove(path)\n',
     'test_pending_file.py'),

    ('…and with no path',
     'app/pending.py',
     '    if not path:\n        return\n    try:\n',
     '    try:\n',
     'test_pending_file.py'),

    ('a pending removal that empties the file really deletes it',
     'app/pending.py',
     '        clear(path)\n',
     '        pass\n',
     'test_pending_file.py'),

    ('the command matrix really seeds an unreadable pending file for Q6',
     'scripts/test_telegram_dispatch_matrix.py',
     '        with open(cfg.pending_file, "wb") as f:\n            f.write(PENDING_RAW[0])\n        return\n',
     '        pass\n',
     'test_telegram_dispatch_matrix.py'),

    ('the callback matrix records an unreadable pending file as its bytes',
     'scripts/test_telegram_callback_matrix.py',
     '                else f"unreadable: {raw!r}"\n',
     '                else []\n',
     'test_telegram_callback_matrix.py'),

    ("the auto-update batch carries the other hosts' pending entries over",
     'app/telegram_bot.py',
     '                _pending.write(self.config.pending_file, others + remaining)\n',
     '                _pending.write(self.config.pending_file, remaining)\n',
     'test_host_state_wiring.py'),

    # Q7b: the drain after a single update gets the checker the tap was
    # called with, so a queued local key no longer runs on the tapped
    # remote host's checker (T3 "queue drain after a remote tap"); the
    # reverse row ("…after a local tap") holds that a queued remote key
    # still runs on its own host's.
    ('a drained entry runs on its own host\'s checker, not the tapped one\'s',
     'app/telegram_bot.py',
     '                base_checker,\n',
     '                checker,\n',
     'test_telegram_callback_matrix.py'),

    ('…and a tapped or queued remote key runs on its own host\'s checker',
     'app/telegram_bot.py',
     '        base_checker = checker\n        checker = self._checker_for(host, checker)\n',
     '        base_checker = checker\n',
     'test_telegram_callback_matrix.py'),

    # Q8: the drain loop moved into UpdateEngine.drain_queue. Its rules
    # had no guard of their own before; test_update_queue drives the
    # engine with a run_one recorder, and Telegram's wrapper on top.
    ("the drain stops for a pending self-update",
     "app/update_engine.py",
     "        if not self._queued_selfupdate and not swapping:\n"
     "            return False\n",
     "        if True:\n"
     "            return False\n",
     "test_update_queue.py"),

    ("…and names what it dropped",
     "app/update_engine.py",
     '            say("update_queue_dropped_selfupdate", {"keys": left})\n',
     "            pass\n",
     "test_update_queue.py"),

    ("a failure in the drain takes its group-mates off the queue",
     "app/update_engine.py",
     "            if last_group:\n                failed_group = last_group\n",
     "            if False:\n                failed_group = last_group\n",
     "test_update_queue.py"),

    ("…and says which, with the group",
     "app/update_engine.py",
     '                    say("update_queue_skipped_group",\n'
     '                        {"group": failed_group, "keys": mates})\n',
     "                    pass\n",
     "test_update_queue.py"),

    ("the group a drained run leaves is the one its failure takes down",
     "app/update_engine.py",
     "                last_group = run_one(key)\n",
     "                run_one(key)\n",
     "test_update_queue.py"),

    # Q10 replaced the probe and its re-enqueue ("a drained entry that
    # loses the race goes back on the queue", "…and the per-entry probe
    # gives the lock straight back") with a claim held through each
    # entry; their entries are further down.

    ("a drained run that raises is said and the drain goes on",
     "app/update_engine.py",
     "                last_group = None\n                continue\n",
     "                return\n",
     "test_update_queue.py"),

    ("…said, with the key and the error",
     "app/update_engine.py",
     '                say("entry_failed", {"key": key, "error": str(e)})\n',
     "                pass\n",
     "test_update_queue.py"),

    ("Telegram's drain hands the engine its last group",
     "app/telegram_bot.py",
     "                                say=self._say_drain, last_group=last_group,\n",
     "                                say=self._say_drain, last_group=None,\n",
     "test_update_queue.py"),

    ("…and a run's failed group, from the run itself",
     "app/telegram_bot.py",
     '            return getattr(self, "_last_update_group", None)\n',
     "            return None\n",
     "test_update_queue.py"),

    ("…and runs each entry as a queued one, not as a fresh tap",
     "app/telegram_bot.py",
     "            self._run_single_update(checker, key, from_queue=True,\n",
     "            self._run_single_update(checker, key,\n",
     "test_update_queue.py"),

    ("…and names the containers the drain reports",
     "app/telegram_bot.py",
     '        kw["names"] = ", ".join(f"`{self._short_key(k)}`"\n',
     '        kw["names"] = "" and ", ".join(f"`{self._short_key(k)}`"\n',
     "test_update_queue.py"),

    ("…and cuts a raised error to 200 characters",
     "app/telegram_bot.py",
     "{params['error'][:200]}\")\n",
     "{params['error']}\")\n",
     "test_update_queue.py"),

    # Q10: the drain claims before it takes an entry, holds the claim
    # through it and is its one releaser. Before, it took the lock, gave
    # it straight back and let the run claim again; a claim landing in
    # between sent the entry to the back with a second "queued".
    ("a drained entry whose claim is lost keeps its place at the head",
     "app/update_engine.py",
     "            if not self.claim_update():\n"
     "                # Something else holds it.",
     "            if not self.claim_update():\n"
     "                self.enqueue_update(self.take_queued_update())\n"
     "                return\n"
     "                # Something else holds it.",
     "test_update_queue.py"),

    ("…and the drain holds its claim through the entry",
     "app/update_engine.py",
     "            key = self.take_queued_update()\n            if key is None:\n"
     "                # Emptied between",
     "            self._update_lock.release()\n"
     "            key = self.take_queued_update()\n            if key is None:\n"
     "                # Emptied between",
     "test_update_queue.py"),

    ("…and gives it back after each entry",
     "app/update_engine.py",
     "            self.release_update(after=after)\n\n"
     "    def _drop_for_selfupdate(",
     "            pass\n\n"
     "    def _drop_for_selfupdate(",
     "test_update_queue.py"),

    ("…handing each release the connection's after()",
     "app/update_engine.py",
     "            self.release_update(after=after)\n\n"
     "    def _drop_for_selfupdate(",
     "            self.release_update(after=None)\n\n"
     "    def _drop_for_selfupdate(",
     "test_update_queue.py"),

    ("…and does not claim at all for an empty queue",
     "app/update_engine.py",
     "            if not self.has_queued_updates():\n                return\n"
     "            # Claimed per entry",
     "            # Claimed per entry",
     "test_update_queue.py"),

    ("a queue emptied between the drain's look and its claim gives the claim back",
     "app/update_engine.py",
     "                # Emptied between the look and the claim.\n"
     "                self.release_update(after=after)\n",
     "                # Emptied between the look and the claim.\n",
     "test_update_queue.py"),

    # Q10 review: the rest was named and dropped before the self-update
    # had run, under the drain's claim. A self-update that came back
    # without a restart lost b and c for nothing, and a `say` that raised
    # there kept the update lock for good.
    ("a drained entry is given back before anything is said about it",
     "app/update_engine.py",
     "            # this entry runs from this release, through `after`, and the\n"
     "            # top of the loop then sees whether it is restarting.\n"
     "            self.release_update(after=after)\n",
     "            # this entry runs from this release, through `after`, and the\n"
     "            # top of the loop then sees whether it is restarting.\n"
     "            if self._drop_for_selfupdate(say):\n"
     "                self.release_update(after=after)\n"
     "                return\n"
     "            self.release_update(after=after)\n",
     "test_update_queue.py"),

    ("…a self-update after a drained entry that does not restart drops nothing",
     "app/update_engine.py",
     "            # top of the loop then sees whether it is restarting.\n"
     "            self.release_update(after=after)\n",
     "            # top of the loop then sees whether it is restarting.\n"
     "            _su = self._queued_selfupdate\n"
     "            self.release_update(after=after)\n"
     "            if _su:\n"
     "                self.drop_queued_updates()\n"
     "                return\n",
     "test_update_queue.py"),

    ("…and a swap under way names the rest it takes with it",
     "app/update_engine.py",
     "        if not self._queued_selfupdate and not swapping:\n",
     "        if not self._queued_selfupdate:\n",
     "test_update_queue.py"),

    ("a drained entry that crashed is given back inside the crash, so after() sees it",
     "app/update_engine.py",
     "            except BaseException as e:\n"
     "                # Given back inside the handler",
     "            except BaseException as _crash:\n"
     "                e = _crash\n"
     "            else:\n"
     "                e = None\n"
     "            if e is not None:\n"
     "                # Given back inside the handler",
     "test_update_queue.py"),

    ("…and a drained run that exits is not swallowed",
     "app/update_engine.py",
     "                if not isinstance(e, Exception):\n                    raise\n",
     "                if False:\n                    raise\n",
     "test_update_queue.py"),

    ("…nor left holding the lock",
     "app/update_engine.py",
     "                self.release_update(after=after)\n"
     "                if not isinstance(e, Exception):\n",
     "                if not isinstance(e, Exception):\n",
     "test_update_queue.py"),

    ("Telegram's drained entry does not claim for itself",
     "app/telegram_bot.py",
     "        if not held_by_caller:\n"
     "            claimed, busy, params",
     "        if True:\n"
     "            claimed, busy, params",
     "test_update_queue.py"),

    ("…nor give back the drain's claim",
     "app/telegram_bot.py",
     "            if not held_by_caller:\n"
     "                self.engine.release_update(after=self._run_queued_selfupdate)\n",
     "            if True:\n"
     "                self.engine.release_update(after=self._run_queued_selfupdate)\n",
     "test_update_queue.py"),

    ("…because Telegram's drain says the claim is held",
     "app/telegram_bot.py",
     "                                    claimed=True)\n",
     "                                    claimed=False)\n",
     "test_update_queue.py"),

    ("…and hands the drain its queued self-update",
     "app/telegram_bot.py",
     "                                after=self._run_queued_selfupdate)\n",
     "                                after=None)\n",
     "test_update_queue.py"),

]

#: Promises with no guard at all — measured, not suspected. Each was
#: mutated and the WHOLE suite run; the number is how many procedures
#: went red, and it is zero for every one of them.
#:
#: `watchers` are the procedures that would plausibly notice, and the
#: only ones run here: proving "nothing in 181 catches it" takes two
#: minutes per entry, which is not a price a pre-commit run can pay. The
#: full sweep is the pre-tag audit's job; this keeps the finding from
#: rotting away between audits.
#: Promises measured to have no guard at all. The list may only get
#: shorter — an entry that starts failing means somebody wrote a real
#: check, and the message says to move it up.
#:
#: **Empty since 16.10.2026.** The six that stood here came out of the
#: 10.10. sweep, and the last three went with
#: `test_debt_closers.py`. That is a milestone and not a finish line:
#: empty means every promise this file knows about is falsifiable, not
#: that every promise is here. The next sweep is what tells us which
#: ones are not.
UNGUARDED = []

#: How many promises are known to be unguarded. May only go down.
#:
#: The sweep found more than these — `/metrics` content, the group
#: wait, the disk-warning threshold, the self-update chain, the Discord
#: restore parity. They are in my audit notes with their measurements; an
#: entry only comes in here once its anchor is pinned down, because a
#: stale anchor is a check that passed without running.
DEBT = 0


def _tree():
    """A copy of the whole program to break, minus history and caches.

    The whole thing, not just `app/` and `scripts/`: the first version
    copied those two and `test_gluetun_docs.py` then died on a missing
    `docs/updates.md` — which this file read as "the guard was caught",
    because a crash and a failed check look the same from out here. It
    reported a promise as guarded that is not. Hence `_baseline` below
    as well; a mutation is only evidence if the procedure passed before
    it.
    """
    d = tempfile.mkdtemp(prefix="guardmut-")
    repo = os.path.join(d, "repo")
    for rel in _program_files(ROOT):
        src = os.path.join(ROOT, rel)
        if not os.path.isfile(src) or _cache(rel):
            continue                        # deleted in the work tree
        dst = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return repo


def _program_files(root):
    """What git counts as the program: tracked files plus new ones that
    no ignore rule covers.

    The copy used to be the whole directory minus a list of names, and
    that took everything local along: my notes, the audit reports, the
    backups, whole worktrees and the dev compose file with its real
    credentials. Eight full copies from interrupted runs sat in /tmp on
    18.11.2026, seven of them with that file inside. A list of names to
    leave out is a list that is always one name short; the ignore rules
    already say what is not the program, so the copy reads them.
    """
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"],
        capture_output=True, check=True).stdout
    return sorted({p for p in out.decode("utf-8").split("\0") if p})


def _cache(rel):
    parts = rel.split("/")
    return "__pycache__" in parts or rel.endswith(".pyc")


def _drop(tree):
    """Remove a copy made by `_tree`, the mkdtemp directory included.

    Removing only `…/repo` left an empty `guardmut-*` behind on every
    run: 68 of them were in /tmp on 18.11.2026.
    """
    shutil.rmtree(os.path.dirname(tree), ignore_errors=True)


def _mutate(tree, rel, anchor, replacement):
    """Apply one mutation. `(ok, note)` — `ok` False means it did not
    apply, which is a failure of this file, not of the suite."""
    path = os.path.join(tree, rel)
    src = open(path, encoding="utf-8").read()
    n = src.count(anchor)
    if n != 1:
        return False, f"anchor occurs {n}x in {rel}, expected once"
    open(path, "w", encoding="utf-8").write(src.replace(anchor, replacement))
    return True, ""


def _restore(tree, rel):
    shutil.copyfile(os.path.join(ROOT, rel), os.path.join(tree, rel))


def _red(tree, procedure, timeout=240):
    """`True` failed a check, `False` passed, `None` could not say.

    The three are different and the first version had two. It returned
    `r.returncode != 0`, so a procedure that CRASHED — a traceback, no
    verdict line, not one check reported — counted as having caught the
    mutation. Two entries lived off that: one mutation opened an auth
    gate and the procedure then died in `save_persistent`, printing no
    ✅ or ❌ at all, and another named a method that does not exist, so
    an `AttributeError` made it red whatever the checks said.
    So a crash is `None` and is reported as such, not banked.

    `PYTHONDONTWRITEBYTECODE` is the other half, and it is not a tidying
    detail. Python invalidates a `.pyc` on `(mtime seconds, size)`; the
    mutations here are written within the same second and several leave
    the size unchanged, so the second run of a procedure imported the
    FIRST mutation's bytecode. Measured: a cosmetic comment edit was
    certified as a caught mutation. Four entries on `update_checker.py`
    were in that position.
    """
    p = os.path.join(tree, "scripts", procedure)
    if not os.path.exists(p):
        return None
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        r = subprocess.run([sys.executable, p], cwd=tree, timeout=timeout,
                           env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        return None
    out = (r.stdout or b"").decode("utf-8", "replace")
    said_something = ("FAIL" in out or "PASS" in out
                      or "✅" in out or "❌" in out)
    if not said_something:
        return None                     # it died before judging anything
    if r.returncode != 0 and "Traceback (most recent call last)" in out:
        # It judged some checks and THEN fell over. The verdict is not
        # trustworthy: the mutation may have broken the fixture rather
        # than tripped a check.
        return None
    return r.returncode != 0


_BASE = {}


def _baseline(tree, procedure):
    """Is `procedure` green on the untouched copy?

    Asked once per procedure and remembered. Without it a procedure that
    cannot run in the copy at all — a missing file, a daemon it needs —
    counts as having caught every mutation, and this file then certifies
    guards that do not exist. It did exactly that once.
    """
    if procedure not in _BASE:
        _BASE[procedure] = _red(tree, procedure) is False
    return _BASE[procedure]


def _mutation_still_parses(rel, anchor, repl):
    """Does this mutation leave a file Python can still read?

    A mutation that produces a SyntaxError does not test anything: every
    import of that module dies, the procedure prints no verdict, and
    `_red` reports None — which lands in `[unrunnable]`, several screens
    away from the cause. That is exactly what two entries did on
    16.11.2026, after their anchors were re-pointed at a changed call
    and the replacement kept a trailing comma the anchor no longer
    covered: `int(_env(...)),,`. Asked here, the message names the
    entry and the syntax error instead.

    Only for Python; a mutation in a .md or .json file has nothing to
    parse.
    """
    if not rel.endswith(".py"):
        return True, ""
    try:
        text = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
    except OSError as e:
        return False, str(e)
    if text.count(anchor) != 1:
        return True, ""          # the stale check below says it better
    try:
        ast.parse(text.replace(anchor, repl))
    except SyntaxError as e:
        return False, f"the mutation leaves invalid Python: {e.msg} (line {e.lineno})"
    return True, ""


def main():
    checks = {}
    tree = _tree()
    try:
        for promise, rel, anchor, repl, procedure in GUARDED:
            if not _baseline(tree, procedure):
                checks[f"[cannot run] {promise}"] = False
                print(f"      → {procedure} is not green on an untouched "
                      f"copy, so breaking things proves nothing")
                continue
            sane, why = _mutation_still_parses(rel, anchor, repl)
            if not sane:
                checks[f"[broken mutation] {promise}"] = False
                print(f"      → {why}")
                continue
            ok, note = _mutate(tree, rel, anchor, repl)
            if not ok:
                checks[f"[stale] {promise}"] = False
                print(f"      → {note}")
                continue
            red = _red(tree, procedure)
            _restore(tree, rel)
            if red is None:
                checks[f"[unrunnable] {promise}"] = False
                print(f"      → {procedure} did not finish")
                continue
            checks[promise] = red
            if not red:
                print(f"      → {rel}: the guard can go and "
                      f"{procedure} stays green")

        still_blind = []
        for promise, rel, anchor, repl, watchers in UNGUARDED:
            watchers = [w for w in watchers if _baseline(tree, w)]
            ok, note = _mutate(tree, rel, anchor, repl)
            if not ok:
                checks[f"[stale debt] {promise}"] = False
                print(f"      → {note}; the entry needs updating")
                continue
            # Only procedures that are green without the mutation count.
            # `_baseline` BEFORE the mutation, always. It used to be
            # asked inside the comprehension, after `_mutate` had run —
            # so a watcher with no cached baseline was measured on the
            # MUTATED tree, went red, was recorded as "not green on an
            # untouched copy" and dropped. A promise that is in fact
            # guarded then stayed on the debt list, and the "move it to
            # GUARDED" message could never fire.
            noticed = [w for w in watchers if _red(tree, w)]
            _restore(tree, rel)
            if noticed:
                checks[f"'{promise}' is guarded now — move it to GUARDED "
                       f"({', '.join(noticed)} caught it)"] = False
            else:
                still_blind.append(promise)

        checks[f"the unguarded list is still {DEBT} long, not longer"] = (
            len(still_blind) <= DEBT)
        if len(still_blind) < DEBT:
            checks[f"…and DEBT is {DEBT} but only {len(still_blind)} are "
                   f"blind — lower it"] = False

        # This file has to be able to fail, or it is the thing it hunts.
        # The debt list IS empty since 16.10.2026, so it cannot be what
        # proves that — the comment above said "it IS empty now" while
        # six entries were sitting in it, which an audit pointed out. What proves the machinery ran is that
        # procedures were actually started and came back green on the
        # untouched copy; a baseline that was never taken means every
        # mutation above was compared against nothing.
        # Not "are they applied" by counting baselines, which is what
        # this measured while saying something else. A control: a
        # mutation that changes NOTHING a check can see must come back
        # green, or every ✅ above is the machinery agreeing with itself.
        _ok, _note = _mutate(tree, "app/hostcheck.py",
                             "def _gone(text):",
                             "def _gone(text):  # control, changes nothing")
        _control = _red(tree, "test_hostcheck.py") if _ok else None
        _restore(tree, "app/hostcheck.py")
        checks["a change no check can see comes back green"] = (
            _control is False)
        if _control is not False:
            print(f"      → the control came back {_control!r}; every "
                  f"caught mutation above is in doubt")
    finally:
        _drop(tree)

    bad = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    if still_blind:
        print(f"\n  {len(still_blind)} promises are kept by nothing the "
              f"suite can see:")
        for p in still_blind:
            print(f"      · {p}")
    print("FAIL" if bad else "PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
