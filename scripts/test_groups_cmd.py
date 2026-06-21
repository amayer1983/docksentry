#!/usr/bin/env python3
"""Logic test for the /groups Telegram command (roadmap, #2).

Verifies the read-only list/detail builders and group resolver against a
synthetic store — no Telegram, no Docker. Exits non-zero on any failure.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from i18n import get_translator


class _StubStore:
    G = {
        "media": {"name": "Media VPN",
                  "containers": ["gluetun", "sonarr", "radarr"],
                  "restart_dependents": True, "wait_seconds": 30},
        "solo": {"name": "Solo", "containers": ["pihole"],
                 "restart_dependents": False},
    }

    def get_groups(self):
        return self.G

    def get_group(self, gid):
        return self.G.get(gid)


class _Stub:
    store = _StubStore()

    def t(self, key, **kw):
        return get_translator("en")(key, **kw)

    def _running_names(self):
        return {"gluetun", "sonarr"}  # radarr stopped


for _m in ("_resolve_group", "_build_groups_list", "_build_group_detail"):
    setattr(_Stub, _m, getattr(TelegramBot, _m))


def main():
    s = _Stub()
    lst = s._build_groups_list()
    detail, markup = s._build_group_detail("media")
    _, solo_markup = s._build_group_detail("solo")
    checks = {
        "list shows both groups": "Media VPN" in lst and "Solo" in lst,
        "head crown only on multi-member": "👑 `gluetun`" in lst and "👑 `pihole`" not in lst,
        "detail running icon": "🟢 `gluetun`" in detail,
        "detail stopped icon": "⚪ `radarr`" in detail,
        "restart button on restart_dependents group": markup is not None and "restart_deps:media" in str(markup),
        "no button on single-member group": solo_markup is None,
        "partial name resolve": s._resolve_group("vpn")[0] == "media",
        "not-found message": "No group matching" in s._build_group_detail("nope")[0],
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
