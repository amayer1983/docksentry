#!/usr/bin/env python3
"""stop / start / restart is one implementation, not one per chat.

Both bots carried the same three refusals and the same two CLI calls,
and they had worded one of them differently: a stop refused during an
update said "⏳ Updates in progress — stop refused so it can't interfere
with a running update. Try again once they finish." in Telegram and "An
update is running — `stop` is refused until it finishes." in Discord.
Same event, same second, two answers depending on which app you had
open. Telegram's kept: it says what to do next.

Globs came along with the move. `/stop web*` worked in Telegram and not
in Discord, purely because the matching sat in one front end.

What this pins:
  * the three refusals, in order, each returning its own key
  * the guards apply to every match of a glob, not just to single names
  * a glob answers once, not once per host, when nothing matches
  * neither front end kept a second copy of any of it
"""
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import lifecycle  # noqa: E402

checks = {}


class Checker:
    def __init__(self, *, self_name="docksentry", stop_ok=True,
                 labels=None, boom=False):
        self.self_name = self_name
        self.stop_ok = stop_ok
        self.labels = labels or {}
        self.boom = boom
        self.stopped = []

    def _would_kill_self(self, name):
        return name == self.self_name

    def _stop_container(self, name):
        if not self.stop_ok:
            return False, "daemon said no"
        self.stopped.append(name)
        return True, ""

    def get_container_labels(self, name):
        if self.boom:
            raise RuntimeError("inspect failed")
        return self.labels.get(name, {})

    def label_bool(self, labels, key):
        return labels.get(key)


class Store:
    def __init__(self, protected=()):
        self._p = set(protected)

    def is_protect_stop(self, name):
        return name in self._p


class Backend:
    def __init__(self, names=("web", "web-db", "plex", "docksentry"),
                 rc=0, stderr=""):
        self.names = list(names)
        self.rc = rc
        self.stderr = stderr
        self.calls = []

    def run(self, argv, timeout=None):
        self.calls.append(argv)
        if argv[:1] == ["ps"]:
            return types.SimpleNamespace(
                returncode=0, stdout="\n".join(self.names + ["web_old"]),
                stderr="")
        return types.SimpleNamespace(returncode=self.rc, stdout="",
                                     stderr=self.stderr)


def run(action, partial, *, checker=None, store=None, backend=None,
        busy=False, targets=(None,)):
    checker = checker or Checker()
    store = store or Store()
    backend = backend or Backend()
    return lifecycle.act(action, list(targets),
                         backend_for=lambda h: backend,
                         checker_for=lambda h: checker,
                         store_for=lambda h: store,
                         partial=partial, update_running=busy), backend, checker


# ── the happy paths ──────────────────────────────────────────────────
o, be, ck = run("stop", "plex")
checks["stop answers with the stopped key"] = (
    o.replies[0].key == "lifecycle_stopped"
    and o.replies[0].params["name"] == "plex")
checks["…and actually stopped it"] = ck.stopped == ["plex"]
checks["…and counts as a change"] = o.changed

o, be, _ = run("start", "plex")
checks["start shells out to `start`"] = ["start", "plex"] in be.calls
checks["…and says started"] = o.replies[0].key == "lifecycle_started"

o, be, _ = run("restart", "plex")
checks["restart is graceful, with a generous timeout"] = (
    ["restart", "--time", "30", "plex"] in be.calls)
checks["…and says restarted"] = o.replies[0].key == "lifecycle_restarted"

o, _, _ = run("stop", "pl")
checks["a partial name resolves"] = o.replies[0].params["name"] == "plex"

# …and an ambiguous one refuses rather than guessing: `we` is both `web`
# and `web-db`, and picking one of them silently is how you stop the
# wrong container.
o, _, ck = run("stop", "we")
checks["an ambiguous partial refuses instead of guessing"] = (
    o.fatal is not None and o.fatal.key == "resolve_multiple"
    and ck.stopped == [])

# ── the three refusals, in order ─────────────────────────────────────
o, _, ck = run("stop", "docksentry")
checks["refusal 1: it will not stop itself"] = (
    o.replies[0].key == "lifecycle_refused_self" and not o.replies[0].ok)
checks["…and did not stop anything"] = ck.stopped == []

o, _, ck = run("restart", "docksentry")
checks["refusal 1 covers restart too"] = (
    o.replies[0].key == "lifecycle_refused_self")

o, _, _ = run("start", "docksentry")
checks["…but not start — that cannot kill PID 1"] = (
    o.replies[0].key == "lifecycle_started")

o, _, ck = run("stop", "plex", busy=True)
checks["refusal 2: nothing runs while an update does"] = (
    o.replies[0].key == "lifecycle_busy" and ck.stopped == [])
checks["…and it is the wording that explains what to do next"] = (
    o.replies[0].params.get("action") == "stop")

o, _, ck = run("stop", "plex", store=Store(protected=["plex"]))
checks["refusal 3: a protected container is not stopped"] = (
    o.replies[0].key == "lifecycle_refused_protected" and ck.stopped == [])

o, _, _ = run("restart", "plex", store=Store(protected=["plex"]))
checks["…but restart stays allowed on it"] = (
    o.replies[0].key == "lifecycle_restarted")

# Self before busy: being told "that's me, use /selfupdate" is the more
# useful answer than "try again later", because later will not help.
o, _, _ = run("stop", "docksentry", busy=True)
checks["the self refusal is checked before the busy one"] = (
    o.replies[0].key == "lifecycle_refused_self")

# ── the protect label beats the toggle, and a flaky inspect does not ──
ck = Checker(labels={"plex": {"protect": True}})
checks["a docksentry.protect label protects on its own"] = (
    lifecycle.is_protected("plex", ck, Store()) is True)
ck = Checker(labels={"plex": {"protect": False}})
checks["…and an explicit false label lifts the toggle"] = (
    lifecycle.is_protected("plex", ck, Store(protected=["plex"])) is False)
checks["a failing inspect falls back to the toggle, not to unprotected"] = (
    lifecycle.is_protected("plex", Checker(boom=True),
                           Store(protected=["plex"])) is True)

# ── failures are reported, not swallowed ─────────────────────────────
o, _, _ = run("stop", "plex", checker=Checker(stop_ok=False))
checks["a failed stop says so"] = (
    o.replies[0].key == "lifecycle_stop_failed" and not o.replies[0].ok)
checks["…and carries the reason"] = "daemon said no" in o.replies[0].params["error"]
checks["…and is not counted as a change"] = not o.changed

o, _, _ = run("start", "plex", backend=Backend(rc=1, stderr="no such image"))
checks["a failed start carries the daemon's own words"] = (
    o.replies[0].key == "lifecycle_start_failed"
    and "no such image" in o.replies[0].params["error"])

o, _, _ = run("stop", "nosuch")
checks["an unknown container is fatal, not a per-host error"] = (
    o.fatal is not None and o.fatal.key == "resolve_not_found")

o, _, _ = run("stop", "")
checks["no argument gets the usage line"] = (
    o.fatal is not None and o.fatal.key == "lifecycle_usage")

# ── globs ────────────────────────────────────────────────────────────
o, _, ck = run("stop", "web*")
keys = [r.key for r in o.replies]
checks["a glob leads with a header"] = keys[0] == "glob_action_header"
checks["…that counts the matches"] = o.replies[0].params["count"] == 2
checks["…and acts on each one"] = sorted(ck.stopped) == ["web", "web-db"]
checks["…rendered as one message, not one per container"] = o.grouped

# Our own rollback leftovers are not containers anyone means.
be = Backend(names=["web"])
checks["`_old` leftovers never match a glob"] = (
    lifecycle.match_glob("web*", backend=be) == ["web"])

# The guards are not skipped for being part of a batch — this is the one
# that would hurt: `/stop *` must not take Docksentry down with it.
o, _, ck = run("stop", "*")
checks["a glob does not exempt the self guard"] = (
    "docksentry" not in ck.stopped
    and "lifecycle_refused_self" in [r.key for r in o.replies])

o, _, ck = run("stop", "*", store=Store(protected=["plex"]))
checks["…nor the protect guard"] = (
    "plex" not in ck.stopped
    and "lifecycle_refused_protected" in [r.key for r in o.replies])

o, _, _ = run("stop", "nothing*")
checks["a glob matching nothing says so once"] = (
    o.fatal is not None and o.fatal.key == "glob_no_match")

checks["a plain name is not treated as a pattern"] = (
    not lifecycle.is_glob("web") and lifecycle.is_glob("web*")
    and lifecycle.is_glob("web?") and lifecycle.is_glob("web[12]"))

# ── multi-host ───────────────────────────────────────────────────────
class Host:
    def __init__(self, name, local=False):
        self.name = name
        self.is_local = local

backends = {"a": Backend(names=["web"]), "b": Backend(names=["plex"])}
checkers = {"a": Checker(), "b": Checker()}
hosts = [Host("a", local=True), Host("b")]
o = lifecycle.act("stop", hosts,
                  backend_for=lambda h: backends[h.name],
                  checker_for=lambda h: checkers[h.name],
                  store_for=lambda h: Store(), partial="plex")
checks["a container living on one host is found there"] = (
    len(o.replies) == 1 and o.replies[0].host.name == "b")
checks["…and the host that does not have it stays quiet"] = (
    checkers["a"].stopped == [] and checkers["b"].stopped == ["plex"])

o = lifecycle.act("stop", hosts,
                  backend_for=lambda h: backends[h.name],
                  checker_for=lambda h: checkers[h.name],
                  store_for=lambda h: Store(), partial="nowhere")
checks["a container on no host is one error, not one per host"] = (
    o.fatal is not None and not o.replies)

# ── and neither front end kept a copy ────────────────────────────────
tb = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
db = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
checks["Telegram has no _lifecycle_action of its own"] = (
    "def _lifecycle_action" not in tb)
checks["Discord has no _lifecycle_action of its own"] = (
    "def _lifecycle_action" not in db)
checks["Telegram's lifecycle branch calls the core"] = (
    "lifecycle.act(" in tb)
checks["Discord's lifecycle path calls the core"] = ("lifecycle.act(" in db)
checks["the status button calls it too"] = tb.count("lifecycle.act(") >= 2
# Telegram still asks the question (to hide a Stop button that would be
# refused); Discord asks it through `lifecycle.plan`. Neither has a
# second implementation of the rule.
checks["neither reimplements the protect rule"] = (
    "lifecycle.is_protected(" in tb
    and "def _is_protected" not in db
    and "label_bool(" not in db)
checks["neither reimplements glob matching"] = (
    "fnmatch" not in tb and "fnmatch" not in db)

# The losing wording is gone from every language, not just from en.
import glob as _glob
import json
langs = sorted(_glob.glob(os.path.join(APP, "lang", "*.json")))
checks["all 16 languages are checked, not just en"] = len(langs) == 16
leftovers = [os.path.basename(f) for f in langs
             if "lifecycle_refused_busy" in json.load(open(f, encoding="utf-8"))]
checks["the duplicate busy wording is gone everywhere"] = leftovers == []
missing = [os.path.basename(f) for f in langs
           if "lifecycle_busy" not in json.load(open(f, encoding="utf-8"))]
checks["…and the surviving one exists everywhere"] = missing == []

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
