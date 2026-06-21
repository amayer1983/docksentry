#!/usr/bin/env python3
"""Logic test for the /check --dry-run preview builder (roadmap dry-run, #2).

Verifies _build_dry_run renders, for a synthetic update set:
  - the standalone-recreate path when the compose file is unreachable,
  - the dependents line for the HEAD of a restart_dependents group,
  - the major-version-jump warning,
  - the "nothing was changed" framing.

Pure logic — no Docker or network. Exits non-zero on any failure.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from i18n import get_translator


class _StubStore:
    def get_groups(self):
        return {"g1": {"containers": ["gluetun", "sonarr"],
                       "restart_dependents": True}}


class _Stub:
    store = _StubStore()

    def _enrich_with_source_url(self, updates):
        pass

    def _display_name(self, u):
        return f"`{u['name']}`"

    def t(self, key, **kw):
        return get_translator("en")(key, **kw)

    def _is_major_bump(self, u, checker):
        return (True, "1.2.3", "2.0.0") if u["name"] == "sonarr" else (False, None, None)


def main():
    updates = [
        {"name": "gluetun", "image": "qmcgaw/gluetun:latest", "size": "50 MB",
         "created": "2026-06-20", "compose_project": "vpn",
         "compose_service": "gluetun", "compose_file": "/does/not/exist"},
        {"name": "sonarr", "image": "linuxserver/sonarr:1.2.3", "size": "180 MB",
         "created": "2026-06-19"},
    ]
    out = TelegramBot._build_dry_run(_Stub(), updates, checker=None)
    checks = {
        "title": "Dry run" in out,
        "standalone path when compose file missing": "standalone recreate" in out,
        "dependents listed for head": "restart dependents" in out and "`sonarr`" in out,
        "major jump flagged": "major jump" in out and "2.0.0" in out,
        "nothing-changed framing": "Nothing was changed" in out,
    }
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
