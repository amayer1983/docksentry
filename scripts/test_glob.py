#!/usr/bin/env python3
"""Test glob matching for <name> commands (#40, @LeeNX).

  - _is_glob recognises *, ?, [...] (and not plain names)
  - _match_glob returns the right containers, case-insensitively, excluding
    _old rollback leftovers

Uses real throwaway containers. Requires Docker. Exits non-zero on failure.
"""
import sys, os, types, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from container_backend import get_backend

NAMES = ["ds_ctf-a-even", "ds_ctf-b-even", "ds_ctf-a-odd"]


def _rm(*names):
    for n in names:
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)


def main():
    # _match_glob lists containers through the container backend seam, so
    # the stub needs one just like a real bot has.
    stub = types.SimpleNamespace(backend=get_backend(None))
    stub._match_glob = types.MethodType(TelegramBot._match_glob, stub)
    is_glob = TelegramBot._is_glob

    _rm(*NAMES, "ds_ctf-a-even_old")
    for n in NAMES:
        subprocess.run(["docker", "run", "-d", "--name", n, "alpine", "sleep", "200"],
                       capture_output=True)
    # an _old rollback leftover that must NOT match
    subprocess.run(["docker", "run", "-d", "--name", "ds_ctf-a-even_old", "alpine", "sleep", "200"],
                   capture_output=True)
    try:
        even = stub._match_glob("ds_ctf-*-even")
        allc = stub._match_glob("ds_ctf-*")
        q = stub._match_glob("ds_ctf-?-odd")
        upper = stub._match_glob("DS_CTF-*-EVEN")  # case-insensitive
    finally:
        _rm(*NAMES, "ds_ctf-a-even_old")

    checks = {
        "_is_glob true for *,?,[": is_glob("a*") and is_glob("a?b") and is_glob("a[0-9]"),
        "_is_glob false for plain name": not is_glob("sonarr"),
        "glob *-even matches both even": even == ["ds_ctf-a-even", "ds_ctf-b-even"],
        "glob ctf-* matches all three": allc == sorted(NAMES),
        "_old leftover excluded": "ds_ctf-a-even_old" not in allc,
        "? single-char glob": q == ["ds_ctf-a-odd"],
        "case-insensitive match": upper == ["ds_ctf-a-even", "ds_ctf-b-even"],
    }
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
