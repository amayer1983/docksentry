#!/usr/bin/env python3
"""The queue around a self-update, and the drain's words (Q16h).

Measured on f6824c8 with the three channels on one engine, as main.py
wires them (scratchpad xq/probes p1, p3, p5):

  * A self-update that swaps took the queue with it, unnamed. A tap
    held the lock, Discord and the Web UI queued `web @nas` and `db`
    behind it, `/selfupdate` queued too; when the tap ended, its release
    started the self-update inside `after`, the helper launched, and the
    process would have been stopped with both still on the queue. Nobody
    was told. The same behind a scheduled pass that held the drainer.
  * While the helper was swapping, a tap was still answered "queued —
    position 2", for a queue the restart was about to throw away.
  * `selfupdate.start` gives its lock back by hand, so a self-update
    that found nothing left whatever had queued meanwhile for the next
    sweep (the poll loop, or the scheduler's tick 30–60 s later).
  * An entry that was updated by the batch it waited behind came off
    the queue as a nameless "⚠️ No pending updates found." — twice, for
    two entries, under three green results.
  * The drain named containers without their host (`web` for nas's) and
    told a Discord channel to "tap them again".

And from the Q16g review (scratchpad q16grr/probe/p2.py): a Web major
confirmation of a pinned container answered only in Telegram, the page
said nothing; a held major in Telegram lost its Confirm/Skip buttons; a
tap on a container whose netns snapshot had just timed out on a dead
host still asked that host for its labels (10 s more).

The self-update runs for real here — `start`, `run`, `swap` — with the
daemon stood in for: every `docker` call answers from a script, and the
30-second wait for the helper's stop waits on an event instead. The
helper's launch is where each row looks: what had been said, and what
was on the queue, the moment the helper would have started.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "app"))

import test_telegram_dispatch_matrix as m  # noqa: E402
import test_telegram_callback_matrix as t3  # noqa: E402
import test_discord_dispatch_matrix as t4  # noqa: E402
import test_web_action_matrix as t5  # noqa: E402
import test_queue_held_back  # noqa: E402,F401  (its pin/label steps)

t4.ENV.update(t5.ENV)

import selfupdate as _selfupdate  # noqa: E402

#: The real body. The matrix harnesses stand `selfupdate.run` in for a
#: line in their event log once a row is built; here it has to run.
REAL_RUN = _selfupdate.run

# ── the sentences, written by hand before the change ───────────────────
DROP = ("⚠️ Docksentry is updating itself and restarts now, so these "
        "queued updates did not run: {}. They stay in the update list — "
        "start them again once it's back.")
REFUSED_SU = ("⏳ Docksentry is updating itself right now — nothing was "
              "queued. Try again once it's back.")
ALREADY = ("✅ `{}` was already updated while this one waited. Nothing "
           "left to do.")
PENDING_UNREADABLE = ("⚠️ `{}` did not run from the queue: the list of "
                      "pending updates can't be read. The next check "
                      "writes it again.")
NAMELESS = "⚠️ No pending updates found."
HELD_PIN = "📌 `{}`: pinned — not updated."
HELD_EXCL = "🚫 `{}`: excluded from updates — not updated."
HELD_PIN_LABEL = "📌 `{}`: has a `docksentry.pin` label — not updated."


def P(name, host, img="nginx:1.25"):
    return {"name": name, "image": img, "host": host,
            "old_version": "1.0.0", "new_version": "1.1.0"}


# ── one engine, three channels ─────────────────────────────────────────
class World:
    """Telegram, Discord and the Web UI on one engine, as main.py wires
    them (T4's row plus T5's handler). Every channel's words are logged
    in order: `tg` for Telegram, `plug` for the notifier (Discord,
    webhooks), `card` for result cards, `toast` for callback answers."""

    def __init__(self, rows):
        import hosts as hostslib
        hostslib.forget_unreachable()
        m.EVENTS.clear()
        del m.STARTED[:]
        del m.THREAD_ERRORS[:]
        del t4.LOG[:]
        t5._patch()
        self.row = t4.Row("multi")
        self.tg, self.eng = self.row.tg, self.row.engine
        self.reg = self.eng.hosts
        self.eng.selfupdate_ctx = self.tg.selfupdate_ctx   # as main.py
        self.cfg = self.row.cfg
        import web_ui
        from audit import AuditLog
        from webauth import SessionStore
        w = t5.Row.__new__(t5.Row)
        w.handler = web_ui.create_handler(
            self.cfg, self.reg.local.checker, self.tg, self.eng.store,
            self.cfg.web_password or None, self.reg.local.backend, self.reg,
            restart_discord=lambda: (True, "", ""))
        w.server = types.SimpleNamespace(
            debug=False, audit=AuditLog(self.cfg), token_seen={},
            sessions=SessionStore(idle_seconds=3600, max_seconds=86400))
        w.cookie = ""
        self.web = w
        self.events = []
        self.ev_lock = threading.Lock()
        self.tg.answer_callback = lambda cid, text: self.log("toast", text)
        self.tg.remove_buttons = lambda *a: None
        self.tg.send_message = lambda text, reply_markup=None, auto=False, \
            _held=False: (self.log("tg", text), True)[1]
        self.tg.api_call = lambda method, data=None, *a, **k: {
            "ok": True, "result": {"message_id": 1}}
        n = self.tg.broadcast.notifier

        def res(name, image, success, detail="", source_url="", **k):
            self.log("card", f"{name} ok={success}")
        n.send_update_result = res
        n.send_message = lambda text, *a, **k: self.log("plug", text)
        n.has_channels = lambda: True
        self.updated = []
        cls = type(self.reg.local.checker)
        self.cls, self.orig_uc = cls, cls.update_container
        self.gate = None
        world = self

        def uc(ch, name, image, *a, **kw):
            key = f"{ch.backend.name}:{name}"
            world.updated.append(key)
            g = world.gate
            if g is not None and key == g[0]:
                world.gate = None
                g[1].set()
                g[2].wait(20)
            return world.orig_uc(ch, name, image, *a, **kw)
        cls.update_container = uc
        with open(self.cfg.pending_file, "w") as f:
            json.dump(rows, f)

    def close(self):
        self.cls.update_container = self.orig_uc
        shutil.rmtree(self.row.data, ignore_errors=True)

    def log(self, kind, text):
        with self.ev_lock:
            self.events.append((kind, str(text)))

    def said(self, kind=None, since=0):
        with self.ev_lock:
            return [t for k, t in self.events[since:]
                    if kind is None or k == kind]

    def block_on(self, key):
        entered, release = threading.Event(), threading.Event()
        self.gate = (key, entered, release)
        return entered, release

    def tg_tap(self, data):
        self.tg._handle_callback({"id": "cb", "data": data,
                                  "from": {"id": int(m.USER)},
                                  "message": {"message_id": 9,
                                              "chat": {"id": int(m.CHAT)}}},
                                 self.reg.local.checker)

    def tg_tap_join(self, data):
        k = len(m.STARTED)
        self.tg_tap(data)
        join_from(k)

    def discord(self, name, *opts):
        n, k = len(t4.LOG), len(m.STARTED)
        self.row.do(t4._c(name, *opts))
        join_from(k)
        said = [e[1][3].get("content", "") for e in t4.LOG[n:]
                if e[0] == "rest" and e[1][1] == "PATCH"
                and isinstance(e[1][3], dict)]
        return said[-1] if said else ""

    def web_post(self, path, *form):
        body = urllib.parse.urlencode(list(form)).encode()
        out = self.web.request("POST", path, body, {})
        loc = [x for x in out.decode(errors="replace").split("\r\n")
               if x.lower().startswith("location:")]
        return loc[0].split(":", 1)[1].strip() if loc else out[:120]

    def queue(self):
        return [k for k, _ in self.eng.queued_updates()]

    def pending(self):
        import pending
        return sorted(f"{u.get('name')}@{u.get('host') or 'local'}"
                      for u in (pending.read(self.cfg.pending_file) or []))

    def settle(self, t=15):
        end = time.time() + t
        while time.time() < end:
            t4._join()
            d = getattr(self.eng, "_drainer", None)
            if d is not None:
                d.join(5)
                continue
            if not m.STARTED:
                return True
        return False


# ── the daemon a self-update talks to, stood in for ─────────────────────
class SelfUpdateEnv:
    """Every `docker` call `selfupdate.run` and `swap` make answers from
    here; the helper's launch records what the world looked like at that
    moment, and the 30-second wait for the stop waits on `stopped`.
    `found`: the pull brought a new image. `launch_ok`: the helper
    started; "raise" makes its launch time out."""

    def __init__(self, world, found=True, launch_ok=True):
        self.world, self.found, self.launch_ok = world, found, launch_ok
        self.at_launch = None
        self.stopped = threading.Event()

    def _run(self, args, **kw):
        a = list(args)
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if a[:2] == ["docker", "inspect"]:
            new = "sha256:new" if self.found else "sha256:old"
            return types.SimpleNamespace(returncode=0, stderr="",
                                         stdout=f"{new}||2026-11-21")
        if a[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        if a[:3] == ["docker", "run", "-d"] and any(
                str(x).endswith("_updater") for x in a):
            w = self.world
            self.at_launch = {
                "queue": w.queue(), "said": w.said(),
                "swap": bool(w.eng._swap_in_flight)}
            if self.launch_ok == "raise":
                import subprocess
                raise subprocess.TimeoutExpired(a, 30)
            if not self.launch_ok:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr="helper refused")
        return ok

    def __enter__(self):
        import selfupdate
        from update_checker import UpdateChecker as UC
        self._saved = (selfupdate.subprocess, UC.inspect_self,
                       UC._image_config, UC._cgroup_version,
                       UC.image_version_label, time.sleep, selfupdate.run)
        selfupdate.run = REAL_RUN
        real_sp, real_sleep = selfupdate.subprocess, time.sleep
        selfupdate.subprocess = types.SimpleNamespace(
            run=self._run, SubprocessError=real_sp.SubprocessError,
            TimeoutExpired=real_sp.TimeoutExpired)
        UC.inspect_self = staticmethod(lambda *a, **k: {
            "Name": "/docksentry", "Image": "sha256:old",
            "Created": "2026-11-01T00:00:00Z",
            "Config": {"Image": "ghcr.io/x/docksentry:latest", "Env": [],
                       "Labels": {}},
            "HostConfig": {"Binds": [], "NetworkMode": "bridge"},
            "Mounts": [], "NetworkSettings": {"Networks": {}}})
        UC._image_config = staticmethod(lambda *a, **k: {})
        UC._cgroup_version = classmethod(lambda cls, backend=None: 2)
        UC.image_version_label = staticmethod(lambda *a, **k: "")
        stopped = self.stopped

        def sleep(seconds):
            # The helper stops this process inside the 30 s wait.
            if seconds >= 30:
                stopped.wait(10)
                return
            real_sleep(seconds)
        time.sleep = sleep
        return self

    def __exit__(self, *exc):
        import selfupdate
        from update_checker import UpdateChecker as UC
        self.stopped.set()
        (selfupdate.subprocess, ic, imc, cg, ivl, time.sleep,
         selfupdate.run) = self._saved
        UC.inspect_self, UC._image_config = ic, imc
        UC._cgroup_version, UC.image_version_label = cg, ivl
        return False


def join_from(k):
    """Join the threads started since `m.STARTED` had `k` entries, and
    only those: one held at a gate on purpose must not be waited for."""
    for th in list(m.STARTED[k:]):
        th.join(20)


def _has(lines, text):
    return any(text in x for x in lines)


# ── 1. the queue at the swap ───────────────────────────────────────────
def swap_checks():
    import selfupdate
    out = []
    # A: a tap holds; Discord and the Web UI queue behind it; /selfupdate
    # queues too; the tap ends and its release starts the self-update.
    w = World([P("web", "local"), P("web", "nas"),
               P("db", "local", "postgres:16")])
    try:
        with SelfUpdateEnv(w) as env:
            entered, release = w.block_on("local:web")
            w.tg_tap("update_one:web")
            entered.wait(10)
            w.discord("update", ("container", "web"), ("host", "nas"))
            w.web_post("/api/update", ("name", "db"))
            selfupdate.start(w.tg.selfupdate_ctx)
            queued = w.queue()
            release.set()
            end = time.time() + 10
            while env.at_launch is None and time.time() < end:
                time.sleep(0.05)
            at = env.at_launch or {}
            drop = DROP.format("`web @nas`, `db`")
            out.append(("swap behind a tap: before the helper starts, every "
                        "channel is told which queued updates did not run, "
                        "with their hosts",
                        queued == ["nas/web", "db"]
                        and _has([t for t in at.get("said", [])], drop)
                        and sum(drop in t for t in at.get("said", [])) == 2,
                        f"queued {queued!r}, said at launch "
                        f"{at.get('said')!r}"))
            out.append(("…and the queue is empty when the helper starts",
                        at.get("queue") == [],
                        f"queue at launch {at.get('queue')!r}"))
            out.append(("…the swap flag is up by then, so nothing can queue "
                        "in the launch's seconds",
                        at.get("swap") is True, f"at launch {at!r}"))
            out.append(("…nothing of it ran, and both stay pending",
                        w.updated == ["local:web"]
                        and w.pending() == ["db@local", "web@nas"],
                        f"updated {w.updated!r}, pending {w.pending()!r}"))
            # The swap is under way: every way in is refused, not queued.
            n0 = len(w.events)
            w.tg_tap_join("update_one:nas/web")
            tap = w.said("tg", n0)
            r_web = w.web_post("/api/update", ("name", "db"))
            r_bulk = w.web_post("/api/bulk", ("action", "update"),
                                ("names", "db,nas/web"))
            r_dc = w.discord("update", ("container", "web"), ("host", "nas"))
            n1 = len(w.events)
            w.tg_tap_join("update_all")
            all_tg, all_toast = w.said("tg", n1), w.said("toast", n1)
            out.append(("during the swap a tap is refused with the reason, "
                        "not queued",
                        _has(tap, REFUSED_SU) and not _has(tap, "queued —"),
                        f"said {tap!r}"))
            out.append(("…the Web row and bulk buttons say it on the page",
                        r_web == "/?update=restarting"
                        and r_bulk == "/?update=restarting",
                        f"row {r_web!r}, bulk {r_bulk!r}"))
            out.append(("…Discord /update says it",
                        REFUSED_SU in r_dc, f"discord {r_dc!r}"))
            out.append(("…Telegram 'Update all' says it once, its toast says "
                        "not started",
                        sum(REFUSED_SU in t for t in all_tg) == 1
                        and _has(all_toast, "Not started"),
                        f"said {all_tg!r}, toast {all_toast!r}"))
            out.append(("…and the queue stays empty through all of it",
                        w.queue() == [], f"queue {w.queue()!r}"))
            env.stopped.set()
            w.settle(10)
    finally:
        w.close()

    # B: a scheduled pass holds the drainer back; a Discord update queues
    # mid-pass; /selfupdate queues; the host's batch ends with the hand-over.
    w = World([P("web", "local"), P("web", "nas")])
    try:
        with SelfUpdateEnv(w) as env:
            w.eng.hold_drain()
            w.eng.claim_update()
            w.discord("update", ("container", "web"), ("host", "nas"))
            selfupdate.start(w.tg.selfupdate_ctx)
            th = threading.Thread(
                target=w.eng.release_update,
                kwargs={"after": w.tg._run_queued_selfupdate})
            th.start()
            end = time.time() + 10
            while env.at_launch is None and time.time() < end:
                time.sleep(0.05)
            at = env.at_launch or {}
            out.append(("swap behind a held scheduled pass: named before the "
                        "helper starts, queue empty",
                        _has(at.get("said", []), DROP.format("`web @nas`"))
                        and at.get("queue") == [],
                        f"at launch {at!r}"))
            env.stopped.set()
            th.join(10)
            w.eng.let_drain_go()
            w.settle(10)
    finally:
        w.close()

    # C: the helper does not start. The flag comes down again, the lock is
    # given back, and the next tap runs instead of being refused.
    w = World([P("web", "local"), P("web", "nas")])
    try:
        with SelfUpdateEnv(w, launch_ok=False):
            with contextlib.redirect_stdout(io.StringIO()):
                selfupdate.start(w.tg.selfupdate_ctx)
            flag, held = w.eng._swap_in_flight, w.eng.update_claimed
            n0 = len(w.events)
            w.tg_tap_join("update_one:web")
            w.settle(10)
            tap = w.said("tg", n0)
        out.append(("a helper that does not start: flag down, lock free, "
                    "the next tap runs",
                    flag is False and held is False
                    and w.updated == ["local:web"]
                    and not _has(tap, REFUSED_SU),
                    f"flag {flag!r}, held {held!r}, updated {w.updated!r}, "
                    f"said {tap!r}"))
    finally:
        w.close()

    # C2: the helper's launch times out. The same: flag down, lock free.
    w = World([P("web", "local")])
    try:
        with SelfUpdateEnv(w, launch_ok="raise"):
            raised = None
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    selfupdate.start(w.tg.selfupdate_ctx)
                except Exception as e:                  # noqa: BLE001
                    raised = type(e).__name__
            flag, held = w.eng._swap_in_flight, w.eng.update_claimed
        out.append(("a helper launch that times out: flag down, lock free",
                    raised == "TimeoutExpired" and flag is False
                    and held is False,
                    f"raised {raised!r}, flag {flag!r}, held {held!r}"))
    finally:
        w.close()

    # D: the other direction of the refusal, on the engine: no swap, a
    # busy lock queues as before.
    w = World([P("web", "local"), P("web", "nas")])
    try:
        w.eng.claim_update()
        _c, outcomes = w.eng.claim_or_queue_many(["web", "nas/web"])
        one = w.eng.claim_or_queue("db")
        w.eng.drop_queued_updates()
        w.eng._swap_in_flight = True
        _c2, outcomes2 = w.eng.claim_or_queue_many(["web", "nas/web"])
        one2 = w.eng.claim_or_queue("db")
        q2 = w.queue()
        w.eng._swap_in_flight = False
        w.eng._update_lock.release()
        out.append(("engine: busy without a swap still queues",
                    [a for _k, a, _p in outcomes] == ["update_queued"] * 2
                    and one[1] == "update_queued",
                    f"outcomes {outcomes!r}, one {one!r}"))
        out.append(("engine: busy during a swap refuses every key, queues "
                    "nothing",
                    [a for _k, a, _p in outcomes2]
                    == ["update_refused_selfupdate"] * 2
                    and one2[:2] == (False, "update_refused_selfupdate")
                    and q2 == [],
                    f"outcomes {outcomes2!r}, one {one2!r}, queue {q2!r}"))
    finally:
        w.close()
    return out


# ── 2. a self-update that finds nothing hands the queue on ──────────────
def handover_checks():
    import selfupdate
    out = []
    w = World([P("web", "local"), P("web", "nas")])
    try:
        with SelfUpdateEnv(w, found=False):
            # The self-update holds the lock while it pulls; two updates
            # queue meanwhile.
            in_pull, go = threading.Event(), threading.Event()
            real_run = selfupdate.subprocess.run

            def run(args, **kw):
                if list(args)[:2] == ["docker", "pull"]:
                    in_pull.set()
                    go.wait(10)
                return real_run(args, **kw)
            selfupdate.subprocess.run = run
            th = threading.Thread(target=selfupdate.start,
                                  args=(w.tg.selfupdate_ctx,))
            th.start()
            in_pull.wait(10)
            r_web = w.web_post("/api/update", ("name", "web"))
            w.discord("update", ("container", "web"), ("host", "nas"))
            queued = w.queue()
            go.set()
            th.join(10)
            # No sweep: only what the release itself starts.
            d = w.eng._drainer
            if d is not None:
                d.join(10)
            ran = list(w.updated)
        out.append(("a self-update that finds nothing: what queued during "
                    "it runs right after, without waiting for a sweep",
                    r_web.startswith("/?update=queued")
                    and queued == ["web", "nas/web"]
                    and sorted(ran) == ["local:web", "nas:web"],
                    f"web {r_web!r}, queued {queued!r}, ran {ran!r}"))
        out.append(("…and the lock is free after",
                    not w.eng.update_claimed, "lock still held"))
    finally:
        w.close()
    return out


# ── 3. an entry the batch already did is named ─────────────────────────
def already_done_checks():
    out = []
    # "Update all" runs web, db and nas/web; db and nas/web were queued
    # behind it from the Web UI and come off the queue with nothing left.
    w = World([P("web", "local"), P("db", "local", "postgres:16"),
               P("web", "nas")])
    try:
        entered, release = w.block_on("local:web")
        w.tg_tap("update_all")
        entered.wait(10)
        r = w.web_post("/api/bulk", ("action", "update"),
                       ("names", "db,nas/web"))
        n0 = len(w.events)
        release.set()
        w.settle()
        tg, plug = w.said("tg", n0), w.said("plug", n0)
        out.append(("an entry 'Update all' already did is named, with its "
                    "host, on every channel",
                    r == "/?update=queued&n=2"
                    and _has(tg, ALREADY.format("db"))
                    and _has(tg, ALREADY.format("web @nas"))
                    and _has(plug, ALREADY.format("db"))
                    and _has(plug, ALREADY.format("web @nas")),
                    f"web {r!r}, telegram {tg!r}, plugins {plug!r}"))
        out.append(("…never the nameless 'No pending updates found'",
                    not _has(tg + plug, NAMELESS), f"said {tg + plug!r}"))
    finally:
        w.close()
    # The pending file unreadable while the entry waited: named, and not
    # called "already updated".
    w = World([P("web", "local"), P("db", "local", "postgres:16")])
    try:
        w.eng.claim_update()
        w.eng.enqueue_updates(["db"])
        with open(w.cfg.pending_file, "w") as f:
            f.write("[{not json")
        n0 = len(w.events)
        w.eng.release_update()
        w.settle()
        said = w.said("plug", n0)
        out.append(("a queued entry over an unreadable pending list: named, "
                    "not 'already updated'",
                    _has(said, PENDING_UNREADABLE.format("db"))
                    and not _has(said, ALREADY.format("db"))
                    and not _has(said, NAMELESS) and w.updated == [],
                    f"said {said!r}, updated {w.updated!r}"))
    finally:
        w.close()
    return out


# ── 4. the drain's other words ─────────────────────────────────────────
def drain_words_checks():
    out = []
    w = World([P("web", "local"), P("web", "nas")])
    try:
        eng = w.eng
        eng.claim_update()
        eng.enqueue_updates(["nas/web", "web"])
        eng.release_update(kick=False)
        n0 = len(w.events)

        def boom(key):
            raise RuntimeError("socket gone")
        with contextlib.redirect_stdout(io.StringIO()):
            eng.drain_queue(boom, group_of=lambda k: None,
                            say=eng._say_drain)
        said = w.said("plug", n0)
        out.append(("a drained entry that raised is named with its host",
                    _has(said, "❌ `web @nas`: socket gone")
                    and _has(said, "❌ `web`: socket gone"),
                    f"said {said!r}"))
        eng.enqueue_updates(["nas/web", "web"])
        n1 = len(w.events)
        eng.drop_group_mates("g1", group_of=lambda k: "g1")
        said = w.said("plug", n1)
        out.append(("group-mates taken off are named with their hosts",
                    _has(said, "`web @nas`, `web`"), f"said {said!r}"))
    finally:
        w.close()
    from i18n import get_translator
    t = get_translator("en")
    words = {k: t(k) for k in ("update_queue_dropped_selfupdate",
                               "update_queue_skipped_group",
                               "update_already_done",
                               "update_queue_skipped_pinned",
                               "update_queue_skipped_pinned_label",
                               "update_queue_skipped_excluded",
                               "update_queue_skipped_pending_unreadable",
                               "update_refused_selfupdate")}
    tapping = [k for k, v in words.items() if "tap" in v.lower()]
    out.append(("no drain sentence tells a channel to tap",
                not tapping, f"say 'tap': {tapping!r}"))
    return out


# ── 5. a held major ────────────────────────────────────────────────────
def major_checks():
    out = []
    PW = t5.P
    for env, name in ((("pin", "local", "web"), "pinned"),
                      (("exclude", ["web"]), "excluded")):
        rec, esc = t5.run_scenario("multi", [
            ("major", "local", "web"), env,
            PW("/api/major_confirm", ("name", "web"), ("action", "confirm"))])
        st = rec["steps"][-1]
        major = rec["state"]["files"].get("major_confirmations.json", "")
        out.append((f"Web: a deferred major {name} since, then confirmed: "
                    f"the page says not updated, nothing ran, the major "
                    f"stays",
                    st.get("http") == "303 | Location: /?update=pinned&n=1"
                    and not [e for e in st.get("events", [])
                             if e.startswith("update_container")]
                    and '"web"' in major and not esc,
                    f"http {st.get('http')!r}, events "
                    f"{st.get('events')!r}, majors {major!r}"))
    # The other direction: nothing holds it, the page goes home and the
    # update runs.
    rec, esc = t5.run_scenario("multi", [
        ("major", "local", "web"),
        PW("/api/major_confirm", ("name", "web"), ("action", "confirm"))])
    st = rec["steps"][-1]
    out.append(("Web: a deferred major nothing holds: confirmed, it runs",
                st.get("http") == "303 | Location: /"
                and "update_container@local:web" in st.get("events", []),
                f"http {st.get('http')!r}, events {st.get('events')!r}"))

    # Telegram: the held answer brings Confirm/Skip back.
    for setup in m.SETUPS:
        for env, said in ((("pin", "local", "web"), HELD_PIN.format("web")),
                          (("exclude", ["web"]), HELD_EXCL.format("web")),
                          (("label", "local", "web", "docksentry.pin",
                            "true"), HELD_PIN_LABEL.format("web"))):
            rec, esc = t3.run_scenario(setup, [
                ("major", "local", "web"), env,
                ("tap", "confirm_major:web")])
            st = rec["steps"][-1]
            line = next((x for x in st["out"] if said in x), "")
            out.append((f"[{setup}] Telegram: a held major ({env[0]}) "
                        f"brings Confirm/Skip back",
                        "→confirm_major:web" in line
                        and "→reject_major:web" in line
                        and rec["state"]["majors"] == {"local": ["web"]}
                        and not esc,
                        f"out {st['out']!r}"))
        rec, esc = t3.run_scenario(setup, [
            ("major", "local", "web"), ("raw_pins", b"{not json"),
            ("tap", "confirm_major:web")])
        st = rec["steps"][-1]
        line = next((x for x in st["out"] if "pin list" in x), "")
        out.append((f"[{setup}] Telegram: a major refused over an unreadable "
                    f"pin list brings Confirm/Skip back",
                    "→confirm_major:web" in line and "→reject_major:web" in line,
                    f"out {st['out']!r}"))
    return out


# ── 5c. no label read after the netns snapshot failed ──────────────────
def snapshot_label_checks():
    out = []
    LABELS = "{{json .Config.Labels}}"
    NETNS = "{{.HostConfig.NetworkMode}}"
    for host, key in (("dead", "dead/web"), ("local", "web")):
        row = t3.Row("multi")
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                if host != "local":
                    row.do(("add_pending", "web", "nginx:1.25", host))
                for b in row.backends.values():
                    del b.calls[:]
                row.do(("tap", f"update_one:{key}"))
                t3._join()
            calls = [c[0] for c in row.backends[host].calls]
        finally:
            shutil.rmtree(row.data, ignore_errors=True)
        at = next((i for i, c in enumerate(calls) if NETNS in c), None)
        after = [c for c in calls[at + 1:]] if at is not None else []
        asked = any(LABELS in c for c in after)
        if host == "dead":
            out.append(("a tap whose netns snapshot timed out on a dead host "
                        "does not ask that host for labels too",
                        at is not None and not asked,
                        f"calls {calls!r}"))
        else:
            out.append(("…while one whose snapshot answered still reads its "
                        "labels before the update",
                        at is not None and asked, f"calls {calls!r}"))
    return out


def main():
    m._TMP_ROOT = tempfile.mkdtemp(prefix="selfupdate-swap-queue-")
    failed = 0
    sink = io.StringIO()
    try:
        for part in (swap_checks, handover_checks, already_done_checks,
                     drain_words_checks, major_checks,
                     snapshot_label_checks):
            try:
                with contextlib.redirect_stdout(sink):
                    results = part()
            except Exception as e:                      # noqa: BLE001
                results = [(f"{part.__name__} ran to the end", False,
                            f"{type(e).__name__}: {e}")]
            for label, ok, detail in results:
                print(f"  {'PASS' if ok else 'FAIL'}  {label}")
                if not ok:
                    print(f"      → {detail}")
                    failed += 1
    finally:
        shutil.rmtree(m._TMP_ROOT, ignore_errors=True)
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
