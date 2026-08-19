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

# /cleanup walks every host, like /check.
tsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "telegram_bot.py"), encoding="utf-8").read()
i = tsrc.index('elif text == "/cleanup"')
block = tsrc[i:i + 1200]
checks["/cleanup walks every managed host"] = "host_checkers" in block
checks["…tagging each answer with its host"] = '@{host_name}' in block
checks["…and one dead host does not stop the rest"] = (
    "except Exception" in block)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
