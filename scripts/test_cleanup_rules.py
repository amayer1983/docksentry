#!/usr/bin/env python3
"""What cleanup is allowed to delete, and what it must keep.

A suite-wide mutation sweep on 11.10.2026 found four promises here that
nothing held — the enforcement could be deleted and none of 201
procedures went red. Three of them delete data, which is the one class
of defect a user cannot undo:

* **`CLEANUP_BACKUP_DAYS`** — drop the `getmtime(path) < cutoff` and
  `_prune_old_backups` deletes *every* tarball it finds. Those tarballs
  exist precisely because the image is gone; they are the only copy.
* **`CLEANUP_GRACE_HOURS`** — drop the `until=` and `docker image prune
  --all --force` is an unfiltered prune of everything unused. The grace
  period is the whole difference between "tidy up what has been idle for
  a day" and "delete the image you built this morning".
* **`CLEANUP_BACKUP_LOCAL_ONLY`** — switch the branch off and the
  locally-built image is pruned with no copy taken. `_backup_local_unused_images`
  was called from no procedure at all.

The fourth is the counter-direction and it matters as much: a prune that
refuses to delete anything fills the disk, which is how a host stops
updating. So each rule is checked in both directions.

Driven, not grepped. A real temp directory with real directories at real
mtimes for the retention half, and a backend stand-in that records the
**keyword** arguments for the prune half. That last point is not
incidental: the fixture in `test_cleanup_shared.py` is
`def image_prune(self, **kw)`, which swallows every kwarg — so `until=`
was passed to something that could not notice it was missing. A stand-in
replaces the environment (the daemon), never the decision, and here the
decision is which flags the call carries.
"""

import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker  # noqa: E402

checks = {}


class _Backend:
    """A daemon that answers, and remembers exactly how it was asked."""

    def __init__(self, images=None, used=None, inspect=None, save_ok=True):
        self.prunes = []
        self.saves = []
        #: Every daemon call that changes something, in the order it
        #: arrived. Counting the two separately said nothing about
        #: which came first, and first is the whole point.
        self.order = []
        self._images = images or []
        self._used = used or []
        self._inspect = inspect or {}
        self._save_ok = save_ok

    def image_prune(self, all=False, force=False, until=None, timeout=None):
        # Named parameters, not `**kw`: a signature that swallows
        # everything cannot tell a missing `until` from a present one,
        # and that is the hole this procedure exists to close.
        self.prunes.append({"all": all, "force": force, "until": until})
        self.order.append("prune")
        return types.SimpleNamespace(
            returncode=0, stdout="Total reclaimed space: 0B", stderr="")

    def image_save(self, img_id, path, timeout=None):
        self.saves.append((img_id, path))
        self.order.append("save")
        if self._save_ok:
            with open(path, "wb") as f:
                f.write(b"tarball")
        return types.SimpleNamespace(
            returncode=0 if self._save_ok else 1, stdout="",
            stderr="" if self._save_ok else "no space left")

    def run(self, args, timeout=None):
        if args[:2] == ["ps", "-a"]:
            out = "\n".join(self._used)
        elif args[:2] == ["image", "ls"]:
            out = "\n".join(self._images)
        elif args[:2] == ["image", "inspect"]:
            meta = self._inspect.get(args[2])
            if meta is None:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="")
            out = json.dumps([meta])
        else:
            out = ""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


def _checker(backend, **cfg):
    d = tempfile.mkdtemp(prefix="cleanup-")
    base = dict(data_dir=d, cleanup_grace_hours=24, cleanup_backup_days=7,
                cleanup_backup_local_only=False,
                cleanup_backup_dir=os.path.join(d, "backups"),
                language="en", debug=False)
    base.update(cfg)
    c = UpdateChecker.__new__(UpdateChecker)
    c.config = types.SimpleNamespace(**base)
    c._backend = backend
    # `backend` is a read-only property that falls back to `_backend`.
    c._debug = lambda *a, **kw: None
    c._t = lambda key, **kw: key
    return c


# ── the grace period reaches the daemon ──────────────────────────────
# `docs/configuration.md` documents `CLEANUP_GRACE_HOURS`. Without the
# `until=` the call is `image prune --all --force`, which deletes every
# unused image including the one built an hour ago.
_b = _Backend()
_checker(_b, cleanup_grace_hours=24).cleanup_images()
checks["the prune carries a grace period"] = (
    len(_b.prunes) == 1 and _b.prunes[0]["until"] == "24h")

_b = _Backend()
_checker(_b, cleanup_grace_hours=72).cleanup_images()
checks["…the one that was configured, not a fixed one"] = (
    bool(_b.prunes) and _b.prunes[0]["until"] == "72h")

_b = _Backend()
_checker(_b, cleanup_grace_hours="nonsense").cleanup_images()
checks["…and an unreadable setting falls back to a day"] = (
    bool(_b.prunes) and _b.prunes[0]["until"] == "24h")

# …and it really is a prune, or the two checks above pass on a call that
# deletes nothing.
checks["…while still being an actual prune"] = (
    bool(_b.prunes) and _b.prunes[0]["all"] and _b.prunes[0]["force"])

# ── retention deletes the old and keeps the young ────────────────────
# Both halves, because they fail in opposite and equally bad ways: no
# cutoff deletes the only copy of a locally-built image, and a cutoff
# that never fires fills the disk the feature exists to protect.
def _retention(days, ages_in_days):
    """Lay out backup dirs at given ages; which survive the prune?"""
    c = _checker(_Backend(), cleanup_backup_days=days)
    root = c.config.cleanup_backup_dir
    os.makedirs(root, exist_ok=True)
    now = time.time()
    for age in ages_in_days:
        p = os.path.join(root, f"age-{age}")
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "image.tar"), "wb") as f:
            f.write(b"x")
        os.utime(p, (now - age * 86400, now - age * 86400))
    c._prune_old_backups()
    return sorted(os.listdir(root))


checks["a backup older than the window is deleted"] = (
    _retention(7, [30]) == [])
checks["…and one inside it is kept"] = (
    _retention(7, [1]) == ["age-1"])
checks["…with the boundary read from the setting"] = (
    _retention(30, [10, 60]) == ["age-10"])
# Zero is not "delete everything": `int(cfg or 7)` reads it as unset
# and falls back to a week. Worth pinning rather than discovering —
# somebody setting `CLEANUP_BACKUP_DAYS=0` to mean "keep nothing" gets
# the default instead, and the behaviour should at least be the safe
# one of the two.
checks["a retention of zero falls back to the default, not to purge"] = (
    _retention(0, [1, 30]) == ["age-1"])
checks["an unreadable retention setting falls back to a week"] = (
    _retention("nonsense", [3, 30]) == ["age-3"])
# A missing directory is not an error — cleanup runs before the first
# backup exists. Asserted on what comes out, not on "it did not throw":
# the prune returns without creating anything, so the directory is still
# absent afterwards.
_c = _checker(_Backend())
_c._prune_old_backups()
checks["cleanup before the first backup leaves nothing behind"] = (
    not os.path.exists(_c.config.cleanup_backup_dir))

# ── the local-image copy, and when it is taken ───────────────────────
# A locally-built image has no registry to come back from. `docker pull`
# cannot recreate it, so the tarball is the only copy there will ever be.
LOCAL = {"RepoDigests": [], "RepoTags": ["myapp:1.0"]}
REGISTRY = {"RepoDigests": ["nginx@sha256:abc"], "RepoTags": ["nginx:1.25"]}
DANGLING = {"RepoDigests": [], "RepoTags": []}

_b = _Backend(images=["sha256:local"], used=[],
              inspect={"sha256:local": LOCAL})
_saved = _checker(_b, cleanup_backup_local_only=True
                  )._backup_local_unused_images()
checks["an unused locally-built image is saved first"] = (
    len(_saved) == 1 and _b.saves and _b.saves[0][0] == "sha256:local")
checks["…to a file that exists afterwards"] = (
    bool(_saved) and os.path.isfile(_saved[0][1]))

# The three it must NOT copy, each for its own reason — without these
# the check above passes on a step that tarballs the whole image store.
_b = _Backend(images=["sha256:reg"], used=[],
              inspect={"sha256:reg": REGISTRY})
checks["an image that lives in a registry is not copied"] = (
    _checker(_b, cleanup_backup_local_only=True
             )._backup_local_unused_images() == [] and _b.saves == [])

_b = _Backend(images=["sha256:dang"], used=[],
              inspect={"sha256:dang": DANGLING})
checks["…nor a dangling build leftover"] = (
    _checker(_b, cleanup_backup_local_only=True
             )._backup_local_unused_images() == [] and _b.saves == [])

_b = _Backend(images=["sha256:local"], used=["sha256:local"],
              inspect={"sha256:local": LOCAL})
checks["…nor one a container is still using"] = (
    _checker(_b, cleanup_backup_local_only=True
             )._backup_local_unused_images() == [] and _b.saves == [])

# And the switch: with the setting off nothing is copied, with it on the
# copy happens before the prune that would delete the image.
_b = _Backend(images=["sha256:local"], used=[],
              inspect={"sha256:local": LOCAL})
_checker(_b, cleanup_backup_local_only=False).cleanup_images()
checks["with the setting off no copy is taken"] = (
    _b.saves == [] and len(_b.prunes) == 1)

# …and the ORDER, which is the file's whole argument and which
# `len(saves)==1 and len(prunes)==1` did not hold: an audit moved the
# backup step to after `image_prune` and this stayed green. The
# tarball exists because the image is gone, so a copy taken afterwards
# is a copy of nothing.
_b = _Backend(images=["sha256:local"], used=[],
              inspect={"sha256:local": LOCAL})
_checker(_b, cleanup_backup_local_only=True).cleanup_images()
checks["…and with it on, a copy is taken and the prune runs"] = (
    len(_b.saves) == 1 and len(_b.prunes) == 1)
checks["…with the copy BEFORE the prune, not after it"] = (
    _b.order[:2] == ["save", "prune"])
if _b.order[:2] != ["save", "prune"]:
    print(f"      → order was {_b.order}")

# A failed save must not be reported as a copy taken — the whole value
# of the step is that the tarball is there when the image is gone.
_b = _Backend(images=["sha256:local"], used=[],
              inspect={"sha256:local": LOCAL}, save_ok=False)
checks["a save that failed is not counted as a backup"] = (
    _checker(_b, cleanup_backup_local_only=True
             )._backup_local_unused_images() == [])

# …and a backup step that throws must not stop the prune: the disk is
# what cleanup is protecting.
class _Exploding(_Backend):
    def run(self, args, timeout=None):
        if args[:2] == ["ps", "-a"]:
            raise RuntimeError("daemon went away")
        return super().run(args, timeout)


_b = _Exploding()
_checker(_b, cleanup_backup_local_only=True).cleanup_images()
checks["a failed backup step does not cancel the prune"] = (
    len(_b.prunes) == 1)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
