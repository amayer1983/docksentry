#!/usr/bin/env python3
"""Logic test for the /selfupdate latest|stable force-re-tag decision (#2).

_should_retag_moving decides whether a same-digest self-update should still
recreate to adopt a rolling tag. Pure logic — no Docker. Exits non-zero on
any failure.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import selfupdate  # noqa: E402
from telegram_bot import TelegramBot

f = selfupdate.should_retag_moving


class _S:
    pass


def main():
    s = _S()
    cases = {
        "latest from pinned tag -> retag": f(s, "latest", "repo:1.19.0", "repo:latest") is True,
        "stable from pinned tag -> retag": f(s, "stable", "repo:1.19.0", "repo:stable") is True,
        "latest while already on latest -> no recreate": f(s, "latest", "repo:latest", "repo:latest") is False,
        "version pin target -> no (digest-differs path handles it)": f(s, "1.20.0", "repo:1.19.0", "repo:1.20.0") is False,
        "None target -> no": f(s, None, "repo:latest", "repo:latest") is False,
        "previous target -> no": f(s, "previous", "repo:1.19.0", "repo:1.18.0") is False,
    }
    for k, v in cases.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(cases.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
