#!/usr/bin/env python3
"""Every batch entry runs on the host it belongs to (#2, @famewolf).

His first real remote update ended with dockmox's disk full. The chain,
reproduced end to end against a dind "remote" before this fix shipped:

The update batch takes ONE checker for the whole list. The scheduled
path passes the right one per host — but the manual paths ("Update
all", /updates) hand over the LOCAL checker with a MIXED list. For an
@dock8520 entry that meant:

  * the remote-compose guard read `backend.name`, saw "local", stood
    down — it guards the right thing, but it was never asked;
  * `docker compose` then ran dockmox's copy of the compose file (his
    machines are copies of each other, so the path existed) against the
    LOCAL daemon;
  * dock8520's 2.4 GB CUDA images were pulled onto dockmox until the
    disk was full. His local /cleanup afterwards reclaimed 14 GB of
    images that were never meant to be there.

Reproduced verbatim — the pre-fix run produced his exact message,
`❌ … @dock8520: Compose pull failed: no such service:
llama-server-small` — and the post-fix run recreated the container on
the remote host with nothing appearing locally.

Also here: "✅ Image pulled. Container inspect failed." is now a ❌.
A pull is half the job; a success mark on a container that still runs
the old version is how "pulled" gets read as "updated".
"""

import os
import sys
import types

import srcslice

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_engine import UpdateEngine  # noqa: E402

checks = {}


def make_engine(hosts):
    e = UpdateEngine.__new__(UpdateEngine)
    import threading
    e._update_lock = threading.Lock()
    e._update_queue = []
    e._update_queue_lock = threading.Lock()
    e._queued_selfupdate = None
    e._updating = {}
    e._swap_in_flight = False
    e.notifier = None
    e.hosts = hosts
    e.config = types.SimpleNamespace(pending_file="/nonexistent",
                                     auto_update_all=False)
    e.store = types.SimpleNamespace(
        get_groups=lambda: {}, get_cooldown=lambda n: 0,
        get_pinned=lambda: [], get_autoupdate=lambda: [],
        get_ask_before_major=lambda: [],
        add_pending_major=lambda *a, **k: None)
    e._enrich_with_source_url = lambda updates: None
    e._display_name = lambda u: u["name"]
    e._is_major_bump = lambda u, c: (False, "", "")
    return e


class FakeChecker:
    def __init__(self, label):
        self.label = label
        self.updated = []
        self.backend = types.SimpleNamespace(name=label)

    def update_container(self, name, image, **kw):
        self.updated.append(name)
        return True, f"OK on {self.label}"

    def netns_target_name(self, name):
        return None

    def image_version_label(self, image):
        return ""


class Host:
    def __init__(self, name, checker, local=False):
        self.name = name
        self.checker = checker
        self.is_local = local


class Registry(list):
    is_multi = True

    def get(self, name):
        for h in self:
            if h.name == name:
                return h
        return None


local = FakeChecker("local")
remote = FakeChecker("dock8520")
reg = Registry([Host("local", local, local=True), Host("dock8520", remote)])
engine = make_engine(reg)

updates = [
    {"name": "web", "image": "nginx:1", "host": "local"},
    {"name": "llama-server", "image": "cuda:1", "host": "dock8520"},
    {"name": "old-entry-no-host-key", "image": "img:1"},
]
results, ok, majors = engine._process_update_batch(updates, local, auto=False)

checks["a local entry runs on the local checker"] = "web" in local.updated
checks["a remote entry runs on ITS host's checker"] = (
    "llama-server" in remote.updated)
checks["…and never on the local one"] = "llama-server" not in local.updated
checks["an entry without a host key stays local (pre-#7 files)"] = (
    "old-entry-no-host-key" in local.updated)
checks["every entry reports"] = len(results) == 3

# A host the registry no longer knows: refuse, never act locally.
gone = [{"name": "ghost", "image": "img:1", "host": "vanished-host"}]
results2, _, _ = engine._process_update_batch(gone, local, auto=False)
checks["an unknown host is refused, not run locally"] = (
    "ghost" not in local.updated)
checks["…and the refusal says which host and why"] = (
    "vanished-host" in results2[0] and "wrong machine" in results2[0])

# The half-done pull is a failure now.
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "update_checker.py"), encoding="utf-8").read()
checks["a pull whose inspect failed is not a success"] = (
    'return True, "Image pulled. Container inspect failed."' not in src)
checks["…it says the container was NOT updated"] = (
    "container NOT updated" in src)

# /cleanup walks every host, like /check. Stated as the intent, not as
# one spelling of it: the walk moved into `container_flags.cleanup`, and
# a check that insisted on the old `host_checkers` line would have failed
# the extraction rather than the behaviour (#63). What the walk actually
# DOES — every host, tagged, one dead box not stopping the rest — is
# driven for real in test_cleanup_shared.py.
tsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "telegram_bot.py"), encoding="utf-8").read()
dsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "discord_bot.py"), encoding="utf-8").read()
block = srcslice.section(tsrc, 'elif text == "/cleanup"', length=1200,
                         label="telegram_bot.py: /cleanup branch")
checks["/cleanup walks every managed host"] = block is not None and (
    "container_flags.cleanup(" in block or "host_checkers" in block)
checks["…and hands it every host, not just the local one"] = (
    block is not None and (
        "write=False" in block or "list(self.hosts)" in block
        or "host_checkers" in block))
# Discord did this locally-only until the walk was shared (#2, @famewolf:
# the full box was dockmox, not the local one).
dblock = srcslice.section(dsrc, "def _cmd_cleanup", "\n    def ",
                          label="discord_bot.py: _cmd_cleanup")
checks["…and Discord's /cleanup walks them too"] = (
    dblock is not None
    and "container_flags.cleanup(" in dblock and "_hosts_for(None)" in dblock)

# ── "Update all" over a mixed list: one batch per host (Q12) ───────────
# The batch takes ONE checker for its netns snapshot and for a group
# head's cascade. Telegram's run_updates handed it the LOCAL checker with
# the whole mixed snapshot: `update_container` already ran per entry (the
# checks above), but the netns owner of a dock8520 container was asked of
# the local daemon, and a dock8520 group head's dependents were restarted
# on this machine. Measured on core-split before the change: the T3 row
# "update_all:<tok> mixed, nas group head" showed `wait_healthy@local:web`
# and a local `restart` of nas's `media`. Driven here through the real
# TelegramBot.run_updates on a stand-in bot.
import subprocess  # noqa: E402

from telegram_bot import TelegramBot  # noqa: E402


class RecChecker(FakeChecker):
    """Records every call that has to reach its own host."""

    def __init__(self, label, disk=0, dead=False):
        super().__init__(label)
        self.netns_asked, self.healthy, self.restarted = [], [], []
        self.cleanups = 0
        self.disk = disk
        self.dead = dead
        ck = self

        class _Backend:
            name = label

            def inspect(self, name, fmt=None, timeout=None):
                return types.SimpleNamespace(stdout="", returncode=1)

            def restart(self, name, timeout=None):
                ck.restarted.append(name)
                return types.SimpleNamespace(returncode=0, stderr="")
        self.backend = _Backend()

    def netns_target_name(self, name):
        self.netns_asked.append(name)
        if self.dead:
            raise subprocess.TimeoutExpired(["inspect", name], 30)
        return None

    def _wait_healthy(self, name, max_wait):
        self.healthy.append(name)
        return "healthy", "running", "healthy"

    def get_disk_usage(self):
        return self.disk, 1, 100

    def cleanup_images(self):
        self.cleanups += 1
        return True, "pruned"


def _host_store(groups=None, cooldowns=None):
    return types.SimpleNamespace(
        get_groups=lambda: dict(groups or {}),
        get_cooldown=lambda n: (cooldowns or {}).get(n, 0),
        get_ask_before_major=lambda: [],
        add_pending_major=lambda *a, **k: None)


def run_update_all(updates, lc, rc, remote_groups=None, local_cooldowns=None,
                   disk_cleanup=False, runs=None):
    """TelegramBot.run_updates(local checker, mixed snapshot), as the
    "Update all" button calls it. Returns (batches, sent, removed, waits).

    `runs`, a list of (checker, updates), calls it once per pair in one
    go instead, the way /update <pattern> does: one host's list with that
    host's checker, and over several hosts `_run_batches` loops those
    calls in one thread, so a raise ends the loop there."""
    if runs is None:
        runs = [(lc, updates)]
    h_local = Host("local", lc, local=True)
    h_local.store = _host_store(cooldowns=local_cooldowns)
    h_remote = Host("dock8520", rc)
    h_remote.store = _host_store(groups=remote_groups)
    eng = make_engine(Registry([h_local, h_remote]))
    eng.config = types.SimpleNamespace(
        pending_file="/nonexistent", auto_update_all=False,
        disk_warn_auto_cleanup=disk_cleanup, disk_warn_percent=85)
    eng._t = lambda key, **kw: key
    waits = []
    eng._sleep = waits.append
    batches, sent, removed = [], [], []

    def batch(ups, ck, **kw):
        batches.append((ck.label, [u["name"] for u in ups]))
        return eng._process_update_batch(ups, ck, **kw)
    bot = types.SimpleNamespace(engine=eng, hosts=eng.hosts,
                                config=eng.config)
    bot.send_message = lambda text, *a, **k: sent.append(text)
    bot.t = lambda key, **kw: key
    bot._process_update_batch = batch
    bot._remove_from_pending = removed.extend
    bot._run_queued_selfupdate = lambda: None
    try:
        for caller, ups in runs:
            TelegramBot.run_updates(bot, caller,
                                    updates=[dict(u) for u in ups])
    except Exception as e:                          # noqa: BLE001
        # Recorded, not raised: a run that ends here is a red check
        # below, not a procedure that dies before its other checks.
        sent.append(f"raised {type(e).__name__}")
    return batches, sent, removed, waits


MIXED = [{"name": "web", "image": "nginx:1", "host": "local"},
         {"name": "llama-server", "image": "cuda:1", "host": "dock8520"},
         {"name": "db", "image": "pg:16", "host": "local"}]
lc, rc = RecChecker("local"), RecChecker("dock8520")
batches, sent, removed, _w = run_update_all(
    MIXED, lc, rc, remote_groups={"g1": {
        "containers": ["llama-server", "sidecar"],
        "restart_dependents": True, "wait_seconds": 0}})
checks["Update all over a mixed list runs one batch per host"] = (
    [b[0] for b in batches] == ["local", "dock8520"])
checks["…each host's entries together, in the order the hosts turn up"] = (
    batches == [("local", ["web", "db"]), ("dock8520", ["llama-server"])])
checks["…a remote entry's netns owner is asked of its own host"] = (
    rc.netns_asked == ["llama-server"] and lc.netns_asked == ["web", "db"])
checks["…a remote group head's health is awaited on its own host"] = (
    rc.healthy == ["llama-server"] and lc.healthy == [])
checks["…and its dependents are restarted there, not locally"] = (
    rc.restarted == ["sidecar"] and lc.restarted == [])
checks["…every host's lines reach the one summary"] = (
    len(sent) == 2 and sent[-1].startswith("update_result")
    and all(f"{n}: OK on" in sent[-1]
            for n in ("web", "llama-server", "db")))
checks["…and every entry of the snapshot leaves pending"] = (
    sorted(removed) == [("dock8520", "llama-server"), ("local", "db"),
                        ("local", "web")])

# A host that stops answering: its snapshot now goes to it and raises.
# That must be its own entry's failure, not the end of "Update all" —
# before the split the snapshot asked the local daemon and never met the
# dead host there.
lc, rc = RecChecker("local"), RecChecker("dock8520", dead=True)
batches, sent, removed, _w = run_update_all(MIXED[:2], lc, rc)
checks["a remote host that stops answering fails its own entry only"] = (
    len(sent) == 2 and "✅ web: OK on local" in sent[-1]
    and "❌ llama-server:" in sent[-1] and "timed out" in sent[-1])
checks["…nothing is recreated there"] = rc.updated == []
checks["…and Update all still clears the snapshot from pending"] = (
    sorted(removed) == [("dock8520", "llama-server"), ("local", "web")])

# The same guard holds for the local daemon, so a single-host install
# sees it too. Measured before Q12: a hung local daemon during the netns
# snapshot raised out of "Update all" — no summary after "Updating…",
# nothing taken off the list. Now each container gets its failure line,
# nothing is recreated, and the snapshot leaves pending like every other
# outcome of "Update all".
lc, rc = RecChecker("local", dead=True), RecChecker("dock8520")
batches, sent, removed, _w = run_update_all(
    [{"name": "web", "image": "nginx:1", "host": "local"},
     {"name": "db", "image": "pg:16", "host": "local"}], lc, rc)
checks["a local daemon that hangs in the snapshot still gets a summary"] = (
    len(sent) == 2 and sent[-1].startswith("update_result")
    and "❌ web:" in sent[-1] and "❌ db:" in sent[-1]
    and sent[-1].count("timed out") == 2)
checks["…nothing is recreated on it"] = lc.updated == []
checks["…and the snapshot leaves pending"] = (
    sorted(removed) == [("local", "db"), ("local", "web")])

# /update <pattern> reaches the same guard: it calls run_updates with one
# host's checker and that host's list, and over several hosts it loops
# those calls in one thread (`_run_batches`). Measured before Q12 with
# these scenarios: a dead dock8520 raised TimeoutExpired out of
# run_updates — no summary, nothing taken off the list, and in the loop
# every later host was never updated.
lc, rc = RecChecker("local"), RecChecker("dock8520", dead=True)
LLAMA = {"name": "llama-server", "image": "cuda:1", "host": "dock8520"}
batches, sent, removed, _w = run_update_all(None, lc, rc,
                                            runs=[(rc, [LLAMA])])
checks["/update <pattern> on a host that stops answering ends with a "
       "summary"] = (
    len(sent) == 2 and sent[-1].startswith("update_result")
    and "❌ llama-server:" in sent[-1] and "timed out" in sent[-1])
checks["…nothing is recreated there, and the entry leaves pending"] = (
    rc.updated == [] and removed == [("dock8520", "llama-server")])
lc, rc = RecChecker("local"), RecChecker("dock8520", dead=True)
batches, sent, removed, _w = run_update_all(
    None, lc, rc, runs=[(rc, [LLAMA]),
                        (lc, [{"name": "web", "image": "nginx:1",
                               "host": "local"}])])
checks["/update <pattern> over several hosts: the host after a dead one "
       "still updates"] = (
    lc.updated == ["web"] and len(sent) == 4
    and "✅ web: OK on local" in sent[-1])


# A group head whose update fails was rolled back, and its dependents
# lost the namespace when it stopped: the batch kicks them all (#27).
# For a remote head that kick belongs on its own host. Measured before
# Q12 with this scenario: `sidecar` restarted and `llama-server` awaited
# on the LOCAL checker.
class FailingChecker(RecChecker):
    def update_container(self, name, image, **kw):
        self.updated.append(name)
        return False, "rolled back"


lc, rc = RecChecker("local"), FailingChecker("dock8520")
batches, sent, removed, _w = run_update_all(
    MIXED[:2], lc, rc, remote_groups={"g1": {
        "containers": ["llama-server", "sidecar"],
        "restart_dependents": True, "wait_seconds": 0}})
checks["a remote head that rolls back has its dependents kicked there"] = (
    rc.restarted == ["sidecar"] and rc.healthy == ["llama-server"])
checks["…and nothing of it is restarted or awaited locally"] = (
    lc.restarted == [] and lc.healthy == [])
checks["…and the summary says the dependents were kicked"] = (
    len(sent) == 2 and "head rollback — dependents kicked" in sent[-1]
    and "`sidecar`" in sent[-1])

# The proactive prune of a full local disk ran once per "Update all"
# (one batch, one prune). One batch per host must not make it once per
# host: the second batch reads the same local disk.
lc, rc = RecChecker("local", disk=95), RecChecker("dock8520")
batches, sent, removed, _w = run_update_all(MIXED[:2], lc, rc,
                                            disk_cleanup=True)
checks["Update all prunes a full local disk once, not once per host"] = (
    lc.cleanups == 1 and rc.cleanups == 0
    and sent[-1].count("cleanup_between_updates") == 1)

# A cooldown lets a heavy container settle before the next recreate on
# its own machine. Between two hosts' batches it is not sat out any more
# (the Web UI and Discord never did); inside a host's batch it still is.
lc, rc = RecChecker("local"), RecChecker("dock8520")
_b, _s, _r, waits = run_update_all(MIXED[:2], lc, rc,
                                   local_cooldowns={"web": 60})
checks["a local cooldown is not waited before the next host's batch"] = (
    waits == [])
lc, rc = RecChecker("local"), RecChecker("dock8520")
_b, _s, _r, waits = run_update_all(MIXED, lc, rc,
                                   local_cooldowns={"web": 60})
checks["…but still between two containers of the same host"] = (
    waits == [60])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
