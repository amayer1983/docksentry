#!/usr/bin/env python3
"""Tests for per-container update cooldown (#2).

  - store set/get roundtrip + clamping + clear-on-zero
  - _maybe_cooldown sleeps only when a cooldown is set AND more updates follow

No Docker; sleep is monkeypatched so the test is instant. Exits non-zero on
any failure.
"""
import sys, os, tempfile, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import telegram_bot
from telegram_bot import TelegramBot
from container_store import ContainerStore

FILE_ATTRS = ["pinned_file", "autoupdate_file", "update_windows_file",
              "ask_before_major_file", "trust_running_file", "cooldown_file",
              "major_pending_file", "groups_file", "notes_file", "links_file"]


def _store():
    d = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(**{a: os.path.join(d, a + ".json") for a in FILE_ATTRS})
    return ContainerStore(cfg)


def main():
    checks = {}

    st = _store()
    st.set_cooldown("llama", 20)
    checks["set/get roundtrip"] = st.get_cooldown("llama") == 20
    checks["clamp over 600"] = st.set_cooldown("big", 9999) == 600
    checks["clamp negative to 0"] = st.set_cooldown("neg", -5) == 0
    checks["bad value -> 0"] = st.set_cooldown("bad", "abc") == 0
    st.set_cooldown("llama", 0)
    checks["zero clears entry"] = "llama" not in st.get_cooldowns()
    checks["unset container -> 0"] = st.get_cooldown("nope") == 0

    # _maybe_cooldown: patch sleep to record durations instead of waiting.
    # The method does a local `import time as _time`, so patch time.sleep.
    import time as _time_mod
    slept = []
    orig_sleep = _time_mod.sleep
    _time_mod.sleep = lambda s: slept.append(s)
    try:
        st2 = _store()
        st2.set_cooldown("heavy", 15)

        class _Bot:
            store = st2
        bot = _Bot()
        mc = TelegramBot._maybe_cooldown

        slept.clear(); mc(bot, "heavy", more_remaining=True)
        checks["sleeps cooldown when more remain"] = slept == [15]

        slept.clear(); mc(bot, "heavy", more_remaining=False)
        checks["no sleep when last in batch"] = slept == []

        slept.clear(); mc(bot, "light", more_remaining=True)  # no cooldown set
        checks["no sleep when cooldown unset"] = slept == []
    finally:
        _time_mod.sleep = orig_sleep

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
