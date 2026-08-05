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

This runs locally rather than in CI on purpose. Six procedures drive a real
container runtime; on a hosted runner they skip, and a report generated
there would show six blanks as though that were coverage. Measured before
deciding: with the runtime taken away, those six went *red* rather than
skipping, which is indistinguishable from a regression — they have guards
now, but the report is only honest where the runtime is real.

Deliberately no timestamp in the output: a generated date would make every
run differ from the committed file and turn the freshness check into noise
that gets ignored.
"""

import os
import re
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
      "monitor_only", "manual_update_gates", "checkimages")),
    ("Self-update",
     ("selfupdate", "self_detection", "release_link", "web_selfupdate",
      "whatsnew")),
    ("Containers & recreate",
     ("container_backend", "dependents", "netns", "protect_stop",
      "op_coordination", "groups_cmd", "disk_", "cooldown")),
    ("Monitoring & events", ("monitor", "event_watcher", "podman_live",
                             "event_resources", "health_output",
                             "crash_diagnostics")),
    ("Multi-host",
     ("hosts", "host_scoped", "host_state", "multihost", "web_multihost",
      "remote_transports")),
    ("Notifications",
     ("notifier", "ntfy", "smtp", "discord", "api_retry", "send_only",
      "edited_message", "websocket", "i18n_partial")),
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


def run_one(path):
    """Run a procedure and return (status, checks, skipped)."""
    r = subprocess.run([sys.executable, path], capture_output=True, text=True,
                       cwd=ROOT, timeout=900)
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
    return status, checks, skipped, disagrees


def collect(quiet=False):
    names = sorted(f for f in os.listdir(SCRIPTS)
                   if f.startswith("test_") and f.endswith(".py"))
    results = []
    for f in names:
        status, checks, skipped, disagrees = run_one(os.path.join(SCRIPTS, f))
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
    return results


def version():
    for line in open(os.path.join(ROOT, "app", "version.py")):
        m = re.search(r'"([\d.]+)"', line)
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
    L.append("> The suite runs locally, not in CI: six procedures drive a "
             "real container runtime, and a hosted runner without one would "
             "report skips as if they were coverage. "
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
    permanently red for the wrong reason: CI has no Podman, so
    `test_podman_live` skips there and passes here. Comparing the full text
    would fail every run, and a check that always fails is one nobody reads.

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
