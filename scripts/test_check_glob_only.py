#!/usr/bin/env python3
"""A globbed /check or /update scopes the check via `only=` (#53, @LeeNX).

Before #53 the globbed Telegram commands checked *every* container and then
filtered the result down to the matched names — wasteful, and unfriendly to
Docker Hub's anonymous rate limit. Now they pass `only=<matched names>` to
`check_all`, so only the named containers hit the registry.

This drives the real `_handle_message` dispatch with a fake checker that
records the `only` argument and counts remote lookups. No Docker, no network:
`_match_glob` is stubbed to a fixed match set, and the /update thread is run
synchronously so its `updates` payload is inspectable. Exits non-zero on any
failure.
"""
import sys, os, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import telegram_bot
from telegram_bot import TelegramBot


# Running set is deliberately larger than the glob match, so "checked only
# the matched ones" is distinguishable from "checked everything".
RUNNING = ["web-1", "web-2", "db", "cache"]
MATCHED = ["web-1", "web-2"]
OUTDATED = {"web-1"}  # only web-1 actually has a pending update


class _FakeChecker:
    """Records what `check_all` was asked to check. `only` is honoured: a
    scoped call looks at exactly those names (mimics update_checker's
    `only=` short-circuit), so `remote_calls` reflects the real cost."""

    def __init__(self):
        self.only_seen = "unset"
        self.remote_calls = []

    def check_all(self, bot=None, only=None):
        self.only_seen = only
        names = list(only) if only is not None else list(RUNNING)
        self.remote_calls = names[:]  # one lookup per checked container
        return [{"name": n, "image": f"reg/{n}:tag"}
                for n in names if n in OUTDATED]


class _SyncThread:
    """threading.Thread stand-in that runs the target synchronously on
    .start(), so the /update branch's run_updates payload is captured
    deterministically (no race)."""

    def __init__(self, target=None, args=(), kwargs=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


def _make_bot():
    stub = types.SimpleNamespace()
    stub.update_running = False
    stub.bot_username = ""
    stub.sent = []
    stub.notified = None
    stub.updated_with = "unset"

    # Real logic under test — the dispatch + the glob→only plumbing.
    stub._handle_message = types.MethodType(TelegramBot._handle_message, stub)
    stub._select_containers = types.MethodType(TelegramBot._select_containers, stub)
    # Static helpers: keep them static so `self._is_glob(arg)` doesn't bind self.
    stub._is_glob = staticmethod(TelegramBot._is_glob.__func__
                                 if hasattr(TelegramBot._is_glob, "__func__")
                                 else TelegramBot._is_glob)
    stub._help_alias = staticmethod(TelegramBot._help_alias.__func__
                                    if hasattr(TelegramBot._help_alias, "__func__")
                                    else TelegramBot._help_alias)

    # Faked-out edges: no Docker, no network, no Telegram.
    stub._check_auth = lambda chat_id, user_id, kind="message": True
    stub._match_glob = lambda pattern, include_stopped=True: list(MATCHED)
    stub.t = lambda key, **kw: key
    stub.send_message = lambda text, **kw: stub.sent.append(text)
    stub.notify_updates = lambda updates, auto=False: setattr(stub, "notified", updates)
    stub.run_updates = lambda checker, updates=None: setattr(stub, "updated_with", updates)
    return stub


def _msg(text):
    return {"text": text, "from": {"id": 1}, "chat": {"id": 1}}


def main():
    checks = {}

    telegram_bot.threading = types.SimpleNamespace(Thread=_SyncThread)

    # ── /check <glob> ───────────────────────────────────────────────
    bot = _make_bot()
    chk = _FakeChecker()
    bot._handle_message(_msg("/check web-*"), chk, None)
    checks["/check glob: check_all got only=matched"] = chk.only_seen == set(MATCHED)
    checks["/check glob: only the matched were looked up"] = \
        sorted(chk.remote_calls) == sorted(MATCHED)
    checks["/check glob: did NOT check db/cache"] = \
        "db" not in chk.remote_calls and "cache" not in chk.remote_calls
    checks["/check glob: notified with web-1's update"] = \
        [u["name"] for u in (bot.notified or [])] == ["web-1"]

    # ── /update <glob> ──────────────────────────────────────────────
    bot = _make_bot()
    chk = _FakeChecker()
    bot._handle_message(_msg("/update web-*"), chk, None)
    checks["/update glob: check_all got only=matched"] = chk.only_seen == set(MATCHED)
    checks["/update glob: only the matched were looked up"] = \
        sorted(chk.remote_calls) == sorted(MATCHED)
    checks["/update glob: run_updates got only pending matches"] = \
        [u["name"] for u in (bot.updated_with or [])] == ["web-1"]

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
