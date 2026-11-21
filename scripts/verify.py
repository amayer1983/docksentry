#!/usr/bin/env python3
"""Run every verification procedure and write VERIFICATION.md from the result.

The suite has grown to a point where "69/69 green" is the only thing anyone
ever sees, and that number is exactly the kind of reassurance this project
has already been burned by. A procedure that SKIPs because Docker is
missing, or that passes because it matched a word in its own comment, is
invisible inside an aggregate. So the report keeps the detail: every named
check, per procedure, with skips shown rather than folded into the total.

Usage:

    python3 scripts/verify.py            # run everything, rewrite the report
    python3 scripts/verify.py --check    # fail if the report is out of date
    python3 scripts/verify.py --quiet    # no per-procedure progress output

`--check` regenerates the report in memory and compares, so the file in git
cannot drift away from what the suite actually does without someone
noticing. It compares *scope* — which procedures exist and what each one
asserts — not the pass/skip column, because status is environment-dependent
and a byte comparison would be permanently red for the wrong reason.

Ten procedures drive a real container runtime: dependents_recreate,
glob, image_inherit, multihost_live, netns_recreate, podman_live,
protect_stop, reaper, remote_transports, trust_running — the files that
call `lf.start(`, `lf.daemon_available(` or `lf.require_daemon(`,
listed 18.11.2026, plus `test_live_fixtures.py`, which drives two of
them. Where a runtime is missing they skip, and the report shows the
skip rather than folding it into the total. "Six" stood here until
16.11.2026 and was never a count; "sixteen" replaced it and came from a
grep with an unescaped dot, which also matched `self.start…` in six
files that drive no daemon.

The same paragraph claimed those procedures "have guards now". They did
not: the guard asked `subprocess.run([cli, "info"]).returncode`, which
answers the daemon question and not the binary one, so with docker off
PATH the call raised FileNotFoundError and the procedure died with a
traceback instead of skipping — 8 of 10 of them, measured. A crash at
the top of a procedure hides every check below it: here it reads as
FAIL with 0 checks, and `test_guard_mutations.py` scores it
[unrunnable], far from the cause. `lf.daemon_available` answers both
questions now, and `test_live_fixtures.py` drives two real procedures
on a PATH without docker to keep it that way.

The run is in CI: `ci.yml` runs this file on every push to main, and
`docker-publish.yml` runs it before an image is built from a tag, so a
tag on a red tree is not published. Its absence cost:
v2.18.0-beta.44 was published with `test_whatsnew.py` red. The CI run
on 98344b5 (17.11.2026) reported 222 procedures ok and no SKIP line,
read from its log.

Deliberately no timestamp in the output: a generated date would make every
run differ from the committed file and turn the freshness check into noise
that gets ignored.
"""

import os
import re
import signal
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
REPORT = os.path.join(ROOT, "VERIFICATION.md")

#: Both output conventions the suite uses. The older procedures print
#: `  ✅ name`, the newer ones `  PASS name`; neither is worth rewriting 69
#: files over, so the reader accepts both.
CHECK_RE = re.compile(r"^\s+(✅|❌|PASS|FAIL)\s+(.+?)\s*$")

#: Grouping is by keyword, and anything unmatched lands in "Other" *visibly*
#: — a new procedure showing up there is a prompt to file it, not a silent
#: mis-classification.
AREAS = [
    ("Registry & update detection",
     ("registry", "image_", "remote_version", "tag_pagination", "private_ca",
      "trust_running", "running_image", "update_policy", "check_", "glob",
      "labels", "env_override", "dry_run", "auto_update_all", "cron_dow",
      "monitor_only", "manual_update_gates", "checkimages",
      "newer_version")),
    ("Self-update",
     ("selfupdate", "self_detection", "release_link", "web_selfupdate",
      "whatsnew")),
    ("Containers & recreate",
     ("container_backend", "dependents", "netns", "protect_stop",
      "op_coordination", "groups_cmd", "disk_", "cooldown", "recovery")),
    ("Monitoring & events", ("monitor", "event_watcher", "podman_live",
                             "event_resources", "health_output",
                             "crash_diagnostics")),
    ("Multi-host",
     ("hosts", "host_scoped", "host_state", "multihost", "web_multihost",
      "remote_transports")),
    ("Notifications",
     ("notifier", "ntfy", "smtp", "discord", "api_retry", "send_only",
      "edited_message", "websocket", "i18n_partial", "telegram_split")),
    ("Web interface",
     ("icons", "form_nesting", "settings_fields", "link_", "metrics_api",
      "disabled_reasons", "audit")),
]

#: Procedures that need something the automated run cannot assume — a live
#: instance, a browser, a second runtime. Listed rather than run, because
#: quietly skipping them is how "all green" starts meaning less than it says.
MANUAL = [
    ("scripts/check_layout_widths.py",
     "Measures the layout at 1024–3840px in a real browser. Needs a running "
     "instance and Docker; skips with a message rather than failing."),
    ("scripts/sim_crashloop.py",
     "Drives a container into a restart loop to exercise the monitor's "
     "crash path end to end. Needs Docker."),
    ("scripts/lint_contracts.py",
     "Checks the internal call contracts between modules."),
]


def area_of(name):
    for area, keys in AREAS:
        if any(k in name for k in keys):
            return area
    return "Other"


def summary_line(path):
    """First sentence of the module docstring — what the procedure covers."""
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r'^"""(.*?)"""', src, re.S | re.M)
    if not m:
        return ""
    first = m.group(1).strip().split("\n\n")[0]
    first = " ".join(first.split())
    return first[:180]


def _ended(code, lines):
    """How a procedure ended, for a log line: exit code or signal."""
    if code < 0:
        try:
            how = f"killed by {signal.Signals(-code).name}"
        except ValueError:
            how = f"killed by signal {-code}"
    else:
        how = f"exit code {code}"
    return how + ("" if lines else ", no output at all")


#: Per procedure. 30 minutes, not 15: the ratchet (test_guard_mutations.py)
#: is one procedure that runs hundreds of others, and it outgrew 15 — 876 s
#: on its own on 21.11.2026 with 768 entries, and past 900 s inside a full
#: run. A test sets it lower to drive the timeout.
PROC_TIMEOUT = 1800


def _as_text(b):
    if b is None:
        return ""
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else b


def _timed_out(e):
    """A procedure that ran past PROC_TIMEOUT: FAIL, with what it had said
    up to then. It used to escape as a TimeoutExpired traceback and end
    the whole run — which is how 900 s ended one on 21.11.2026."""
    out = _as_text(e.stdout) + _as_text(e.stderr)
    checks = [(m.group(1) in ("✅", "PASS"), m.group(2))
              for m in (CHECK_RE.match(l) for l in out.split("\n")) if m]
    lines = [l.strip() for l in out.split("\n") if l.strip()]
    tail = "\n".join(lines[-12:]
                     + [f"timed out after {PROC_TIMEOUT} s and was killed"])
    return "fail", checks, False, False, tail


def run_one(path):
    """Run a procedure and return (status, checks, skipped)."""
    try:
        r = subprocess.run([sys.executable, path], capture_output=True,
                           text=True, cwd=ROOT, timeout=PROC_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        return _timed_out(e)
    out = r.stdout + r.stderr
    checks = [(m.group(1) in ("✅", "PASS"), m.group(2))
              for m in (CHECK_RE.match(l) for l in out.split("\n")) if m]
    skipped = bool(re.search(r"^\s*SKIP\b", out, re.M))
    lines = [l.strip() for l in out.split("\n") if l.strip()]
    verdict = lines[-1] if lines else ""
    # The exit code is the authority, not the wording. Several procedures
    # end with a sentence rather than a bare verdict ("PASS: 1 warn under
    # sustained full disk, …") and an earlier version of this runner marked
    # one of them failing purely because the last line was not exactly
    # "PASS" — a false alarm is as bad here as a missed one.
    said_fail = verdict.startswith("FAIL")
    if r.returncode != 0 or said_fail:
        status = "fail"
    elif skipped:
        status = "skipped"
    else:
        status = "pass"
    # A procedure whose printed verdict and exit code disagree is reporting
    # something it does not mean; surface it rather than picking a winner.
    disagrees = (r.returncode == 0) != (not said_fail)
    # What a failing procedure gets to say in the run log, in order:
    #
    # - its "      → …" diagnostics, if it printed any. Procedures write
    #   them beside a red check (what the list held, what the thing under
    #   test printed), and they are the explanation.
    # - otherwise, with no check at all, its last 12 lines: it fell over
    #   before judging anything, and those lines are all there is.
    #   Three procedures failed with 0 checks on a hosted runner on
    #   16.11.2026 and the log had nothing else to say about it.
    # - and with no check at all, always the exit code, or the signal
    #   that ended it. A procedure that prints nothing has no last lines:
    #   the audit run of 18.11.2026 logged "FAIL guard_mutations 0
    #   checks" and not a word more, so whether it was killed or exited
    #   could not be told afterwards.
    tail = ""
    if status == "fail":
        notes = [l for l in lines if l.lstrip().startswith("→")]
        if notes:
            tail = "\n".join(notes[:20])
        elif not checks:
            tail = "\n".join(lines[-12:] + [_ended(r.returncode, lines)])
    return status, checks, skipped, disagrees, tail


def collect(quiet=False):
    names = sorted(f for f in os.listdir(SCRIPTS)
                   if f.startswith("test_") and f.endswith(".py"))
    results = []
    for f in names:
        status, checks, skipped, disagrees, tail = run_one(
            os.path.join(SCRIPTS, f))
        results.append({
            "disagrees": disagrees,
            "file": f,
            "name": f[len("test_"):-len(".py")].replace("_", " "),
            "area": area_of(f),
            "summary": summary_line(os.path.join(SCRIPTS, f)),
            "status": status,
            "checks": checks,
        })
        if not quiet:
            mark = {"pass": "ok  ", "skipped": "SKIP", "fail": "FAIL"}[status]
            print(f"  {mark} {f[5:-3]:<26} {len(checks):>3} checks")
            if status == "fail":
                # Name the checks that went red. Without this the log
                # says "FAIL monitor  31 checks" and the reader has to
                # re-run it somewhere else to learn which one — which is
                # not possible when "somewhere else" is a hosted runner.
                for ok, label in checks:
                    if not ok:
                        print(f"       ✗ {label}")
            if tail:
                for line in tail.split("\n"):
                    print(f"       | {line}")
    return results


def version():
    # `[\d.]+` matched 2.6.0 and nothing else — a pre-release like
    # 2.7.0-rc.2 has a hyphen and letters in it, so the search failed and
    # this file went out saying "How Docksentry v? is checked". Harmless
    # until release candidates became part of how things ship here, at
    # which point every RC produced a verification report that could not
    # name what it had verified.
    for line in open(os.path.join(ROOT, "app", "version.py")):
        m = re.search(r'"(\d+\.\d+\.\d+[\w.+-]*)"', line)
        if m:
            return m.group(1)
    return "?"


def bar(done, total, width=22):
    if not total:
        return ""
    filled = round(width * done / total)
    return "█" * filled + "░" * (width - filled)


def render(results):
    total_checks = sum(len(r["checks"]) for r in results)
    failing = [r for r in results if r["status"] == "fail"]
    skipped = [r for r in results if r["status"] == "skipped"]
    odd = [r for r in results if r.get("disagrees")]
    ok = len(results) - len(failing) - len(skipped)

    L = []
    L.append("# Verification")
    L.append("")
    L.append(f"How Docksentry v{version()} is checked, and what each check "
             "actually asserts.")
    L.append("")
    L.append("> Generated by `scripts/verify.py` from a real run of the "
             "suite — do not edit by hand.")
    # No count of daemon-driving procedures: "six" and "sixteen" were
    # both wrong, and the sentence does not need one.
    L.append("> CI runs the suite on every push to main and before an "
             "image is built from a tag, so a tag on a red tree is not "
             "published. Procedures that need a container runtime skip "
             "where there is none, and a skip is shown here, not counted "
             "as a pass. "
             "`scripts/verify.py --check` re-runs it and fails if this file "
             "no longer describes what the suite does.")
    L.append("")

    if failing:
        L.append(f"**{len(failing)} procedure(s) failing.**")
    elif skipped:
        L.append(f"**{ok} of {len(results)} procedures pass, "
                 f"{len(skipped)} skipped** (see below — a skip is not a pass).")
    else:
        L.append(f"**All {len(results)} procedures pass**, "
                 f"{total_checks} named checks.")
    if odd:
        L.append("")
        L.append("> ⚠️ " + ", ".join(r["file"] for r in odd)
                 + " print a verdict that disagrees with their exit code.")
    L.append("")

    # ── by area ──────────────────────────────────────────────────
    L.append("## By area")
    L.append("")
    L.append("| Area | Procedures | Checks | |")
    L.append("|---|---:|---:|---|")
    order = [a for a, _ in AREAS] + ["Other"]
    for area in order:
        rs = [r for r in results if r["area"] == area]
        if not rs:
            continue
        c = sum(len(r["checks"]) for r in rs)
        good = sum(1 for r in rs if r["status"] == "pass")
        L.append(f"| {area} | {len(rs)} | {c} | `{bar(good, len(rs))}` |")
    L.append(f"| **Total** | **{len(results)}** | **{total_checks}** | |")
    L.append("")

    # ── what needs a live system ─────────────────────────────────
    L.append("## Not in the automated run")
    L.append("")
    L.append("These need something a plain checkout cannot assume. They are "
             "listed so their absence from the total above is visible.")
    L.append("")
    for path, why in MANUAL:
        L.append(f"- **`{path}`** — {why}")
    L.append("")

    # ── the procedures themselves ────────────────────────────────
    L.append("## Procedures")
    L.append("")
    for area in order:
        rs = [r for r in results if r["area"] == area]
        if not rs:
            continue
        L.append(f"### {area}")
        L.append("")
        for r in rs:
            icon = {"pass": "✅", "skipped": "⏭️", "fail": "❌"}[r["status"]]
            L.append(f"<details><summary>{icon} <b>{r['name']}</b> — "
                     f"{len(r['checks'])} checks</summary>")
            L.append("")
            if r["summary"]:
                L.append(f"{r['summary']}")
                L.append("")
            for good, label in r["checks"]:
                L.append(f"- {'' if good else '**FAILED** '}{label}")
            if not r["checks"]:
                L.append("- _(this procedure reports only a final verdict)_")
            L.append("")
            L.append("</details>")
            L.append("")
    return "\n".join(L).rstrip() + "\n"


def _scope(text):
    """The report reduced to *what is checked*, dropping *how it went*.

    The freshness check compares this rather than the file byte for byte,
    because status is environment-dependent and a byte comparison would be
    permanently red for the wrong reason: a procedure that needs a
    container runtime skips on a machine without one and passes on one
    with it. Comparing the full text would fail on every machine that
    differs from the one that wrote the file, and a check that always
    fails is one nobody reads.

    What still gets caught is the thing that matters — a procedure or a
    named check added, removed or renamed without regenerating the report.
    An actual failure is reported separately, from the run itself, not from
    this comparison.
    """
    out = []
    for line in text.split("\n"):
        if line.startswith(("**", "> ⚠️", "How Docksentry v")):
            continue                      # summary counts, warnings, version
        line = re.sub(r"[✅⏭️❌]", "", line)
        # A red check is a failure, not staleness. Without this the run
        # reports "out of date" alongside the real error and the reader
        # chases the wrong one.
        line = line.replace("- **FAILED** ", "- ")
        line = re.sub(r"`[█░]+`", "", line)      # the per-area bars
        line = re.sub(r"\|\s*\d+\s*\|", "||", line)   # per-area counts
        out.append(line.rstrip())
    return "\n".join(out)


def main():
    check_mode = "--check" in sys.argv
    quiet = "--quiet" in sys.argv or check_mode
    results = collect(quiet=quiet)
    text = render(results)

    failing = [r for r in results if r["status"] == "fail"]

    if check_mode:
        current = open(REPORT, encoding="utf-8").read() if os.path.exists(REPORT) else ""
        if _scope(current) != _scope(text):
            print("VERIFICATION.md is out of date — run: python3 scripts/verify.py")
            return 1
        if failing:
            print(f"FAIL — {len(failing)} procedure(s) failing: "
                  + ", ".join(r["file"] for r in failing))
            return 1
        print(f"PASS — report current, {len(results)} procedures")
        return 0

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(text)
    print()
    print(f"  wrote {os.path.relpath(REPORT, ROOT)} — {len(results)} procedures, "
          f"{sum(len(r['checks']) for r in results)} checks")
    if failing:
        print(f"  FAIL — {len(failing)}: " + ", ".join(r["file"] for r in failing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
