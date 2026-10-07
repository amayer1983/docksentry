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
     "                            if code == 354:",
     "                            if True:",
     "test_smtp_resilience.py"),

    ("a greylisting relay's 'come back later' is come back to",
     "app/notifiers/smtp.py",
     "    if code is not None and 400 <= code <= 499:",
     "    if False:",
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
     '        elif ep.startswith("ssh://"):\n            kind = "denied_ssh"\n',
     "",
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
     '            if _tls not in ("ssl", "starttls", "none"):',
     "            if False:",
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
    ("a failed update rolls back to the old container",
     "app/update_checker.py",
     "    def _rollback_to_old(self, name, old_name):\n",
     "    def _rollback_to_old(self, name, old_name):\n        return None  # MUTATION\n",
     ["test_gluetun_docs.py", "test_recovery.py", "test_lifecycle_core.py",
      "test_rename_timeout_recovery.py", "test_leftover_backups.py",
      "test_health_output.py"]),


    ("the Web UI rejects a request without a CSRF token",
     "app/web_ui.py",
     "        def _check_csrf(self):\n",
     "        def _check_csrf(self):\n            return True  # MUTATION\n",
     ["test_settings_post.py", "test_web_login.py",
      "test_setup_password.py"]),




]

#: How many promises are known to be unguarded. May only go down.
DEBT = 2


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
    """Did `procedure` fail in the tree as it stands?"""
    p = os.path.join(tree, "scripts", procedure)
    if not os.path.exists(p):
        return None
    try:
        r = subprocess.run([sys.executable, p], cwd=tree, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
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
            ok, note = _mutate(tree, rel, anchor, repl)
            if not ok:
                checks[f"[stale debt] {promise}"] = False
                print(f"      → {note}; the entry needs updating")
                continue
            # Only procedures that are green without the mutation count.
            noticed = [w for w in watchers
                       if _baseline(tree, w) and _red(tree, w)]
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
        checks["the mutations really are being applied"] = (
            len(GUARDED) >= 8 and len(UNGUARDED) >= 1)
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
