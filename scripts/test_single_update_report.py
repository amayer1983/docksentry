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
    # The engine's waits (a cooldown, a group's pause) land in the same
    # log, on a clock that stands still: no second is sat out, and the
    # wait is the whole configured one.
    row.bot.engine._sleep = lambda s: row.log.append(("wait", f"{s:g}", ""))
    row.bot.engine._clock = lambda: 1000.0
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

# ── a failed tap takes its queued group-mate with it (Q11c) ─────────
# The drain drops a failed container's queued group-mates (held at the
# engine in test_update_queue, with a stand-in group lookup). Through
# Telegram it never fired until Q11c: `TelegramBot._group_of` read the
# store's `get_group_for_container` as a dict, the store returns `(gid,
# group)`, the `.get` raised and the `except` made every answer None —
# measured 20.11.2026, `web` failed and its group-mate `db` still ran.
# `web` is the group's head with restart_dependents on, so the batch
# also kicks `db` back onto the rolled-back head, as "Update all" does.
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
checks["a failed tap: Telegram's group lookup finds the container's group"] = (
    row.bot._group_of("db") == "g1" and row.bot._group_of("web") == "g1"
    and row.bot._group_of("nas/web") is None)
checks["…so its queued group-mate does not run, and the chat says why"] = (
    [b for k, b, _ in row.log if k == "ran"] == ["local:web"]
    and any("g1" in s and "`db`" in s for s in said)
    and row.bot.engine.queued_updates() == [])
checks["…and only the failure is reported, once"] = (
    [r[:3] for r in rec.results] == [("web", "nginx:1.25", False)])
checks["…and the head's dependents are kicked back, said on Telegram once"] = (
    len([s for s in said if s.startswith("🔁 head rollback")]) == 1)
checks["…and the group-mate is still pending"] = (
    "db@local" in (row.state()["pending"] or []))
_done(row)

# ── a remote container's group is looked up on its own host ─────────
row, rec = _world()
row.do(("group", "nas", "g2", ["web", "media"]))
checks["a remote container's group is looked up on its own host"] = (
    row.bot._group_of("nas/web") == "g2"
    and row.bot._group_of("nas/media") == "g2"
    and row.bot._group_of("web") is None)
_done(row)

# ── a cooldown holds the queued container behind it (Q11c) ──────────
# "Update all" waits a container's cooldown before the next recreate.
# A tap with something queued behind it does the same now: the wait is
# carried from the container that ran to the next one that runs.
row, rec = _world(extra_pending=[("db", "postgres:16", "local")])
row.bot._store_for("local").set_cooldown("web", 60)
_wait_rec = row.bot.engine._sleep
_during = []


def _wait_and_tap(s):
    # A tap on the waiting container while it waits: it is off the
    # queue and this run's, so it is "the same one", not queued again.
    _during.append(dict(row.bot.engine.updating))
    row.bot._run_single_update(row.checker, "db")
    return _wait_rec(s)


row.bot.engine._sleep = _wait_and_tap
_drive(row, [("lock",), ("tap", "update_one:db"), ("unlock",),
             ("tap", "update_one:web")])
_order = [(k, b) for k, b, _ in row.log if k in ("ran", "wait")]
checks["a cooldown container: the queued one waits its cooldown first"] = (
    _order == [("ran", "local:web"), ("wait", "60"), ("ran", "local:db")])
_kinds = [(k, b) for k, b, _ in row.log if k in ("send", "wait")]
_upd_db = next((i for i, (k, b) in enumerate(_kinds)
                if k == "send" and b.startswith("⏳ Updating `db`")), None)
_wait_at = next((i for i, (k, b) in enumerate(_kinds) if k == "wait"), None)
# Said before the wait, not after it: a cooldown can be minutes, and
# until then the toast was all the chat had to show for the tap.
checks["…and 'Updating' is said before it waits, not after"] = (
    None not in (_upd_db, _wait_at) and _upd_db < _wait_at)
checks["…and a tap on it while it waits is 'the same one', not queued"] = (
    _during == [{"db": "1.1.0"}]
    and any("same one" in s for s in _said(row))
    and row.bot.engine.queued_updates() == [])
checks["…and each is reported once"] = (
    [r[0] for r in rec.results] == ["web", "db"]
    and len(_results(_said(row), "web")) == 1
    and len(_results(_said(row), "db")) == 1)
_done(row)

row, rec = _world()
row.bot._store_for("local").set_cooldown("web", 60)
_drive(row, [("notify", None), ("tap", "update_one:web")])
checks["a cooldown container with nothing after it: nobody waits"] = (
    [k for k, b, _ in row.log if k == "wait"] == []
    and [b for k, b, _ in row.log if k == "ran"] == ["local:web"])
_done(row)

# ── a queued group-mate gets the group's pause (Q11c) ───────────────
row, rec = _world(extra_pending=[("db", "postgres:16", "local")])
_drive(row, [("group", "local", "g1", ["web", "db"]), ("lock",),
             ("tap", "update_one:db"), ("unlock",),
             ("tap", "update_one:web")])
said = _said(row)
_order = [(k, b) for k, b, _ in row.log if k in ("ran", "wait")]
checks["a queued group-mate: it waits the group's pause after the head"] = (
    _order == [("ran", "local:web"), ("wait", "5"), ("ran", "local:db")])
checks["…and the head's dependents are restarted, said once"] = (
    [s for s in said if s.startswith("🔁")]
    == ["🔁 `web` dependents restarted: `db`"])
checks["…and each container is reported once, everywhere"] = (
    [r[0] for r in rec.results] == ["web", "db"]
    and len(_results(said, "web")) == 1 and len(_results(said, "db")) == 1)
_done(row)

# ── a tap that runs out of disk gets the host's cleanup (Q11c) ──────
row, rec = _world()
_cls = type(row.checker)
_ok = _cls.update_container


def _full(self, name, image, *a, **kw):
    _ok(self, name, image, *a, **kw)
    return False, "write /var/lib: no space left on device"


_cls.update_container = _full
_drive(row, [("notify", None), ("tap", "update_one:nas/web")])
_cls.update_container = _ok
said = _said(row)
checks["out of disk on a tap: that host's images are pruned"] = (
    any(c[0][:2] == ["image", "prune"] for c in row.backends["nas"].calls)
    and not any(c[0][:2] == ["image", "prune"]
                for c in row.backends["local"].calls))
_fail_at = next((i for i, s in enumerate(said)
                 if s in _results(said, "web @nas")), None)
_clean_at = next((i for i, s in enumerate(said) if s.startswith("🧹 nas:")),
                 None)
checks["…and Telegram says the cleanup once, after the failure"] = (
    len([s for s in said if s.startswith("🧹 nas:")]) == 1
    and None not in (_fail_at, _clean_at) and _fail_at < _clean_at)
checks["…and the failure is reported once"] = (
    [r[:3] for r in rec.results] == [("web", "nginx:1.25", False)])
_done(row)

# ── a local disk over the warning line: no prune per single update ──
# "Update all" prunes once per batch when DISK_WARN_AUTO_CLEANUP is on
# and the local disk is over DISK_WARN_PERCENT. A single update never
# did that, and Q11c is not meant to start: one batch per key would
# prune once per tap and once per drained entry, and each result line
# would wait for a whole prune (measured on 3641bb7: three prunes and
# three 🧹 lines for a tap with two entries queued behind it).
row, rec = _world(extra_pending=[("db", "postgres:16", "local"),
                                 ("cache", "redis:7", "local")])
for _cfg in {id(c): c for c in (row.bot.config,
                                row.bot.engine.config)}.values():
    _cfg.disk_warn_auto_cleanup = True
    _cfg.disk_warn_percent = 85
_cls = type(row.checker)
_prunes = []
_du, _ci = _cls.get_disk_usage, _cls.cleanup_images
_cls.get_disk_usage = lambda self: (90, 1, 10)
_cls.cleanup_images = lambda self: (_prunes.append(self.backend.name)
                                    or (True, "freed 1 GB"))
try:
    _drive(row, [("lock",), ("tap", "update_one:db"),
                 ("tap", "update_one:cache"), ("unlock",),
                 ("tap", "update_one:web")])
finally:
    _cls.get_disk_usage, _cls.cleanup_images = _du, _ci
said = _said(row)
checks["a full-ish local disk: a tap and its queue run without a prune"] = (
    [b for k, b, _ in row.log if k == "ran"]
    == ["local:web", "local:db", "local:cache"] and _prunes == [])
checks["…and nothing says it cleaned between updates"] = (
    not any(s.startswith("🧹") for s in said))
_done(row)

# ── a tap on a sidecar is recreated against its owner's name (Q11c) ──
row, rec = _world()
_seen = []
_cls = type(row.checker)
_ok = _cls.update_container
_nt = _cls.netns_target_name


def _records(self, name, image, *a, **kw):
    _seen.append(kw.get("netns_name", "<not passed>"))
    return _ok(self, name, image, *a, **kw)


_cls.update_container = _records
_cls.netns_target_name = lambda self, name: "vpn" if name == "web" else None
_drive(row, [("notify", None), ("tap", "update_one:web")])
_cls.update_container = _ok
_cls.netns_target_name = _nt
checks["a tapped sidecar is recreated against its netns owner's name"] = (
    _seen == ["vpn"])
_done(row)

# ── a second tap while "all done" is being said ─────────────────────
# The tapped container is still the one in flight until that line is
# out: a second tap on it in that moment is "the same one", not a new
# job for the queue (claim_or_queue, 08.10.2026). Q11a first cleared
# the in-flight mark before the line went out; then the second tap
# queued, and after the release the drain said "was already updated
# while this one waited" (measured on 1041ac7). The second tap is made
# from inside send_message, in the first one's thread, so the moment is
# exact and not a race.
row, rec = _world()
_drive(row, [("notify", None), ("tap", "update_one:web")])
_send = row.bot.send_message
_second = []


def _send_and_tap(text, *a, **kw):
    if _all_done in text and not _second:
        _second.append(dict(row.bot.engine.updating))
        row.bot._run_single_update(row.checker, "nas/web")
    return _send(text, *a, **kw)


row.bot.send_message = _send_and_tap
del row.log[:]
_drive(row, [("tap", "update_one:nas/web")])
row.bot.send_message = _send
said = _said(row)
checks["a second tap during 'all done': the container is still in flight"] = (
    _second == [{"nas/web": "1.26.0"}])
checks["…so it is answered 'the same one', not queued"] = (
    any("same one" in s for s in said)
    and not any("queued" in s for s in said)
    and row.bot.engine.queued_updates() == [])
checks["…and nothing says it was already updated while it waited"] = (
    not any("while this one waited" in s for s in said))
_done(row)

# ── nothing ran, nothing reported ────────────────────────────────────
row, rec = _world()
_drive(row, [("tap", "update_one:db")])
checks["a key that is not pending reports nothing to anyone"] = (
    rec.results == [] and not [s for s in _said(row)
                               if s.startswith(("✅", "❌"))])
_done(row)

# ── a queued key whose host has left the registry (Q11b) ─────────────
# A button from before `gone` was dropped from DOCKER_HOSTS, tapped while
# another update held the lock: it waits in the queue and comes up in the
# drain after the local tap. Before Q11b it ran on the local checker and
# updated the local `web` a second time (the callback matrix's "update_one
# gone host" rows measured the tap; this is the queued half).
row, rec = _world(extra_pending=[("web", "nginx:1.25", "gone")])
_drive(row, [("lock",), ("tap", "update_one:gone/web"), ("unlock",),
             ("tap", "update_one:web")])
said = _said(row)
checks["queued, host gone: only the tapped local container runs"] = (
    [b for k, b, _ in row.log if k == "ran"] == ["local:web"])
checks["…the drain says the host is unknown"] = any(
    s.startswith("❌ Unknown host `gone`") for s in said)
checks["…nobody hears a result for it"] = (
    [r[0] for r in rec.results] == ["web"]
    and not _results(said, "web @gone"))
checks["…and its entry stays in the file, the queue is empty"] = (
    "web@gone" in (row.state()["pending"] or [])
    and row.bot.engine.queued_updates() == [])
_done(row)

# ── a pending file that cannot be rewritten (a full disk) ────────────
# The update ran; the report of it must not depend on the file write
# that comes after it. Nothing in the chat says the write's error: on a
# tap the OSError ends the callback thread before any drain, and the
# entry stays in the file (measured on 1041ac7^ and after, the same).
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

    def netns_target_name(self, name):
        return None

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
        try:
            return eng.run_keys(keys, checker_for=lambda k: ck,
                                group_of=lambda k: f"g-{k}",
                                enrich=lambda us: None, pending_file=pf,
                                **kw)
        except TypeError as e:
            # A hook this run_keys does not take yet.
            return [("TypeError: " + str(e),) * 5], None


_pending_mod.write(pf, [{"name": "web", "image": "nginx:1.25",
                         "new_version": "1.26.0"},
                        {"name": "web", "image": "nginx:1.25", "host": "nas"}])
ck = _Ck((True, "ok"))
_started = []
_dones = []
res, done = _rk(["web"], ck, starting=lambda k, t: _started.append(
    (k, ck.seen[:])), done=lambda: _dones.append(dict(eng.updating)))
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
checks["…and is not all done while another entry is pending"] = (
    done is False and _dones == [])
checks["…and leaves the lock to its caller"] = (
    [x[3] for x in ck.seen] == [False] and not eng._update_lock.locked())

ck = _Ck((False, "pull failed"))
res, done = _rk(["nas/web"], ck,
                done=lambda: _dones.append(dict(eng.updating)))
checks["run_keys: a failure names the group it takes down"] = (
    [r[2:] for r in res] == [(False, "pull failed", "g-nas/web")])
checks["…and the last entry out is all done, leaving []"] = (
    done is True and _pending_mod.load(pf) == [])
checks["…calls `done` once for it, while the key is still in flight"] = (
    len(_dones) == 1 and "nas/web" in _dones[0] and eng.updating == {})

_pending_mod.write(pf, [{"name": "db", "image": "postgres:16"}])
from errfmt import clip  # noqa: E402

_err = "a" * 300 + "z" * 300
ck = _Ck(RuntimeError(_err))
_rep = []
res, done = _rk(["db"], ck, report=lambda *a: _rep.append(a[2:]))
# Clipped the way the batch clips it ("Update all" always did; the
# single update cut at 200 characters until Q11c and lost the end).
checks["run_keys: an update that raises is a failure, clipped like the batch"] = (
    [r[2:] for r in res] == [(False, clip(_err), "g-db")] and done is True
    and clip(_err).endswith("z" * 20))
checks["…and is reported once, as that failure"] = _rep == [(False, clip(_err))]

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

# Q11b: a pending key whose host the registry does not know is refused
# right after its pending lookup, before anything is marked or run —
# whatever checker the caller hands over for it (Telegram's falls back to
# the local one). After the lookup on purpose: a key that is not pending
# still says so, whatever its host.
_gone = [{"name": "web", "image": "nginx:1.25", "host": "gone"}]
_pending_mod.write(pf, _gone)
_before = open(pf, "rb").read()
ck = _Ck((True, "ok"))
_marks, _rep, _started, _dones = [], [], [], []
_mark = eng.mark_updating
eng.mark_updating = lambda k, v: (_marks.append(k), _mark(k, v))
try:
    res, done = _rk(["gone/web"], ck, report=lambda *a: _rep.append(a),
                    starting=lambda *a: _started.append(a),
                    done=lambda: _dones.append(1))
finally:
    eng.mark_updating = _mark
checks["run_keys: a host the registry does not know is 'host_unknown'"] = (
    res == [("gone/web", None, None, "host_unknown", None)])
checks["…nothing ran, nothing was marked, said or reported"] = (
    ck.seen == [] and _marks == [] and _started == [] and _rep == []
    and _dones == [] and done is False)
checks["…and the file is left exactly as it was"] = (
    open(pf, "rb").read() == _before)
# With no registry at all (a single-host install that never set
# DOCKER_HOSTS) the local host is still known and nothing else is.
_hosts = eng.hosts
eng.hosts = None
_pending_mod.write(pf, _gone + [{"name": "db", "image": "postgres:16"}])
ck = _Ck((True, "ok"))
try:
    res, done = _rk(["gone/web", "db"], ck)
finally:
    eng.hosts = _hosts
checks["run_keys without a registry: a remote key is 'host_unknown'"] = (
    res[:1] == [("gone/web", None, None, "host_unknown", None)])
checks["…and a local key still runs"] = (
    [r[:1] + r[2:4] for r in res[1:]] == [("db", True, "ok")]
    and [x[:2] for x in ck.seen] == [("db", "postgres:16")])

# A checker the batch refuses (None: nobody can reach the host) runs
# nothing and leaves the entry, like a host the registry does not know.
_pending_mod.write(pf, [{"name": "db", "image": "postgres:16"}])
_before = open(pf, "rb").read()
_rep, _started, _marks = [], [], []
_mark = eng.mark_updating
eng.mark_updating = lambda k, v: (_marks.append(k), _mark(k, v))
with contextlib.redirect_stdout(io.StringIO()):
    try:
        res, done = eng.run_keys(["db"], checker_for=lambda k: None,
                                 enrich=lambda us: None, pending_file=pf,
                                 report=lambda *a: _rep.append(a),
                                 starting=lambda *a: _started.append(a))
    except Exception as e:                          # noqa: BLE001
        # Red, not a traceback.
        res, done = [("raised " + type(e).__name__,)], None
    finally:
        eng.mark_updating = _mark
checks["run_keys: a key whose checker is None is 'host_unknown'"] = (
    res == [("db", None, None, "host_unknown", None)] and _rep == []
    and done is False and open(pf, "rb").read() == _before
    and eng.updating == {})
# Answered before the mark and "Updating …": the batch would refuse the
# None checker too, but only after both (and the wait between them).
checks["…before anything is marked or said"] = (
    _marks == [] and _started == [])

# ── what a single update waits before it runs (Q11c) ────────────────
# The last recreate's cooldown, plus its group's pause when this one is
# in the same group on the same host — counted from when that recreate
# finished.
_store = row.bot._store_for("local")
_store.set_cooldown("web", 60)
row.do(("group", "local", "g1", ["web", "db"]))
row.do(("group", "nas", "g2", ["web", "media"]))


def _waits(last, host, name, now):
    got = []
    eng._last_recreate = last
    eng._clock = lambda: now
    eng._sleep = got.append
    with contextlib.redirect_stdout(io.StringIO()):
        eng._wait_after_last_recreate(host, name)
    return got


checks["the wait: the last recreate's cooldown, then its group's pause"] = (
    _waits(("local", "g1", "web", 1000.0), "local", "db", 1000.0)
    == [60, 5])
checks["…counted from when that recreate finished"] = (
    _waits(("local", "g1", "web", 1000.0), "local", "db", 1045.0)
    == [15, 5]
    and _waits(("local", "g1", "web", 1000.0), "local", "db", 1063.0)
    == [2])
checks["…and nothing once both have passed"] = (
    _waits(("local", "g1", "web", 1000.0), "local", "db", 1065.0) == [])
checks["…no pause for a container outside that group"] = (
    _waits(("local", "g1", "web", 1000.0), "local", "other", 1000.0)
    == [60])
checks["…nor for a container in another host's group"] = (
    _waits(("local", "g1", "web", 1000.0), "nas", "media", 1000.0)
    == [60])
checks["…and no wait at all with nothing run before"] = (
    _waits(None, "local", "db", 1000.0) == [])
# Group ids are global in the raw store and the per-host views only
# split the members, so one id can hold members on two hosts. Then the
# other host's view has a group of the same id with `media` in it, and
# only the host check keeps `media @nas` from waiting `web`'s pause —
# the batch keys its group gates on (host, id) for the same reason.
from container_store import host_key  # noqa: E402

eng.store.save_group("g3", "g3", ["web", host_key("nas", "media")],
                     wait_seconds=5)
checks["…nor for another host's member of a group id that spans hosts"] = (
    "media" in (row.bot._store_for("nas").get_groups().get("g3") or {})
    .get("containers", [])
    and _waits(("local", "g3", "web", 1000.0), "nas", "media", 1000.0)
    == [60])
_done(row)

# ── a host that stops answering (Q11c review) ───────────────────────
# A tap on `web @nas` once nas raises TimeoutExpired on every call, with
# the local `web` queued behind it. The batch's netns snapshot is the
# first thing that asks nas; it used to raise past the batch, so the tap
# said "Updating …" and then nothing, and the queue stayed. Now it is
# that container's failure: said, carded, out of the file, nothing
# recreated on nas, and the drain runs the queued one.
row, rec = _world()
_drive(row, [("notify", None)])
row.backends["nas"].mode = "dead"
del row.log[:]
del rec.results[:]
_drive(row, [("lock",), ("tap", "update_one:web"), ("unlock",),
             ("tap", "update_one:nas/web")])
said = _said(row)
_dead = _results(said, "web @nas")
checks["dead host: the tap ends in no crashed thread"] = (
    m.THREAD_ERRORS == [])
checks["…Telegram says one failure for web @nas, naming the timeout"] = (
    len(_dead) == 1 and _dead[0].startswith("❌") and "timed out" in _dead[0])
checks["…the other channels get its failure card, then the drained one"] = (
    [(r[0], r[2]) for r in rec.results] == [("web", False), ("web", True)])
checks["…nothing was recreated on nas"] = (
    not any(k == "ran" and b == "nas:web" for k, b, _ in row.log))
checks["…and the queued local web ran after it"] = (
    any(k == "ran" and b == "local:web" for k, b, _ in row.log)
    and len(_results(said, "web")) == 1
    and [k for k, _ in row.bot.engine.queued_updates()] == [])
checks["…both entries are out of the file"] = (row._pending() == [])
_done(row)

# The batch's side of it, with two entries on the dead host: a failure
# per container when asked for it (what run_keys asks), and the raise it
# has always been without (Update all, the scheduler, the Web UI; T5
# pins the Web UI's answer).
row, rec = _world()
row.backends["nas"].mode = "dead"
_nas_ck = next(h.checker for h in row.bot.engine.hosts if h.name == "nas")


def _dead_batch(**kw):
    batch = [{"name": "web", "image": "nginx:1.25", "host": "nas"},
             {"name": "media", "image": "jellyfin/jellyfin:10.9",
              "host": "nas"}]
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            lines, ok, _maj = row.bot.engine._process_update_batch(
                batch, _nas_ck, auto=False, enrich=lambda us: None, **kw)
        except Exception as e:                      # noqa: BLE001
            # Red, not a traceback.
            return ["raised " + type(e).__name__], None
    return lines, ok


_lines, _ok = _dead_batch(snapshot_fails_entry=True)
checks["dead host, batch asked to: one failure line per container"] = (
    len(_lines) == 2 and all(ln.startswith("❌") and "timed out" in ln
                             for ln in _lines) and _ok == 0)
checks["…and a failure card for each"] = (
    [(r[0], r[2]) for r in rec.results]
    == [("web", False), ("media", False)])
del rec.results[:]
checks["…and without it the batch raises, as it always has"] = (
    _dead_batch() == (["raised TimeoutExpired"], None) and rec.results == [])
_done(row)

# A link lookup that raises is logged and the update goes on without the
# link: a missing link is cosmetic. Until this follow-up it ended the tap
# with nothing said, the error went to the thread, both entries stayed
# in the file and the queued one was never drained (review, measured
# with the review's probe). Still looked up before "Updating …".
row, rec = _world()
_drive(row, [("notify", None)])
del row.log[:]
del rec.results[:]
_orig_enrich = row.bot._enrich_with_source_url


def _raising_enrich(updates):
    if any(u.get("host") == "nas" for u in updates):
        raise RuntimeError("link lookup failed (stand-in)")
    return _orig_enrich(updates)


row.bot._enrich_with_source_url = _raising_enrich
_sink = io.StringIO()
with contextlib.redirect_stdout(_sink), contextlib.redirect_stderr(_sink):
    for _step in [("lock",), ("tap", "update_one:web"), ("unlock",),
                  ("tap", "update_one:nas/web")]:
        row.do(_step)
        t3._join()
said = _said(row)
_nas = _results(said, "web @nas")
checks["raising link lookup: the tap ends in no crashed thread"] = (
    m.THREAD_ERRORS == [])
checks["…'Updating' goes out and web @nas is updated, without a link"] = (
    any(s.startswith("⏳") and "web @nas" in s for s in said)
    and any(k == "ran" and b == "nas:web" for k, b, _ in row.log)
    and _nas == [s for s in _nas if s.startswith("✅ `web` @nas: ")]
    and len(_nas) == 1)
checks["…the lookup's failure is in the log"] = (
    "link lookup failed (stand-in)" in _sink.getvalue())
checks["…the queued local web runs after it, and both leave the file"] = (
    any(k == "ran" and b == "local:web" for k, b, _ in row.log)
    and len(_results(said, "web")) == 1
    and [k for k, _ in row.bot.engine.queued_updates()] == []
    and row._pending() == [] and row.bot.engine.updating == {})
checks["…the other channels hear both results once"] = (
    [(r[0], r[2]) for r in rec.results] == [("web", True), ("web", True)])
_done(row)

# ── a head's cascade that raises after a good recreate (followup) ───
# The head's health check, the first thing the cascade asks, raises: the
# host stopped answering a moment after the recreate (TimeoutExpired
# from `_restart_count`). That came out of the batch's except after the
# ✅ had gone out: a second ❌ for the same container, a failure card
# after the success card, and its group-mate dropped from the queue
# (review, measured with the review's probe on e6e1617). The update ran
# and stays a success; the cascade goes on as it does for a head that is
# not healthy.
import subprocess  # noqa: E402


def _wh_raises(ch, name, max_starting=None, interval=10):
    m.EVENTS.append(f"wait_healthy@{ch.backend.name}:{name}")
    raise subprocess.TimeoutExpired(
        ["docker", "-H", "ssh://nas", "inspect", name], 10)


row, rec = _world()
_drive(row, [("group", "nas", "g", ["web", "media"]),
             ("add_pending", "media", "jellyfin/jellyfin:10.9", "nas"),
             ("notify", None)])
del row.log[:]
del rec.results[:]
# Each row has a checker class of its own; its stand-in is put back.
_cls = type(row.checker)
_wh_kept = _cls._wait_healthy
_cls._wait_healthy = _wh_raises
try:
    _drive(row, [("lock",), ("tap", "update_one:nas/media"), ("unlock",),
                 ("tap", "update_one:nas/web")])
finally:
    _cls._wait_healthy = _wh_kept
said = _said(row)
_web = _results(said, "web @nas")
checks["head's health check raises: one ✅ for web @nas, no ❌"] = (
    len(_web) == 1 and _web[0].startswith("✅"))
checks["…the other channels hear one success for it, no failure"] = (
    [(r[0], r[2]) for r in rec.results if r[0] == "web"] == [("web", True)])
checks["…its dependent is still put back, said once"] = (
    [s for s in said if s.startswith("🔁")]
    == ["🔁 `web` dependents restarted: `media`"])
checks["…the group is not skipped: the queued media runs after it"] = (
    not any(s.startswith("⚠️") for s in said)
    and [b for k, b, _ in row.log if k == "ran"] == ["nas:web", "nas:media"]
    and len(_results(said, "media @nas")) == 1
    and [k for k, _ in row.bot.engine.queued_updates()] == []
    and m.THREAD_ERRORS == [])
checks["…and only the untouched local web is left in the file"] = (
    [(u["name"], u.get("host")) for u in row._pending()]
    == [("web", "local")])
_done(row)


def _head_batch(row, **kw):
    """`web` alone in a batch, the head of local group g1 = [web, db]
    with restart_dependents, the way "Update all" runs it."""
    batch = [{"name": "web", "image": "nginx:1.25", "host": "local"}]
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            lines, ok, _maj = row.bot.engine._process_update_batch(
                batch, row.checker, auto=False, enrich=lambda us: None,
                **kw)
        except Exception as e:                      # noqa: BLE001
            # Red, not a traceback.
            return ["raised " + type(e).__name__], None
    return lines, ok


# The batch on its own, as "Update all" and the scheduler run it: the
# same raise came out of the same except there.
row, rec = _world()
_drive(row, [("group", "local", "g1", ["web", "db"])])
_cls = type(row.checker)
_wh_kept = _cls._wait_healthy
_cls._wait_healthy = _wh_raises
try:
    _lines, _ok = _head_batch(row)
finally:
    _cls._wait_healthy = _wh_kept
checks["batch, head's health check raises: ✅ and the cascade line only"] = (
    len(_lines) == 2 and _lines[0].startswith("✅ `web`: ")
    and _lines[1] == "🔁 `web` dependents restarted: `db`" and _ok == 1)
checks["…one success card, no failure card"] = (
    [(r[0], r[2]) for r in rec.results] == [("web", True)])
_done(row)

# A dependent whose fix raises something that is not a subprocess error
# (a CLI that is gone: FileNotFoundError, a backend's own RuntimeError)
# is that dependent's failure, with its reason, not the head's.
row, rec = _world()
_drive(row, [("group", "local", "g1", ["web", "db"])])
_lb = row.backends["local"]


def _restart_raises(name, *a, **kw):
    raise RuntimeError("restart blew up (stand-in)")


_lb.restart = _restart_raises
_lines, _ok = _head_batch(row)
checks["batch, a dependent's restart raises: the head stays ✅"] = (
    len(_lines) == 2 and _lines[0].startswith("✅ `web`: ") and _ok == 1
    and [(r[0], r[2]) for r in rec.results] == [("web", True)])
checks["…and the dependent is the failure, with its reason"] = (
    len(_lines) == 2 and _lines[1] == (
        "❌ `web` dependents: 1 FAILED\n"
        "   • `db`: restart blew up (stand-in)"))
_done(row)

# A head that failed: the rollback kicks its dependents. A health check
# raising there told the other channels a second time that it failed.
row, rec = _world()
_drive(row, [("group", "local", "g1", ["web", "db"])])


def _head_fails(self, name, image, *a, **kw):
    return False, "rolled back (stand-in)"


_cls = type(row.checker)
_wh_kept = _cls._wait_healthy
_cls_uc = _cls.update_container
_cls.update_container = _head_fails
_cls._wait_healthy = _wh_raises
try:
    _lines, _ok = _head_batch(row)
finally:
    _cls._wait_healthy = _wh_kept
    _cls.update_container = _cls_uc
checks["batch, failed head whose health check raises: one ❌, one card"] = (
    [ln for ln in _lines if ln.startswith("❌ `web`: ")]
    == ["❌ `web`: rolled back (stand-in)"]
    and [(r[0], r[2]) for r in rec.results] == [("web", False)])
checks["…and its dependents are still kicked"] = (
    any(ln == "🔁 head rollback — dependents kicked: "
        "🔁 `web` dependents restarted: `db`" for ln in _lines))
_done(row)

for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
bad = [k for k, v in checks.items() if not v]
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
