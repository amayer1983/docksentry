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

Deliberately NOT here: anything whose mutation needs a live daemon, and
anything where the mutated line is the only caller of itself. Those are
unfalsifiable in a copy of the tree, and a check that cannot fail is
what this file exists to find.
"""
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
     '    bad = {"scheme_bad", "unreachable", "denied", "denied_ssh",',
     '    bad = {"scheme_bad", "unreachable",',
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
     '            if name in self._get_pinned():',
     '            if False and name in self._get_pinned():',
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
     '            current = getattr(config, "web_password", "") or ""\n            if not current:\n                return True',
     '            current = ""\n            if not current:\n                return True',
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
     "app/telegram_bot.py",
     "            if container_key in self.engine.updating:",
     "            if False:",
     "test_update_queue_duplicate.py"),

    ("…which needs the single-update path to say what it is doing",
     "app/telegram_bot.py",
     "            self.engine.mark_updating(container_key,\n                                      target.get(\"new_version\") or \"\")",
     "            pass  # MUTATION: nothing says what is running",
     "test_update_queue_duplicate.py"),

    ("…and finished work is not called a mistake",
     "app/telegram_bot.py",
     '                    "update_already_done" if from_queue else "container_not_in_list",',
     '                    "container_not_in_list",',
     "test_update_queue_duplicate.py"),

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

    ("a backup import is refused when it is too large",
     "app/web_ui.py",
     "                if length > _cap:",
     "                if False:",
     "test_backup_channels.py"),

]

#: Promises with no guard at all — measured, not suspected. Each was
#: mutated and the WHOLE suite run; the number is how many procedures
#: went red, and it is zero for every one of them.
#:
#: `watchers` are the procedures that would plausibly notice, and the
#: only ones run here: proving "nothing in 181 catches it" takes two
#: minutes per entry, which is not a price a pre-commit run can pay. The
#: full sweep is the audit's job (AUDIT.md); this keeps the finding from
#: rotting away between audits.
UNGUARDED = [
    # Found on 10.10.2026 by a suite-wide sweep in its own worktree:
    # delete the line that enforces the promise, run all 194
    # procedures, nothing goes red. Three were reproduced by hand
    # before being written here; the rest carry the sweep's own
    # measurement, which is why they are debt and not yet findings.
    #
    # Listed so the number can only go down. `watchers` are the
    # procedures that ought to notice — when one starts noticing, this
    # file says so and the entry moves up to GUARDED.

    ("a major version bump waits for confirmation",
     "app/update_engine.py",
     "            if auto and ask_this:",
     "            if False:",
     ["test_update_policy.py", "test_v271_batch.py"]),

    ("an image too fresh to trust is held back",
     "app/telegram_bot.py",
     "            if age is not None:",
     "            if False:",
     ["test_image_age.py", "test_update_policy.py"]),

    ("secrets are redacted before anything is written to the audit log",
     "app/audit.py",
     "        clean = redact(detail)",
     "        clean = detail",
     ["test_audit.py"]),

    ("an OOM kill is recognised as one",
     "app/monitor.py",
     '"oom": bool(state.get("OOMKilled"))',
     '"oom": False',
     ["test_monitor.py", "test_event_labels.py"]),

    ("a held notification is not kept past its usefulness",
     "app/notify_retry.py",
     "MAX_AGE_SECONDS = 900",
     "MAX_AGE_SECONDS = 999999",
     ["test_notifier_retry.py", "test_channel_backoff.py"]),

    ("only the registries named in the setting are treated as insecure",
     "app/update_checker.py",
     '            host, getattr(self.config, "insecure_registries", []))',
     "            host, [])",
     ["test_registry_reach.py", "test_check_diag.py"]),
]

#: How many promises are known to be unguarded. May only go down.
#:
#: The sweep found more than these — `/metrics` content, the group
#: wait, the disk-warning threshold, the self-update chain, the Discord
#: restore parity. They are in OPEN.md with their measurements; an
#: entry only comes in here once its anchor is pinned down, because a
#: stale anchor is a check that passed without running.
DEBT = 6


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
    shutil.copytree(ROOT, os.path.join(d, "repo"),
                    ignore=shutil.ignore_patterns(
                        ".git", "__pycache__", "*.pyc", "data", "*.sock"))
    return os.path.join(d, "repo")


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
        # Not "the debt list is not empty" any more — it IS empty now,
        # and that was the goal. What proves the machinery ran is that
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
        shutil.rmtree(tree, ignore_errors=True)

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
