#!/usr/bin/env python3
"""Every place that takes, gives back or looks at the update lock, listed and driven.

There is one update lock, `UpdateEngine._update_lock`, and eleven
functions claim it by hand with `acquire(blocking=False)`. Each one has
its own answer for "somebody else holds it": Telegram's per-container
tap queues, `/update all` and a confirmed major refuse, the scheduler
skips its pass, a cleanup hands its caller a busy sentence, a
self-update queues itself. The core split replaces the hand-written
claims with one claim/release pair. Before that moves anything, this
writes down what each site does today, so a site that is forgotten,
added or quietly changes its answer turns red instead of slipping
through.

Two halves.

The enumeration walks every app/*.py (and the packages under it) with
`ast` and compares what it finds, by qualified name, with the explicit
lists below:

  * lock operations: `.acquire(` / `.release(` / `.locked(` and `with`
    on the update lock, whether it is spelled `self._update_lock`,
    `engine._update_lock`, `ctx._update_lock`, Discord's `self._lock()`,
    a local name assigned from one of those, or `getattr(x,
    "_update_lock")`; plus calls to Discord's `_release_lock()`;
  * every function that mentions the lock at all (the accessors too, so
    a new alias is a new entry);
  * every read of `update_running` (the pre-gates);
  * every call to a helper that only looks at the lock (a READER or
    PROBE below that is neither `update_running` nor a queue call —
    today `WebHandler._update_lock_held`);
  * every call that feeds or works off the container queue;
  * every busy sentence: a string constant that names a busy or queue
    key, a `t()` built from a prefix of one (the Web banner's
    `t("web_update_" + _upd)`), and the `?update=busy` redirect, each
    labelled claim-site answer, pre-gate, queue, render or not-this-lock.

The drive builds the Discord matrix's world (T4's `Row("multi")`: a real
UpdateEngine, TelegramBot, DiscordBot, selfupdate.Context, Broadcast and
the five-host registry, wired as main.py wires them) plus a Web handler
on the same bot, and calls each claim, probe and reader directly twice:
once with the real lock held by this test, once with it free. Held, the
site must do no work, must not give back a lock it did not take, and
must answer with its kind:

  QUEUE       the key is on the engine's queue (Telegram tap)
  QUEUE_SELF  the self-update is remembered for later
  REJECT      the person is told, nothing is remembered
  SKIP        nobody is told (a log line at most), nothing is remembered
  SKIP_MSG    returns `(None, busy sentence)` for its caller to use

Free, the work runs with the lock held and the lock is free afterwards.
The busy record (kind, i18n keys, what was said or returned, log lines,
queue, lock) is compared with the table at the bottom; that table was
taken from main (8a74ba7) with `--app`.

Stand-ins beyond T2/T4's (see test_telegram_dispatch_matrix.py and
test_discord_dispatch_matrix.py for theirs): every host checker's
`update_container` and `cleanup_images` record whether the lock was held
and answer success; `selfupdate.run` and the bot's
`_check_selfupdate_auto_locked` (the pull-and-swap body behind the
wrapper) do the same; `i18n.get_translator` hands out the real
translator with a recorder in front, so the keys a site used are seen.

`python3 scripts/test_update_lock_sites.py --snapshot [--app DIR]`
prints the enumeration and the busy records from DIR's app code.
"""

import ast
import collections
import contextlib
import glob
import io
import os
import pprint
import re
import shutil
import sys
import tempfile

import test_telegram_dispatch_matrix as m
import test_discord_dispatch_matrix as dm

APP = m.APP

# ═══════════════════════════════════════════════════════════════════════
# The explicit lists, keyed by qualified name (never by line).
# ═══════════════════════════════════════════════════════════════════════

#: (file, qualname) → (role, lock operations in that function, sorted).
#: Roles: CLAIM takes the lock to do work under it; PROBE takes it and
#: gives it straight back to learn whether it was free; READER only
#: looks (`.locked()`); RELEASE gives back a lock somebody else took.
LOCK_OPS = {
    # ── claims ──
    ("telegram_bot.py", "TelegramBot._run_single_update"):
        ("CLAIM", ["acquire(blocking=False)", "release"]),
    ("telegram_bot.py", "TelegramBot._confirm_major_update"):
        ("CLAIM", ["acquire(blocking=False)", "release"]),
    ("telegram_bot.py", "TelegramBot.handle_autoupdates"):
        ("CLAIM", ["acquire(blocking=False)", "release"]),
    ("telegram_bot.py", "TelegramBot.cleanup_guarded"):
        ("CLAIM", ["acquire(blocking=False)", "release"]),
    ("telegram_bot.py", "TelegramBot.check_selfupdate_auto"):
        ("CLAIM", ["acquire(blocking=False)", "release"]),
    ("telegram_bot.py", "TelegramBot.run_updates"):
        ("CLAIM", ["acquire(blocking=False)", "release"]),
    ("discord_bot.py", "DiscordBot._cmd_update"):
        ("CLAIM", ["_release_lock()", "acquire(blocking=False)"]),
    ("discord_bot.py", "DiscordBot._do_updateall"):
        ("CLAIM", ["_release_lock()", "acquire(blocking=False)"]),
    ("discord_bot.py", "DiscordBot._cleanup_guarded"):
        ("CLAIM", ["_release_lock()", "acquire(blocking=False)"]),
    ("web_ui.py", "create_handler.WebHandler._run_web_update_batch"):
        ("CLAIM", ["acquire(blocking=False)", "release"]),
    # Sanctioned raw site (Q3): the self-update keeps its own raw claim by
    # design, because it keeps the lock past its own return once the
    # helper container is launched.
    ("selfupdate.py", "start"):
        ("CLAIM", ["acquire(blocking=False)", "release"]),
    # ── probes ──
    ("update_engine.py", "UpdateEngine.update_running"):
        ("PROBE", ["acquire(blocking=False)", "release"]),
    ("telegram_bot.py", "TelegramBot._drain_update_queue"):
        ("PROBE", ["acquire(blocking=False)", "release"]),
    # ── readers ──
    ("web_ui.py", "create_handler.WebHandler._update_lock_held"):
        ("READER", ["locked"]),
    ("telegram_bot.py", "TelegramBot._sweep_update_queue"):
        ("READER", ["locked"]),
    # ── release helper ──
    # Release, then hand on a queued self-update. Discord's three claims
    # give the lock back only through here.
    ("discord_bot.py", "DiscordBot._release_lock"):
        ("RELEASE", ["release"]),
}

#: Functions that mention the lock without operating on it.
LOCK_MENTIONS = {
    ("update_engine.py", "UpdateEngine.__init__"): "builds the one lock",
    ("telegram_bot.py", "TelegramBot._update_lock"):
        "read-only property onto engine._update_lock",
    ("selfupdate.py", "Context._update_lock"):
        "read-only property onto engine._update_lock",
    ("discord_bot.py", "DiscordBot._lock"): "returns engine._update_lock",
}

#: (file, qualname) → how often it reads `update_running`, and why.
UPDATE_RUNNING_READS = {
    ("update_engine.py", "UpdateEngine.update_running"): (0, "the probe itself"),
    ("telegram_bot.py", "TelegramBot.update_running"):
        (1, "property onto engine.update_running"),
    ("telegram_bot.py", "TelegramBot._handle_callback"):
        (2, "lifecycle buttons pass it to lifecycle._guard"),
    ("telegram_bot.py", "TelegramBot._handle_message"):
        (5, "pre-gates: /check --dry-run, /check <name>, /update, /check; "
            "/start|stop|restart pass it to lifecycle"),
    ("discord_bot.py", "DiscordBot._cmd_check"): (1, "pre-gate for /check"),
    ("discord_bot.py", "DiscordBot._cmd_lifecycle"):
        (1, "passed to lifecycle._guard"),
    ("discord_bot.py", "DiscordBot._run_lifecycle"):
        (1, "passed to lifecycle._guard"),
    ("web_ui.py", "create_handler.WebHandler.do_POST"):
        (2, "pre-gate for /api/update (getattr), lifecycle passes it on"),
    ("web_ui.py", "create_handler.WebHandler._api_check"):
        (1, "pre-gate for GET /api/check"),
    ("web_ui.py", "create_handler.WebHandler._api_check_one"):
        (1, "pre-gate for /api/check_one"),
    ("monitor.py", "ContainerMonitor.tick"):
        (1, "no restart alerts while an update recreates containers"),
}
# The probe's own definition holds no read; it is listed with 0 so the
# table names every function on the lock's side and the check below only
# compares the non-zero ones.

#: (file, qualname, callee) → count. Who feeds the container queue and
#: who works it off. Today only Telegram does either (Q13).
QUEUE_CALLS = {
    ("telegram_bot.py", "TelegramBot._run_single_update", "enqueue_update"): 1,
    ("telegram_bot.py", "TelegramBot._run_single_update",
     "_drain_update_queue"): 1,
    ("telegram_bot.py", "TelegramBot._sweep_update_queue",
     "_drain_update_queue"): 1,
    ("telegram_bot.py", "TelegramBot._drain_update_queue",
     "take_queued_update"): 1,
    # Lost the race between take and run: the entry goes back (to the end).
    ("telegram_bot.py", "TelegramBot._drain_update_queue", "enqueue_update"): 1,
    ("telegram_bot.py", "TelegramBot.listen", "_sweep_update_queue"): 1,
}
QUEUE_CALLEES = ("enqueue_update", "take_queued_update",
                 "_drain_update_queue", "_sweep_update_queue")

#: (file, qualname, callee) → (count, why). Who asks the lock through a
#: helper that only looks at it. The callees are the READER and PROBE
#: names in LOCK_OPS that are counted nowhere else (`update_running` has
#: its own table, the queue helpers are in QUEUE_CALLS), so a new reader
#: listed there brings its callers along.
LOCK_HELPER_CALLS = {
    ("web_ui.py", "create_handler.WebHandler.do_POST", "_update_lock_held"):
        (1, "pre-gate for /api/update"),
}


def _helper_callees():
    return sorted({q.rsplit(".", 1)[-1] for (_f, q), (role, _o)
                   in LOCK_OPS.items() if role in ("READER", "PROBE")}
                  - set(QUEUE_CALLEES) - {"update_running"})

#: (file, qualname, key) → (count, label). `key` is the string constant,
#: or `t(<prefix>…)` for a key built at run time. Labels: claim = the
#: answer when that function's own acquire fails; pre-gate = asked of
#: `update_running` before (or instead of) a claim; queue = what a queued
#: request is told; render = the Web page turning `?update=busy` into
#: the sentence; other lock = not the update lock.
BUSY_MESSAGES = {
    # Telegram
    ("telegram_bot.py", "TelegramBot._run_single_update",
     "update_same_running"): (1, "claim: same container tapped again"),
    ("telegram_bot.py", "TelegramBot._run_single_update",
     "update_queue_full"): (1, "claim: queue at UPDATE_QUEUE_MAX"),
    ("telegram_bot.py", "TelegramBot._run_single_update",
     "update_already_queued"): (1, "claim: already on the queue"),
    ("telegram_bot.py", "TelegramBot._run_single_update",
     "update_queued"): (1, "claim: queued"),
    ("telegram_bot.py", "TelegramBot._drain_update_queue",
     "update_queue_dropped_selfupdate"): (1, "queue: dropped for a self-update"),
    ("telegram_bot.py", "TelegramBot._drain_update_queue",
     "update_queue_skipped_group"): (1, "queue: group-mates of a failure"),
    ("telegram_bot.py", "TelegramBot._confirm_major_update",
     "update_already_running"): (1, "claim"),
    ("telegram_bot.py", "TelegramBot.cleanup_guarded",
     "cleanup_busy"): (1, "claim"),
    ("telegram_bot.py", "TelegramBot.run_updates",
     "update_already_running"): (1, "claim"),
    ("telegram_bot.py", "TelegramBot._handle_message",
     "update_already_running"): (4, "pre-gate: /check --dry-run, "
                                    "/check <name>, /update, /check"),
    ("telegram_bot.py", "TelegramBot._handle_message",
     "status_queued"): (1, "queue: /status shows it"),
    # Discord
    ("discord_bot.py", "DiscordBot._cmd_check",
     "web_check_one_busy"): (1, "pre-gate"),
    ("discord_bot.py", "DiscordBot._cmd_update",
     "chan_update_busy"): (1, "claim"),
    ("discord_bot.py", "DiscordBot._do_updateall",
     "chan_update_busy"): (1, "claim"),
    ("discord_bot.py", "DiscordBot._cleanup_guarded",
     "cleanup_busy"): (1, "claim"),
    ("discord_bot.py", "DiscordBot._refuse_busy._say",
     "chan_busy_capacity"): (1, "other lock: the worker pool is full"),
    # Web
    ("web_ui.py", "create_handler.WebHandler.do_POST",
     "/?update=busy"): (1, "pre-gate: /api/update"),
    ("web_ui.py", "create_handler.WebHandler._page_status",
     "busy"): (1, "render: the banners the page knows"),
    ("web_ui.py", "create_handler.WebHandler._page_status",
     "t(web_update_…)"): (2, "render: web_update_busy(_title)"),
    ("web_ui.py", "create_handler.WebHandler._status_row",
     "web_check_one_busy"): (1, "other lock: the check-one button's text"),
    ("web_ui.py", "create_handler.WebHandler._api_check_one",
     "busy"): (2, "other lock: _CHECK_LOCK's JSON answer"),
    ("web_ui.py", "create_handler.WebHandler._run_web_update_batch",
     "update_already_running"): (1, "claim"),
    # Self-update and lifecycle
    ("selfupdate.py", "start", "selfupdate_queued"): (1, "claim: queued"),
    ("selfupdate.py", "run_queued", "selfupdate_queue_cancelled"):
        (1, "queue: a crashed batch cancels the queued self-update"),
    ("lifecycle.py", "_guard", "lifecycle_busy"):
        (1, "pre-gate: start/stop/restart refused while update_running"),
}

#: Which string constants count as busy or queue sentences. Wider than
#: the five keys the plan names, on purpose: a new key is far more
#: likely to be called `…_busy` than to reuse an old one.
BUSY_RE = re.compile(r"busy|already_running|same_running|_queued|_queue_")

# ═══════════════════════════════════════════════════════════════════════
# Enumeration
# ═══════════════════════════════════════════════════════════════════════
LOCK = "_update_lock"
ACCESSORS = ("_lock",)             # DiscordBot._lock() → engine._update_lock
RELEASE_HELPERS = ("_release_lock",)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _sources(app=None):
    app = app or APP
    for path in sorted(glob.glob(os.path.join(app, "**", "*.py"),
                                 recursive=True)):
        rel = os.path.relpath(path, app).replace(os.sep, "/")
        with open(path, encoding="utf-8") as f:
            yield rel, f.read()


def _scopes(tree):
    """(qualname, node) for the module and every def and class in it."""
    out = [("<module>", tree)]

    def rec(node, prefix):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, _DEFS):
                q = prefix + [c.name]
                out.append((".".join(q), c))
                rec(c, q)
            else:
                rec(c, prefix)
    rec(tree, [])
    return out


def _own(scope):
    """The nodes of `scope`, without the bodies of defs nested in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, _DEFS):
            stack.extend(ast.iter_child_nodes(n))


def _callee(call):
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _names_lock(n):
    """`x._update_lock`, `getattr(x, "_update_lock")`, `self._lock()`."""
    return ((isinstance(n, ast.Attribute) and n.attr == LOCK)
            or (isinstance(n, ast.Constant) and n.value == LOCK)
            or (isinstance(n, ast.Call) and _callee(n) in ACCESSORS
                and not n.args and not n.keywords))


def _is_lock(expr, aliases):
    return any(_names_lock(n) or (isinstance(n, ast.Name)
                                  and n.id in aliases)
               for n in ast.walk(expr))


def _aliases(nodes):
    """Local names bound to the lock (`lock = getattr(eng, "_update_lock")`)."""
    found = set()
    for _ in range(3):                      # a = lock; b = a
        before = len(found)
        for n in nodes:
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) \
                    and n.value is not None and _is_lock(n.value, found):
                targets = n.targets if isinstance(n, ast.Assign) \
                    else [n.target]
                for t in targets:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            found.add(x.id)
        if len(found) == before:
            break
    return found


def _acquire_form(call):
    blocking = None
    if call.args:
        blocking = call.args[0]
    for k in call.keywords:
        if k.arg == "blocking":
            blocking = k.value
    if isinstance(blocking, ast.Constant) and blocking.value is False:
        return "acquire(blocking=False)"
    return "acquire()" if blocking is None else \
        f"acquire({ast.unparse(blocking)})"


def _busy_sites(node):
    """The busy-sentence key `node` stands for, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value
        if re.fullmatch(r"[a-z][a-z0-9_]*", v) and BUSY_RE.search(v):
            return v
        if "=busy" in v:
            return v
        return None
    if isinstance(node, ast.Call) and _callee(node) == "t" and node.args \
            and not isinstance(node.args[0], ast.Constant):
        arg = node.args[0]
        head = arg
        while isinstance(head, ast.BinOp):
            head = head.left
        if isinstance(head, ast.JoinedStr) and head.values:
            head = head.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str) \
                and any(k.startswith(head.value) for k in _busy_keys()):
            return f"t({head.value}…)"
    return None


_KEYS = []


def _busy_keys():
    if not _KEYS:
        import json
        with open(os.path.join(APP, "lang", "en.json"),
                  encoding="utf-8") as f:
            _KEYS.extend(k for k in json.load(f) if BUSY_RE.search(k))
    return _KEYS


def enumerate_sites(app=None):
    ops = collections.defaultdict(list)
    mentions = set()
    running = collections.Counter()
    queue = collections.Counter()
    helpers = collections.Counter()
    busy = collections.Counter()
    helper_callees = set(_helper_callees())
    for rel, src in _sources(app):
        tree = ast.parse(src)
        docstrings = {id(b.value) for _q, s in _scopes(tree)
                      for b in getattr(s, "body", [])[:1]
                      if isinstance(b, ast.Expr)
                      and isinstance(b.value, ast.Constant)}
        for qual, scope in _scopes(tree):
            nodes = list(_own(scope))
            aliases = _aliases(nodes)
            for n in nodes:
                if _names_lock(n):
                    mentions.add((rel, qual))
                if isinstance(n, ast.Call):
                    c = _callee(n)
                    f = n.func
                    if isinstance(f, ast.Attribute) and c in (
                            "acquire", "release", "locked") \
                            and _is_lock(f.value, aliases):
                        ops[(rel, qual)].append(
                            _acquire_form(n) if c == "acquire" else c)
                    elif c in RELEASE_HELPERS:
                        ops[(rel, qual)].append(c + "()")
                    if c in QUEUE_CALLEES:
                        queue[(rel, qual, c)] += 1
                    if c in helper_callees:
                        helpers[(rel, qual, c)] += 1
                if isinstance(n, (ast.With, ast.AsyncWith)):
                    for item in n.items:
                        if _is_lock(item.context_expr, aliases):
                            ops[(rel, qual)].append("with")
                if (isinstance(n, ast.Attribute)
                        and n.attr == "update_running") or (
                        isinstance(n, ast.Constant)
                        and n.value == "update_running"):
                    running[(rel, qual)] += 1
                if id(n) not in docstrings:
                    key = _busy_sites(n)
                    if key is not None:
                        busy[(rel, qual, key)] += 1
    ops = {k: sorted(v) for k, v in ops.items()}
    return ops, mentions, running, queue, busy, helpers


def enumeration_checks(found):
    ops, mentions, running, queue, busy, helpers = found
    out = []

    want_ops = {k: v[1] for k, v in LOCK_OPS.items()}
    for k in sorted(set(ops) | set(want_ops)):
        role = LOCK_OPS.get(k, ("NEW",))[0]
        label = f"lock ops {k[0]}:{k[1]} ({role}): {ops.get(k)}"
        out.append((label, ops.get(k) == want_ops.get(k),
                    f"expected {want_ops.get(k)}"))

    want_m = set(LOCK_MENTIONS) | set(LOCK_OPS)
    for k in sorted(mentions | want_m):
        ok = k in mentions and k in want_m
        what = "listed but gone" if k not in mentions else \
            "mentions the lock and is in no list"
        out.append((f"mentions the lock: {k[0]}:{k[1]}", ok, what))

    want_r = {k: v[0] for k, v in UPDATE_RUNNING_READS.items() if v[0]}
    for k in sorted(set(running) | set(want_r)):
        out.append((f"reads update_running: {k[0]}:{k[1]} ×{running.get(k, 0)}",
                    running.get(k, 0) == want_r.get(k, 0),
                    f"expected ×{want_r.get(k, 0)}"))
    out.append(("the update_running table names its own probe",
                ("update_engine.py", "UpdateEngine.update_running")
                in UPDATE_RUNNING_READS
                and ("update_engine.py", "UpdateEngine.update_running")
                in ops, "the probe moved"))

    for k in sorted(set(queue) | set(QUEUE_CALLS)):
        out.append((f"queue call {k[0]}:{k[1]} → {k[2]} ×{queue.get(k, 0)}",
                    queue.get(k, 0) == QUEUE_CALLS.get(k, 0),
                    f"expected ×{QUEUE_CALLS.get(k, 0)}"))
    want_h = {k: v[0] for k, v in LOCK_HELPER_CALLS.items()}
    for k in sorted(set(helpers) | set(want_h)):
        out.append((f"asks the lock through {k[2]}: {k[0]}:{k[1]} "
                    f"×{helpers.get(k, 0)}",
                    helpers.get(k, 0) == want_h.get(k, 0),
                    f"expected ×{want_h.get(k, 0)}"))
    out.append((f"the lock-helper callees are {_helper_callees()}",
                _helper_callees() == ["_update_lock_held"],
                "a READER/PROBE was added or renamed in LOCK_OPS; "
                "check LOCK_HELPER_CALLS"))

    drains = {k[0] for k in queue if k[2] != "enqueue_update"}
    out.append((f"only these files work the queue off: {sorted(drains)}",
                drains == {"telegram_bot.py"}, "Q13: today Telegram only"))

    want_b = {k: v[0] for k, v in BUSY_MESSAGES.items()}
    for k in sorted(set(busy) | set(want_b)):
        lab = BUSY_MESSAGES.get(k, (0, "NEW, unlabelled"))[1]
        out.append((f"busy sentence {k[0]}:{k[1]} {k[2]!r} ×{busy.get(k, 0)}"
                    f" [{lab}]", busy.get(k, 0) == want_b.get(k, 0),
                    f"expected ×{want_b.get(k, 0)}"))
    return out


# ═══════════════════════════════════════════════════════════════════════
# Drive
# ═══════════════════════════════════════════════════════════════════════
KEYS = []          # i18n keys translated during a drive, in order
WORK = []          # (what, lock held at the time)
_I18N = []


def _patch_i18n():
    import i18n
    if _I18N:
        return
    real = i18n.get_translator
    _I18N.append(real)

    def get_translator(language="en"):
        t = real(language)

        def recording(key, **kw):
            KEYS.append(key)
            return t(key, **kw)
        return recording
    i18n.get_translator = get_translator


class World:
    """T4's multi-host world, plus a Web handler on the same bot."""

    def __init__(self):
        _patch_i18n()
        import selfupdate
        import web_ui
        dm.CLOCK[0] = 10_000.0
        self.row = dm.Row("multi")
        self.engine = self.row.engine
        self.lock = self.engine._update_lock
        self.discord = self.row.bot
        self.tg = self.discord.telegram
        self.ctx = self.tg.selfupdate_ctx
        self.registry = self.engine.hosts
        self.local = self.registry.local.checker
        for h in self.registry:
            self._record(h.name, h.checker)
        self.tg._check_selfupdate_auto_locked = self._work(
            "selfupdate check body", False)
        self._run = selfupdate.run
        selfupdate.run = self._work("selfupdate.run", None)
        self.handler = web_ui.create_handler(
            self.row.cfg, self.local, self.tg, self.engine.store, None,
            self.registry.local.backend, self.registry)

    def close(self):
        import selfupdate
        selfupdate.run = self._run
        if self.lock.locked():
            self.lock.release()
        shutil.rmtree(self.row.data, ignore_errors=True)

    def _work(self, what, answer):
        def f(*a, **k):
            WORK.append((what, self.lock.locked()))
            return answer
        return f

    def _record(self, host, checker):
        def update_container(name, image, *a, **k):
            WORK.append((f"update_container@{host}:{name}",
                         self.lock.locked()))
            return True, "updated (stand-in)"

        def cleanup_images(*a, **k):
            WORK.append((f"cleanup_images@{host}", self.lock.locked()))
            return True, "Total reclaimed space: 0B"
        checker.update_container = update_container
        checker.cleanup_images = cleanup_images

    def web(self):
        return self.handler.__new__(self.handler)

    def pending(self, host="local", name="web"):
        return {"name": name, "image": "nginx:1.25", "host": host,
                "old_version": "1.25.0", "new_version": "1.26.0"}


def _major(w):
    w.tg._store_for("local").add_pending_major("web", {
        "image": "nginx:1.25", "current": "1.25.0", "latest": "2.0.0"})


def _auto(w):
    w.tg._store_for("local").save_autoupdate(["web"])


def _queued(w):
    # `web` because it is the local entry in the pending file: a queued
    # key that is not pending returns before the update (T3's note on
    # the early returns), and the free drive would see no work.
    w.engine.enqueue_update("web")


def _telegram_off(w):
    w.row.cfg.bot_token = ""


#: (file, qualname) of the site → (label, prepare, call). Every entry of
#: LOCK_OPS except the release helper has at least one drive.
DRIVES = [
    (("telegram_bot.py", "TelegramBot._run_single_update"),
     "tap update_one:web", None,
     lambda w: w.tg._run_single_update(w.local, "web")),
    (("telegram_bot.py", "TelegramBot._confirm_major_update"),
     "confirm_major:web", _major,
     lambda w: w.tg._confirm_major_update(w.local, "web")),
    (("telegram_bot.py", "TelegramBot.handle_autoupdates"),
     "scheduler auto pass, web on auto", _auto,
     lambda w: w.tg.handle_autoupdates([w.pending()], w.local)),
    (("telegram_bot.py", "TelegramBot.cleanup_guarded"),
     "image cleanup", None,
     lambda w: w.tg.cleanup_guarded(w.local)),
    (("telegram_bot.py", "TelegramBot.check_selfupdate_auto"),
     "scheduled self-update check", None,
     lambda w: w.tg.check_selfupdate_auto()),
    (("telegram_bot.py", "TelegramBot.run_updates"),
     "Update all (snapshot web@local)", None,
     lambda w: w.tg.run_updates(w.local, updates=[w.pending()])),
    (("discord_bot.py", "DiscordBot._cmd_update"),
     "/update container:web host:nas", None,
     lambda w: w.discord._cmd_update({"container": "web", "host": "nas"})),
    (("discord_bot.py", "DiscordBot._do_updateall"),
     "updateall confirmed, host:nas", None,
     lambda w: w.discord._do_updateall({"host": "nas"})),
    (("discord_bot.py", "DiscordBot._cleanup_guarded"),
     "image cleanup", None,
     lambda w: w.discord._cleanup_guarded(w.local)),
    (("web_ui.py", "create_handler.WebHandler._run_web_update_batch"),
     "worker for nas/web, Telegram on", None,
     lambda w: w.web()._run_web_update_batch(["nas/web"])),
    (("web_ui.py", "create_handler.WebHandler._run_web_update_batch"),
     "worker for nas/web, Telegram off", _telegram_off,
     lambda w: w.web()._run_web_update_batch(["nas/web"])),
    (("selfupdate.py", "start"),
     "/selfupdate", None,
     lambda w: __import__("selfupdate").start(w.ctx)),
    (("update_engine.py", "UpdateEngine.update_running"),
     "probe", None,
     lambda w: w.engine.update_running),
    (("telegram_bot.py", "TelegramBot._drain_update_queue"),
     "drain with web queued", _queued,
     lambda w: w.tg._drain_update_queue(w.local)),
    (("web_ui.py", "create_handler.WebHandler._update_lock_held"),
     "status banner question", None,
     lambda w: w.web()._update_lock_held(w.tg)),
    (("telegram_bot.py", "TelegramBot._sweep_update_queue"),
     "poll-cycle sweep with web queued", _queued,
     lambda w: w.tg._sweep_update_queue(w.local)),
]


def _kind(rec, role="CLAIM"):
    if rec["work"]:
        return "RAN"
    if role in ("PROBE", "READER"):
        # These never act; what they say is "busy" (True) or, for the
        # queue's two, that they left the queue for later.
        if rec.get("returned") is True:
            return "SAYS_BUSY"
        if rec.get("queue_after"):
            return "LEAVES_QUEUED"
        return "SAYS_FREE"
    if rec.get("queue"):
        return "QUEUE"
    if rec.get("queued_selfupdate"):
        return "QUEUE_SELF"
    ret = rec.get("returned")
    if isinstance(ret, tuple) and ret and ret[0] is None:
        return "SKIP_MSG"
    if rec.get("said") or isinstance(ret, str):
        return "REJECT"
    return "SKIP"


def _short(v):
    if isinstance(v, str) and len(v) > 90:
        return v[:90] + "…"
    if isinstance(v, tuple):
        return tuple(_short(x) for x in v)
    return v


def drive(prepare, call, held):
    """One call. Returns the record (with the lock held by us when
    `held`), plus whatever escaped the stand-ins."""
    m.EVENTS.clear()
    del m.ESCAPES[:]
    del m.STARTED[:]
    del m.THREAD_ERRORS[:]
    del dm.LOG[:]
    w = World()
    try:
        if prepare:
            prepare(w)
        _queue_before = [k for k, _s in w.engine.queued_updates()]
        del KEYS[:]
        del WORK[:]
        del dm.LOG[:]
        if held:
            w.lock.acquire()
        sink = io.StringIO()
        raised = None
        with contextlib.redirect_stdout(sink), \
                contextlib.redirect_stderr(sink):
            try:
                ret = call(w)
            except Exception as e:                  # noqa: BLE001
                ret, raised = None, f"{type(e).__name__}: {e}"
            hung = dm._join()
        still = w.lock.locked()
        rec = {
            "keys": list(dict.fromkeys(KEYS)),
            "said": [_short(p) for k, p in dm.LOG if k == "telegram"],
            "returned": _short(ret),
            "printed": [ln for ln in sink.getvalue().splitlines() if ln],
            "work": list(WORK),
            "queue": [k for k, _s in w.engine.queued_updates()
                      if k not in _queue_before],
            "queue_after": [k for k, _s in w.engine.queued_updates()],
            "queued_selfupdate": w.engine._queued_selfupdate,
            "lock_after": "held" if still else "free",
        }
        if raised:
            rec["raised"] = raised
        if hung:
            rec["hung"] = hung
        if m.THREAD_ERRORS:
            rec["thread_errors"] = list(m.THREAD_ERRORS)
        return rec, list(m.ESCAPES)
    finally:
        w.close()


def _busy_view(rec, role):
    v = {"kind": _kind(rec, role)}
    for k in ("keys", "said", "returned", "printed", "queue_after",
              "queued_selfupdate", "lock_after", "raised", "hung",
              "thread_errors"):
        if rec.get(k) not in (None, [], ""):
            v[k] = rec[k]
    return v


def drive_checks():
    out = []
    driven = set()
    for site, label, prepare, call in DRIVES:
        driven.add(site)
        role = LOCK_OPS.get(site, ("?",))[0]
        name = f"{site[0]}:{site[1].rsplit('.', 1)[-1]} — {label}"

        rec, esc = drive(prepare, call, held=True)
        view = _busy_view(rec, role)
        want = EXPECTED_BUSY.get((site, label))
        why = []
        if esc:
            why.append(f"escaped the stand-ins: {esc}")
        if want is None:
            why.append("no expectation for this drive")
        elif view != want:
            for k in sorted(set(view) | set(want)):
                if view.get(k) != want.get(k):
                    why.append(f"{k}: expected {want.get(k)!r}, "
                               f"got {view.get(k)!r}")
        out.append((f"held: {name} → {view['kind']}", not why,
                    "; ".join(why)))

        # A claim that loses must not give back the lock somebody else
        # holds: that would let a third flow in mid-update. Named on its
        # own so the reason is readable without the record.
        out.append((f"held: {name} leaves the holder's lock alone",
                    rec["lock_after"] == "held" and not rec["work"],
                    f"lock {rec['lock_after']}, work {rec['work']}"))

        rec, esc = drive(prepare, call, held=False)
        if role == "CLAIM" or site[1].endswith(("_drain_update_queue",
                                                "_sweep_update_queue")):
            ok = (bool(rec["work"]) and all(h for _w, h in rec["work"])
                  and rec["lock_after"] == "free" and not esc
                  and "raised" not in rec and not rec["queue_after"])
            out.append((f"free: {name} works under the lock and gives it back",
                        ok, f"work {rec['work']}, lock {rec['lock_after']}, "
                            f"queue {rec['queue_after']}, "
                            f"raised {rec.get('raised')}, escaped {esc}"))
        else:
            ok = (not rec["work"] and rec["lock_after"] == "free"
                  and not esc and "raised" not in rec
                  and rec["returned"] is False)
            out.append((f"free: {name} answers False and leaves it free",
                        ok, f"returned {rec['returned']!r}, "
                            f"lock {rec['lock_after']}, work {rec['work']}"))

    for site, (role, _ops) in sorted(LOCK_OPS.items()):
        if role == "RELEASE":
            continue
        out.append((f"driven: {site[0]}:{site[1]}", site in driven,
                    "a listed site has no drive"))
    for key in sorted(k for k in EXPECTED_BUSY
                      if k not in {(s, lab) for s, lab, _p, _c in DRIVES}):
        out.append((f"expectation without a drive: {key!r}", False, ""))
    return out


# ═══════════════════════════════════════════════════════════════════════
def snapshot():
    found = enumerate_sites()
    ops, mentions, running, queue, busy, helpers = found
    print("# lock ops");  pprint.pprint(ops, width=100)
    print("# mentions");  pprint.pprint(sorted(mentions), width=100)
    print("# update_running"); pprint.pprint(dict(running), width=100)
    print("# queue");     pprint.pprint(dict(queue), width=100)
    print("# lock helpers"); pprint.pprint(dict(helpers), width=100)
    print("# busy");      pprint.pprint(dict(busy), width=100)
    print("EXPECTED_BUSY = {")
    for site, label, prepare, call in DRIVES:
        rec, esc = drive(prepare, call, held=True)
        print(f"    ({site!r},\n     {label!r}):")
        role = LOCK_OPS.get(site, ("?",))[0]
        print(textwrap_indent(pprint.pformat(_busy_view(rec, role), width=88),
                              "        ") + ",")
        if esc:
            print(f"    # ESCAPED: {esc}")
    print("}")
    return 0


def textwrap_indent(text, pad):
    return "\n".join(pad + ln for ln in text.splitlines())


def check():
    failed = 0
    for label, ok, why in enumeration_checks(enumerate_sites()) \
            + drive_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failed += 1
            if why:
                print(f"      → {why}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


def main():
    m._TMP_ROOT = tempfile.mkdtemp(prefix="lock-sites-")
    try:
        if "--snapshot" in sys.argv:
            return snapshot()
        return check()
    finally:
        shutil.rmtree(m._TMP_ROOT, ignore_errors=True)


# ── busy records, generated by --snapshot from main (8a74ba7), reviewed ──
EXPECTED_BUSY = {
    (('telegram_bot.py', 'TelegramBot._run_single_update'),
     'tap update_one:web'):
        {'keys': ['update_queued'],
         'kind': 'QUEUE',
         'lock_after': 'held',
         'queue_after': ['web'],
         'said': ['🕒 `web` queued — position 1. It starts when the current update finishes.']},
    (('telegram_bot.py', 'TelegramBot._confirm_major_update'),
     'confirm_major:web'):
        {'keys': ['update_already_running'],
         'kind': 'REJECT',
         'lock_after': 'held',
         'said': ['⚠️ Update already in progress...']},
    (('telegram_bot.py', 'TelegramBot.handle_autoupdates'),
     'scheduler auto pass, web on auto'):
        {'kind': 'SKIP',
         'lock_after': 'held',
         'printed': ['Auto-update skipped: another update flow is running (will retry next '
                     'tick)'],
         'returned': 0},
    (('telegram_bot.py', 'TelegramBot.cleanup_guarded'),
     'image cleanup'):
        {'keys': ['cleanup_busy'],
         'kind': 'SKIP_MSG',
         'lock_after': 'held',
         'returned': (None,
                      "⏳ Updates in progress — cleanup skipped so it can't delete a freshly "
                      'pulled image. Try aga…')},
    (('telegram_bot.py', 'TelegramBot.check_selfupdate_auto'),
     'scheduled self-update check'):
        {'kind': 'SKIP',
         'lock_after': 'held',
         'printed': ['Selfupdate check: skipped — another update flow is running.'],
         'returned': False},
    (('telegram_bot.py', 'TelegramBot.run_updates'),
     'Update all (snapshot web@local)'):
        {'keys': ['update_already_running'],
         'kind': 'REJECT',
         'lock_after': 'held',
         'said': ['⚠️ Update already in progress...']},
    (('discord_bot.py', 'DiscordBot._cmd_update'),
     '/update container:web host:nas'):
        {'keys': ['chan_update_busy'],
         'kind': 'REJECT',
         'lock_after': 'held',
         'returned': '⏳ An update is already running — nothing was started. Try again once it '
                     'finishes.'},
    (('discord_bot.py', 'DiscordBot._do_updateall'),
     'updateall confirmed, host:nas'):
        {'keys': ['chan_update_busy'],
         'kind': 'REJECT',
         'lock_after': 'held',
         'returned': '⏳ An update is already running — nothing was started. Try again once it '
                     'finishes.'},
    (('discord_bot.py', 'DiscordBot._cleanup_guarded'),
     'image cleanup'):
        {'keys': ['cleanup_busy'],
         'kind': 'SKIP_MSG',
         'lock_after': 'held',
         'returned': (None,
                      "⏳ Updates in progress — cleanup skipped so it can't delete a freshly "
                      'pulled image. Try aga…')},
    (('web_ui.py', 'create_handler.WebHandler._run_web_update_batch'),
     'worker for nas/web, Telegram on'):
        {'keys': ['update_already_running'],
         'kind': 'REJECT',
         'lock_after': 'held',
         'said': ['⚠️ Update already in progress...']},
    (('web_ui.py', 'create_handler.WebHandler._run_web_update_batch'),
     'worker for nas/web, Telegram off'):
        {'kind': 'SKIP', 'lock_after': 'held'},
    (('selfupdate.py', 'start'),
     '/selfupdate'):
        {'keys': ['selfupdate_queued'],
         'kind': 'QUEUE_SELF',
         'lock_after': 'held',
         'queued_selfupdate': (None,),
         'said': ['⏳ Container updates in progress — self-update queued, it will start '
                  'automatically once the…']},
    (('update_engine.py', 'UpdateEngine.update_running'),
     'probe'):
        {'kind': 'SAYS_BUSY', 'lock_after': 'held', 'returned': True},
    (('telegram_bot.py', 'TelegramBot._drain_update_queue'),
     'drain with web queued'):
        {'kind': 'LEAVES_QUEUED', 'lock_after': 'held', 'queue_after': ['web']},
    (('web_ui.py', 'create_handler.WebHandler._update_lock_held'),
     'status banner question'):
        {'kind': 'SAYS_BUSY', 'lock_after': 'held', 'returned': True},
    (('telegram_bot.py', 'TelegramBot._sweep_update_queue'),
     'poll-cycle sweep with web queued'):
        {'kind': 'LEAVES_QUEUED', 'lock_after': 'held', 'queue_after': ['web']},
}


if __name__ == "__main__":
    sys.exit(main())
