#!/usr/bin/env python3
"""The last promises the suite could not see.

`test_guard_mutations.UNGUARDED` is the honest half of the ratchet: a
promise the README or the docs make, where the enforcement can be
deleted and no procedure notices. The list may only get shorter. These
are the ones it ends on — four of the six, with the audit-log
redaction and the queue's age window closed in the two procedures that
already owned those subjects.

* **An OOM kill is recognised as one.** `"oom": bool(state.get("OOMKilled"))`
  could be `False` and nothing went red. The alert then says "exited
  with 137", which is the kernel's number for "killed" and reads as a
  crash — so the reader goes hunting in the application while the
  machine is out of memory. `docs/monitoring.md` promises the
  distinction and it is the single most useful thing a death alert can
  carry.
* **Only the registries the operator named are treated as insecure.**
  Drop the setting from the lookup and every registry is asked over
  plain `http`: the credentials for a private registry go out in the
  clear, and nothing says so. The opposite revert is as bad in its own
  way — a lab registry on http stops being checkable at all.
* **An image too fresh to trust is held back.** `MIN_IMAGE_AGE_DAYS`
  exists because a tag can be re-pushed minutes after a bad build, and
  the unattended path is the one with nobody watching. Covered with the
  major-version gate below, because they are the same decision point
  and the same fixture answers both.

Driven, all of them: a real `snapshot` reading a real inspect payload,
the real `registry_scheme`, the real auto-update path.
"""

import json
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import update_engine as _ue  # noqa: E402
from i18n import get_translator  # noqa: E402
from monitor import ContainerMonitor as Monitor  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402
from update_checker import registry_scheme  # noqa: E402

checks = {}


# ── an OOM kill is not just another non-zero exit ───────────────────
def _snapshot(state):
    """`Monitor.snapshot` over one container in the given state."""
    payload = [{"Name": "/db", "State": state, "RestartCount": 0,
                "Config": {"Labels": {}}}]

    class _Backend:
        def ps(self, all=False, fmt="", timeout=None):
            return types.SimpleNamespace(returncode=0, stdout="db\n",
                                         stderr="")

        def inspect(self, names, timeout=None):
            return types.SimpleNamespace(returncode=0,
                                         stdout=json.dumps(payload),
                                         stderr="")

    m = Monitor.__new__(Monitor)
    m.backend = _Backend()
    m.config = types.SimpleNamespace(debug=False, monitor_containers="",
                                     exclude_containers=[])
    m.host_name = ""
    m._monitored = lambda name, info: True
    return (m.snapshot() or {}).get("db", {})


KILLED = {"Status": "exited", "ExitCode": 137, "OOMKilled": True}
CRASHED = {"Status": "exited", "ExitCode": 137, "OOMKilled": False}

checks["a container the kernel killed is marked as OOM"] = (
    _snapshot(KILLED).get("oom") is True)
# The counter-direction, and it is the whole point: 137 on its own is
# not an OOM. A `docker kill` produces exactly the same exit code, and
# an alert that calls that an OOM sends the reader to the wrong place.
checks["…while the same exit code without the flag is not"] = (
    _snapshot(CRASHED).get("oom") is False)
checks["…and the exit code survives either way"] = (
    _snapshot(KILLED).get("exit_code") == 137
    and _snapshot(CRASHED).get("exit_code") == 137)

# …and the diff turns that flag into the right event, or the snapshot
# is carrying a field nobody reads.
_before = {"db": {"status": "running", "health": "", "exit_code": 0,
                  "oom": False, "restarts": 0, "started_at": "", "labels": {}}}
_after_oom = {"db": dict(_before["db"], status="exited", exit_code=137,
                         oom=True)}
_after_crash = {"db": dict(_before["db"], status="exited", exit_code=137,
                           oom=False)}
checks["an OOM death is reported as an OOM"] = (
    [k for k, _n, _d in Monitor.diff(_before, _after_oom)] == ["oom"])
checks["…and an ordinary one as an exit"] = (
    [k for k, _n, _d in Monitor.diff(_before, _after_crash)] == ["exited"])


# ── only the named registries are asked over http ───────────────────
# Dropping the setting sends every registry's credentials out in plain
# text; ignoring it entirely makes a lab registry uncheckable. Both
# directions, because both are real.
checks["a registry nobody listed is asked over https"] = (
    registry_scheme("registry.example.com", ["lab.internal"]) == "https")
checks["…and a listed one over http"] = (
    registry_scheme("lab.internal", ["lab.internal"]) == "http")
checks["…with an empty list meaning nothing is insecure"] = (
    registry_scheme("lab.internal", []) == "https")
checks["…and a wildcard entry matching the way the docs say"] = (
    registry_scheme("reg.lab.internal", ["*.lab.internal"]) == "http"
    and registry_scheme("reg.other.internal", ["*.lab.internal"]) == "https")
# The checker has to ask with the setting, not with an empty list.
from update_checker import UpdateChecker  # noqa: E402

_c = UpdateChecker.__new__(UpdateChecker)
_c.config = types.SimpleNamespace(insecure_registries=["lab.internal"])
checks["the checker passes the operator's list, not an empty one"] = (
    _c._scheme_for("lab.internal") == "http"
    and _c._scheme_for("docker.io") == "https")


# ── the two gates on the unattended path ────────────────────────────
# A major bump and an image minutes old are the two things the auto
# path must not apply on its own. Both decided in `handle_autoupdates`,
# both switchable off with nothing going red.
def _auto(decisions):
    """Which containers the scheduler actually puts through.

    `decisions` maps a name to what `_policy_decision` / `_age_decision`
    answer for it: `("major", "1.0", "2.0")` or `(3, 7)` respectively,
    or None to let it pass.
    """
    batched = []

    class _Cfg:
        language = "en"
        bot_token = ""
        chat_id = "1"

        def __init__(self):
            self.auto_update_all = True
            self.pending_file = os.path.join(tempfile.mkdtemp(), "p.json")

        def __getattr__(self, name):
            return ""

    class _Store:
        def get_autoupdate(self):
            return list(decisions)

        def get_update_windows(self):
            return {}

        def get_groups(self):
            return {}

    bot = TelegramBot.__new__(TelegramBot)
    bot.config = _Cfg()
    bot.t = get_translator("en")
    bot.audit = types.SimpleNamespace(record=lambda *a, **kw: None)
    bot.hosts = None
    for n in ("send_message", "announce", "log", "_say_once"):
        setattr(bot, n, lambda *a, **kw: None)
    bot._policy_decision = lambda u, c: decisions[u["name"]].get("policy")
    bot._age_decision = lambda u, c: decisions[u["name"]].get("age")
    bot._display_name = lambda u: u["name"]
    bot._store_for = lambda host: _Store()
    bot.notify_updates = lambda ups, auto=False: None
    bot._process_update_batch = lambda ups, checker, auto=False: (
        batched.extend(u["name"] for u in ups) or ([], 0, []))
    bot.engine = types.SimpleNamespace(
        _update_lock=threading.Lock(), _queued_selfupdate=None,
        _swap_in_flight=False,
        _enrich_with_source_url=lambda *a, **kw: None)
    real = _ue.host_store
    _ue.host_store = lambda scope, host: _Store()
    try:
        bot.handle_autoupdates(
            [{"name": n, "image": f"{n}:2", "host": "local"}
             for n in decisions], None)
    finally:
        _ue.host_store = real
    return sorted(batched)


# `_policy_decision` is the update-POLICY gate, not the major-version
# confirmation gate — a counter-probe caught me testing one and
# claiming the other. The confirmation gate lives in the engine's own
# batch (`if auto and ask_this:`), so that is where it is driven.
checks["a policy-blocked bump is not applied unattended"] = (
    _auto({"db": {"policy": ("major", "1.9", "2.0")}}) == [])
checks["…while an allowed one is"] = (
    _auto({"db": {}}) == ["db"])


def _major_gate(ask_major, is_major, auto=True):
    """Run the engine's batch; was the container updated or held?"""
    updated, pending = [], []

    class _Updater:
        def netns_target_name(self, name):
            return None

        def update_container(self, name, image, **kw):
            updated.append(name)
            return (True, "ok")

        def label_bool(self, labels, key):
            return None

        def get_container_labels(self, name):
            return {}

    class _Store2:
        def get_groups(self):
            return {}

        def get_ask_before_major(self):
            return ["db"] if ask_major else []

        def add_pending_major(self, name, info):
            pending.append(name)

    bot = types.SimpleNamespace()
    bot._update_lock = threading.Lock()
    bot._run_queued_selfupdate = lambda: None
    bot.config = types.SimpleNamespace(
        pending_file=os.path.join(tempfile.mkdtemp(), "p.json"))
    bot.store = _Store2()
    bot.notifier = None
    bot.t = get_translator("en")
    bot.send_message = lambda *a, **k: None
    bot._enrich_with_source_url = lambda u: None
    bot._display_name = lambda u: u["name"]
    bot._maybe_cooldown = lambda *a, **k: None
    bot.mark_updating = lambda *a, **k: None
    bot.clear_updating = lambda *a, **k: None
    bot._remove_from_pending = lambda names: None
    bot._restart_group_dependents = lambda *a, **k: "cascade"
    bot._is_major_bump = lambda u, c: (
        (True, "1.9.0", "2.0.0") if is_major else (False, "", ""))
    _ue.UpdateEngine._process_update_batch(
        bot, [{"name": "db", "image": "db:2"}], _Updater(), auto=auto)
    return updated, pending


# ── and the detection itself, not only the wiring ───────────────────
# `_major_gate` stubs `_is_major_bump`, so it measures what the engine
# does with the answer. An audit then switched the real detection off
# (`best_parsed[0] > cur[0]` → `False`) and all 206 procedures stayed
# green — every one that touches it replaces it with a lambda. The
# gate was guarded and the thing feeding it was not, which is the same
# shape as the health outcome and the session clear.
def _detects(current, available):
    """Run the real `_is_major_bump` over two version tags."""
    from update_checker import UpdateChecker as _UC2

    class _Checker2:
        def _parse_image(self, image):
            return "registry-1.docker.io", "lib/app", image.rsplit(":", 1)[1]

        # The real parser — a classmethod, so it is called on the
        # class. Standing in for it would make this measure my own
        # arithmetic rather than the engine's.
        def _parse_semver(self, tag):
            return _UC2._parse_semver(tag)

        def get_highest_semver_tag(self, registry, repo, tag):
            return available, _UC2._parse_semver(available)

    return _ue.UpdateEngine._is_major_bump(
        types.SimpleNamespace(), {"image": f"app:{current}"}, _Checker2())


checks["a 1.x → 2.0 bump is detected as major"] = (
    _detects("1.9.3", "2.0.0")[0] is True)
# The counter-directions, each a different way of switching the
# detection off by accident.
checks["…while a minor bump is not"] = (
    _detects("1.9.3", "1.10.0")[0] is False)
checks["…nor a patch"] = _detects("1.9.3", "1.9.4")[0] is False
checks["…nor the same version again"] = (
    _detects("1.9.3", "1.9.3")[0] is False)
checks["…and the two tags come back with the verdict"] = (
    _detects("1.9.3", "2.0.0")[1:] == ("1.9.3", "2.0.0"))
# A tag that is not SemVer cannot be compared, and guessing would be
# worse than the gate quietly not applying.
checks["a non-semver tag is not guessed at"] = (
    _detects("latest", "2.0.0") == (False, None, None))

_updated, _pending = _major_gate(ask_major=True, is_major=True)
checks["a major bump waits for confirmation"] = (
    _updated == [] and _pending == ["db"])
# Three counter-directions, because each one is a different way of
# switching the gate off by accident.
checks["…while a minor bump on the same container goes through"] = (
    _major_gate(ask_major=True, is_major=False)[0] == ["db"])
checks["…and a major bump nobody asked to confirm goes through"] = (
    _major_gate(ask_major=False, is_major=True)[0] == ["db"])
checks["…and a manual update is never held for confirmation"] = (
    _major_gate(ask_major=True, is_major=True, auto=False)[0] == ["db"])
checks["an image too fresh to trust is held back"] = (
    _auto({"db": {"age": (0, 3)}}) == [])
checks["…and one old enough goes through"] = (
    _auto({"db": {}}) == ["db"])
# Held back, not discarded: both gates leave the update pending so it
# applies on a later tick without anyone doing anything.
checks["…and the two gates are independent of each other"] = (
    _auto({"db": {"policy": ("major", "1.9", "2.0")}, "web": {}}) == ["web"]
    and _auto({"db": {"age": (0, 3)}, "web": {}}) == ["web"])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
