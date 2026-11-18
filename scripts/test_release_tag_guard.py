#!/usr/bin/env python3
"""A tag that disagrees with app/version.py must not build.

It happened: `v2.18.0` was tagged on a commit whose `app/version.py`
still read `2.18.0-beta.35`. The publish workflow decides "is this a
pre-release?" from the TAG NAME — no hyphen means stable, which moves
`:latest` — so the stable channel was built from code that calls itself
a beta, and every `:latest` install has reported itself as a beta since.

Nobody noticed for two months. @famewolf did, from the outside: "somehow
i've ended up on a beta even though I never opted to leave stable" (#2,
22.10.2026). He had not left it. The code was the released code; the
name on it was wrong.

This drives the guard's actual shell, lifted out of the workflow file
and run — not a search for the step's name, which a comment would also
satisfy. The two directions that matter are the historical failure
(`v2.18.0` against `2.18.0-beta.35`, which must abort) and every tag
this project has actually shipped (which must pass).
"""

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "docker-publish.yml")

checks = {}

# ── lift the guard's shell out of the workflow ───────────────────────
# No YAML parser: this project has no third-party dependencies, and
# adding one for a test would be the tail wagging the dog. The step is
# found by name and its `run:` block taken by indentation, which is
# enough — and the check below fails loudly if it is not found, so this
# can never silently measure nothing.

with open(WORKFLOW) as f:
    lines = f.read().splitlines()

script = []
state = None
indent = None
for line in lines:
    if state is None:
        if re.match(r"\s*- name: Tag must match app/version\.py\s*$", line):
            state = "in-step"
        continue
    if state == "in-step":
        m = re.match(r"(\s*)run: \|\s*$", line)
        if m:
            state = "in-run"
            indent = None
        elif re.match(r"\s*- name:", line):
            break
        continue
    if state == "in-run":
        if not line.strip():
            script.append("")
            continue
        lead = len(line) - len(line.lstrip())
        if indent is None:
            indent = lead
        if lead < indent:
            break
        script.append(line[indent:])

guard = "\n".join(script).strip()
checks["the guard step is in the workflow"] = bool(guard)
checks["…and it is a script, not a placeholder"] = (
    "app/version.py" in guard and "exit 1" in guard)
# It has to run before anything is pushed, or it guards nothing. The
# build/push steps come later in the file.
_names = [i for i, l in enumerate(lines)
          if re.match(r"\s*- name: Tag must match", l)]
_pushes = [i for i, l in enumerate(lines) if "docker/build-push-action" in l]
checks["…and it runs before the push step"] = (
    bool(_names) and bool(_pushes) and _names[0] < _pushes[0])


def _run(tag, version_py):
    """Run the guard with `tag` as the ref and `version_py` on disk."""
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, "app"))
    with open(os.path.join(work, "app", "version.py"), "w") as f:
        f.write(version_py)
    env = dict(os.environ, GITHUB_REF=f"refs/tags/v{tag}")
    r = subprocess.run(["bash", "-c", guard], cwd=work, env=env,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr)


if guard:
    # The direction that was actually wrong.
    rc, out = _run("2.18.0", 'VERSION = "2.18.0-beta.35"\n')
    checks["the release that went wrong is refused"] = rc != 0
    # A refusal nobody can act on is half a guard: it has to name both
    # sides, or whoever reads the failed build has to go digging.
    checks["…and the message names the tag"] = "2.18.0" in out
    checks["…and the version it found"] = "2.18.0-beta.35" in out

    # The counter-direction. Without this, a guard hardwired to fail
    # passes every check above.
    rc, out = _run("2.18.0", 'VERSION = "2.18.0"\n')
    checks["a matching stable tag passes"] = rc == 0
    rc, _ = _run("2.18.0-beta.42", 'VERSION = "2.18.0-beta.42"\n')
    checks["…and so does a matching pre-release tag"] = rc == 0

    # The other mismatches worth naming: the reverse of the real one,
    # and the neighbouring version, which is the mistake a careless
    # bump makes.
    rc, _ = _run("2.18.0-beta.42", 'VERSION = "2.18.0"\n')
    checks["a stable file under a beta tag is refused"] = rc != 0
    rc, _ = _run("2.19.0", 'VERSION = "2.18.0"\n')
    checks["a stale version file is refused"] = rc != 0

    # A version.py that cannot be read must abort, not pass. An empty
    # read compared against a tag would otherwise be "not equal" by
    # accident rather than on purpose — and if the comparison were ever
    # reordered, an unreadable file would sail through.
    rc, out = _run("2.18.0", "# nothing here\n")
    checks["an unreadable version file is refused"] = rc != 0
    checks["…and says so rather than guessing"] = "version.py" in out
else:
    for k in ("the release that went wrong is refused",
              "a matching stable tag passes"):
        checks[k] = False

# ── and against this repository's own history ────────────────────────
# The guard must pass every tag that was correct and fail the one that
# was not. Driven against the real files from git, so this cannot drift
# away from what was actually shipped.

# Only where the history is reachable. The mutation sweep runs this
# against a COPY of the tree with `.git` left out, so `git tag` comes
# back empty there — and a check that fails for that reason would make
# every mutation in this file look caught when it was not. The cases
# that matter are driven synthetically above and do not depend on it;
# this adds the real shipped tags on top, where they exist.
tags = subprocess.run(["git", "tag"], cwd=ROOT, capture_output=True,
                      text=True).stdout.split()
shipped = [t for t in tags if re.match(r"^v2\.1[0-9]+\.", t)]
HAVE_HISTORY = len(shipped) >= 10

wrong, right = [], []
for t in (shipped if HAVE_HISTORY else []):
    blob = subprocess.run(["git", "show", f"{t}:app/version.py"], cwd=ROOT,
                          capture_output=True, text=True)
    if blob.returncode != 0:
        continue
    rc, _ = _run(t[1:], blob.stdout)
    (right if rc == 0 else wrong).append(t)

# v2.18.0 is the one that was wrong, and it must still come out wrong —
# this is the regression test for the actual incident.
if HAVE_HISTORY:
    checks["v2.18.0 is still caught"] = "v2.18.0" in wrong
    checks["…and it is the only tag that is"] = wrong == ["v2.18.0"]
    checks["…while everything else passes"] = len(right) >= 15
else:
    print("  (übersprungen: keine git-Historie erreichbar — "
          "die synthetischen Fälle oben decken denselben Fehlschlag)")

# ── a tag on a red tree is not published ─────────────────────────────
# v2.18.0-beta.44 went out with test_whatsnew.py red. ci.yml ran the
# suite on main by then, but CI only marks a commit red after the push;
# this workflow builds on the tag and waited for nothing. So the publish
# workflow runs the suite itself and the build job `needs` that job.
# Read the same way as the guard above, by indentation and without a
# YAML parser; the parser is fed a workflow without `needs` first, so a
# reader that sees nothing cannot make these checks pass.


def _jobs(text):
    """{job name: [its lines]} for the top-level `jobs:` block."""
    jobs, cur, inside = {}, None, False
    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            inside = True
            continue
        if not inside:
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            break                                  # next top-level key
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            cur = m.group(1)
            jobs[cur] = []
        elif cur is not None:
            jobs[cur].append(line)
    return jobs


def _job_key(body, key):
    """The value of a job-level `key:` (four spaces in), or None."""
    for line in body:
        m = re.match(rf"^    {key}:\s*(.*?)\s*$", line)
        if m:
            return m.group(1)
    return None


def _needs(body):
    v = _job_key(body, "needs") or ""
    return {n.strip() for n in v.strip("[]").split(",") if n.strip()}


def _suite_gate(text):
    """(suite job that runs verify.py, build job, what the build needs)."""
    jobs = _jobs(text)
    runs = {n for n, b in jobs.items()
            if any(re.match(r"^(- )?run: python3 scripts/verify\.py$",
                            l.strip()) for l in b)}
    builds = [n for n, b in jobs.items()
              if any("docker/build-push-action" in l for l in b)]
    build = builds[0] if len(builds) == 1 else None
    needs = _needs(jobs[build]) if build else set()
    gated = bool(build) and bool(runs & needs) and build not in runs
    # A suite that is allowed to fail, or a build that runs whatever its
    # needs did, waits for nothing.
    loose = any(_job_key(jobs[n], "continue-on-error") for n in runs & needs)
    build_if = (_job_key(jobs[build], "if") or "") if build else ""
    loose = loose or "always()" in build_if
    return gated, loose, jobs


_ungated = ("jobs:\n  suite:\n    runs-on: x\n    steps:\n"
            "      - run: python3 scripts/verify.py\n"
            "  build-and-push:\n    runs-on: x\n    steps:\n"
            "      - uses: docker/build-push-action@v7\n")
checks["the job reader sees a build that waits for nothing"] = (
    _suite_gate(_ungated)[0] is False
    and set(_suite_gate(_ungated)[2]) == {"suite", "build-and-push"})
checks["…and one that waits for the suite"] = _suite_gate(
    _ungated.replace("  build-and-push:\n",
                     "  build-and-push:\n    needs: suite\n"))[0] is True

_wf_text = "\n".join(lines)
_gated, _loose, _wf_jobs = _suite_gate(_wf_text)
checks["the publish workflow runs the suite and the image build needs it"] = (
    _gated)
checks["…and the suite job may not fail its way through"] = (
    _gated and not _loose)
if not _gated:
    print(f"      → jobs read from docker-publish.yml: "
          f"{ {n: sorted(_needs(b)) for n, b in _wf_jobs.items()} }")
# The same command as ci.yml's suite job, so the two cannot drift into
# running different things under the same name.
_ci = os.path.join(ROOT, ".github", "workflows", "ci.yml")
_ci_runs = [l.strip() for l in open(_ci, encoding="utf-8").read().splitlines()
            if l.strip().startswith("run: python3 scripts/verify.py")]
checks["…and it is the same run ci.yml makes"] = (
    _ci_runs == ["run: python3 scripts/verify.py"])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print(f"\n  geprüft gegen {len(right) + len(wrong)} echte Tags; "
      f"abgewiesen: {', '.join(wrong) or 'keiner'}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
