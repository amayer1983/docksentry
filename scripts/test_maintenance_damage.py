#!/usr/bin/env python3
"""A maintenance file of the wrong shape must not stop the updates.

`maintenance._read` returned whatever `json.load` gave it. Everything
downstream then does `state.get("until")` — and `null`, `[]`, `"on"`,
`5` and `[{"until": "forever"}]` are all valid JSON that answer `.get`
with an AttributeError. `UnicodeDecodeError` escaped the same way: it is
neither a JSONDecodeError nor an OSError, and the `open()` carried no
`encoding=`, so whether it raised at all followed the host's locale.

Eleven call sites across six modules ask this — the scheduler on every
tick (`scheduler.py:401`, `:482`, `:496`), Telegram, Discord, the Web UI
and the notifier. None of them catches it. So the failure was a quiet
one: the tick that died is the same tick that would have reported
anything, and the instance simply stopped updating.

Both reading functions go through `_read`, which is why the fix is one
place and not eleven — and why this drives both of them over every
shape rather than testing `_read` on its own.

Unreadable and wrong-shaped both have to mean "no maintenance". That is
the safe direction: guessing "yes" would stop updates on a damaged
file, which is the failure we are removing.
"""

import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import maintenance  # noqa: E402

checks = {}
root = tempfile.mkdtemp(prefix="ds-maint-")


def _cfg(body):
    """A config pointing at a file holding exactly `body` (bytes)."""
    d = tempfile.mkdtemp(dir=root)
    path = os.path.join(d, "maintenance.json")
    if body is not None:
        # bytes, not str: the Latin-1 case only exists at the byte level.
        with open(path, "wb") as f:
            f.write(body)
    return types.SimpleNamespace(maintenance_file=path)


# ── the damage ───────────────────────────────────────────────────────
DAMAGE = {
    "null": b"null",
    "a list": b"[]",
    "a bare string": b'"on"',
    "a number": b"5",
    "a list holding the right dict": b'[{"until": "forever"}]',
    "a latin-1 byte": b'{"until": "M\xfcnster"}',
    "truncated json": b"{nope",
}

for label, body in DAMAGE.items():
    cfg = _cfg(body)

    raised = None
    active = None
    try:
        active = maintenance.is_active(cfg)
    except Exception as e:
        raised = e
    checks[f"{label}: the scheduler's question is answered"] = raised is None
    # Naming the old failure: a check for "no exception" would pass for
    # the wrong reason if the module started swallowing everything.
    checks[f"{label}: …and not with AttributeError"] = not isinstance(
        raised, AttributeError)
    checks[f"{label}: …with 'no maintenance', so updates run"] = (
        active is False)

    raised2 = None
    state = None
    try:
        state = maintenance.get_state(cfg)
    except Exception as e:
        raised2 = e
    checks[f"{label}: the UI banner can be rendered too"] = raised2 is None
    checks[f"{label}: …and says inactive"] = state == {"active": False}

# A missing file has always been the normal "not in maintenance" case.
checks["a missing file is not damage"] = (
    maintenance.is_active(_cfg(None)) is False
    and maintenance.get_state(_cfg(None)) == {"active": False})

# ── the counter-directions ───────────────────────────────────────────
# Without these, every check above is satisfied by `is_active` simply
# returning False and `get_state` returning {"active": False}.

cfg = _cfg(b'{"until": "forever"}')
checks["an open-ended maintenance really is active"] = (
    maintenance.is_active(cfg) is True)
checks["…and the UI is told so"] = (
    maintenance.get_state(cfg)["active"] is True)

soon = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
cfg = _cfg(f'{{"until": "{soon}"}}'.encode())
checks["a window that has not ended yet is active"] = (
    maintenance.is_active(cfg) is True)

past = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
cfg = _cfg(f'{{"until": "{past}"}}'.encode())
checks["…and one that has ended is not"] = (
    maintenance.is_active(cfg) is False)

# An empty object is what `disable()` writes. It is a legitimate file.
cfg = _cfg(b"{}")
checks["an empty object is a legitimate file, not damage"] = (
    maintenance.is_active(cfg) is False
    and os.path.exists(cfg.maintenance_file))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
