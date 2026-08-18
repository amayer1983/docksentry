#!/usr/bin/env python3
"""Self-update recovery net (#43, @LeeNX).

If the recreate `docker run` fails (seen on rootless Podman), the swap must
NOT leave Docksentry dead (renamed to `_old`, stopped). The helper script now
rolls back: rename `_old` → current + start it. We run the real shell the
helper would run, with a fake `docker` (and instant `sleep`) on PATH, and
assert the rollback fires on run-failure but not on success.

No Docker, no real waits. Exits non-zero on any failure.
"""
import sys, os, tempfile, subprocess, stat, shlex

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot


def _run(fail_run):
    d = tempfile.mkdtemp()
    log = os.path.join(d, "log")
    bindir = os.path.join(d, "bin")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "docker"), "w") as f:
        f.write("#!/bin/sh\n"
                f'echo "$@" >> "{log}"\n'
                'if [ "$1" = run ] && [ "$FAIL_RUN" = 1 ]; then exit 1; fi\n'
                "exit 0\n")
    with open(os.path.join(bindir, "sleep"), "w") as f:  # instant
        f.write("#!/bin/sh\nexit 0\n")
    for b in ("docker", "sleep"):
        p = os.path.join(bindir, b)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    script = TelegramBot._build_selfupdate_script("ds", "-e X=1", "img:latest")
    env = dict(os.environ, PATH=bindir + ":" + os.environ.get("PATH", ""),
               FAIL_RUN="1" if fail_run else "0")
    subprocess.run(["sh", "-c", script], env=env, timeout=30)
    with open(log) as f:
        return f.read()


def _run_with_run_parts(run_args):
    """Run the real helper script through `sh` with a fake `docker` that
    logs its argv, using run_parts built exactly the way _do_selfupdate
    does. Returns (log_text, sh_stderr) so we can assert the run-args
    survive intact and sh performed NO command substitution."""
    d = tempfile.mkdtemp()
    log = os.path.join(d, "log")
    bindir = os.path.join(d, "bin")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "docker"), "w") as f:
        # Log each arg on its own line so we can match a full label token
        # even though the label itself contains spaces once expanded.
        f.write("#!/bin/sh\n"
                'for a in "$@"; do printf "%s\\n" "$a" >> "' + log + '"; done\n'
                "exit 0\n")
    with open(os.path.join(bindir, "sleep"), "w") as f:  # instant
        f.write("#!/bin/sh\nexit 0\n")
    for b in ("docker", "sleep"):
        p = os.path.join(bindir, b)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    # Mirror the production line in _do_selfupdate exactly.
    run_parts = " ".join(shlex.quote(a) for a in run_args)
    script = TelegramBot._build_selfupdate_script("ds", run_parts, "img:latest")
    env = dict(os.environ, PATH=bindir + ":" + os.environ.get("PATH", ""),
               FAIL_RUN="0")
    r = subprocess.run(["sh", "-c", script], env=env, timeout=30,
                       capture_output=True, text=True)
    with open(log) as f:
        return f.read(), r.stderr, run_parts


def main():
    fail = _run(fail_run=True)
    ok = _run(fail_run=False)
    checks = {
        # failure → old container restored, cleanup of _old NOT done
        # `stop -t <n>` since #62: a bare `docker stop` uses Docker's own
        # ten seconds and then SIGKILLs us mid-shutdown, which is how
        # @NotRetarded's instance died with exit 137 during a self-update.
        "fail: stop + rename to _old happened":
            "stop -t " in fail and "rename ds ds_old" in fail,
        "fail: the stop carries a timeout, not Docker's default":
            "stop ds" not in fail,
        "fail: rolls back (rename _old -> ds + start)": "rename ds_old ds" in fail and "start ds" in fail,
        "fail: does NOT rm the _old backup": "rm ds_old" not in fail,
        # success → new container kept, _old cleaned up, no rollback
        "ok: recreate + rm _old cleanup ran": "run -d" in ok and "rm ds_old" in ok,
        "ok: no rollback (no rename back / start)": "rename ds_old ds" not in ok and "start ds" not in ok,
    }

    # ── shell-quoting of run args (#49, @LeeNX): a Traefik Host(`...`) label
    #    or a value with `$` must NOT trigger command substitution in sh ──
    backtick = "traefik.http.routers.docksentry.rule=Host(`docksentry.h.leenx.nz`)"
    dollar = "traefik.http.middlewares.x.headers.customrequestheaders.X-Foo=$HOME/bar"
    args = ["--name", "ds", "--label", backtick, "--label", dollar, "-e", "PW=a b`c"]
    log, stderr, run_parts = _run_with_run_parts(args)
    # Both dangerous chars must sit inside single quotes in the built string,
    # which is what shlex.quote produces and what neutralises sh expansion.
    checks["quote: backtick label is single-quoted"] = "'" + backtick + "'" in run_parts
    checks["quote: $ value is single-quoted"] = "'" + dollar + "'" in run_parts
    # sh must not have tried to execute the hostname / expand $HOME.
    checks["quote: no command substitution error from sh"] = (
        "not found" not in stderr and "docksentry.h.leenx.nz" not in stderr)
    # The label reaches docker verbatim (backticks intact, $HOME unexpanded).
    checks["quote: backtick label reaches docker intact"] = backtick in log
    checks["quote: $ stays literal (not expanded)"] = dollar in log and "$HOME" in log
    # And the quoted string round-trips back to the exact original tokens.
    checks["quote: run_parts round-trips via shlex.split"] = shlex.split(run_parts) == args
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL\n--- fail log ---\n" + fail + "\n--- ok log ---\n" + ok)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
