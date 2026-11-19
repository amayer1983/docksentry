#!/usr/bin/env python3
"""A globbed /check or /update scopes the check via `only=` (#53, @LeeNX).

Before #53 the globbed Telegram commands checked *every* container and then
filtered the result down to the matched names — wasteful, and unfriendly to
Docker Hub's anonymous rate limit. Now they pass `only=<matched names>` to
`check_all`, so only the named containers hit the registry.

This drives the real `_handle_message` dispatch on a real `TelegramBot`
(built with `__new__`, no Telegram, no Docker) whose local backend is a
fake CLI: `ps -a --format {{.Names}}` lists a fixed set, so both the glob
(`lifecycle.match_glob`) and the plain name (`container_flags.find_on_hosts`)
are resolved by the real code, not by a stand-in. The checker records the
`only` argument and counts remote lookups, and the /update thread is run
synchronously so its `updates` payload is inspectable. Exits non-zero on
any failure.
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


class _FakeBackend:
    """The local CLI: answers `ps` with RUNNING, records every call."""

    def __init__(self):
        self.calls = []

    def run(self, args, **kw):
        import subprocess
        self.calls.append(list(args))
        out = "\n".join(RUNNING) if args and args[0] == "ps" else ""
        return subprocess.CompletedProcess(args, 0, out, "")


def _make_bot():
    bot = TelegramBot.__new__(TelegramBot)
    # No registry: `_multi()` is False, `_resolve_targets` hands back the
    # argument untouched and `targets=None` — the single-host path.
    bot.hosts = None
    bot.backend = _FakeBackend()
    bot.bot_username = ""
    bot.engine = types.SimpleNamespace(update_running=False)
    bot.sent = []
    bot.notified = None
    bot.updated_with = "unset"

    # Faked-out edges only: auth, translation, the chat transport, and the
    # two hand-offs whose payload is what this test reads.
    bot._check_auth = lambda chat_id, user_id, kind="message": True
    bot.t = lambda key, **kw: key
    bot.send_message = lambda text, **kw: bot.sent.append(text)
    bot.notify_updates = lambda updates, auto=False: setattr(bot, "notified", updates)
    bot.run_updates = lambda checker, updates=None: setattr(bot, "updated_with", updates)
    return bot


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

    # ── the same with a plain name: one container, still scoped ────
    bot = _make_bot()
    chk = _FakeChecker()
    bot._handle_message(_msg("/check web-1"), chk, None)
    checks["/check name: check_all got only={web-1}"] = chk.only_seen == {"web-1"}
    checks["/check name: notified with web-1's update"] = \
        [u["name"] for u in (bot.notified or [])] == ["web-1"]
    checks["/check name: the name was looked up on the local CLI"] = \
        ["ps", "-a", "--format", "{{.Names}}"] in bot.backend.calls

    bot = _make_bot()
    chk = _FakeChecker()
    bot._handle_message(_msg("/update web-2"), chk, None)
    checks["/update name: check_all got only={web-2}"] = chk.only_seen == {"web-2"}
    checks["/update name: web-2 has nothing pending, nothing runs"] = \
        bot.updated_with == "unset" and "update_scoped_none" in bot.sent

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
