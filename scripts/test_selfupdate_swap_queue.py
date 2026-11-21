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

Q16i, from the review of 2ddd2a0 (scratchpad q16hrr/probe h1–h4),
measured on 2ddd2a0:

  * The queue was named "did not run" and dropped BEFORE the helper
    started. A helper that then failed (rc≠0) or whose launch timed out
    left the queue gone for a swap that never happened; the timeout said
    nothing at all, only the log had it (h1).
  * A helper that started but never stopped this process left the swap
    flag up and the lock held after the 30-second wait: every later tap
    was refused "updating itself" for the life of the process (h2 a).
  * `claim_or_queue` read the flag before it queued; an entry that was
    past the check when the swap began went onto the queue after the
    drop (h2 b).
  * "was already updated" came for every entry whose pending row was
    gone — also for one the batch it waited behind had FAILED (h3).
  * The drain's unreadable-pin-list refusal named no container, once per
    entry (h4); `host_unknown` named only the host.
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
NOT_LISTED = "ℹ️ `{}` is no longer in the update list — nothing to do."
NOT_HAPPENED = ("❌ The self-update did not happen: the helper container "
                "did not start ({}). Docksentry keeps running the current "
                "version, and queued updates go on.")
NOT_STOPPED = ("❌ The self-update did not happen: the helper started, but "
               "Docksentry was still running 30 seconds later. It keeps "
               "running the current version, and updates can be started "
               "again.")
PIN_LIST_SKIP = ("📌 `{}` did not run from the queue: the pin list "
                 "(`pinned_containers.json`) could not be read, so every "
                 "container counts as pinned. Repair or remove the file, then "
                 "start it again.")
REFUSED_PIN = "📌 No update was started: the pin list"
HOST_SKIP = ("❌ `{}` did not run from the queue: host `{}` is not managed "
             "here. Managed hosts: {}")
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
            ok, msg = world.orig_uc(ch, name, image, *a, **kw)
            # Into the update history, as the real `update_container`
            # writes it on every outcome: the drain decides "already
            # updated" from it (Q16i).
            cls._save_history(ch, name, image, ok, msg)
            return ok, msg
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

    def history(self, name, success, host=None, ago=0):
        """One update-history row, as `_save_history` writes it, `ago`
        seconds in the past."""
        try:
            with open(self.cfg.history_file, encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError):
            rows = []
        row = {"timestamp": time.strftime(
                   "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - ago)),
               "container": name, "image": "x", "success": success,
               "detail": ""}
        if host:
            row["host"] = host
        rows.append(row)
        with open(self.cfg.history_file, "w", encoding="utf-8") as f:
            json.dump(rows, f)

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
    started; "raise" makes its launch time out, "oserror" fail to run. `stops`: the helper stops
    this process inside the wait — as main.py's SIGTERM handler does, the
    stand-in writes the exit marker first (on `stopped`, or after 10 s);
    False, the wait runs out with nothing having stopped it.
    `at_wait` is the world when the wait began."""

    def __init__(self, world, found=True, launch_ok=True, stops=True):
        self.world, self.found, self.launch_ok = world, found, launch_ok
        self.stops = stops
        self.at_launch = None
        self.at_wait = None
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
            if self.launch_ok == "oserror":
                raise OSError("docker: not found")
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
        stopped, env = self.stopped, self

        def sleep(seconds):
            # The helper stops this process inside the 30 s wait.
            if seconds >= 30:
                w = env.world
                env.at_wait = {"queue": w.queue(), "said": w.said(),
                               "swap": bool(w.eng._swap_in_flight)}
                if not env.stops:
                    return
                stopped.wait(10)
                with open(w.cfg.last_exit_file, "w") as f:
                    json.dump({"reason": "signal", "signal": "SIGTERM",
                               "ts": time.time(), "done": False}, f)
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
            while env.at_wait is None and time.time() < end:
                time.sleep(0.05)
            at, aw = env.at_launch or {}, env.at_wait or {}
            drop = DROP.format("`web @nas`, `db`")
            # Q16i: named and dropped once the helper has started, not
            # before — a helper that fails must not have cost the queue.
            out.append(("swap behind a tap: once the helper has started, "
                        "every channel is told which queued updates did not "
                        "run, with their hosts",
                        queued == ["nas/web", "db"]
                        and sum(drop in t for t in aw.get("said", [])) == 2,
                        f"queued {queued!r}, said when the wait began "
                        f"{aw.get('said')!r}"))
            out.append(("…not before: at the helper's launch the queue is "
                        "still there and nothing has been said of it",
                        at.get("queue") == ["nas/web", "db"]
                        and not _has(at.get("said", []), drop),
                        f"at launch {at!r}"))
            out.append(("…and the queue is empty once it is named",
                        aw.get("queue") == [],
                        f"queue when the wait began {aw.get('queue')!r}"))
            out.append(("…the swap flag is up already at the launch, so "
                        "nothing can queue in the launch's seconds",
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
            n2 = len(w.events)
            env.stopped.set()
            w.settle(10)
            # The helper stopped this process (the exit marker is there):
            # nothing is said about a swap that is happening, and the
            # lock stays with the process that is going away.
            out.append(("a helper that stops the process: the flag stays "
                        "up, the lock held, no 'did not happen'",
                        w.eng._swap_in_flight is True
                        and w.eng.update_claimed
                        and not _has(w.said(since=n2), NOT_STOPPED),
                        f"flag {w.eng._swap_in_flight!r}, held "
                        f"{w.eng.update_claimed!r}, said "
                        f"{w.said(since=n2)!r}"))
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
            while env.at_wait is None and time.time() < end:
                time.sleep(0.05)
            aw = env.at_wait or {}
            out.append(("swap behind a held scheduled pass: named once the "
                        "helper has started, queue empty",
                        _has(aw.get("said", []), DROP.format("`web @nas`"))
                        and aw.get("queue") == [],
                        f"when the wait began {aw!r}"))
            env.stopped.set()
            th.join(10)
            w.eng.let_drain_go()
            w.settle(10)
    finally:
        w.close()

    # C: the helper does not start, or its launch times out (Q16i, h1).
    # Behind a tap, with Discord and the Web UI queued: the queue is not
    # named "did not run" and stays; the flag comes down, the lock is
    # given back, every channel hears that the self-update did not
    # happen, and the queued updates run.
    for launch, err in ((False, "helper refused"),
                        ("raise", "docker run timed out after 30 s")):
        w = World([P("web", "local"), P("web", "nas"),
                   P("db", "local", "postgres:16")])
        try:
            with SelfUpdateEnv(w, launch_ok=launch) as env:
                entered, release = w.block_on("local:web")
                w.tg_tap("update_one:web")
                entered.wait(10)
                w.discord("update", ("container", "web"), ("host", "nas"))
                w.web_post("/api/update", ("name", "db"))
                selfupdate.start(w.tg.selfupdate_ctx)
                n0 = len(w.events)
                with contextlib.redirect_stdout(io.StringIO()):
                    release.set()
                    end = time.time() + 10
                    while env.at_launch is None and time.time() < end:
                        time.sleep(0.05)
                    time.sleep(0.5)
                    w.settle(10)
                said = w.said(since=n0)
                tg, plug = w.said("tg", n0), w.said("plug", n0)
                flag, held = w.eng._swap_in_flight, w.eng.update_claimed
                with open(w.cfg.pending_file, "w") as f:
                    json.dump([P("web", "local")], f)
                n1, u1 = len(w.events), len(w.updated)
                w.tg_tap_join("update_one:web")
                w.settle(10)
                tap, again = w.said("tg", n1), w.updated[u1:]
            what = ("does not start" if launch is False
                    else "times out at its launch")
            nh = NOT_HAPPENED.format(err)
            out.append((f"a helper that {what}: every channel hears the "
                        f"self-update did not happen",
                        _has(tg, nh) and _has(plug, nh),
                        f"telegram {tg!r}, plugins {plug!r}"))
            out.append((f"…the queue is not called 'did not run', and it "
                        f"runs: both queued updates are done",
                        not _has(said, "queued updates did not run")
                        and w.updated[:3] == ["local:web", "nas:web",
                                              "local:db"],
                        f"said {said!r}, updated {w.updated!r}"))
            out.append((f"…flag down, lock free, the next tap runs",
                        flag is False and held is False
                        and again == ["local:web"]
                        and not _has(tap, REFUSED_SU),
                        f"flag {flag!r}, held {held!r}, "
                        f"updated {w.updated!r}, said {tap!r}"))
        finally:
            w.close()

    # C2: started directly on a free lock, a launch that times out or
    # cannot run does not raise out of `start` any more: it is reported.
    for launch, err in (("raise", "docker run timed out after 30 s"),
                        ("oserror", "docker: not found")):
        w = World([P("web", "local")])
        try:
            with SelfUpdateEnv(w, launch_ok=launch):
                raised = None
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        selfupdate.start(w.tg.selfupdate_ctx)
                    except Exception as e:              # noqa: BLE001
                        raised = type(e).__name__
                flag, held = w.eng._swap_in_flight, w.eng.update_claimed
                said = w.said()
            what = "times out" if launch == "raise" else "cannot run"
            out.append((f"a helper launch that {what} on a free lock: "
                        f"nothing raises, it is said, flag down, lock free",
                        raised is None and flag is False and held is False
                        and _has(said, NOT_HAPPENED.format(err)),
                        f"raised {raised!r}, flag {flag!r}, held {held!r}, "
                        f"said {said!r}"))
        finally:
            w.close()

    # C3: the helper starts, and 30 s later this process is still running
    # (Q16i, h2 a): flag down, lock free, said, and updates run again.
    w = World([P("web", "local")])
    try:
        with SelfUpdateEnv(w, stops=False):
            with contextlib.redirect_stdout(io.StringIO()):
                selfupdate.start(w.tg.selfupdate_ctx)
            tg, plug = w.said("tg"), w.said("plug")
            flag, held = w.eng._swap_in_flight, w.eng.update_claimed
            n0 = len(w.events)
            w.tg_tap_join("update_one:web")
            w.settle(10)
            tap = w.said("tg", n0)
            r = w.web_post("/api/update", ("name", "web"))
        out.append(("a helper that started but never stopped the process: "
                    "every channel hears it did not happen",
                    _has(tg, NOT_STOPPED) and _has(plug, NOT_STOPPED),
                    f"telegram {tg!r}, plugins {plug!r}"))
        out.append(("…flag down, lock free, the next tap runs, the Web UI "
                    "is not sent to 'restarting'",
                    flag is False and held is False
                    and w.updated[:1] == ["local:web"]
                    and not _has(tap, REFUSED_SU)
                    and r != "/?update=restarting",
                    f"flag {flag!r}, held {held!r}, updated {w.updated!r}, "
                    f"said {tap!r}, web {r!r}"))
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

    # E: the flag goes up while a request is past its first look at it
    # (Q16i, h2 b): stopped at the pin question, the swap begins, then it
    # goes on. The queue itself refuses, under its own lock.
    for swapping in (True, False):
        for many in (False, True):
            w = World([P("web", "local"), P("db", "local", "postgres:16")])
            try:
                eng = w.eng
                eng.claim_update()
                gate, go = threading.Event(), threading.Event()
                orig = eng.held_back

                def slow(key, *a, _o=orig, _g=gate, _go=go, **k):
                    if key == "db":
                        _g.set()
                        _go.wait(5)
                    return _o(key, *a, **k)
                eng.held_back = slow
                res = {}
                call = ((lambda: eng.claim_or_queue_many(["web", "db"]))
                        if many else (lambda: eng.claim_or_queue("db")))
                th = threading.Thread(target=lambda: res.setdefault(
                    "r", call()))
                th.start()
                gate.wait(5)
                eng._swap_in_flight = swapping
                go.set()
                th.join(5)
                q = w.queue()
                eng._swap_in_flight = False
                eng.drop_queued_updates()
                eng._update_lock.release()
            finally:
                w.close()
            r = res.get("r")
            if many:
                got = [a for _k, a, _p in (r[1] if r else [])]
                want = (["update_refused_selfupdate"] * 2 if swapping
                        else ["update_queued"] * 2)
            else:
                got = list(r[:2]) if r else None
                want = ([False, "update_refused_selfupdate"] if swapping
                        else [False, "update_queued"])
            what = "several" if many else "one"
            if swapping:
                out.append((f"engine: the swap begins while a request for "
                            f"{what} is past its first look: refused, "
                            f"nothing queued",
                            got == want and q == [],
                            f"answer {r!r}, queue {q!r}"))
            else:
                out.append((f"engine: …the same request for {what} without "
                            f"a swap is queued",
                            got == want and sorted(q) == (
                                ["db", "web"] if many else ["db"]),
                            f"answer {r!r}, queue {q!r}"))
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
    # The history has a success for each since they were queued, so
    # "already updated" is a fact here (Q16i).
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

    # Q16i (h3): the batch's update of db FAILED; "Update all" empties the
    # pending list all the same. The queued db is not "already updated".
    w = World([P("web", "local"), P("db", "local", "postgres:16")])
    try:
        orig = w.orig_uc

        def uc(ch, name, image, *a, **kw):
            if name == "db":
                return False, "pull failed (stand-in)"
            return orig(ch, name, image, *a, **kw)
        w.orig_uc = uc
        entered, release = w.block_on("local:web")
        w.tg_tap("update_all")
        entered.wait(10)
        r = w.web_post("/api/update", ("name", "db"))
        n0 = len(w.events)
        release.set()
        w.settle()
        tg, plug = w.said("tg", n0), w.said("plug", n0)
        out.append(("an entry whose update the batch FAILED is not called "
                    "already updated: named as no longer listed, on every "
                    "channel",
                    r == "/?update=queued&pos=1"
                    and not _has(tg + plug, ALREADY.format("db"))
                    and _has(tg, NOT_LISTED.format("db"))
                    and _has(plug, NOT_LISTED.format("db")),
                    f"web {r!r}, telegram {tg!r}, plugins {plug!r}"))
    finally:
        w.close()

    # The evidence has to be this key, since it was queued. Each row: the
    # pending entry is gone (no file, or a file without it) and the
    # history says something that is NOT that.
    def gone(w, keys, setup, drop_file=True):
        w.eng.claim_update()
        w.eng.enqueue_updates(keys)
        setup(w)
        if drop_file:
            os.unlink(w.cfg.pending_file)
        else:
            with open(w.cfg.pending_file, "w") as f:
                json.dump([P("web", "local")], f)
        n0 = len(w.events)
        w.eng.release_update()
        w.settle()
        return w.said("plug", n0)

    for what, keys, label, setup, drop_file in (
            ("no history at all, a pending file without it",
             ["db"], "db", lambda w: None, False),
            ("a success for it from before it was queued",
             ["db"], "db", lambda w: w.history("db", True, ago=3600), True),
            ("a success for the same name on another host",
             ["nas/web"], "web @nas", lambda w: w.history("web", True),
             True),
            ("only a failure for it since it was queued",
             ["db"], "db", lambda w: w.history("db", False), True)):
        w = World([P("web", "local"), P("db", "local", "postgres:16"),
                   P("web", "nas")])
        try:
            said = gone(w, keys, setup, drop_file)
        finally:
            w.close()
        out.append((f"gone from the pending list, {what}: named as no "
                    f"longer listed, not as already updated",
                    _has(said, NOT_LISTED.format(label))
                    and not _has(said, ALREADY.format(label))
                    and w.updated == [],
                    f"said {said!r}, updated {w.updated!r}"))
    # The other direction: a success for exactly this key since then.
    for keys, label, setup in (
            (["db"], "db", lambda w: w.history("db", True)),
            (["nas/web"], "web @nas",
             lambda w: w.history("web", True, host="nas"))):
        w = World([P("web", "local"), P("db", "local", "postgres:16"),
                   P("web", "nas")])
        try:
            said = gone(w, keys, setup)
        finally:
            w.close()
        out.append((f"gone, and the history has a success for `{label}` "
                    f"since it was queued: already updated",
                    _has(said, ALREADY.format(label))
                    and not _has(said, NOT_LISTED.format(label)),
                    f"said {said!r}"))
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

    # Q16i (h4): the pin list unreadable when the drain gets to two
    # entries: each is named, not two nameless refusals.
    w = World([P("web", "local"), P("web", "nas"),
               P("db", "local", "postgres:16")])
    try:
        w.eng.claim_update()
        w.eng.enqueue_updates(["nas/web", "db"])
        with open(os.path.join(w.cfg.data_dir, "pinned_containers.json"),
                  "w") as f:
            f.write("{not json")
        n0 = len(w.events)
        w.eng.release_update()
        w.settle()
        tg, plug = w.said("tg", n0), w.said("plug", n0)
        out.append(("the drain over an unreadable pin list names each "
                    "entry, on every channel",
                    all(_has(x, PIN_LIST_SKIP.format(n))
                        for x in (tg, plug) for n in ("web @nas", "db"))
                    and not _has(tg + plug, REFUSED_PIN)
                    and w.updated == [],
                    f"telegram {tg!r}, plugins {plug!r}, updated "
                    f"{w.updated!r}"))
        # The same where `run_keys` finds it (unreadable after the drain's
        # own question): named too.
        said = w.eng._drain_skipped_text("nas/web", "pin_list_unreadable")
        out.append(("…and where the update's own pin check finds it",
                    said == PIN_LIST_SKIP.format("web @nas"),
                    f"said {said!r}"))
    finally:
        w.close()

    # A queued entry whose host is no longer managed: the container is
    # named, not only the host.
    w = World([P("web", "local"), P("web", "nas"), P("web", "ghost")])
    try:
        w.eng.claim_update()
        w.eng.enqueue_updates(["ghost/web"])
        n0 = len(w.events)
        w.eng.release_update()
        w.settle()
        said = w.said("plug", n0)
        line = next((x for x in said if "ghost" in x), "")
        out.append(("a queued entry whose host is gone is named with its "
                    "container",
                    line.startswith(HOST_SKIP.format("web @ghost", "ghost",
                                                     ""))
                    and "`nas`" in line and w.updated == [],
                    f"said {said!r}, updated {w.updated!r}"))
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
                               "update_refused_selfupdate",
                               "update_queue_skipped_not_listed",
                               "update_queue_skipped_pin_list",
                               "update_queue_skipped_host_unknown",
                               "selfupdate_not_happened",
                               "selfupdate_not_stopped")}
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
