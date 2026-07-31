#!/usr/bin/env python3
"""Per-container update policy (v1.53.0, roadmap #2 — @NotRetarded/@famewolf).

Covers:
  - UpdateChecker._bump_level classification (major/minor/patch/None + prefixes)
  - the policy × level decision matrix (None always allowed, fail-open)
  - label-over-env precedence for _resolve_update_policy
  - integration: an auto batch caps a major bump under policy=minor (held +
    reported, not applied) while a minor bump applies; the SAME major bump on
    the MANUAL path applies (policy bypassed).

Pure logic — no Docker. Exits non-zero on any failure.
"""
import sys, os, types, threading, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot
from update_engine import UpdateEngine
from update_checker import UpdateChecker
from i18n import get_translator


def test_bump_level():
    checks = {
        "major 1.2.3 -> 2.0.0": UpdateChecker._bump_level("1.2.3", "2.0.0") == "major",
        "minor 1.2.3 -> 1.3.0": UpdateChecker._bump_level("1.2.3", "1.3.0") == "minor",
        "patch 1.2.3 -> 1.2.4": UpdateChecker._bump_level("1.2.3", "1.2.4") == "patch",
        "equal -> None": UpdateChecker._bump_level("1.2.3", "1.2.3") is None,
        "unparseable -> None": UpdateChecker._bump_level("latest", "1.2.4") is None,
        "empty old -> None": UpdateChecker._bump_level("", "1.2.4") is None,
        "empty new -> None": UpdateChecker._bump_level("1.2.3", "") is None,
        "both empty -> None": UpdateChecker._bump_level("", "") is None,
        "prefix v1.2.3 -> v1.3.0 = minor": UpdateChecker._bump_level("v1.2.3", "v1.3.0") == "minor",
        "prefix mixed v1.2.3 -> 2.0.0 = major": UpdateChecker._bump_level("v1.2.3", "2.0.0") == "major",
    }
    return checks


def test_decision_matrix():
    """policy × level -> allow?  None level always ALLOW."""
    A = TelegramBot._policy_allows_level
    expected = {
        ("all", "major"): True, ("all", "minor"): True, ("all", "patch"): True, ("all", None): True,
        ("minor", "major"): False, ("minor", "minor"): True, ("minor", "patch"): True, ("minor", None): True,
        ("patch", "major"): False, ("patch", "minor"): False, ("patch", "patch"): True, ("patch", None): True,
    }
    checks = {}
    for (pol, lvl), want in expected.items():
        got = A(pol, lvl)
        checks[f"policy={pol} level={lvl} -> {'allow' if want else 'block'}"] = (got == want)
    return checks


def _fake_bot(update_policy="all", label_policy=None):
    bot = types.SimpleNamespace()
    bot.config = types.SimpleNamespace(update_policy=update_policy)
    checker = types.SimpleNamespace()
    checker.get_container_labels = lambda name: (
        {"docksentry.policy": label_policy} if label_policy is not None else {})
    return bot, checker


def test_precedence():
    checks = {}
    # _resolve_update_policy now lives on UpdateEngine; call it there
    # directly (the bare stub only needs `config`, which the method uses).
    # label wins over env
    bot, ck = _fake_bot(update_policy="all", label_policy="minor")
    checks["label minor overrides env all"] = (
        UpdateEngine._resolve_update_policy(bot, "c", ck) == "minor")
    bot, ck = _fake_bot(update_policy="minor", label_policy="all")
    checks["label all overrides env minor"] = (
        UpdateEngine._resolve_update_policy(bot, "c", ck) == "all")
    # no label -> env
    bot, ck = _fake_bot(update_policy="patch", label_policy=None)
    checks["no label falls back to env patch"] = (
        UpdateEngine._resolve_update_policy(bot, "c", ck) == "patch")
    # invalid label -> fail-open all
    bot, ck = _fake_bot(update_policy="patch", label_policy="bogus")
    checks["invalid label -> all (fail-open)"] = (
        UpdateEngine._resolve_update_policy(bot, "c", ck) == "all")
    # invalid env -> all
    bot, ck = _fake_bot(update_policy="nonsense", label_policy=None)
    checks["invalid env -> all (fail-open)"] = (
        UpdateEngine._resolve_update_policy(bot, "c", ck) == "all")
    return checks


class _IntChecker:
    """Minimal checker stub for the integration test."""
    def __init__(self):
        self.calls = []

    # policy resolution + classification reuse these
    def get_container_labels(self, name):
        return {}

    _parse_semver = UpdateChecker._parse_semver
    _bump_level = UpdateChecker._bump_level
    _SEMVER_RE = UpdateChecker._SEMVER_RE

    def _parse_image(self, image):
        return ("r", "repo", image.rsplit(":", 1)[-1] if ":" in image else "latest")

    def netns_target_name(self, name):
        return None

    def update_container(self, name, image, **kw):
        self.calls.append(name)
        return True, "ok"


def _int_bot(update_policy):
    bot = types.SimpleNamespace()
    bot.config = types.SimpleNamespace(
        update_policy=update_policy, auto_update_all=True,
        pending_file=os.path.join(tempfile.mkdtemp(), "p.json"))
    bot.store = types.SimpleNamespace(
        get_update_windows=lambda: {},
        get_autoupdate=lambda: set(),
        get_groups=lambda: {},
        get_ask_before_major=lambda: set())
    bot._update_lock = threading.Lock()
    bot._run_queued_selfupdate = lambda: None
    bot._get_autoupdate = lambda: set()
    bot.t = get_translator("en")
    bot.notifier = None
    sent = []
    bot.send_message = lambda *a, **k: sent.append(a[0] if a else "")
    bot._sent = sent
    manual = []
    bot.notify_updates = lambda upd, **k: manual.extend(u["name"] for u in upd)
    bot._manual = manual
    bot._enrich_with_source_url = lambda u: None
    bot._display_name = lambda u: u["name"]
    bot._maybe_cooldown = lambda *a, **k: None
    # bind the real methods under test (they now live on UpdateEngine)
    bot._resolve_update_policy = lambda n, c: UpdateEngine._resolve_update_policy(bot, n, c)
    bot._policy_allows_level = UpdateEngine._policy_allows_level
    bot._policy_decision = lambda u, c: UpdateEngine._policy_decision(bot, u, c)
    bot._process_update_batch = lambda *a, **k: UpdateEngine._process_update_batch(bot, *a, **k)
    return bot


def test_integration_auto():
    checks = {}
    ck = _IntChecker()
    bot = _int_bot(update_policy="minor")
    updates = [
        {"name": "majorapp", "image": "majorapp:1.2.3", "old_version": "1.2.3", "new_version": "2.0.0"},
        {"name": "minorapp", "image": "minorapp:1.2.3", "old_version": "1.2.3", "new_version": "1.3.0"},
    ]
    TelegramBot.handle_autoupdates(bot, updates, checker=ck)
    checks["minor bump applied under policy=minor"] = ("minorapp" in ck.calls)
    checks["major bump NOT applied under policy=minor"] = ("majorapp" not in ck.calls)
    held = [m for m in bot._sent if "majorapp" in m and "policy" in m.lower()]
    checks["major bump reported as policy-held"] = (len(held) == 1)
    checks["policy-held update surfaced in updates-available"] = ("majorapp" in bot._manual)
    return checks


def test_integration_manual():
    checks = {}
    ck = _IntChecker()
    bot = _int_bot(update_policy="minor")
    # Manual path: same major bump, policy=minor still in effect. Drive the
    # shared engine directly with auto=False (what run_updates / "Update all"
    # do) — the policy gate lives only on the auto path, so this must apply.
    updates = [{"name": "majorapp", "image": "majorapp:1.2.3",
                "old_version": "1.2.3", "new_version": "2.0.0"}]
    UpdateEngine._process_update_batch(bot, updates, ck, auto=False)
    checks["major bump applied on manual path (policy bypassed)"] = ("majorapp" in ck.calls)
    return checks


def main():
    all_checks = {}
    all_checks.update({f"[bump] {k}": v for k, v in test_bump_level().items()})
    all_checks.update({f"[matrix] {k}": v for k, v in test_decision_matrix().items()})
    all_checks.update({f"[precedence] {k}": v for k, v in test_precedence().items()})
    all_checks.update({f"[auto] {k}": v for k, v in test_integration_auto().items()})
    all_checks.update({f"[manual] {k}": v for k, v in test_integration_manual().items()})

    for k, v in all_checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(all_checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
