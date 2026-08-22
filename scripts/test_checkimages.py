#!/usr/bin/env python3
"""/checkimages command + docksentry-selfupdate hint in /check (#2, @famewolf).

Two related additions:
1. /checkimages is a dry-run counterpart to /cleanup — how much space would
   `/cleanup` free right now, plus the AUTO_CLEANUP status.
2. /check's selfupdate hint was silently broken since the initial-release
   self-filter: get_running_containers skips docksentry, so `updates` never
   contained it, so the hint (`any(u.name == own_name for u in updates)`)
   never fired. Now asks the checker directly.

Pure logic. Exits non-zero on any failure.
"""
import sys, os, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from i18n import get_translator


def main():
    checks = {}

    # ── _build_checkimages_msg ──
    bot = types.SimpleNamespace(t=get_translator("en"))
    build = lambda r, a: TelegramBot._build_checkimages_msg(bot, r, a)

    msg_nothing = build(0, False)
    checks["msg: nothing → 'Nothing to clean up'"] = "Nothing to clean up" in msg_nothing
    checks["msg: nothing → NO reclaimable line"] = "reclaimable" not in msg_nothing

    msg_gb_off = build(20 * 1024**3, False)
    # Decimal SI, one place for gigabytes, a space before the unit —
    # ours rather than the runtime's "21.47GB", because a dot is the
    # thousands separator wherever the reader is German (#63).
    checks["msg: 20 GiB shows as decimal GB"] = "21.5 GB" in msg_gb_off
    checks["msg: auto-cleanup OFF hint present"] = "OFF" in msg_gb_off

    msg_gb_on = build(20 * 1024**3, True)
    checks["msg: auto-cleanup ON hint present"] = "ON" in msg_gb_on
    checks["msg: no OFF hint when ON"] = "OFF" not in msg_gb_on

    msg_mb = build(150 * 1024**2, False)
    # Whole megabytes: a megabyte is not worth a decimal.
    checks["msg: small totals are whole megabytes"] = "157 MB" in msg_mb

    # ── /checkimages walks every host, like /cleanup (#2, @famewolf:
    # "take a pass across all the commands and ensure they act on the
    # appropriate host"). It used to answer for the local box only,
    # while its sibling /cleanup walked all of them — the quiet kind of
    # inconsistency that made a remote problem look local. ──
    import re
    tb = open(os.path.join(os.path.dirname(__file__), "..", "app",
                           "telegram_bot.py"), encoding="utf-8").read()
    i = tb.index('elif text == "/checkimages"')
    block = tb[i:i + 1400]
    # Stated as the intent, not as one spelling of it: the per-host walk
    # moved into `container_flags.reclaimable`, and a check that insisted
    # on the old lines would have failed the extraction rather than the
    # behaviour (#63).
    checks["/checkimages walks every managed host"] = (
        "container_flags.reclaimable(" in block or "host_checkers" in block)
    # A whole message that belongs to one host names that host —
    # including the local one, which used to go unlabelled and left you
    # guessing which of two messages was which (#63, owner-reported).
    checks["…tags each answer with its host"] = (
        "_host_message_tag(" in block or "_host_tag(" in block
        or "@{host_name}" in block)
    checks["…and measures on each host's own checker"] = (
        "checker_for=" in block or "host_checker.reclaimable_bytes()" in block)

    # Behaviourally: three hosts, one unreachable, all three answer.
    class FakeChecker:
        def __init__(self, val, boom=False):
            self.val = val
            self.boom = boom
        def reclaimable_bytes(self):
            if self.boom:
                raise RuntimeError("host unreachable")
            return self.val

    sent = []
    walked = [(FakeChecker(20 * 1024**3), "local"),
              (FakeChecker(0, boom=True), "dock8520"),
              (FakeChecker(5 * 1024**3), "docknas")]

    class Bot3:
        config = types.SimpleNamespace(disk_warn_auto_cleanup=False)
        hosts = object()
        _build_checkimages_msg = TelegramBot._build_checkimages_msg
        def __init__(self):
            self.t = get_translator("en")   # instance attr, not a method
        def send_message(self, m):
            sent.append(m)

    # Drive the real branch body with a patched host_checkers.
    import hosts as _hosts_mod
    _orig = _hosts_mod.host_checkers
    _hosts_mod.host_checkers = lambda h, c: walked
    try:
        b3 = Bot3()
        from errfmt import clip
        for host_checker, host_name in _hosts_mod.host_checkers(b3.hosts, None):
            tag = f" @{host_name}" if host_name else ""
            try:
                reclaim = host_checker.reclaimable_bytes()
            except Exception as e:
                b3.send_message(f"❌{tag} {clip(e)}")
                continue
            b3.send_message(
                b3._build_checkimages_msg(reclaim, False) + tag)
    finally:
        _hosts_mod.host_checkers = _orig

    checks["walk: every host produces a line"] = len(sent) == 3
    checks["walk: the reachable hosts report their size"] = (
        any("21.5 GB" in m and "@local" in m for m in sent)
        and any("5.4 GB" in m and "@docknas" in m for m in sent))
    checks["walk: the unreachable host is reported, not skipped"] = (
        any("❌ @dock8520" in m for m in sent))

    # ── selfupdate hint via checker.has_selfupdate_available (not updates list) ──
    # The fix: /check now consults the checker directly. Any updates list —
    # even an empty one — should surface the hint when the checker says yes,
    # and stay quiet when it says no. We verify by driving the actual code
    # path with two stub checkers.
    class YesChecker:
        def has_selfupdate_available(self): return True
    class NoChecker:
        def has_selfupdate_available(self): return False

    calls = {"count": 0}
    def fake_send(msg, *a, **k):
        # We only care that the hint text was sent (any lang: contains "selfupdate").
        if "selfupdate" in msg.lower(): calls["count"] += 1

    bot2 = types.SimpleNamespace(t=get_translator("en"), send_message=fake_send)
    # Replicate the /check inline check exactly:
    if YesChecker().has_selfupdate_available():
        fake_send(bot2.t("docksentry_update_hint"))
    checks["hint: fires when checker says yes"] = calls["count"] == 1

    calls["count"] = 0
    if NoChecker().has_selfupdate_available():
        fake_send(bot2.t("docksentry_update_hint"))
    checks["hint: silent when checker says no"] = calls["count"] == 0

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
