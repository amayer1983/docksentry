#!/usr/bin/env python3
"""A pin list that cannot be read must stop updates, not wave them through.

`ContainerStore.is_pinned` was reversed on 18.10.2026 to fail closed, and
its docstring says the reason plainly: "Treating 'I cannot read the list'
as 'everything is protected' is the wrong answer in at most one
direction… The other direction loses a database."

The same docstring then claims `update_checker.py` skips pinned
containers on that answer. It did not. The checker never calls the store
— `_get_pinned` reads `pinned_containers.json` itself, and on damage it
returned `[]`, which reads as "nothing is pinned". So the guard sat on
the detail page while the code that actually recreates containers failed
open. Measured on 07.11.2026: corrupt JSON gave `[]`, and a single
Latin-1 byte did not even get that far — `UnicodeDecodeError` is neither
a `JSONDecodeError` nor an `IOError` and escaped the method outright.

Driven to the effect, not to the return value: the question that matters
is whether `check_all` puts the container in the pending list, because
that is what a later update run reads.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import EVERYTHING_PINNED, UpdateChecker  # noqa: E402

checks = {}


class _Daemon:
    """Answers `ps` with one container. Environment, not decision."""

    name = "local"

    def run(self, args, timeout=None, **kw):
        if args[:1] == ["ps"]:
            return types.SimpleNamespace(
                returncode=0, stdout="db|reg/db:1\n", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")


def _checker(pin_bytes):
    """A checker whose pin file holds exactly `pin_bytes` (None = no file)."""
    tmp = tempfile.mkdtemp(prefix="ds-pin-")
    pinned = os.path.join(tmp, "pinned.json")
    if pin_bytes is not None:
        with open(pinned, "wb") as f:
            f.write(pin_bytes)
    cfg = types.SimpleNamespace(
        debug=False, pinned_file=pinned, exclude_containers=[],
        pending_file=os.path.join(tmp, "pending.json"),
        advisories_file=os.path.join(tmp, "adv.json"))
    # The daemon is stood in for; `get_running_containers` is NOT. The
    # pin check lives inside that method, so replacing it would remove
    # the very decision under test — the first version of this file did
    # exactly that and passed while measuring nothing.
    o = UpdateChecker(cfg, backend=_Daemon())
    o._own_container_name = lambda: ""
    o._labels_for = lambda names: {}
    o._parse_image = lambda img: ("reg", "db", "1")
    o._container_image_id = lambda name: "sha256:RUN"
    o._get_local_digests = lambda img: ["sha256:LOCAL"]
    o._get_local_repo_digests = lambda img: ["db@sha256:LOCAL"]
    o._get_remote_digest = lambda *a, **k: "sha256:MOVED"   # a real update
    o._list_remote_tags = lambda reg, repo: []
    o._get_image_size = lambda img: "1 GB"
    o._get_image_created = lambda img: "2026-01-01"
    o._get_image_version_label = lambda img: ""
    o.get_remote_image_meta = lambda *a, **k: {}
    o._registry_environment = lambda: ""
    o._parse_semver = lambda t: None
    return o, cfg


def _pending_after_check(o, cfg):
    with contextlib.redirect_stdout(io.StringIO()):
        o.check_all()
    if not os.path.exists(cfg.pending_file):
        return []
    with open(cfg.pending_file, encoding="utf-8") as f:
        return [u.get("name") for u in json.load(f)]


DAMAGE = {
    "truncated json": b'["db"',
    "a latin-1 byte": b'["M\xfcnster"]',
    "an object, not a list": b'{"db": true}',
    "null": b"null",
    "a bare string": b'"db"',
}

for label, body in DAMAGE.items():
    o, cfg = _checker(body)
    raised = None
    pinned = None
    try:
        pinned = o._get_pinned()
    except Exception as e:                                  # noqa: BLE001
        raised = e
    # Named, because the Latin-1 case failed exactly here: the method
    # did not answer wrongly, it did not answer at all.
    checks[f"{label}: reading the list answers instead of raising"] = (
        raised is None)
    if raised is not None:
        print(f"      → {label}: {type(raised).__name__}: {raised}")
    checks[f"{label}: …and every container counts as pinned"] = (
        pinned is EVERYTHING_PINNED and "db" in pinned)
    o, cfg = _checker(body)
    checks[f"{label}: …so nothing is queued for update"] = (
        _pending_after_check(o, cfg) == [])

# ── the counter-directions ───────────────────────────────────────────
# Each of them alone would be satisfied by a checker that pins
# everything always, or by one that never finds an update at all.

o, cfg = _checker(b'["db"]')
checks["a healthy list pins what it names"] = (
    o._get_pinned() == ["db"] and o._get_pinned() is not EVERYTHING_PINNED)
o, cfg = _checker(b'["db"]')
checks["…and that container is not queued"] = _pending_after_check(o, cfg) == []

o, cfg = _checker(b'["something-else"]')
checks["…while a container it does not name is left alone"] = (
    o._get_pinned() == ["something-else"])
o, cfg = _checker(b'["something-else"]')
checks["…and really is queued, so the check can find updates at all"] = (
    _pending_after_check(o, cfg) == ["db"])

o, cfg = _checker(None)
checks["no pin file at all means nothing was ever pinned"] = (
    o._get_pinned() == [] and o._get_pinned() is not EVERYTHING_PINNED)
o, cfg = _checker(None)
checks["…and the update is found"] = _pending_after_check(o, cfg) == ["db"]

o, cfg = _checker(b"[]")
checks["an empty list is a legitimate file, not damage"] = (
    o._get_pinned() == [] and o._get_pinned() is not EVERYTHING_PINNED)

# ── …and it has to SAY so ────────────────────────────────────────────
# Fail-closed stops every update on this host. That is the right
# direction and the wrong way to do it silently: the Web UI goes on
# listing containers, the next run finds nothing, `merge_pending` then
# drops the host's whole pending list, and the report says "all up to
# date". Two docstrings claimed the read "already prints a loud
# message" — it does, in ContainerStore, on a path the checker never
# takes (audit, 16.11.2026).
import contextlib  # noqa: E402
import io as _io  # noqa: E402


def _said(payload):
    """What reading this pin file prints on stdout."""
    o, _cfg = _checker(payload)
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        o._get_pinned()
    return buf.getvalue()


_damaged = _said(b'["db"')
checks["an unreadable pin list says the file could not be read"] = (
    "could not be read" in _damaged)
checks["…and names the file, so it can be found"] = (
    "pinned" in _damaged)
checks["…and says what it means: no update will run"] = (
    "no update will run" in _damaged)
# The shape check is the other way into EVERYTHING_PINNED and was the
# half with no `except` to hang a message on.
checks["a pin list of the wrong shape says it too"] = (
    "could not be read" in _said(b'{"db": true}'))
# Counter-direction. Without this, a `print` on every read — or one
# moved above the try — would pass everything above, and the log would
# cry damage on every healthy run until nobody read it any more.
checks["…while a healthy list says nothing at all"] = _said(b'["db"]') == ""
checks["…and neither does a missing one"] = _said(None) == ""
checks["…nor an empty one"] = _said(b"[]") == ""

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
