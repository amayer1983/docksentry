#!/usr/bin/env python3
"""Picking up after Docksentry itself was killed (#2, @NotRetarded).

His Docksentry exited 137 during an update, and he found out from a
third-party monitor rather than from us. Two separate holes behind that.

**The update was left mid-swap.** A recreate goes stop → rename to
`<name>_old` → build the run arguments → run. The rollback that guards
every other failure lives in an `except` handler, and a SIGKILL raises
nothing — the process is simply gone. The container stays stopped under a
backup name, and until now nothing ever looked for it. A service could be
down indefinitely with no notification at all.

Recovery reads a journal written *before* the rename, not the `_old`
suffix. Someone may legitimately run a container called `foo_old`, and
renaming theirs would be a worse bug than the one being fixed. Verified
against real containers: stopped and renamed, then restored and started.

**The hard kill itself was never reported.** The exit marker is written
only on SIGTERM/SIGINT. The old code read an absent marker as "first boot
or unclean kill, can't prove which" and said nothing — correct when
written, obsolete since v2.0.0, because every successful start now records
its version. A state file with no exit marker beside it is a hard kill.
"""

import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from recovery import (recover_interrupted_update, previous_run_died,
                      INFLIGHT_TRUST_SECONDS)


class FakeBackend:
    """Just enough of a backend: a name list and a rename that moves it."""

    def __init__(self, names, rename_ok=True):
        self.names = list(names)
        self.rename_ok = rename_ok
        self.started = []

    def ps(self, **kw):
        return types.SimpleNamespace(returncode=0, stdout="\n".join(self.names))

    def rename(self, old, new, **kw):
        if not self.rename_ok:
            return types.SimpleNamespace(returncode=1, stderr="no")
        self.names = [new if n == old else n for n in self.names]
        return types.SimpleNamespace(returncode=0, stderr="")

    def run(self, args, **kw):
        if args and args[0] == "start":
            self.started.append(args[1])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _cfg(rec=None, age=0):
    d = tempfile.mkdtemp()
    cfg = types.SimpleNamespace(
        inflight_file=os.path.join(d, "update_inflight.json"),
        version_state_file=os.path.join(d, "version_state.json"),
        last_exit_file=os.path.join(d, "last_exit.json"))
    if rec is not None:
        rec.setdefault("ts", time.time() - age)
        json.dump(rec, open(cfg.inflight_file, "w"))
    return cfg


def main():
    checks = {}
    REC = {"name": "web", "old_name": "web_old", "image": "nginx:1"}

    # ── the case that cost a service its uptime ──────────────────
    cfg = _cfg(dict(REC))
    b = FakeBackend(["web_old", "db"])
    msg = recover_interrupted_update(cfg, b)
    checks["a mid-swap container is restored"] = msg == "recovery_restored"
    checks["…renamed back to its own name"] = "web" in b.names and "web_old" not in b.names
    checks["…and started, not just renamed"] = b.started == ["web"]
    checks["the journal is cleared"] = not os.path.exists(cfg.inflight_file)

    # ── the swap actually completed ──────────────────────────────
    # We died after the rename landed, or someone fixed it by hand. The
    # leftover backup then belongs to the cleanup grace period, not here —
    # touching it would remove a container the user is running.
    cfg = _cfg(dict(REC))
    b = FakeBackend(["web", "web_old"])
    checks["a live container is left alone"] = (
        recover_interrupted_update(cfg, b) == "")
    checks["…and nothing was renamed"] = b.names == ["web", "web_old"]
    checks["…and nothing was started"] = b.started == []

    # ── nothing to restore ───────────────────────────────────────
    cfg = _cfg(dict(REC))
    b = FakeBackend(["db"])
    checks["both names gone is reported, not invented"] = (
        recover_interrupted_update(cfg, b) == "recovery_gone")
    checks["…without conjuring a container"] = b.started == []

    # ── too old to act on ────────────────────────────────────────
    # A day-old note describes a world the operator has had time to change.
    # Reported, never acted on.
    cfg = _cfg(dict(REC), age=INFLIGHT_TRUST_SECONDS + 60)
    b = FakeBackend(["web_old"])
    checks["a stale journal is reported only"] = (
        recover_interrupted_update(cfg, b) == "recovery_stale")
    checks["…and moves nothing"] = b.names == ["web_old"] and b.started == []

    # ── the rename itself fails ──────────────────────────────────
    cfg = _cfg(dict(REC))
    b = FakeBackend(["web_old"], rename_ok=False)
    checks["a failed rename asks for a hand"] = (
        recover_interrupted_update(cfg, b) == "recovery_failed")
    checks["…and does not claim to have started it"] = b.started == []

    # ── nothing in flight ────────────────────────────────────────
    cfg = _cfg()
    b = FakeBackend(["web"])
    checks["no journal, no message"] = recover_interrupted_update(cfg, b) == ""
    # A backend that cannot be read must not stop Docksentry starting.
    cfg = _cfg(dict(REC))

    class Broken:
        def ps(self, **kw):
            return types.SimpleNamespace(returncode=1, stdout="")

    # The reason is appended on this path — it is the one message where a
    # technical detail earns its place, because somebody has to go and look.
    out = recover_interrupted_update(cfg, Broken())
    checks["an unreadable daemon is survived"] = out.startswith("recovery_failed")
    checks["…and says what went wrong"] = "could not list" in out

    # ── the hard-kill discriminator ──────────────────────────────
    # This is the whole reason it can be reported at all now.
    cfg = _cfg()
    open(cfg.version_state_file, "w").write("{}")
    checks["state file and no exit marker = hard kill"] = previous_run_died(cfg)
    open(cfg.last_exit_file, "w").write("{}")
    checks["a clean shutdown is not a hard kill"] = not previous_run_died(cfg)
    cfg2 = _cfg()
    checks["a first-ever boot is not a hard kill"] = not previous_run_died(cfg2)

    # ── every language says it ───────────────────────────────────
    from i18n import available_languages, get_translator
    bad = []
    for lang in available_languages():
        t = get_translator(lang)
        for key in ("startup_hard_kill", "recovery_restored", "recovery_stale",
                    "recovery_failed", "recovery_gone"):
            out = t(key, name="web", old="web_old")
            if out == key or "{" in out:
                bad.append(f"{lang}/{key}")
    checks["every language renders the messages"] = not bad
    if bad:
        print(f"    missing: {', '.join(bad[:6])}")

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
