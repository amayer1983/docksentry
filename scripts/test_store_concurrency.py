#!/usr/bin/env python3
"""Two things the state files must survive: two writers, and damage.

Both were broken until 18.10.2026 and both were found by an audit, not
by this suite — which is the reason this file exists.

**Two writers.** `atomic_write_json` wrote to `<path>.tmp`, a name every
writer shared. Two threads opened the same temp file, interleaved their
`json.dump` output into it, and `os.replace` published the mixture. The
audit measured 7309 reads: 262 came back as invalid JSON and 75 came
back as *valid* JSON holding fields from both writes. The second kind is
the dangerous one — nothing raises.

**Damage.** A `pinned.json` that would not parse came back as `[]`, and
`[]` reads as "nothing is pinned". So a damaged pin file caused exactly
the thing a pin exists to prevent: `update_checker.py` skips pinned
containers, saw an empty list, and skipped nothing.

Put together those were one chain: two writers → damaged file → "nothing
pinned" → a pinned container updated unattended, silently.

Nothing here is stood in for. Real threads, real files, the real
`ContainerStore`. The only thing bounded is how long the writers run.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from container_store import (  # noqa: E402
    ContainerStore, HostScopedStore, atomic_write_json, path_lock)

checks = {}
tmpdir = tempfile.mkdtemp(prefix="ds-store-")

_FILES = ("pinned autoupdate update_windows ask_before_major trust_running "
          "cooldown protect_stop major_pending groups notes links").split()


def _store():
    """A real ContainerStore over a fresh set of files."""
    d = tempfile.mkdtemp(dir=tmpdir)
    cfg = types.SimpleNamespace(
        **{f"{n}_file": os.path.join(d, f"{n}.json") for n in _FILES})
    return ContainerStore(cfg), cfg


# ── two writers must not be able to publish a mixture ────────────────
# The counter-direction is covered by construction: the two payloads
# share no element, so ANY read containing both came from one write
# seeing the other's bytes. If the writers never ran, `reads` stays 0
# and the last check below fails — a stand-in that does nothing cannot
# pass this.

A = [f"a{i}" for i in range(80)]
B = [f"b{i}" for i in range(80)]
SET_A, SET_B = set(A), set(B)

path = os.path.join(tmpdir, "contended.json")
atomic_write_json(path, [])

state = {"stop": False, "reads": 0, "unparsable": 0, "mixed": 0,
         "writes": 0, "error": None}


def _writer(payload):
    try:
        while not state["stop"]:
            atomic_write_json(path, payload)
            state["writes"] += 1
    except Exception as e:                      # pragma: no cover
        state["error"] = f"writer: {e!r}"


def _reader():
    try:
        while not state["stop"]:
            try:
                with open(path) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, ValueError):
                state["unparsable"] += 1
                state["reads"] += 1
                continue
            except FileNotFoundError:
                # The target must never vanish: `os.replace` is atomic,
                # so a reader either sees the old file or the new one.
                state["error"] = "target disappeared mid-write"
                return
            state["reads"] += 1
            seen = set(data)
            if seen & SET_A and seen & SET_B:
                state["mixed"] += 1
    except Exception as e:                      # pragma: no cover
        state["error"] = f"reader: {e!r}"


threads = ([threading.Thread(target=_writer, args=(p,)) for p in (A, B)]
           + [threading.Thread(target=_reader) for _ in range(3)])
for t in threads:
    t.start()
time.sleep(2.0)
state["stop"] = True
for t in threads:
    t.join(timeout=10)

checks["two writers produce no unparsable file"] = state["unparsable"] == 0
checks["…and no file mixing both writes"] = state["mixed"] == 0
checks["…and the target never disappears"] = state["error"] is None
# Without this the two above pass on a probe that never ran.
checks["…and the contention actually happened"] = (
    state["reads"] > 500 and state["writes"] > 50)

# The temp files must not pile up either — one per write would fill the
# state directory, and `mkstemp` names are not reused.
_leftovers = [f for f in os.listdir(tmpdir)
              if f.startswith("contended.json.") and f.endswith(".tmp")]
checks["…and no temp file is left behind"] = _leftovers == []

# A failed write must clean up after itself too — the `except` path.
_fail_dir = tempfile.mkdtemp(dir=tmpdir)
_fail_path = os.path.join(_fail_dir, "x.json")
try:
    # sets are not JSON-serialisable: json.dump raises mid-write.
    atomic_write_json(_fail_path, {"a"})
except TypeError:
    pass
checks["a failed write leaves nothing behind"] = os.listdir(_fail_dir) == []

# ── a damaged protection list means protected, not unprotected ───────

for guard, filename, setter in (
        ("is_pinned", "pinned_file", "pin"),
        ("is_ask_before_major", "ask_before_major_file",
         "toggle_ask_before_major"),
        ("is_protect_stop", "protect_stop_file", "toggle_protect_stop")):
    st, cfg = _store()
    ask = getattr(st, guard)
    target = getattr(cfg, filename)
    getattr(st, setter)("db")

    checks[f"{guard}: healthy file answers truthfully"] = (
        ask("db") is True and ask("other") is False)

    raw = open(target).read()
    with open(target, "w") as f:
        f.write(raw[:len(raw) // 2])
    # Both directions: the named container AND one that was never in the
    # list. "Unreadable" must protect everything, not just what we happen
    # to remember — the list is precisely what we no longer know.
    checks[f"{guard}: a truncated file protects anyway"] = ask("db") is True
    checks[f"{guard}: …including a container not in the list"] = (
        ask("never-seen") is True)

    # A file holding valid JSON of the wrong shape is damage too.
    with open(target, "w") as f:
        f.write('{"not": "a list"}')
    checks[f"{guard}: a wrong-shaped file protects anyway"] = ask("db") is True

    # …and the counter-direction, which is what keeps the two above from
    # passing on a guard hardwired to True: no file at all is not damage,
    # it means nothing was ever set, and that must still answer no.
    os.unlink(target)
    checks[f"{guard}: a missing file is not damage"] = ask("db") is False

# An unreadable list must not be silent — the operator has to learn that
# an update is being held back and why.
st, cfg = _store()
st.save_pinned(["db"])
with open(cfg.pinned_file, "w") as f:
    f.write("{")
import io                                                     # noqa: E402
import contextlib                                             # noqa: E402
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    st.is_pinned("db")
checks["an unreadable list says so on stdout"] = (
    cfg.pinned_file in _out.getvalue())

# ── concurrent pins must not lose each other ─────────────────────────
# `pin()` is load-modify-save. Without a lock both threads read the same
# list and the second save drops the first name. No file damage, no
# error, one container silently unprotected.

st, cfg = _store()
NAMES = [f"c{i}" for i in range(60)]
errors = []


def _pin(chunk):
    try:
        for n in chunk:
            st.pin(n)
    except Exception as e:                      # pragma: no cover
        errors.append(repr(e))


half = len(NAMES) // 2
pinners = [threading.Thread(target=_pin, args=(NAMES[:half],)),
           threading.Thread(target=_pin, args=(NAMES[half:],))]
for t in pinners:
    t.start()
for t in pinners:
    t.join(timeout=30)

_final = st.get_pinned()
checks["concurrent pins all survive"] = sorted(_final) == sorted(NAMES)
checks["…without raising"] = errors == []
# Counter-direction: unpin has to still work, or "all survive" could be
# satisfied by a store that never removes anything.
st.unpin(NAMES[0])
checks["…and unpin still removes"] = NAMES[0] not in st.get_pinned()

# ── the per-host view locks the same path as the store ───────────────
# `HostScopedStore.save_pinned` reads the whole list, keeps the other
# hosts' entries and writes it back. If it took a different lock than
# `ContainerStore.pin`, two hosts saving at once would drop one host's
# entries entirely — the bug this guards is "host B vanished".

st, cfg = _store()
views = [HostScopedStore(st, f"srv{i}") for i in range(4)]


def _save(view, names):
    try:
        for i in range(25):
            view.save_pinned(names[:i + 1])
    except Exception as e:                      # pragma: no cover
        errors.append(repr(e))


hosts = [threading.Thread(target=_save, args=(v, [f"c{i}" for i in range(25)]))
         for v in views]
for t in hosts:
    t.start()
for t in hosts:
    t.join(timeout=30)

checks["every host keeps its own entries"] = all(
    len(v.get_pinned()) == 25 for v in views)
checks["…and no host's entries leak into another's"] = all(
    sorted(v.get_pinned()) == sorted(f"c{i}" for i in range(25))
    for v in views)

# The lock identity itself, stated rather than inferred: the view and
# the store must get the SAME object, or none of the above means
# anything on a multi-host install.
checks["view and store share one lock object"] = (
    path_lock(cfg.pinned_file) is path_lock(st.pinned_file))
checks["…and two different files do not"] = (
    path_lock(cfg.pinned_file) is not path_lock(cfg.autoupdate_file))

shutil.rmtree(tmpdir, ignore_errors=True)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print(f"\n  gemessen: {state['reads']} Lesevorgänge, {state['writes']} Schreibvorgänge, "
      f"{state['unparsable']} kaputt, {state['mixed']} gemischt")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
