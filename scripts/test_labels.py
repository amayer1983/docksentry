#!/usr/bin/env python3
"""Test container-label config overrides (#42, @LeeNX) and the `-?` help
alias (#15, @LeeNX).

Pure logic — no Docker. Covers the label parser/interpreter, the
get_running_containers exclude decision, the _is_protected precedence
(label overrides stored toggle; absence + inspect-failure fall back to the
toggle and never silently unprotect), and the /cmd -? → /help cmd rewrite.
"""
import sys, os, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker
from telegram_bot import TelegramBot


def main():
    checks = {}

    # ── _parse_ps_labels ───────────────────────────────────────
    p = UpdateChecker._parse_ps_labels
    checks["parse: basic"] = p("a=1,docksentry.protect=true,b=x") == {
        "a": "1", "docksentry.protect": "true", "b": "x"}
    checks["parse: empty/None -> {}"] = p("") == {} and p(None) == {}

    # ── label_bool ─────────────────────────────────────────────
    lb = UpdateChecker.label_bool
    checks["bool: true variants"] = all(
        lb({"docksentry.protect": v}, "protect") is True
        for v in ("true", "True", "1", "yes", "on", "  TRUE "))
    checks["bool: false value"] = lb({"docksentry.protect": "false"}, "protect") is False
    checks["bool: absent -> None"] = lb({"a": "b"}, "protect") is None
    checks["bool: empty/None -> None"] = lb({}, "x") is None and lb(None, "x") is None

    # ── exclude decision (mirrors get_running_containers) ───────
    def excluded(labels):
        return lb(labels, "enable") is False or lb(labels, "exclude") is True
    checks["exclude: enable=false"] = excluded({"docksentry.enable": "false"}) is True
    checks["exclude: exclude=true"] = excluded({"docksentry.exclude": "true"}) is True
    checks["exclude: enable=true -> kept"] = excluded({"docksentry.enable": "true"}) is False
    checks["exclude: no labels -> kept"] = excluded({}) is False

    # ── _is_protected precedence ───────────────────────────────
    class _Store:
        def __init__(self, toggle): self._t = toggle
        def is_protect_stop(self, name): return self._t

    class _Checker:
        def __init__(self, labels, boom=False): self._l, self._boom = labels, boom
        label_bool = staticmethod(UpdateChecker.label_bool)
        def get_container_labels(self, name):
            if self._boom:
                raise RuntimeError("inspect failed")
            return self._l

    def protected(label_val, toggle, boom=False):
        bot = types.SimpleNamespace(store=_Store(toggle))
        labels = {"docksentry.protect": label_val} if label_val is not None else {}
        return TelegramBot._is_protected(bot, "c", _Checker(labels, boom))

    checks["protect: label true overrides toggle off"] = protected("true", False) is True
    checks["protect: label false overrides toggle on"] = protected("false", True) is False
    checks["protect: no label -> toggle (on)"] = protected(None, True) is True
    checks["protect: no label -> toggle (off)"] = protected(None, False) is False
    checks["protect: inspect failure -> toggle (on, never unprotect)"] = protected("false", True, boom=True) is True

    # ── docksentry.pin (mirrors get_running_containers) ────────
    checks["pin: label true -> skipped"] = lb({"docksentry.pin": "true"}, "pin") is True
    checks["pin: label false -> kept"] = lb({"docksentry.pin": "false"}, "pin") is False
    checks["pin: absent -> stored pin rules"] = lb({}, "pin") is None

    # ── docksentry.auto precedence (mirrors handle_autoupdates) ─
    def effective_auto(label_val, all_auto, in_list):
        labels = {"docksentry.auto": label_val} if label_val is not None else {}
        lab = lb(labels, "auto")
        if lab is not None:
            return lab
        return all_auto or in_list

    checks["auto: label false beats AUTO_UPDATE_ALL"] = effective_auto("false", True, True) is False
    checks["auto: label true beats missing toggle"] = effective_auto("true", False, False) is True
    checks["auto: no label + all_auto"] = effective_auto(None, True, False) is True
    checks["auto: no label + toggle"] = effective_auto(None, False, True) is True
    checks["auto: no label + nothing -> manual"] = effective_auto(None, False, False) is False

    # ── docksentry.trust-running via checker (#9 + #42) ─────────
    class _TRConfig:
        trust_running_file = None
    def trust(label_val):
        ck = UpdateChecker.__new__(UpdateChecker)
        ck.config = _TRConfig()
        labels = {"docksentry.trust-running": label_val} if label_val is not None else {}
        ck.get_container_labels = lambda name: labels
        return ck._is_trust_running("c")
    checks["trust: label true"] = trust("true") is True
    checks["trust: label false"] = trust("false") is False
    checks["trust: absent -> stored (off)"] = trust(None) is False

    # ── docksentry.ask-major precedence (mirrors batch gate) ────
    def ask_major(label_val, in_list):
        labels = {"docksentry.ask-major": label_val} if label_val is not None else {}
        lab = lb(labels, "ask-major")
        return lab if lab is not None else in_list
    checks["ask-major: label true forces gate"] = ask_major("true", False) is True
    checks["ask-major: label false skips gate"] = ask_major("false", True) is False
    checks["ask-major: absent -> stored list"] = ask_major(None, True) is True

    # ── /cmd -? help alias (#15) ───────────────────────────────
    ha = TelegramBot._help_alias
    checks["alias: /protect -? -> /help protect"] = ha("/protect -?") == "/help protect"
    checks["alias: /status -? -> /help status"] = ha("/status -?") == "/help status"
    checks["alias: plain command unchanged"] = ha("/protect") == "/protect"
    checks["alias: -? not sole arg unchanged"] = ha("/notify @x -?") == "/notify @x -?"
    checks["alias: command with arg unchanged"] = ha("/status foo") == "/status foo"

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
