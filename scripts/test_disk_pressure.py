#!/usr/bin/env python3
"""Disk pressure handled where it happens, and a doomed layer named
before it dies (#2 @famewolf, #63).

famewolf, after the routing fix: "It should never get to 'no space left
on device' if docksentry is doing its job. It needs to do cleanup on a
container by container basis as it updates them." Two honest halves,
because the two kinds of host allow different honesty:

* **reactive, any host** — an update failing on ENOSPC gets that host's
  cleanup immediately, mid-batch. It is the only disk signal a remote
  host gives us at all: free space is a filesystem question, and the
  Docker API does not answer it (`get_disk_usage()` reads the LOCAL
  data dir — measured, `shutil.disk_usage`, and honest about it).
* **proactive, local only** — between containers the local disk is
  checked against DISK_WARN_PERCENT, because locally we CAN see it
  coming. Behind DISK_WARN_AUTO_CLEANUP (the user's existing opt-in),
  and once per batch: prune walks every image on the host, and a batch
  that prunes after every entry spends longer pruning than updating.

Both call `cleanup_images()` DIRECTLY — `cleanup_guarded` takes the
update lock, and the batch is already holding it. That path deadlocks;
this test pins that it is never taken from inside the batch.

And the writable layer: a recreate destroys it by design. Below the
threshold that is caches and temp files; above it, an application has
been storing data where the next update deletes it. Said twice — in the
status detail (⚠, before the fact) and in the update result (after,
because by then the evidence is gone with the layer).
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_engine import UpdateEngine  # noqa: E402
import status_render  # noqa: E402

checks = {}


def make_engine(hosts, config=None):
    e = UpdateEngine.__new__(UpdateEngine)
    import threading
    e._update_lock = threading.Lock()
    e._update_queue = []
    e._update_queue_lock = threading.Lock()
    e._queued_selfupdate = None
    e._swap_in_flight = False
    e.notifier = None
    e.hosts = hosts
    e.config = config or types.SimpleNamespace(
        pending_file="/nonexistent", auto_update_all=False,
        disk_warn_auto_cleanup=False, disk_warn_percent=85)
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
    def __init__(self, label, *, fail_with="", disk=(50, 10**11, 2 * 10**11)):
        self.label = label
        self.updated = []
        self.cleanups = 0
        self.fail_with = fail_with
        self.disk = disk
        self.backend = types.SimpleNamespace(name=label)

    def update_container(self, name, image, **kw):
        self.updated.append(name)
        if self.fail_with:
            return False, self.fail_with
        return True, f"OK on {self.label}"

    def cleanup_images(self):
        self.cleanups += 1
        return True, "freed 2.4GB"

    def get_disk_usage(self):
        return self.disk

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


# ── reactive: ENOSPC on the remote gets THE REMOTE's cleanup ─────────
local = FakeChecker("local")
remote = FakeChecker("dock8520",
                     fail_with="Compose pull failed: write /var/lib/docker: "
                               "no space left on device")
reg = Registry([Host("local", local, local=True), Host("dock8520", remote)])
engine = make_engine(reg)
results, ok, _ = engine._process_update_batch(
    [{"name": "web", "image": "nginx:1", "host": "local"},
     {"name": "llama", "image": "cuda:1", "host": "dock8520"}],
    local, auto=False)

checks["ENOSPC on a remote host triggers cleanup THERE"] = remote.cleanups == 1
checks["…and not locally"] = local.cleanups == 0
checks["…and the batch says so, naming the host"] = any(
    "dock8520" in r and "cleanup" in r for r in results)
checks["…while the failed update still reports as ❌"] = any(
    r.startswith("❌") and "llama" in r for r in results)

# ── an ordinary failure does NOT prune anything ──────────────────────
local2 = FakeChecker("local", fail_with="Health check failed — rolled back")
reg2 = Registry([Host("local", local2, local=True)])
engine2 = make_engine(reg2)
engine2._process_update_batch(
    [{"name": "web", "image": "nginx:1", "host": "local"}], local2, auto=False)
checks["a non-disk failure triggers no cleanup"] = local2.cleanups == 0

# ── proactive: local threshold, opt-in, once per batch ───────────────
full = FakeChecker("local", disk=(93, 5 * 10**9, 10**11))
reg3 = Registry([Host("local", full, local=True)])
cfg = types.SimpleNamespace(pending_file="/nonexistent",
                            auto_update_all=False,
                            disk_warn_auto_cleanup=True,
                            disk_warn_percent=85)
engine3 = make_engine(reg3, cfg)
results3, _, _ = engine3._process_update_batch(
    [{"name": "a", "image": "i:1", "host": "local"},
     {"name": "b", "image": "i:1", "host": "local"},
     {"name": "c", "image": "i:1", "host": "local"}],
    full, auto=False)
checks["local disk over the threshold gets a mid-batch cleanup"] = (
    full.cleanups == 1)
checks["…exactly once, not once per container"] = full.cleanups < 2
checks["…reported with the measured percentage"] = any(
    "93%" in r for r in results3)

# Off by default: same disk, switch off, nothing pruned.
quiet = FakeChecker("local", disk=(93, 5 * 10**9, 10**11))
reg4 = Registry([Host("local", quiet, local=True)])
engine4 = make_engine(reg4)
engine4._process_update_batch(
    [{"name": "a", "image": "i:1", "host": "local"}], quiet, auto=False)
checks["without DISK_WARN_AUTO_CLEANUP nothing is pruned"] = (
    quiet.cleanups == 0)

# The scheduled per-host path hands over a REMOTE checker. The disk
# reading is local either way (`get_disk_usage()` reads the local data
# dir no matter which checker it hangs off) — so the prune must go to
# the LOCAL checker, or a local reading would prune a remote host:
# the routing bug all over again, in reverse.
loc5 = FakeChecker("local", disk=(93, 5 * 10**9, 10**11))
rem5 = FakeChecker("dock8520", disk=(93, 5 * 10**9, 10**11))
reg5 = Registry([Host("local", loc5, local=True), Host("dock8520", rem5)])
cfg5 = types.SimpleNamespace(pending_file="/nonexistent",
                             auto_update_all=False,
                             disk_warn_auto_cleanup=True,
                             disk_warn_percent=85)
engine5 = make_engine(reg5, cfg5)
engine5._process_update_batch(
    [{"name": "llama", "image": "cuda:1", "host": "dock8520"}],
    rem5, auto=True)
checks["a local disk reading prunes the LOCAL host…"] = loc5.cleanups == 1
checks["…never the remote one the batch happens to run for"] = (
    rem5.cleanups == 0)

# A second batch measures again — the once-per-batch mark resets.
engine3._process_update_batch(
    [{"name": "d", "image": "i:1", "host": "local"}], full, auto=False)
checks["the once-per-batch mark resets for the next batch"] = (
    full.cleanups == 2)

# ── the deadlock that must never be built ────────────────────────────
esrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "update_engine.py"), encoding="utf-8").read()
i = esrc.index("def _process_update_batch")
body = esrc[i:esrc.index("\n    def ", i + 10)]
checks["the batch never calls cleanup_guarded (it holds the lock)"] = (
    "cleanup_guarded" not in body)
checks["…it calls cleanup_images directly"] = "cleanup_images()" in body

# ── the doomed layer, said before it dies ────────────────────────────
csrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "update_checker.py"), encoding="utf-8").read()
checks["one measurement, shared by both recreate paths"] = (
    csrc.count("self._layer_farewell(name)") == 2)
def _method_body(src, name):
    i = src.index(f"def {name}")
    j = src.find("\n    def ", i + 10)
    return src[i:j if j != -1 else len(src)]


sbody = _method_body(csrc, "_update_standalone")
checks["…standalone measures before the recreate"] = (
    sbody.index("_layer_farewell") < sbody.index("Recreating container"))
cbody = _method_body(csrc, "_update_compose")
checks["…compose measures before force-recreate"] = (
    cbody.index("_layer_farewell") < cbody.index("--force-recreate"))
checks["…and both success messages carry the note"] = (
    csrc.count("{layer_note}") == 2)

# The helper itself, behaviourally: big layer speaks, small stays quiet.
from update_checker import UpdateChecker  # noqa: E402
uc = UpdateChecker.__new__(UpdateChecker)


def _fake_run(rc, out):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")


uc._backend = types.SimpleNamespace(
    run=lambda *a, **k: _fake_run(0, "9800000000\n"))
note = uc._layer_farewell("ollama")
checks["9.8 GB in the layer produces a warning"] = "9.8 GB" in note
checks["…that says where the data belongs"] = "volume" in note
uc._backend = types.SimpleNamespace(
    run=lambda *a, **k: _fake_run(0, "120000\n"))
checks["120 kB of caches stays quiet"] = uc._layer_farewell("web") == ""
uc._backend = types.SimpleNamespace(
    run=lambda *a, **k: _fake_run(1, ""))
checks["a failed inspect never blocks the update"] = (
    uc._layer_farewell("web") == "")
checks["the threshold is past caches territory (500 MB)"] = (
    UpdateChecker.LAYER_WARN_BYTES == 500 * 1000 * 1000)

# ── and in the status detail, before the fact ────────────────────────
big = status_render.lines({"name": "ollama", "running": True,
                           "state": "running", "layer_bytes": 9.8e9})
flat = "\n".join(big)
checks["a big layer is marked in /status"] = (
    "⚠" in flat and "lost on next update" in flat)
small = "\n".join(status_render.lines(
    {"name": "web", "running": True, "state": "running",
     "layer_bytes": 120000}))
checks["a small one is shown unmarked"] = (
    "layer" in small and "⚠" not in small)
zero = "\n".join(status_render.lines(
    {"name": "db", "running": True, "state": "running", "layer_bytes": 0}))
checks["+0B is still shown (the owner insisted, and it IS an answer)"] = (
    "+0B layer" in zero)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
