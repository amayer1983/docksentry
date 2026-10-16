#!/usr/bin/env python3
"""The three group rules nothing was holding.

`docs/updates.md:41` — a container belongs to one group.
`docs/updates.md:44` — container 1 updates, then a wait, then container 2.
`docs/updates.md:120-123` — the dependent restart never waits less than
thirty seconds.

A sweep on 10.10.2026 reverted each of these and no procedure noticed.
The group machinery itself is well covered — the ordering, the abort on
failure, the head being the first member, a sick head not blocking the
repair — so what was missing is narrow and specific: the pauses between
the steps, and the rule that keeps a container out of two groups.

The waits are what make a group a group. A stack whose database and
application are recreated in the same second is a stack where the
application starts against a database that is not listening yet, which
is the whole reason somebody grouped them.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import update_engine  # noqa: E402
from i18n import get_translator  # noqa: E402
from container_store import ContainerStore  # noqa: E402

checks = {}


def _store():
    d = tempfile.mkdtemp(prefix="groups-")
    cfg = types.SimpleNamespace(data_dir=d)
    for n in ("pinned", "autoupdate", "update_windows", "ask_before_major",
              "trust_running", "cooldown", "protect_stop", "major_pending",
              "groups", "notes", "links", "pending_updates"):
        setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
    return ContainerStore(cfg)


# ── a container belongs to one group ─────────────────────────────────
# Two groups claiming the same container means the update order is
# whichever group the loop reached first, which is no order at all.
s = _store()
s.save_group("media", "media", ["gluetun", "sonarr", "radarr"], wait_seconds=30)
s.save_group("downloads", "downloads", ["gluetun", "qbittorrent"], wait_seconds=30)
_groups = s.get_groups()
checks["moving a container to another group takes it out of the first"] = (
    "gluetun" not in (_groups.get("media") or {}).get("containers", []))
checks["…and it is in the new one"] = (
    "gluetun" in (_groups.get("downloads") or {}).get("containers", []))
checks["…while the other members stay where they were"] = (
    sorted((_groups.get("media") or {}).get("containers", []))
    == ["radarr", "sonarr"])

# A group left with nothing in it is removed rather than kept as an
# empty shell that shows up in the interface.
s2 = _store()
s2.save_group("solo", "solo", ["only"], wait_seconds=30)
s2.save_group("other", "other", ["only"], wait_seconds=30)
checks["a group emptied by the move disappears"] = "solo" not in s2.get_groups()

# ── the wait is bounded and has a sane default ───────────────────────
s3 = _store()
s3.save_group("g", "g", ["a", "b"], wait_seconds=45)
checks["a wait inside the range is kept"] = (
    s3.get_groups()["g"]["wait_seconds"] == 45)
s3.save_group("g", "g", ["a", "b"], wait_seconds=9999)
checks["…one that is absurd is capped"] = (
    s3.get_groups()["g"]["wait_seconds"] == 600)
s3.save_group("g", "g", ["a", "b"], wait_seconds=-5)
checks["…a negative one becomes zero, not a hang"] = (
    s3.get_groups()["g"]["wait_seconds"] == 0)
s3.save_group("g", "g", ["a", "b"], wait_seconds="nonsense")
checks["…and something unreadable falls back to thirty seconds"] = (
    s3.get_groups()["g"]["wait_seconds"] == 30)

# ── the two waits inside the batch, driven ───────────────────────────
# Read as a tree until 16.10.2026, and an audit showed what that was
# worth: setting both cascade call sites to `max_wait=0` left the
# `max(…, 30)` lines untouched, so the shape check still found them and
# the documented promise was gone with the procedure green. Same for
# short-circuiting `_wait`'s body and for deleting the group-boundary
# condition — three mutations, three passes.
#
# So the batch is driven now. It wanted a dozen collaborators and I
# said so last time; what changed is that the alternative turned out to
# be worse than the stubs. Every stand-in below replaces the
# environment — the clock, the daemon, the store — and none of them
# replaces a decision.
import threading  # noqa: E402

from update_engine import UpdateEngine  # noqa: E402


def _batch(members, waits=30, fail=None, group=None):
    """Run one group through the real batch loop.

    Returns `(sleeps, cascades)` — every pause it took, and every
    dependent cascade with the wait it was given.
    """
    sleeps, cascades, updated = [], [], []

    class _Updater:
        def netns_target_name(self, name):
            return None

        def update_container(self, name, image, **kw):
            updated.append(name)
            return (False, "boom") if name == fail else (True, "ok")

    class _Store:
        def get_groups(self):
            return {"g": {"name": "G",
                          "containers": list(group or members),
                          "restart_dependents": True,
                          "wait_seconds": waits}}

    bot = types.SimpleNamespace()
    bot._update_lock = threading.Lock()
    bot._run_queued_selfupdate = lambda: None
    bot.config = types.SimpleNamespace(
        pending_file=os.path.join(tempfile.mkdtemp(), "p.json"))
    bot.store = _Store()
    bot.notifier = None
    bot.t = get_translator("en")
    bot.send_message = lambda *a, **k: None
    bot._enrich_with_source_url = lambda u: None
    bot._display_name = lambda u: u["name"]
    bot._maybe_cooldown = lambda *a, **k: None
    bot.mark_updating = lambda *a, **k: None
    bot.clear_updating = lambda *a, **k: None
    bot._remove_from_pending = lambda names: None
    bot._restart_group_dependents = lambda head, deps, checker, max_wait=30: (
        cascades.append((head, tuple(deps), max_wait)) or "cascade")

    # `_wait` honours `engine._sleep` when the engine has one, which is
    # the seam the module put there for exactly this: the clock is
    # environment, the decision to wait is not.
    bot._sleep = lambda s: sleeps.append(s)
    UpdateEngine._process_update_batch(
        bot, [{"name": n, "image": "i"} for n in members],
        _Updater(), auto=False)
    return sleeps, cascades, updated


# The pause between two containers of one group. A database and its
# application recreated in the same second means the application starts
# against a database that is not listening yet, which is the whole
# reason somebody grouped them.
_sleeps, _cascades, _updated = _batch(["head", "dep"], waits=45)
checks["the batch pauses between containers of one group"] = 45 in _sleeps
checks["…for as long as the group asks"] = (
    45 in _batch(["head", "dep"], waits=45)[0]
    and 45 not in _batch(["head", "dep"], waits=10)[0])
checks["…and not at all when it asks for nothing"] = (
    _batch(["head", "dep"], waits=0)[0] == [])
# …while both still get updated, or "no pause" passes on a batch that
# does nothing.
checks["…and both members are updated either way"] = (
    _updated == ["head", "dep"])

# The dependent cascade's floor. `docs/updates.md:120-123` — a sidecar
# rejoining a namespace needs the head to be listening, and a zero here
# is a race the user cannot see. Read off the call, not off the shape
# of a `max()` somewhere in the function.
#
# The sidecar has to be in the GROUP and not in the batch: an in-batch
# member self-heals through its own update, so the explicit kick is
# only for the ones the batch will not reach.
# Five, not zero: `grp.get("wait_seconds", 30) or 30` reads a zero as
# unset and gives 30 back regardless, so a zero cannot tell the floor
# from its absence. Five can.
_s, _c, _ = _batch(["head"], waits=5, group=["head", "sidecar"])
checks["a dependent cascade never waits less than thirty seconds"] = (
    bool(_c) and all(w >= 30 for _h, _d, w in _c))
if _c and any(w < 30 for _h, _d, w in _c):
    print(f"      → cascade waits: {[w for _h, _d, w in _c]}")

_s, _c, _ = _batch(["head"], waits=120, group=["head", "sidecar"])
checks["…and a longer wait than that is kept"] = (
    bool(_c) and all(w == 120 for _h, _d, w in _c))

# The other cascade site: a head whose update FAILED still kicks its
# dependents, because the rollback restored the head and their
# namespace went with the old one. Same floor.
_s, _c, _ = _batch(["head"], waits=5, fail="head",
                   group=["head", "sidecar"])
checks["…including the cascade after a head rollback"] = (
    bool(_c) and all(w >= 30 for _h, _d, w in _c))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
