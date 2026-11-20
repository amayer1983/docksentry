#!/usr/bin/env python3
"""A tapped or queued single update is reported once per channel.

`test_no_double_update_report.py` holds that for a batch: the plugins
get a card per container, Telegram gets the list. It never reaches the
single update, which reports on its own — a result line in the chat and
one `send_update_result` for the other channels. That path is being cut
in two (Q11a: the engine runs the keys, Telegram says how it went), and
then moved onto the shared batch (Q11c), which reports through the
broadcast seam. Either move can lose a report or send one twice, and
neither would show anywhere else in the suite.

So this drives the real thing: a real `TelegramBot` from the callback
matrix's fixture, the real `_handle_callback` for `update_one:`, the
real drain, with a recording notifier behind a real `Broadcast` on both
the bot and the engine. Counted per container: the update results the
other channels heard, and the result lines Telegram said. Also pinned:
the "Updating …" line goes out before the update runs, not after it —
a report that waits for the end tells you nothing while it runs.
"""
import contextlib
import io
import os
import re
import sys

import test_telegram_callback_matrix as t3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from broadcast import Broadcast  # noqa: E402

m = t3.m
checks = {}


class _Rec:
    """The other channels: Discord, webhook, e-mail, ntfy."""

    def __init__(self):
        self.results = []
        self.messages = []

    def has_channels(self):
        return True

    def send_update_result(self, name, image, success, detail="",
                           source_url=""):
        self.results.append((name, image, success, detail))

    def send_message(self, text, skip=(), urgent=False):
        self.messages.append(text)

    def __getattr__(self, name):
        # Everything else the bot tells the plugins (the "updates
        # available" card, …) is taken and kept, not counted here.
        if name.startswith("send_"):
            return lambda *a, **k: self.messages.append((name, a))
        raise AttributeError(name)


def _world(extra_pending=()):
    """A multi-host row with the recorder wired in the way main.py does."""
    import hosts as hostslib
    hostslib.forget_unreachable()
    m.EVENTS.clear()
    del m.STARTED[:]
    del m.THREAD_ERRORS[:]
    row = t3.Row("multi")
    rec = _Rec()
    row.bot.notifier = rec
    seam = Broadcast(telegram=row.bot, notifier=rec, log=lambda *a: None)
    row.bot.broadcast = seam
    row.bot.engine.broadcast = seam
    for args in extra_pending:
        row.do(("add_pending",) + args)
    # Where the update runs, in the same log as what Telegram says, so
    # the order of the two can be read off.
    cls = type(row.checker)
    orig = cls.update_container

    def update_container(self, name, image, *a, **kw):
        row.log.append(("ran", f"{self.backend.name}:{name}", ""))
        return orig(self, name, image, *a, **kw)
    cls.update_container = update_container
    row._restore = lambda: setattr(cls, "update_container", orig)
    del row.log[:]
    return row, rec


def _drive(row, steps):
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        for step in steps:
            row.do(step)
            t3._join()


def _said(row):
    return [body for kind, body, _ in row.log if kind == "send"]


_LINE = re.compile(r"^[✅❌] (?:`([^`]+)`|\[([^\]]+)\]\([^)]*\))"
                   r"( @[\w-]+)?: ")


def _results(said, label):
    """Telegram's result lines for one container, as `_display_name`
    writes them: `✅ `web`: …` or `✅ [web](link): …` locally, with
    ` @nas` after the name remotely. `label` is `web` or `web @nas`."""
    out = []
    for s in said:
        mt = _LINE.match(s)
        if mt and (mt.group(1) or mt.group(2)) + (mt.group(3) or "") == label:
            out.append(s)
    return out


def _done(row):
    row._restore()
    import shutil
    shutil.rmtree(row.data, ignore_errors=True)


# ── a tap ─────────────────────────────────────────────────────────────
row, rec = _world()
_drive(row, [("notify", None), ("tap", "update_one:web")])
said = _said(row)
checks["a tap: the other channels hear the result once"] = (
    [r[0] for r in rec.results] == ["web"])
checks["…as a success, with the image and the checker's words"] = (
    rec.results[:1] == [("web", "nginx:1.25", True, "updated (stand-in)")])
checks["…and Telegram says it once"] = (
    len(_results(said, "web")) == 1)
checks["…and the summary is not sent to the other channels as well"] = (
    not any("stand-in" in str(t) for t in rec.messages))
_all_done = "All updates complete"
checks["…and with another entry still pending, nobody hears 'all done'"] = (
    not any(_all_done in s for s in said))
_kinds = [(k, b) for k, b, _ in row.log if k in ("send", "ran")]
_start = next((i for i, (k, b) in enumerate(_kinds)
               if k == "send" and b.startswith("⏳")), None)
_ran = next((i for i, (k, b) in enumerate(_kinds) if k == "ran"), None)
_line = next((i for i, (k, b) in enumerate(_kinds)
              if k == "send" and b.startswith("✅")), None)
checks["…and 'Updating' is said before the update runs, the result after"] = (
    None not in (_start, _ran, _line) and _start < _ran < _line)
_done(row)

# ── a remote tap: the other channels hear the container's own name ────
row, rec = _world()
_drive(row, [("notify", None), ("tap", "update_one:nas/web")])
said = _said(row)
checks["a remote tap: one result to the other channels"] = (
    [r[0] for r in rec.results] == ["web"])
checks["…and one Telegram line naming the host"] = (
    len(_results(said, "web @nas")) == 1
    and not _results(said, "web"))
_done(row)

# ── queued entries, worked off after the tap that held the lock ──────
row, rec = _world(extra_pending=[("db", "postgres:16", "local")])
_drive(row, [("lock",), ("tap", "update_one:web"),
             ("tap", "update_one:nas/web"), ("unlock",),
             ("tap", "update_one:db")])
said = _said(row)
_names = [r[0] for r in rec.results]
checks["drained: every container is reported to the other channels once"] = (
    sorted(_names) == ["db", "web", "web"])
checks["…in the order they ran"] = _names == ["db", "web", "web"]
checks["…and Telegram says each of them once"] = (
    len(_results(said, "db")) == 1 and len(_results(said, "web")) == 1
    and len(_results(said, "web @nas")) == 1)
checks["…and 'all done' is said once, after the last of them"] = (
    [s for s in said if _all_done in s] == [said[-1]])
checks["…and the queue is worked off"] = (
    row.bot.engine.queued_updates() == []
    and [b for k, b, _ in row.log if k == "ran"]
    == ["local:db", "local:web", "nas:web"])
_done(row)

# ── queued, worked off by the poll loop's sweep ──────────────────────
row, rec = _world()
_drive(row, [("lock",), ("tap", "update_one:web"), ("unlock",)])
with contextlib.redirect_stdout(io.StringIO()):
    row.bot._sweep_update_queue(row.checker)
said = _said(row)
checks["swept: the queued update is reported to the other channels once"] = (
    [r[0] for r in rec.results] == ["web"])
checks["…and Telegram says it once"] = len(_results(said, "web")) == 1
_done(row)

# ── a failure is reported once too, as a failure ─────────────────────
row, rec = _world()
_cls = type(row.checker)
_ok = _cls.update_container


def _fails(self, name, image, *a, **kw):
    _ok(self, name, image, *a, **kw)
    return False, "pull failed (stand-in)"


_cls.update_container = _fails
_drive(row, [("notify", None), ("tap", "update_one:web")])
_cls.update_container = _ok
said = _said(row)
checks["a failed update: one failure to the other channels"] = (
    rec.results == [("web", "nginx:1.25", False, "pull failed (stand-in)")])
checks["…and one ❌ line on Telegram"] = (
    [s.split(": ", 1)[1] for s in _results(said, "web")]
    == ["pull failed (stand-in)"]
    and _results(said, "web")[0].startswith("❌"))
_done(row)

# ── an update that raises is a failure, reported once ────────────────
row, rec = _world()
_cls = type(row.checker)
_ok = _cls.update_container


def _raises(self, name, image, *a, **kw):
    raise RuntimeError("socket gone")


_cls.update_container = _raises
_drive(row, [("notify", None), ("tap", "update_one:web")])
_cls.update_container = _ok
said = _said(row)
checks["an update that raises: one failure to the other channels"] = (
    rec.results == [("web", "nginx:1.25", False, "socket gone")])
checks["…and one ❌ line on Telegram"] = (
    [s.split(": ", 1)[1] for s in _results(said, "web")] == ["socket gone"]
    and _results(said, "web")[0].startswith("❌"))
checks["…and the entry still leaves the pending list"] = (
    "web@local" not in (row.state()["pending"] or []))
_done(row)

# ── a failed tap and its queued group-mate: measured, not promised ──
# The drain drops a failed container's queued group-mates (held at the
# engine in test_update_queue, with a stand-in group lookup). Through
# Telegram it never fires: `TelegramBot._group_of` reads the store's
# `get_group_for_container` as a dict, the store returns `(gid, group)`,
# the `.get` raises, and the `except` makes every answer None. Measured
# 20.11.2026 on this fixture: `web` fails, its group-mate `db` still
# runs. That has been so since the queue arrived (f9f32f5); this slice
# moves code and changes nothing, so it is pinned as it is. Whoever
# fixes `_group_of` flips these two checks.
row, rec = _world(extra_pending=[("db", "postgres:16", "local")])
_cls = type(row.checker)
_ok = _cls.update_container


def _web_fails(self, name, image, *a, **kw):
    r = _ok(self, name, image, *a, **kw)
    return (False, "pull failed (stand-in)") if name == "web" else r


_cls.update_container = _web_fails
_drive(row, [("group", "local", "g1", ["web", "db"]), ("lock",),
             ("tap", "update_one:db"), ("unlock",),
             ("tap", "update_one:web")])
_cls.update_container = _ok
said = _said(row)
checks["known defect: Telegram's group lookup finds no group (store gives a tuple)"] = (
    row.bot._group_of("db") is None
    and row.bot._store_for(None).get_group_for_container("db")[0] == "g1")
checks["known defect: so a failed tap's queued group-mate still runs"] = (
    [b for k, b, _ in row.log if k == "ran"] == ["local:web", "local:db"]
    and not any("g1" in s for s in said))
checks["…and each of the two is reported once, the failure as one"] = (
    [r[:3] for r in rec.results] == [("web", "nginx:1.25", False),
                                     ("db", "postgres:16", True)])
_done(row)

# ── nothing ran, nothing reported ────────────────────────────────────
row, rec = _world()
_drive(row, [("tap", "update_one:db")])
checks["a key that is not pending reports nothing to anyone"] = (
    rec.results == [] and not [s for s in _said(row)
                               if s.startswith(("✅", "❌"))])
_done(row)

# ── a pending file that cannot be rewritten (a full disk) ────────────
# The update ran; the report of it must not depend on the file write
# that comes after it. The write's error still surfaces — the drain
# says it — but after the result, not instead of it.
import pending as _pending_mod  # noqa: E402

row, rec = _world()
_write = _pending_mod.write


def _enospc(path, entries):
    raise OSError(28, "No space left on device")


_drive(row, [("notify", None)])
_pending_mod.write = _enospc
try:
    _drive(row, [("tap", "update_one:web")])
finally:
    _pending_mod.write = _write
said = _said(row)
checks["a full disk after the update: the result still reaches everyone"] = (
    rec.results == [("web", "nginx:1.25", True, "updated (stand-in)")]
    and len(_results(said, "web")) == 1)
checks["…and the lock is not left held"] = not row.bot._update_lock.locked()
_done(row)

# ── the engine's half, on its own ────────────────────────────────────
# `run_keys` is what Web and Discord will call (Q13): it must answer in
# data, so they can say it in their own words.
row, rec = _world()
eng = row.bot.engine
pf = row.bot.config.pending_file


class _Ck:
    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def update_container(self, name, image, **kw):
        self.seen.append((name, image, dict(eng.updating),
                          eng._update_lock.locked()))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _rk(keys, ck, **kw):
    """`eng.run_keys(…)`, or a result no check accepts when there is none
    (or it raised) — red, not a traceback."""
    if not hasattr(eng, "run_keys"):
        return [("no run_keys",) * 5], None
    with contextlib.redirect_stdout(io.StringIO()):
        return eng.run_keys(keys, checker_for=lambda k: ck,
                            group_of=lambda k: f"g-{k}",
                            enrich=lambda us: None, pending_file=pf, **kw)


_pending_mod.write(pf, [{"name": "web", "image": "nginx:1.25",
                         "new_version": "1.26.0"},
                        {"name": "web", "image": "nginx:1.25", "host": "nas"}])
ck = _Ck((True, "ok"))
_started = []
res, done = _rk(["web"], ck, starting=lambda k, t: _started.append(
    (k, ck.seen[:])))
checks["run_keys: a success is (key, entry, True, msg, no group)"] = (
    len(res) == 1 and res[0][0] == "web"
    and (res[0][1] or {}).get("name") == "web"
    and res[0][2:] == (True, "ok", None))
checks["…runs on the checker it is handed, with the entry's image"] = (
    [x[:2] for x in ck.seen] == [("web", "nginx:1.25")])
checks["…says the container is in flight while it runs, and not after"] = (
    [x[2] for x in ck.seen] == [{"web": "1.26.0"}] and eng.updating == {})
checks["…calls `starting` before the update, not after"] = (
    _started == [("web", [])])
checks["…takes the entry out of the file, the other host's survives"] = (
    [(_pending_mod.key_of(u)) for u in _pending_mod.read(pf)]
    == [("nas", "web")])
checks["…and is not all done while another entry is pending"] = done is False
checks["…and leaves the lock to its caller"] = (
    [x[3] for x in ck.seen] == [False] and not eng._update_lock.locked())

ck = _Ck((False, "pull failed"))
res, done = _rk(["nas/web"], ck)
checks["run_keys: a failure names the group it takes down"] = (
    [r[2:] for r in res] == [(False, "pull failed", "g-nas/web")])
checks["…and the last entry out is all done, leaving []"] = (
    done is True and _pending_mod.load(pf) == [])

_pending_mod.write(pf, [{"name": "db", "image": "postgres:16"}])
ck = _Ck(RuntimeError("x" * 300))
_rep = []
res, done = _rk(["db"], ck, report=lambda *a: _rep.append(a[2:]))
checks["run_keys: an update that raises is a failure, clipped to 200"] = (
    [r[2:] for r in res] == [(False, "x" * 200, "g-db")] and done is True)
checks["…and is reported once, as that failure"] = _rep == [(False, "x" * 200)]

ck = _Ck((True, "ok"))
_rep = []
res, done = _rk(["db"], ck, report=lambda *a: _rep.append(a))
checks["run_keys: a key that is not pending is 'not_pending', nothing ran"] = (
    res == [("db", None, None, "not_pending", None)] and ck.seen == []
    and _rep == [] and done is False)
os.unlink(pf)
res, done = _rk(["db"], ck)
checks["run_keys: no file is 'no_pending_updates', nothing ran"] = (
    res == [("db", None, None, "no_pending_updates", None)]
    and ck.seen == [] and done is False and not os.path.exists(pf))
_done(row)

for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
bad = [k for k, v in checks.items() if not v]
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
