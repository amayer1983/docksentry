#!/usr/bin/env python3
"""Test glob matching for <name> commands (#40, @LeeNX).

  - _is_glob recognises *, ?, [...] (and not plain names)
  - the bot's own glob lookup for /check and /update (`_select_containers`,
    which goes through `find_on_hosts` and `lifecycle.match_glob_checked`
    since W4) returns the right containers, case-insensitively, excluding
    _old rollback leftovers, and says "no match" for a pattern that
    matches nothing — on a daemon that answered, not "unreachable"
  - `lifecycle.match_glob`, which /start, /stop and /restart still use,
    returns the same sets

Uses real throwaway containers. Requires Docker. Exits non-zero on failure.
"""
import sys, os, types, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livefixture as lf
from telegram_bot import TelegramBot
from container_backend import get_backend
from i18n import get_translator
import lifecycle

# A missing container runtime is not a regression. Without this the
# procedure goes red on a machine that simply has no Docker, which is
# indistinguishable from a real failure — measured: five procedures did
# exactly that before the guard, while test_multihost_live skipped
# cleanly. CI must be able to tell the two apart.
if not lf.daemon_available():
    print("SKIP: no Docker daemon available")
    __import__("sys").exit(0)

# The run's own stem, so two suites at once do not force-remove each
# other's containers and then each report globbing as broken. It sits
# in front of the part the patterns below vary, because a suffix would
# stop `ds_ctf-*-even` matching anything at all.
STEM = f"ds_ctf{lf.TAG}"
NAMES = [f"{STEM}-a-even", f"{STEM}-b-even", f"{STEM}-a-odd"]
OLD = f"{STEM}-a-even_old"


def main():
    # The bot's real lookup, bound to a stub that carries what it reads:
    # the backend seam and the translator. Until W4 this drove
    # `TelegramBot._match_glob`, which nothing in the bot called any more.
    stub = types.SimpleNamespace(backend=get_backend(None),
                                 t=get_translator("en"))
    select = types.MethodType(TelegramBot._select_containers, stub)
    is_glob = TelegramBot._is_glob

    def bot_glob(pattern):
        return select(pattern)

    def life_glob(pattern):
        return lifecycle.match_glob(pattern, backend=stub.backend)

    # The two checks that need no daemon, computed before anything is
    # started, and handed to `lf.start`: a registry that will not serve
    # `alpine` then reports a SKIP *with* these two rather than
    # discarding them. They used to be built after the start and went
    # with it — the same defect this fixture family fixed in
    # `test_remote_transports.py`.
    checks = {
        "_is_glob true for *,?,[": (is_glob("a*") and is_glob("a?b")
                                    and is_glob("a[0-9]")),
        "_is_glob false for plain name": not is_glob("sonarr"),
    }

    lf.rm(*NAMES, OLD)
    # `lf.start` rather than a discarded return code: on a machine with
    # no route to a registry the containers are simply not there, and
    # the five checks below then report globbing as broken. That reading
    # points at code that is fine.
    for n in NAMES:
        lf.start(["run", "-d", "--name", n, "alpine", "sh", "-c",
                  "trap 'exit 0' TERM; sleep 200 & wait"], checks=checks)
    # an _old rollback leftover that must NOT match
    lf.start(["run", "-d", "--name", OLD, "alpine", "sh", "-c",
              "trap 'exit 0' TERM; sleep 200 & wait"], checks=checks)
    pats = {"even": f"{STEM}-*-even", "all": f"{STEM}-*",
            "q": f"{STEM}-?-odd",
            "upper": f"{STEM.upper()}-*-EVEN"}  # case-insensitive
    nosuch = f"{STEM}-zzz*"
    try:
        bot = {k: bot_glob(p) for k, p in pats.items()}
        bot_none = bot_glob(nosuch)
        life = {k: life_glob(p) for k, p in pats.items()}
    finally:
        lf.rm(*NAMES, OLD)

    want = {"even": [f"{STEM}-a-even", f"{STEM}-b-even"],
            "all": sorted(NAMES),
            "q": [f"{STEM}-a-odd"],
            "upper": [f"{STEM}-a-even", f"{STEM}-b-even"]}
    # `_select_containers` answers (names, error, unreachable).
    even, allc = bot["even"][0], bot["all"][0]
    checks.update({
        "glob *-even matches both even": bot["even"] == (want["even"], None, None),
        "glob ctf-* matches all three": bot["all"] == (want["all"], None, None),
        "_old leftover excluded": OLD not in allc,
        "? single-char glob": bot["q"] == (want["q"], None, None),
        "case-insensitive match": bot["upper"] == (want["upper"], None, None),
        "no match says so, and not unreachable": bot_none == (
            [], stub.t("glob_no_match", pattern=nosuch), None),
        "lifecycle.match_glob (/start|stop|restart) gives the same sets":
            life == want,
    })
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("even=", even, "all=", allc)
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
