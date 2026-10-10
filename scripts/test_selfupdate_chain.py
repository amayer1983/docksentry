#!/usr/bin/env python3
"""The chain that replaces Docksentry with a new Docksentry.

Six reverts in this path went unnoticed by all 194 procedures on
10.10.2026: a failed pull no longer stopping the update, the swap
happening even when the image bits are identical, `/selfupdate
previous` resolving to nothing, the old container never cleaned up, no
restart after a failed rename, and the boot message that says a
recreate was rolled back.

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
checks["the container name and image are quoted into the shell"] = (
    "'ds'" in S or '"ds"' in S or "ds_old" in S)
_odd = selfupdate.build_script("a b; rm -rf /", "", "img; touch /tmp/x")
checks["…so a hostile name cannot become a second command"] = (
    "; rm -rf /" not in _odd.replace("'a b; rm -rf /'", "")
    .replace('"a b; rm -rf /"', ""))

# ── the boot message that reports a rollback ─────────────────────────
# `main.py` reads the helper's log on the next start and tells the
# operator what happened while it was not running. The script has to
# write the phrase that is looked for, or the only report of a failed
# self-update is a log line nobody reads.
_main = open(os.path.join(os.path.dirname(__file__), "..", "app", "main.py"),
             encoding="utf-8").read()
checks["the boot check looks for the phrase the script writes"] = (
    '"rolling back" in _hcontent' in _main and "rolling back" in S)

# ── `/selfupdate previous` ───────────────────────────────────────────
# A one-step rollback target without having to look up version numbers.
_entries = [{"version": "2.19.0"}, {"version": "2.18.0"},
            {"version": "2.17.4"}, {"version": "2.17.3"}]


class _Ctx:
    config = types.SimpleNamespace(language="en")

    def t(self, key, **kw):
        return key

    def send_message(self, text, **kw):
        pass


def _resolve(target, running):
    real = selfupdate.latest_released_version
    import version as _v
    was = _v.VERSION
    _v.VERSION = running
    selfupdate.latest_released_version = lambda *a, **kw: "2.19.0"
    try:
        return selfupdate.resolve_target(_Ctx(), f"repo/ds:{running}", target)
    finally:
        selfupdate.latest_released_version = real
        _v.VERSION = was


_img, _err = _resolve("1.2.3", "2.18.0")
checks["an explicit version is pinned as given"] = (
    _img == "repo/ds:1.2.3" and _err is None)
_img, _err = _resolve("nonsense", "2.18.0")
checks["…and something that is not a version is refused"] = bool(_err)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
