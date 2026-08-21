#!/usr/bin/env python3
"""Going down on purpose lives in the core, not on a front end (#63).

Fourth step of the core extraction. `selfrestart.policy` / `record_request`
/ `go_down` were `TelegramBot._restart_policy` and `restart_self`, and
Discord borrowed both — where they never worked at all.

`_restart_policy(checker=None)` fell back to `self.checker`. The Telegram
bot has no such attribute, Discord called it with no argument, so the
answer was always ("", "I cannot tell which container I am.") and every
bare /restart on Discord refused — then advised adding a restart policy
the container almost certainly already had. Measured before the fix, on
a bot whose daemon reports `unless-stopped`.

That one is worth naming precisely because the borrow guard could not
catch it: `_restart_policy` EXISTS on TelegramBot and is callable, so a
scan for missing names sees nothing wrong. What was missing was the state
the borrowed method needed. A borrowed method is not just a name — it is
a name plus the instance it grew on, and only the extraction fixes that.
"""
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}

import selfrestart  # noqa: E402

for fn in ("policy", "record_request", "go_down"):
    checks[f"selfrestart.{fn} exists"] = callable(
        getattr(selfrestart, fn, None))

# ── the checker is required, not quietly defaulted ───────────────────
# The default WAS the bug: it hid that nobody had one to give.
import inspect  # noqa: E402
_params = inspect.signature(selfrestart.policy).parameters
checks["policy() demands a checker rather than defaulting"] = (
    "checker" in _params
    and _params["checker"].default is inspect.Parameter.empty
    and _params["backend"].default is inspect.Parameter.empty)

# ── the front ends own the words, the core owns the mechanism ────────
tsrc = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
checks["telegram_bot no longer defines the policy check"] = (
    "def _restart_policy" not in tsrc)
i = tsrc.index("    def restart_self(self")
tg = tsrc[i:tsrc.index("\n    def ", i + 10)]
checks["…and its restart_self is the adapter onto the core"] = (
    "selfrestart.policy(" in tg and "selfrestart.go_down()" in tg
    and "SIGTERM" not in tg)

dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
i = dsrc.index("    def _cmd_restart_self(self):")
dc = dsrc[i:dsrc.index("\n    def ", i + 10)]
dc_code = dc.split('"""')[2] if dc.count('"""') >= 2 else dc
checks["Discord asks the core, with its own checker"] = (
    "selfrestart.policy(" in dc_code and "self.checker" in dc_code)
checks["…and stops borrowing the restart from the Telegram bot"] = (
    "bot._restart_policy" not in dc_code and "bot.restart_self" not in dc_code
    and "self.telegram" not in dc_code)

# ── the bug itself, pinned ───────────────────────────────────────────
class Checker:
    def __init__(self, name="docksentry"):
        self._name = name
    def _own_container_name(self):
        return self._name

class Daemon:
    """Answers `unless-stopped` — a container that WILL come back."""
    def __init__(self, out="unless-stopped\n", rc=0):
        self.out, self.rc = out, rc
    def run(self, args, timeout=None):
        return types.SimpleNamespace(returncode=self.rc, stdout=self.out,
                                     stderr="")

name, why = selfrestart.policy(Daemon(), Checker())
checks["a container that will come back is allowed to restart"] = (
    name == "unless-stopped" and why == "")

# What Discord got before the extraction: the same daemon, the same
# container — and a refusal, because the checker went missing on the way.
name_no_checker, why_no_checker = selfrestart.policy(Daemon(), None)
checks["…and without a checker it refuses, visibly"] = (
    name_no_checker == "" and "which container I am" in why_no_checker)

for policy_name in ("no", "none", "<no value>"):
    n, w = selfrestart.policy(Daemon(out=policy_name + "\n"), Checker())
    checks[f"a policy of {policy_name!r} means we stay up"] = (
        n == "" and policy_name in w)

n, w = selfrestart.policy(Daemon(rc=1), Checker())
checks["a daemon that will not answer counts as no"] = n == "" and bool(w)

class Boom:
    def run(self, args, timeout=None):
        raise RuntimeError("socket is gone")
n, w = selfrestart.policy(Boom(), Checker())
checks["…and so does a daemon that is not there"] = (
    n == "" and "would not answer" in w)

# ── the marker the next boot reads ───────────────────────────────────
marker = "/tmp/ds-selfrestart-decoupled.json"
if os.path.exists(marker):
    os.unlink(marker)
cfg = types.SimpleNamespace(restart_request_file=marker)
ok = selfrestart.record_request(cfg, by="discord")
checks["the request is recorded for the next boot"] = ok and os.path.exists(marker)
import json  # noqa: E402
rec = json.load(open(marker))
checks["…with a timestamp the staleness rule can read"] = (
    isinstance(rec.get("ts"), (int, float)) and rec["ts"] > 0)
checks["…and it names which front end asked"] = rec.get("by") == "discord"
os.unlink(marker)

checks["an unwritable marker does not stop the restart"] = (
    selfrestart.record_request(
        types.SimpleNamespace(restart_request_file="/nope/nope.json")) is False)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
