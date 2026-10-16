#!/usr/bin/env python3
"""The chain that replaces Docksentry with a new Docksentry.

Six reverts in this path went unnoticed by every procedure on
10.10.2026: a failed pull no longer stopping the update, the swap
happening even when the image bits are identical, `/selfupdate
previous` resolving to nothing, the old container never cleaned up, no
restart after a failed rename, and the boot message that says a
recreate was rolled back.

Three of the six were still open after the first attempt at this file,
which claimed all six. An audit on 16.10.2026 reverted them again and
the suite stayed green: the `return` after a failed pull, the
identical-image short-circuit, and `previous`. All three are driven
now, which is what the first three had been and these three had not.

This is the one path where a mistake has nowhere to report itself
from. Everything else can fail and still have a running Docksentry to
say so; here the thing being replaced is the thing that would tell you.
#43 (@LeeNX) and #62 (@NotRetarded) are both scars from exactly that:
one left the container renamed to `_old`, stopped, with no new one, and
the other was SIGKILLed mid-update while the update reported success.

The swap script is a shell string the helper container runs. Reading it
as a string is not ideal and is the trade taken here: there is no way
to run it without a daemon and a container willing to be destroyed. So
the ORDER and the guards are asserted as a sequence, which is what the
scars were about — not that the words are present, but that `rename`
has a fallback, `run` has a fallback, and the cleanup comes last.
"""
import os
import shlex
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import selfupdate  # noqa: E402

checks = {}


def _order(script, *needles):
    """Do these appear in this order? Missing one counts as out of order."""
    at = -1
    for n in needles:
        i = script.find(n, at + 1)
        if i <= at:
            return False
        at = i
    return True


# ── the swap script: the order IS the safety ─────────────────────────
S = selfupdate.build_script("ds", "-e X=1 --label y", "repo/ds:1.2.3",
                            stop_timeout=90)

checks["the old container is cleared before anything else"] = _order(
    S, "docker rm -f ds_old", "docker stop")
checks["…then we stop, with the configured grace"] = "stop -t 90 " in S
checks["…then rename ourselves aside, then start the new image"] = _order(
    S, "docker stop", "docker rename ds ds_old", "docker run -d")
checks["…and only then remove the backup"] = _order(
    S, "docker run -d", "docker rm ds_old")

# #62: a bare `docker stop` uses Docker's ten seconds and then SIGKILLs.
# The grace has to be the configured one, not a default.
checks["the stop grace is the configured one, not Docker's ten seconds"] = (
    "stop -t 90 " in S and "stop ds" not in S.replace("stop -t 90 ds", ""))

# #43: if the rename fails we are about to have no container at all.
# The fallback has to start the old one again and stop.
#
# Scoped to the rename's OWN clause. `_order(S, "docker rename …",
# "docker start ds", "exit 1")` over the whole script passed with the
# fallback deleted, because `docker start ds` appears a second time in
# the rollback arm further down and the search found that one.
def _clause(script, after, until="&& "):
    """The piece of the script that belongs to one step."""
    i = script.find(after)
    if i < 0:
        return ""
    j = script.find(until, i)
    return script[i:j if j > i else len(script)]


_rename_clause = _clause(S, "docker rename ds ds_old")
checks["a failed rename restarts the container we still have"] = (
    "docker start ds" in _rename_clause and "exit 1" in _rename_clause)
# …and if the new container will not run — a flag rootless Podman
# rejects — the old one has to come back. That is #43 exactly: the
# chain used to leave Docksentry renamed, stopped, and gone.
checks["a failed run rolls back instead of leaving nothing"] = _order(
    S, "docker run -d", "rolling back", "exit 1")
checks["…and says so in words the boot can find again"] = "rolling back" in S

# The two failure arms must not be the same arm. A single `||` covering
# both would restart the old container after a failed RUN, by which
# point it has been renamed and the name is taken.
#
# Each clause is asked about its own guard rather than the script being
# asked for a count. `S.count("exit 1") >= 2` was the first version and
# `test_check_quality.py` rejected it on sight — a floor over a count
# passes while the guarded thing is missing, as long as something else
# gained an occurrence. It was right.
_run_clause = _clause(S, "docker run -d")
checks["the rename failure has a guard of its own"] = (
    "||" in _rename_clause and "exit 1" in _rename_clause)
checks["…and so does the run failure, a different one"] = (
    "||" in _run_clause and "exit 1" in _run_clause
    and "rolling back" in _run_clause)
checks["…and they are not the same clause"] = (
    _rename_clause != _run_clause
    and "docker run -d" not in _rename_clause)

# Every name and image interpolated into a shell string is quoted —
# this runs as `sh -c`, so an unquoted one is a command.
#
# What stood here was `"'ds'" in S or '"ds"' in S or "ds_old" in S`,
# and `shlex.quote("ds")` is a no-op, so the first two were always
# False and the third was a literal the script always contains: the
# check could not fail. And the hostile probe built an image payload
# and then only looked at the name. Both halves are driven now, each
# with its own payload, and the test is whether the payload survives
# the quoting as *data*.
_bad_name = selfupdate.build_script("a b; touch /tmp/pwned", "", "img")
checks["a hostile container name cannot become a second command"] = (
    "; touch /tmp/pwned" not in
    _bad_name.replace(shlex.quote("a b; touch /tmp/pwned"), ""))
_bad_img = selfupdate.build_script("ds", "", "img; touch /tmp/pwned")
checks["…and neither can a hostile image reference"] = (
    "; touch /tmp/pwned" not in
    _bad_img.replace(shlex.quote("img; touch /tmp/pwned"), ""))
# …and the quoting really is `shlex.quote`'s and not a coincidence of
# the payload: a name needing no quoting must come through unchanged,
# or the two checks above also pass on a script that drops the name.
checks["…while an ordinary name still reaches the script"] = (
    "ds_plain" in selfupdate.build_script("ds_plain", "", "img"))

# ── the boot message that reports a rollback ─────────────────────────
# `main.py` reads the helper's log on the next start and tells the
# operator what happened while it was not running. The script has to
# write the phrase that is looked for, or the only report of a failed
# self-update is a log line nobody reads.
_main = open(os.path.join(os.path.dirname(__file__), "..", "app", "main.py"),
             encoding="utf-8").read()
# Two halves, and the join between them is what rots. The script's
# half is driven: the phrase has to be in what it writes. The boot
# half is read as a tree rather than driven, because the report sits
# in `main.py`'s startup path and nothing short of starting the
# program reaches it — stated rather than papered over.
#
# A tree and not a grep, because `'"rolling back" in _hcontent' in
# _main` was the check, and that is satisfied by the line sitting
# there as a comment: I switched the whole report off with
# `if False:  # "rolling back" in _hcontent` and it stayed green.
import ast  # noqa: E402

checks["the script writes the phrase on a rollback"] = "rolling back" in S

_tree = ast.parse(_main)
_guards = [n for n in ast.walk(_tree)
           if isinstance(n, ast.If)
           and isinstance(n.test, ast.Compare)
           and isinstance(n.test.left, ast.Constant)
           and n.test.left.value == "rolling back"
           and any(isinstance(o, ast.In) for o in n.test.ops)]
checks["…and the boot check tests for it as live code"] = len(_guards) == 1
# …and the branch actually tells somebody. A condition that is reached
# and then does nothing is the same silence with extra steps.
checks["…and announces when it is found"] = bool(_guards) and any(
    isinstance(c, ast.Call) and getattr(c.func, "id", "") == "boot_announce"
    for c in ast.walk(_guards[0]))

# ── `/selfupdate previous`, driven ───────────────────────────────────
# The section below announced the feature and exercised nothing: with
# `if target.lower() == "previous":` turned off, everything here stayed
# green. `_entries`, `_resolve` and two `_Ctx` members were scaffolding
# that made it look driven.
# A one-step rollback target without having to look up version numbers.
#: An upstream CHANGELOG, in the shape `resolve_target` really parses —
#: `## [x.y.z]` headings. `_entries` was a list of dicts nothing read.
CHANGELOG = """
## [2.19.0] - 2026-10-01
## [2.18.0] - 2026-09-20
## [2.17.4] - 2026-09-02
## [2.17.3] - 2026-08-30
"""


class _Ctx:
    config = types.SimpleNamespace(language="en")

    def t(self, key, **kw):
        return key

    def send_message(self, text, **kw):
        pass


def _resolve(target, running, changelog_ok=True, body=CHANGELOG):
    """`resolve_target`, with only the network stood in for.

    `changelog.fetch` is the one thing that would otherwise reach
    GitHub. The version walk, the comparison and the refusal are the
    real ones.
    """
    real = selfupdate.latest_released_version
    real_fetch = selfupdate.changelog.fetch
    import version as _v
    was = _v.VERSION
    _v.VERSION = running
    selfupdate.latest_released_version = lambda *a, **kw: "2.19.0"
    selfupdate.changelog.fetch = lambda *a, **kw: (
        (True, body) if changelog_ok else (False, "502 from GitHub"))
    try:
        return selfupdate.resolve_target(_Ctx(), f"repo/ds:{running}", target)
    finally:
        selfupdate.latest_released_version = real
        selfupdate.changelog.fetch = real_fetch
        _v.VERSION = was


# `previous` is the released version immediately below the running
# one, not the newest in the file and not the running one again.
_img, _err = _resolve("previous", "2.18.0")
checks["'previous' resolves to the release below the running one"] = (
    _img == "repo/ds:2.17.4" and _err is None)
_img, _err = _resolve("previous", "2.17.4")
checks["…counted from what is running, not from the top of the file"] = (
    _img == "repo/ds:2.17.3")
# The oldest release has nothing below it, and saying so beats picking
# something arbitrary.
_img, _err = _resolve("previous", "2.17.3")
checks["…and the oldest release says there is nothing before it"] = (
    _img is None and bool(_err))
# A changelog we could not fetch is not "there is no previous version".
_img, _err = _resolve("previous", "2.18.0", changelog_ok=False)
checks["…and an unreachable changelog says that, not 'none'"] = (
    _img is None and "fetch" in str(_err))

_img, _err = _resolve("1.2.3", "2.18.0")
checks["an explicit version is pinned as given"] = (
    _img == "repo/ds:1.2.3" and _err is None)
_img, _err = _resolve("nonsense", "2.18.0")
checks["…and something that is not a version is refused"] = bool(_err)

# ── the two guards in `run()` before anything is swapped ────────────
# Both were named in the opening paragraph as closed and neither was:
# an audit deleted the `return` after a failed pull and short-circuited
# the identical-image check, and the suite stayed green both times. A
# failed pull followed by a swap recreates Docksentry onto an image
# that was never fetched; an identical-image swap tears the container
# down and builds it again to arrive exactly where it started.
#
# `run()` is driven with the daemon stood in for — every `docker`
# invocation answers from a script — and what is read is whether
# `swap` was reached.
def _ran(pull_ok=True, same_image=True):
    """Run `selfupdate.run`; was the swap reached?"""
    swapped = []
    said = []

    import tempfile as _tf

    class _Ctx2:
        config = types.SimpleNamespace(
            language="en", container_cli="docker",
            history_file=os.path.join(_tf.mkdtemp(), "history.json"))
        _update_lock = __import__("threading").Lock()
        _swap_in_flight = False
        _queued_selfupdate = None

        def t(self, key, **kw):
            return key

        def send_message(self, text, **kw):
            said.append(text)

        def tell(self, text, **kw):
            said.append(text)

        def with_reply(self, reply):
            return self

        def log(self, *a, **kw):
            pass

    OLD_ID, NEW_ID = "sha256:old", ("sha256:old" if same_image
                                    else "sha256:new")

    def _fake_run(args, **kw):
        a = list(args)
        if a[:2] == ["docker", "pull"]:
            return types.SimpleNamespace(
                returncode=0 if pull_ok else 1, stdout="",
                stderr="" if pull_ok else "manifest unknown")
        if a[:2] == ["docker", "inspect"]:
            return types.SimpleNamespace(
                returncode=0, stdout=f"{NEW_ID}||2026-10-16", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    # `run()` resolves its own container first; without this it bails
    # with `selfupdate_failed_container` and every check below passes
    # because nothing was swapped — which is how the first version of
    # this block was green on a path it never entered. The two negative
    # checks are what caught it.
    from update_checker import UpdateChecker as _UC
    real_run = selfupdate.subprocess.run
    real_swap = selfupdate.swap
    real_self = _UC.inspect_self
    _UC.inspect_self = staticmethod(lambda *a, **kw: {
        "Name": "/ds", "Image": OLD_ID, "Created": "2026-01-01T00:00:00Z",
        "Config": {"Image": "repo/ds:latest"}})
    selfupdate.subprocess.run = _fake_run
    selfupdate.swap = lambda *a, **kw: swapped.append(True)
    try:
        selfupdate.run(_Ctx2(), target=None)
    except Exception as e:                                  # noqa: BLE001
        said.append(f"raised {type(e).__name__}: {e}")
    finally:
        selfupdate.subprocess.run = real_run
        selfupdate.swap = real_swap
        _UC.inspect_self = real_self
    return bool(swapped), "\n".join(said)


# `same_image=False`, deliberately: with a matching image the next
# guard would stop the swap anyway, so a failed pull would look
# guarded by a `return` that is no longer there. The counter-probe
# said exactly that — removing the `return` left this green until the
# probe stopped giving it a second reason to stop.
_swapped, _said = _ran(pull_ok=False, same_image=False)
checks["a failed pull stops the update before the swap"] = (
    not _swapped and "failed_container" not in _said)
checks["…and says the pull was what failed"] = "pull" in _said.lower()

_swapped, _said = _ran(pull_ok=True, same_image=True)
checks["an identical image is not swapped onto itself"] = not _swapped

_swapped, _said = _ran(pull_ok=True, same_image=False)
checks["…while a genuinely new image is"] = _swapped

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
