#!/usr/bin/env python3
"""How the local backup bundle is written to disk.

Two findings from the 18.10.2026 audit, both in the same four lines of
`backup.write_local`.

**The permission.** The bundle holds every channel credential the
install has — bot tokens, webhook URLs, the SMTP password. It was
written through a plain `open()`, so its mode came from the umask:
0644 in practice. Four metres away, `settings.json` is deliberately
held at 0600 and there is a method whose whole job is keeping it there.
Same secrets, two standards.

**The temp name.** `<path>.tmp`, shared by every writer, exactly as in
`container_store.atomic_write_json` — a second copy of the same defect,
which is why it survived the fix to the first one. Two bundles written
at the same moment interleaved into one temp file and `os.replace`
published the mixture.

The bundle is built for real here; only the store is stood in for,
because what is measured is the file on disk, not its contents.
"""

import os
import shutil
import stat
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import backup  # noqa: E402

checks = {}
root = tempfile.mkdtemp(prefix="ds-bk-")


class _Store:
    """Enough of a ContainerStore to build a bundle."""

    def get_pinned(self):
        return []

    def get_autoupdate(self):
        return []

    def get_ask_before_major(self):
        return []

    def get_groups(self):
        return {}

    def get_notes(self):
        return {}

    def get_links(self):
        return {}

    def get_update_windows(self):
        return {}


def _cfg(label="probe"):
    d = tempfile.mkdtemp(dir=root)
    return types.SimpleNamespace(data_dir=d, bot_label=label,
                                 settings_file="", docker_hosts=[])


# ── the permission ───────────────────────────────────────────────────

cfg = _cfg()
path = backup.write_local(cfg, _Store(), "9.9.9")
checks["a bundle is actually written"] = bool(path) and os.path.exists(path)
mode = stat.S_IMODE(os.stat(path).st_mode) if path else None
checks["…readable only by its owner"] = mode == 0o600
# Said as the thing that matters rather than as a number, because 0640
# would also be "not 0644" and would still hand the tokens to a group.
checks["…not group-readable"] = bool(mode is not None and not mode & 0o040)
checks["…and not world-readable"] = bool(
    mode is not None and not mode & 0o004)

# The umask must not be able to loosen it. A process started with a
# permissive umask is the normal case in a container.
_old_umask = os.umask(0o000)
try:
    cfg2 = _cfg("umask")
    p2 = backup.write_local(cfg2, _Store(), "9.9.9")
    checks["…even under a wide-open umask"] = (
        bool(p2) and stat.S_IMODE(os.stat(p2).st_mode) == 0o600)
finally:
    os.umask(_old_umask)

# ── two writers ──────────────────────────────────────────────────────
# Shared directory, same instant. The bundles have different labels, so
# a file holding both labels can only have come from one write seeing
# the other's bytes.

shared = _cfg("shared")
errors = []
written = []


def _write(label):
    cfg_l = types.SimpleNamespace(data_dir=shared.data_dir, bot_label=label,
                                  settings_file="", docker_hosts=[])
    try:
        for _ in range(40):
            p = backup.write_local(cfg_l, _Store(), "9.9.9", keep=500)
            if p:
                written.append(p)
    except Exception as e:                      # pragma: no cover
        errors.append(repr(e))


ts = [threading.Thread(target=_write, args=(f"host{i}",)) for i in range(4)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=60)

checks["concurrent writes do not raise"] = errors == []
checks["…and actually ran"] = len(written) >= 40

_dir = backup.local_dir(shared)
_files = [f for f in os.listdir(_dir) if f.endswith(".json")]
_broken, _mixed = [], []
for f in _files:
    body = open(os.path.join(_dir, f)).read()
    try:
        import json
        data = json.loads(body)
    except ValueError:
        _broken.append(f)
        continue
    # One bundle names one instance. Two in one file means interleaving.
    if sum(body.count(f'"host{i}"') for i in range(4)) > 1:
        _mixed.append(f)

checks["no bundle on disk is unparsable"] = _broken == []
checks["…and none carries two instances' data"] = _mixed == []
checks["…and every one of them is 0600"] = all(
    stat.S_IMODE(os.stat(os.path.join(_dir, f)).st_mode) == 0o600
    for f in _files)
checks["…and no temp file is left behind"] = [
    f for f in os.listdir(_dir) if f.endswith(".tmp")] == []

# Counter-direction: the writer must still be able to fail and say so,
# or "no broken files" is satisfied by one that writes nothing. An
# unwritable directory returns "" rather than raising.
_ro = tempfile.mkdtemp(dir=root)
_ro_cfg = types.SimpleNamespace(data_dir=_ro, bot_label="ro",
                                settings_file="", docker_hosts=[])
os.makedirs(os.path.join(_ro, "backups"))
if os.geteuid() != 0:
    os.chmod(os.path.join(_ro, "backups"), 0o500)
    checks["an unwritable directory reports failure, not a crash"] = (
        backup.write_local(_ro_cfg, _Store(), "9.9.9") == "")
    checks["…and leaves no debris"] = os.listdir(
        os.path.join(_ro, "backups")) == []
    os.chmod(os.path.join(_ro, "backups"), 0o700)
else:
    print("  (übersprungen: als root greifen Verzeichnisrechte nicht)")

shutil.rmtree(root, ignore_errors=True)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print(f"\n  gemessen: {len(_files)} Bündel von 4 gleichzeitigen Schreibern")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
