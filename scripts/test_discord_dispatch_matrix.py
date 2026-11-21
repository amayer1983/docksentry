#!/usr/bin/env python3
"""Discord command matrix: what every slash command, button and autocomplete answers today, through the real `_on_event`.

The core split moves Discord's `_dispatch` chain into a table, gives
`host: all` a meaning on read commands (A2), and changes how a dead or
refused host is reported (W5d, W7d; W3d measured /status <container>
and found nothing to fix). test_discord_bot.py drives one command at a
time against duck-typed fakes; this one drives every command in every
option form against a real `DiscordBot` and compares the result with a
table taken from main before anything moved.

Two set-ups, the same rows in each:

  * `multi` — the Telegram matrices' five-host registry: `local` and
              `nas` answer, `dead` raises TimeoutExpired on every call,
              `refused` returns rc=255 with ssh's "Connection refused",
              `flaky` lists its containers but its checker's `check_all`
              raises;
  * `one`   — a registry holding only the local host. main.py always
              hands Discord a registry, so there is no `hosts=None` row.

Every row enters where the gateway would hand it over: an
INTERACTION_CREATE through `_on_event`, so the guild and allow-list
check, the deferred acknowledgement, the worker pool, `_dispatch` /
`_on_component` / `_autocomplete`, `_deliver` and `_shutdown_if_asked`
all run for real. The worker thread is started for real and joined
before the next step. A row is a short scenario, because a button only
means something after the command that offered it.

Per step the table holds the transport log in order (every REST call
the bot made, as acknowledgement / edit / channel post / followup /
upload / autocomplete, with components as label→custom_id; Telegram
sends through the shared broadcast seam; the bot's own log lines),
which host backends were called and with what (argv verb, flags,
timeout), and the environment events. After the last step: the pending
file, the live confirmations, the update lock, a queued self-update,
the armed shutdown, the audit records (without their timestamp) and
the JSON state files in the data dir.

What is real: everything the Telegram matrices list as real (it is
their fixture, imported: Config from a scrubbed environment, the
stores, the registry, the checkers apart from their registry half),
plus `DiscordBot`, a real `UpdateEngine` shared with a real
`TelegramBot`, the `Broadcast` seam and `Notifier` built as main.py
builds them, `selfupdate.Context`, `AuditLog`, and `DiscordREST` down
to its payloads.

Stand-ins, on top of the Telegram matrices' (their header lists them):

  * the Discord transport: `DiscordREST.request` and `DiscordREST.upload`
    record instead of sending; everything above them — payload shape,
    ephemeral flag, components — is the real client's. Patched in
    `discord_bot` (the bot and `send_private`) and in `discord_rest`
    (the bot-channel notifier imports it there). The gateway is never
    started; `_on_event` is called the way it would call it;
  * the attachment fetch in `/restore`: `urlopen` answers four fixed CDN
    URLs (a bundle, plain text, a JSON list, a 404) and stays the trap
    for everything else;
  * the Telegram bot's `send_message` / `api_call` record (it is only
    there because main.py hands it to Discord and to the seam);
  * the bot's clock (`_now`), so an `age` step can move past the
    15-minute confirmation TTL without waiting.

`subprocess.run`, `Popen`, `urlopen` and the socket connects stay the
Telegram matrices' trap: any of those past the stand-ins turns its row
red (the list is in test_telegram_dispatch_matrix's docstring). The
first checks drive that trap on purpose through this entry path.

`python3 scripts/test_discord_dispatch_matrix.py --snapshot [--app DIR]`
prints a fresh expectation table from DIR's app code (default this
tree's). The committed table was generated from main (8a74ba7) and then
read row by row against the branch each one lands in.
"""

import contextlib
import copy
import io
import json
import os
import pprint
import re
import shutil
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request

import test_telegram_dispatch_matrix as m

GUILD = "555"
CHANNEL = "chan-1"
APP_ID = "app-1"
USER, OTHER, STRANGER = m.USER, "8", "9"
ENV = {"DISCORD_BOT_TOKEN": "discord-matrix", "DISCORD_APP_ID": APP_ID,
       "DISCORD_GUILD_ID": GUILD, "DISCORD_ALLOWED_USERS": f"{USER},{OTHER}",
       "DISCORD_BOT_CHANNEL": CHANNEL}
SETUPS = ("multi", "one")

# ── the Discord transport ──────────────────────────────────────────────
LOG = []          # (kind, payload) in the order things happened


def _make_rest_class():
    from discord_rest import DiscordREST

    class RecordingREST(DiscordREST):
        """The real client with the wire cut: `request` and `upload`
        record what would have gone out."""

        who = "rest"

        def request(self, method, path, payload=None, *, attempts=3):
            LOG.append(("rest", (self.who, method, path,
                                 copy.deepcopy(payload))))
            if method == "POST" and path == "/users/@me/channels":
                return {"id": "dm-" + str((payload or {}).get(
                    "recipient_id"))}
            return None

        def upload(self, path, filename, data, content="", *, method="POST"):
            try:
                keys = sorted(json.loads(data))
            except (ValueError, TypeError):
                keys = ["<not json>"]
            LOG.append(("rest", (self.who, "UPLOAD", path,
                                 {"filename": filename, "keys": keys,
                                  "content": content})))
            return None

    return RecordingREST


# ── the attachment fetch ───────────────────────────────────────────────
CDN = "https://cdn.discordapp.invalid/attachments/"


def _bundle():
    import backup
    return {"schema_version": backup.SCHEMA_VERSION, "instance": "matrix",
            "generated_at": "2026-11-19T10:00:00", "docksentry_version": "x",
            "pinned": ["db"], "notes": {"web": "hello"}}


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self, n=-1):
        return self.raw if n is None or n < 0 else self.raw[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_cdn(trap):
    def urlopen(req, *a, **kw):
        url = getattr(req, "full_url", req)
        if isinstance(url, str) and url.startswith(CDN):
            m.EVENTS.append("fetch " + url[len(CDN):])
            what = url[len(CDN):]
            if what == "backup.json":
                return _Body(json.dumps(_bundle()).encode())
            if what == "text.json":
                return _Body(b"this is not json")
            if what == "list.json":
                return _Body(b"[1, 2]")
            if what == "gone.json":
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return trap(req, *a, **kw)
    return urlopen


_PATCHED = False


def _patch():
    global _PATCHED
    m._patch_environment()
    if _PATCHED:
        return
    _PATCHED = True
    import discord_bot
    import discord_rest
    cls = _make_rest_class()
    discord_bot.DiscordREST = cls
    discord_rest.DiscordREST = cls
    urllib.request.urlopen = _make_cdn(urllib.request.urlopen)


# ── one row ────────────────────────────────────────────────────────────
CLOCK = [10_000.0]


def _opt_type(cmd, opt):
    """The option type Discord would send, from the registered table
    (4 = integer for `seconds` and `lines`); 3 for anything unlisted."""
    from discord_bot import COMMANDS
    for c in COMMANDS:
        if c["name"] == cmd:
            for o in c.get("options") or []:
                if o["name"] == opt:
                    return o["type"]
    return 3


class Row:
    def __init__(self, setup):
        _patch()
        from audit import AuditLog
        from broadcast import Broadcast
        from discord_bot import DiscordBot
        from notifier import Notifier
        from telegram_bot import TelegramBot
        from update_engine import UpdateEngine
        import selfupdate

        self.setup = setup
        (cfg, store, managed, registry, self.backends,
         self.data) = m.build_world(setup, ENV)
        self.cfg = cfg
        # As main.py wires them.
        engine = UpdateEngine(cfg, store, hosts=registry)
        tg = TelegramBot(cfg, store, engine, hosts=registry)
        self.tg = tg                # W5d asks it for its own /status
        tg.backend = self.backends["local"]
        tg.send_message = lambda text, reply_markup=None, auto=False, \
            _held=False: (LOG.append(("telegram", text)), True)[1]
        tg.api_call = lambda method, data=None, *a, **k: (
            LOG.append(("telegram", f"<api {method}>")),
            {"ok": True, "result": {}})[1]
        notifier = Notifier(cfg)
        seam = Broadcast(telegram=tg, notifier=notifier)
        tg.broadcast = seam
        engine.broadcast = seam
        ctx = selfupdate.Context(engine, cfg, seam.tell)
        tg.selfupdate_ctx = ctx
        self.engine = engine
        self.bot = DiscordBot(cfg, store, engine, hosts=registry,
                              checker=registry.local.checker,
                              log=lambda s: LOG.append(("log", s)),
                              telegram=tg, broadcast=seam,
                              selfupdate_ctx=ctx)
        self.bot.rest.who = "bot"
        self.bot.audit = AuditLog(cfg)
        self.bot._now = lambda: CLOCK[0]
        m._seed_pending(cfg, setup)
        self.n = 0
        self.tokens = {}            # confirmation token → "<tokN>"
        self.offered = []           # (action, token), newest last

    # ── interactions ──
    def _interaction(self, kind, data, user=USER, guild=GUILD):
        self.n += 1
        return {"id": f"i{self.n}", "token": f"itok{self.n}", "type": kind,
                "guild_id": guild, "channel_id": CHANNEL,
                "member": {"user": {"id": user, "username": f"u{user}"}},
                "data": data}

    def _token_for(self, action):
        for act, tok in reversed(self.offered):
            if action in (act, "cancel", "*"):
                return tok
        return "0" * 16

    def do(self, step):
        kind, args = step[0], step[1:]
        bot = self.bot
        if kind == "cmd":
            name, opts = args[0], args[1]
            who = args[2] if len(args) > 2 else {}
            data = {"name": name, "type": 1, "options": [
                {"name": k, "type": _opt_type(name, k), "value": v}
                for k, v in opts]}
            if name == "restore":
                data["options"] = [{"name": "file", "type": 11,
                                    "value": "att1"}]
                data["resolved"] = {"attachments": {"att1": dict(
                    opts, id="att1")}}
            self.cur = self._interaction(2, data, **who)
            bot._on_event("INTERACTION_CREATE", self.cur)
        elif kind == "press":
            action = args[0]
            who = args[1] if len(args) > 1 else {}
            tok = self._token_for(args[2] if len(args) > 2 else action)
            self.cur = self._interaction(
                3, {"custom_id": f"ds:{action}:{tok}", "component_type": 2},
                **who)
            bot._on_event("INTERACTION_CREATE", self.cur)
        elif kind == "raw_press":
            cid = args[0]
            for tok, rep in self.tokens.items():
                cid = cid.replace(rep, tok)
            self.cur = self._interaction(
                3, {"custom_id": cid, "component_type": 2})
            bot._on_event("INTERACTION_CREATE", self.cur)
        elif kind == "auto":
            name, opts = args[0], args[1]
            who = args[2] if len(args) > 2 else {}
            data = {"name": name, "type": 1, "options": [
                dict({"name": k, "type": _opt_type(name, k), "value": v},
                     **({"focused": True} if f else {}))
                for k, v, f in opts]}
            self.cur = self._interaction(4, data, **who)
            bot._on_event("INTERACTION_CREATE", self.cur)
        elif kind == "event":
            self.cur = self._interaction(args[1], {"name": "status"})
            bot._on_event(args[0], self.cur)
        elif kind == "lock":
            self.engine._update_lock.acquire()
        elif kind == "unlock":
            self.engine._update_lock.release()
        elif kind == "release":
            # The holder lets go through the door, as every flow does:
            # the release kicks the engine's drainer (Q13), which runs
            # what Discord queued (Q15).
            self.engine.release_update()
        elif kind == "enqueue":
            self.engine.enqueue_updates(list(args))
        elif kind == "fill":
            # The queue up to all but `args[0]` places, with filler keys
            # (Q16 raised its size from 10 to 50).
            self.engine.enqueue_updates([
                f"q{i}" for i in range(
                    self.engine.UPDATE_QUEUE_MAX - args[0])])
        elif kind == "age":
            import discord_bot
            CLOCK[0] += discord_bot.CONFIRM_TTL + 1
        elif kind == "add_pending":
            from container_store import atomic_write_json
            atomic_write_json(self.cfg.pending_file, self._pending_raw() + [
                {"name": args[0], "image": args[1], "host": args[2],
                 "old_version": "1.0.0", "new_version": "1.1.0"}])
        elif kind == "drop_pending":
            if os.path.exists(self.cfg.pending_file):
                os.remove(self.cfg.pending_file)
        elif kind == "call":            # the W7d checks only, never a row
            args[0]()
        else:
            raise ValueError(f"unknown step {kind!r}")

    def _pending_raw(self):
        try:
            with open(self.cfg.pending_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    # ── recording ──
    def norm(self, s):
        from version import VERSION
        s = str(s).replace(self.data, "{DATA}").replace(VERSION, "{VERSION}")
        for tok, rep in self.tokens.items():
            s = s.replace(tok, rep)
        s = re.sub(r"\d{4}-\d{2}-\d{2}[T _]\d{2}[:\-]?\d{2}([:\-]?\d{2})?",
                   "<ts>", s)
        s = re.sub(r"\b\d{8}-\d{6}\b", "<stamp>", s)
        s = re.sub(r"\buntil \d{2}:\d{2}\b", "until <hh:mm>", s)
        s = re.sub(r"\b1\d{9}(\.\d+)?\b", "<epoch>", s)
        return m._short(s)

    def _learn(self, payload):
        for row in (payload or {}).get("components") or []:
            for c in row.get("components") or []:
                mt = re.match(r"ds:(\w+):([0-9a-f]{16})$",
                              c.get("custom_id") or "")
                if mt:
                    tok = mt.group(2)
                    if tok not in self.tokens:
                        self.tokens[tok] = f"<tok{len(self.tokens) + 1}>"
                    if mt.group(1) != "cancel":
                        self.offered.append((mt.group(1), tok))

    def _components(self, payload):
        rows = (payload or {}).get("components")
        if rows is None:
            return ""
        if not rows:
            return "  []"
        out = "  " + " ".join("[" + " | ".join(
            f"{c.get('label', '')}→{c.get('custom_id', '')}"
            for c in r.get("components") or []) + "]" for r in rows)
        for tok, rep in self.tokens.items():
            out = out.replace(tok, rep)
        return out

    def _embeds(self, payload):
        embeds = (payload or {}).get("embeds") or []
        if not embeds:
            return ""
        return "  embeds=" + self.norm(json.dumps(
            [{k: e[k] for k in ("title", "description") if k in e}
             for e in embeds], ensure_ascii=False))

    def line(self, entry):
        kind, body = entry
        if kind == "telegram":
            return "<telegram> " + self.norm(body)
        if kind == "log":
            return "<log> " + self.norm(body)
        who, method, path, p = body
        p = p or {}
        self._learn(p)
        d = p.get("data") or {}
        if path.endswith("/callback"):
            eph = " ephemeral" if (d.get("flags") == 64) else ""
            if p.get("type") == 5:
                s = f"<ack deferred{eph}>"
            elif p.get("type") == 8:
                s = "<autocomplete> " + ", ".join(
                    c["value"] for c in d.get("choices") or [])
            else:
                s = (f"<reply{eph}> " + self.norm(d.get("content", ""))
                     + self._components(d))
        elif method == "PATCH" and path.endswith("/messages/@original"):
            tok = path.split("/")[3]
            here = tok == self.cur.get("token")
            s = (("<edit> " if here else f"<edit {tok}> ")
                 + self.norm(p.get("content", "")) + self._components(p))
        elif method == "POST" and path.startswith("/channels/"):
            s = (f"<channel {path.split('/')[2]}> "
                 + self.norm(p.get("content", "")) + self._components(p)
                 + self._embeds(p))
        elif method == "POST" and path == "/users/@me/channels":
            s = f"<dm-open {p.get('recipient_id')}>"
        elif method == "POST" and path.startswith("/webhooks/"):
            eph = " ephemeral" if p.get("flags") == 64 else ""
            s = f"<followup{eph}> " + self.norm(p.get("content", ""))
        elif method == "UPLOAD":
            s = (f"<upload {path.split('/')[1]}> "
                 + self.norm(p["filename"]) + f" {p['keys']} "
                 + self.norm(p["content"]))
        else:
            s = f"<{method} {path}> " + self.norm(json.dumps(p))
        return s if who == "bot" else f"[{who}] {s}"

    def state(self):
        from container_store import entry_host
        pf = self.cfg.pending_file
        pending = None
        if os.path.exists(pf):
            pending = [f"{u.get('name')}@{entry_host(u)}"
                       for u in self._pending_raw() if isinstance(u, dict)]
        audit = []
        apath = getattr(self.cfg, "audit_file", "")
        if apath and os.path.exists(apath):
            with open(apath, encoding="utf-8") as f:
                for e in json.load(f):
                    e = {k: v for k, v in e.items() if k != "timestamp"}
                    audit.append(self.norm(json.dumps(e, sort_keys=True)))
        files = {}
        skip = {os.path.abspath(pf), os.path.abspath(apath or "-")}
        for root, _dirs, names in os.walk(self.data):
            for n in sorted(names):
                p = os.path.join(root, n)
                if os.path.abspath(p) in skip or not n.endswith(".json"):
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        body = json.dumps(json.load(f), sort_keys=True)
                except (OSError, ValueError):
                    body = "<unreadable>"
                files[self.norm(os.path.relpath(p, self.data))] = \
                    self.norm(body)
        live = sorted(r["action"] for r in self.bot._confirmations.values())
        out = {"pending": pending, "confirmations": live,
               "locked": self.engine._update_lock.locked(),
               "queued": [k for k, _w in self.engine.queued_updates()],
               "queued_selfupdate": self.engine._queued_selfupdate,
               "shutdown_armed": self.bot._shutdown_after_answer,
               "audit": audit, "files": files}
        out["queued"] = _compact_queue(out["queued"])
        return {k: v for k, v in out.items() if v not in (None, False, [], {})
                or k == "pending"}


def _compact_queue(keys):
    """The queue as the table shows it: a run of the filler keys `q0`,
    `q1`, … (the `fill` step) as one "q0…q48 (49)", the rest as they
    are. The queue holds fifty since Q16, and fifty names in a row would
    bury the one key a row is about."""
    out, run = [], []

    def flush():
        if len(run) > 3:
            out.append(f"{run[0]}…{run[-1]} ({len(run)})")
        else:
            out.extend(run)
        del run[:]
    for k in keys:
        if re.fullmatch(r"q\d+", k) and (
                not run or int(k[1:]) == int(run[-1][1:]) + 1):
            run.append(k)
            continue
        flush()
        if re.fullmatch(r"q\d+", k):
            run.append(k)
        else:
            out.append(k)
    flush()
    return out


def _join():
    hung, i = [], 0
    while i < len(m.STARTED):                       # threads may start threads
        m.STARTED[i].join(20)
        if m.STARTED[i].is_alive():
            hung.append(m.STARTED[i].name)
        i += 1
    del m.STARTED[:]
    return hung


#: The dead-host memory's clock; only a "call" step moves it.
HOSTS_CLOCK = [4242.0]


class _FrozenClock:
    """`hosts.time` while a row runs: the dead-host memory's cooldown
    does not tick, so "not asked again for 60s" is 60 whatever the
    machine's load (W7d)."""

    @staticmethod
    def monotonic():
        return HOSTS_CLOCK[0]


#: The raw content of each step's last edit of the original response,
#: in step order, for the last row driven — the table clips long lines,
#: the W7d checks read them whole. Not part of the record.
STEP_TEXT = []


def run_scenario(setup, steps):
    """Drive one row; return (record, escapes). The dead-host memory
    starts empty and its clock stands still for the whole row."""
    import hosts as hostslib
    real = hostslib.time
    hostslib.time = _FrozenClock
    HOSTS_CLOCK[0] = 4242.0
    try:
        return _run_scenario(setup, steps)
    finally:
        hostslib.time = real


def _run_scenario(setup, steps):
    import hosts as hostslib
    hostslib.forget_unreachable()
    del STEP_TEXT[:]
    m.EVENTS.clear()
    del m.ESCAPES[:]
    del m.STARTED[:]
    del m.THREAD_ERRORS[:]
    del LOG[:]
    CLOCK[0] = 10_000.0
    row = Row(setup)
    m.EVENTS.clear()                # building leaves nothing worth pinning
    for b in row.backends.values():
        del b.calls[:]
    del LOG[:]
    _join()
    out_steps = []
    sink = io.StringIO()
    for step in steps:
        row.cur = {}
        mark = {n: len(b.calls) for n, b in row.backends.items()}
        m.EVENTS.clear()
        del m.THREAD_ERRORS[:]
        del LOG[:]
        raised = None
        with contextlib.redirect_stdout(sink), \
                contextlib.redirect_stderr(sink):
            try:
                row.do(step)
            except Exception as e:                  # noqa: BLE001
                raised = type(e).__name__
            hung = _join()
        rec = {"do": row.norm(_label_step(step))}
        STEP_TEXT.append(_w5d_edit_text())
        out = [row.line(e) for e in LOG]
        touched = {n: m._calls_summary(b.calls[mark[n]:], keep=("--tail",))
                   for n, b in row.backends.items()
                   if len(b.calls) > mark[n]}
        events = [row.norm(e) for e in m.EVENTS] + list(m.THREAD_ERRORS) \
            + [f"thread did not finish: {h}" for h in hung]
        for k, v in (("out", out), ("hosts", touched), ("events", events),
                     ("raised", raised)):
            if v:
                rec[k] = v
        out_steps.append(rec)
    state = row.state()
    if row.engine._update_lock.locked():
        row.engine._update_lock.release()
    escapes = list(m.ESCAPES)
    shutil.rmtree(row.data, ignore_errors=True)
    return {"steps": out_steps, "state": state}, escapes


def _label_step(step):
    kind, args = step[0], step[1:]
    if kind in ("cmd", "auto"):
        opts = " ".join(
            f"{o[0]}:{o[1]!r}" + ("*" if len(o) > 2 and o[2] else "")
            for o in args[1])
        who = f" {args[2]}" if len(args) > 2 else ""
        return f"{kind} /{args[0]} {opts}".rstrip() + who
    return " ".join([kind] + [x if isinstance(x, str) else repr(x)
                              for x in args])


# ── the rows ───────────────────────────────────────────────────────────
HOSTS = (None, "nas", "all", "zzz")
CONTAINERS = (None, "web", "web*", "nosuch")


def _opts_of(cmd):
    return [o["name"] for o in cmd.get("options") or []]


def _cmd_name(name, opts):
    return "/" + name + "".join(f" {k}:{v}" for k, v in opts)


def _command_rows():
    from discord_bot import COMMANDS
    rows = []
    for cmd in COMMANDS:
        names = _opts_of(cmd)
        cs = CONTAINERS if "container" in names else (None,)
        hs = HOSTS if "host" in names else (None,)
        for c in cs:
            for h in hs:
                opts = []
                if c is not None:
                    opts.append(("container", c))
                if h is not None:
                    opts.append(("host", h))
                rows.append((_cmd_name(cmd["name"], opts),
                             [("cmd", cmd["name"], opts)]))
    return rows


def _c(name, *opts, **who):
    return ("cmd", name, list(opts)) + ((who,) if who else ())


def _att(kind):
    return {
        "ok": {"filename": "backup.json", "size": 300,
               "url": CDN + "backup.json"},
        "txt": {"filename": "backup.txt", "size": 300,
                "url": CDN + "backup.json"},
        "big": {"filename": "backup.json", "size": 3 * 1024 * 1024,
                "url": CDN + "backup.json"},
        "text": {"filename": "backup.json", "size": 16,
                 "url": CDN + "text.json"},
        "list": {"filename": "backup.json", "size": 6,
                 "url": CDN + "list.json"},
        "gone": {"filename": "backup.json", "size": 300,
                 "url": CDN + "gone.json"},
    }[kind]


def _restore(kind, **who):
    return ("cmd", "restore", list(_att(kind).items())) \
        + ((who,) if who else ())


EXTRA = [
    # other options, one at a time
    ("/note container:web text:'a  b'", [_c("note", ("container", "web"),
                                             ("text", "a  b"))]),
    ("/note container:web text:'x' host:nas",
     [_c("note", ("container", "web"), ("text", "x"), ("host", "nas"))]),
    ("/setlink container:web url:https", [_c(
        "setlink", ("container", "web"), ("url", "https://example.org/x"))]),
    ("/setlink container:web url:notaurl", [_c(
        "setlink", ("container", "web"), ("url", "notaurl"))]),
    ("/cooldown container:web seconds:3600", [_c(
        "cooldown", ("container", "web"), ("seconds", 3600))]),
    ("/cooldown container:web seconds:0", [_c(
        "cooldown", ("container", "web"), ("seconds", 0))]),
    ("/cooldown container:web seconds:-5", [_c(
        "cooldown", ("container", "web"), ("seconds", -5))]),
    ("/cooldown container:web seconds:3600 host:all", [_c(
        "cooldown", ("container", "web"), ("seconds", 3600),
        ("host", "all"))]),
    ("/logs container:web lines:5", [_c("logs", ("container", "web"),
                                        ("lines", 5))]),
    ("/logs container:web lines:500", [_c("logs", ("container", "web"),
                                          ("lines", 500))]),
    ("/logs container:web lines:0", [_c("logs", ("container", "web"),
                                        ("lines", 0))]),
    ("/logs container:web host:local", [_c("logs", ("container", "web"),
                                           ("host", "local"))]),
    ("/status host:local", [_c("status", ("host", "local"))]),
    ("/status container:web host:local", [_c(
        "status", ("container", "web"), ("host", "local"))]),
    ("/status container:media", [_c("status", ("container", "media"))]),
    ("/status container:we", [_c("status", ("container", "we"))]),
    ("/status host:dead", [_c("status", ("host", "dead"))]),
    ("/status host:refused", [_c("status", ("host", "refused"))]),
    ("/status container:web host:dead", [_c(
        "status", ("container", "web"), ("host", "dead"))]),
    ("/status container:web host:refused", [_c(
        "status", ("container", "web"), ("host", "refused"))]),
    ("/check host:dead", [_c("check", ("host", "dead"))]),
    ("/check host:refused", [_c("check", ("host", "refused"))]),
    ("/check host:flaky", [_c("check", ("host", "flaky"))]),
    ("/update container:media host:nas", [_c(
        "update", ("container", "media"), ("host", "nas"))]),
    ("/selfupdate version:previous", [_c("selfupdate",
                                         ("version", "previous"))]),
    ("/selfupdate version:2.0.0", [_c("selfupdate", ("version", "2.0.0"))]),
    ("/lang code:de", [_c("lang", ("code", "de"))]),
    ("/lang code:xx", [_c("lang", ("code", "xx"))]),
    ("/lang code:' DE '", [_c("lang", ("code", " DE "))]),
    ("/maintenance duration:on", [_c("maintenance", ("duration", "on"))]),
    ("/maintenance duration:forever", [_c("maintenance",
                                          ("duration", "forever"))]),
    ("/maintenance duration:off", [_c("maintenance", ("duration", "off"))]),
    ("/maintenance duration:abc", [_c("maintenance", ("duration", "abc"))]),
    ("/maintenance duration:2h, then bare", [
        _c("maintenance", ("duration", "2h")), _c("maintenance")]),
    ("/hosts check:ssh://x", [_c("hosts", ("check", "ssh://x"))]),
    ("/debug twice", [_c("debug"), _c("debug")]),
    ("/restore .txt", [_restore("txt")]),
    ("/restore too large", [_restore("big")]),
    ("/restore not json", [_restore("text")]),
    ("/restore not a backup", [_restore("list")]),
    ("/restore 404", [_restore("gone")]),
    ("unknown command", [_c("nosuchcmd")]),
    # who may ask
    ("stranger user", [_c("status", user=STRANGER)]),
    ("other guild", [_c("status", guild="666")]),
    ("not an interaction", [("event", "MESSAGE_CREATE", 2)]),
    ("modal submit", [("event", "INTERACTION_CREATE", 5)]),
    # the lock held by another flow
    ("lock: /update container:web", [
        ("lock",), _c("update", ("container", "web")), ("unlock",)]),
    # Q15: queued while the lock is held, run when it is given back.
    ("lock: /update container:web, then released", [
        ("lock",), _c("update", ("container", "web")), ("release",)]),
    ("lock: /update container:web twice", [
        ("lock",), _c("update", ("container", "web")),
        _c("update", ("container", "web")), ("unlock",)]),
    ("lock: /update container:web host:all, then released", [
        ("lock",), _c("update", ("container", "web"), ("host", "all")),
        ("release",)]),
    ("lock: /update container:web, queue full", [
        ("lock",), ("fill", 0),
        _c("update", ("container", "web")), ("unlock",)]),
    ("lock: /update container:web host:all, queue nearly full", [
        ("lock",), ("fill", 1),
        _c("update", ("container", "web"), ("host", "all")), ("unlock",)]),
    # Three hosts with `web` pending on a free lock: one claim, every
    # host's batch under it, nothing queued.
    ("/update container:web host:all, three hosts pending", [
        ("add_pending", "web", "nginx:1.25", "flaky"),
        _c("update", ("container", "web"), ("host", "all"))]),
    ("lock: /check", [("lock",), _c("check"), ("unlock",)]),
    ("lock: /cleanup", [("lock",), _c("cleanup"), ("unlock",)]),
    ("lock: /stop container:web", [
        ("lock",), _c("stop", ("container", "web")), ("unlock",)]),
    ("lock: /restart container:web", [
        ("lock",), _c("restart", ("container", "web")), ("unlock",)]),
    ("lock: /selfupdate, then /update releases it", [
        ("lock",), _c("selfupdate"), ("unlock",),
        _c("update", ("container", "web"))]),
    # a second read right after a host failed (W7d's cache)
    ("/status twice", [_c("status"), _c("status")]),
    ("/check twice", [_c("check"), _c("check")]),
    ("pending emptied: /update container:web", [
        ("drop_pending",), _c("update", ("container", "web"))]),
    ("pending emptied: /updates", [("drop_pending",), _c("updates")]),
]

SCENARIOS = [
    # ── stop ────────────────────────────────────────────────────────
    ("stop: press, press again", [
        _c("stop", ("container", "web")), ("press", "stop"),
        ("press", "stop")]),
    ("stop host:nas: press", [
        _c("stop", ("container", "web"), ("host", "nas")),
        ("press", "stop")]),
    ("stop host:all: press", [
        _c("stop", ("container", "web"), ("host", "all")),
        ("press", "stop")]),
    ("stop web*: press", [
        _c("stop", ("container", "web*")), ("press", "stop")]),
    ("stop: foreign press, then owner", [
        _c("stop", ("container", "web")), ("press", "stop", {"user": OTHER}),
        ("press", "stop")]),
    ("stop: stranger press", [
        _c("stop", ("container", "web")),
        ("press", "stop", {"user": STRANGER})]),
    ("stop: expired", [
        _c("stop", ("container", "web")), ("age",), ("press", "stop")]),
    ("stop: cancel, then press", [
        _c("stop", ("container", "web")), ("press", "cancel"),
        ("press", "stop")]),
    ("stop: pressed as updateall", [
        _c("stop", ("container", "web")), ("press", "updateall", {}, "stop")]),
    ("stop: two offers, the first pressed", [
        _c("stop", ("container", "web")), _c("stop", ("container", "db")),
        ("raw_press", "ds:stop:<tok1>")]),
    ("stop: lock taken before the press", [
        _c("stop", ("container", "web")), ("lock",), ("press", "stop"),
        ("unlock",)]),
    # ── updateall ───────────────────────────────────────────────────
    ("updateall: press, press again", [
        _c("updateall"), ("press", "updateall"), ("press", "updateall")]),
    ("updateall host:nas: press", [
        _c("updateall", ("host", "nas")), ("press", "updateall")]),
    ("updateall host:all: press", [
        _c("updateall", ("host", "all")), ("press", "updateall")]),
    ("updateall: foreign press", [
        _c("updateall"), ("press", "updateall", {"user": OTHER})]),
    ("updateall: expired", [
        _c("updateall"), ("age",), ("press", "updateall")]),
    ("updateall: cancel", [
        _c("updateall"), ("press", "cancel"), ("press", "updateall")]),
    ("updateall host:all: pending emptied before the press", [
        _c("updateall", ("host", "all")), ("drop_pending",),
        ("press", "updateall")]),
    ("updateall: new entry before the press", [
        _c("updateall"), ("add_pending", "db", "postgres:16", "local"),
        ("press", "updateall")]),
    ("updateall: lock taken before the press", [
        _c("updateall"), ("lock",), ("press", "updateall"), ("unlock",)]),
    # Q15: the approved set is queued, and runs when the lock is free.
    ("updateall: lock taken before the press, then released", [
        _c("updateall"), ("lock",), ("press", "updateall"), ("release",)]),
    ("updateall host:all: lock taken before the press, then released", [
        _c("updateall", ("host", "all")), ("lock",), ("press", "updateall"),
        ("release",)]),
    # Only what was approved goes on the queue, not what turned up since.
    ("updateall: new pending, lock taken before the press, then released", [
        _c("updateall"), ("add_pending", "db", "postgres:16", "local"),
        ("lock",), ("press", "updateall"), ("release",)]),
    # ── restore ─────────────────────────────────────────────────────
    ("restore: press, press again", [
        _restore("ok"), ("press", "restore"), ("press", "restore")]),
    ("restore: foreign press", [
        _restore("ok"), ("press", "restore", {"user": OTHER})]),
    ("restore: expired", [
        _restore("ok"), ("age",), ("press", "restore")]),
    ("restore: cancel", [
        _restore("ok"), ("press", "cancel"), ("press", "restore")]),
    # ── odd buttons ─────────────────────────────────────────────────
    ("press: unknown token", [("raw_press", "ds:stop:" + "0" * 16)]),
    ("press: unknown action", [("raw_press", "ds:bogus:" + "0" * 16)]),
    ("press: two-part ds id", [("raw_press", "ds:stop")]),
    ("press: another bot's button", [("raw_press", "other:stop:x")]),
    # ── restart self ────────────────────────────────────────────────
    ("restart self", [_c("restart")]),
]

AUTOCOMPLETE = [
    ("auto host ''", [("auto", "status", [("host", "", True)])]),
    ("auto host 'na'", [("auto", "status", [("host", "na", True)])]),
    ("auto container ''", [("auto", "logs", [("container", "", True)])]),
    ("auto container 'we'", [("auto", "logs", [("container", "we", True)])]),
    ("auto container host:nas", [("auto", "logs", [
        ("container", "", True), ("host", "nas", False)])]),
    ("auto container host:all", [("auto", "logs", [
        ("container", "", True), ("host", "all", False)])]),
    ("auto container host:zzz", [("auto", "logs", [
        ("container", "", True), ("host", "zzz", False)])]),
    ("auto container host:dead", [("auto", "logs", [
        ("container", "", True), ("host", "dead", False)])]),
    ("auto container host:refused", [("auto", "logs", [
        ("container", "", True), ("host", "refused", False)])]),
    ("auto container host:dead twice", [
        ("auto", "logs", [("container", "", True), ("host", "dead", False)]),
        ("auto", "logs", [("container", "", True), ("host", "dead", False)])]),
    ("auto nothing focused", [("auto", "logs", [
        ("container", "we", False)])]),
    ("auto stranger", [("auto", "status", [("host", "", True)],
                        {"user": STRANGER})]),
]


def all_scenarios():
    seen, out = set(), []
    for name, steps in _command_rows() + EXTRA + SCENARIOS + AUTOCOMPLETE:
        if name in seen:
            raise ValueError(f"duplicate row name {name!r}")
        seen.add(name)
        out.append((name, steps))
    return out


def all_rows():
    return [(s, name) for s in SETUPS for name, _ in all_scenarios()]


#: Rows that are wrong today and pinned as they are, with the slice
#: meant to change them. Read off the generated table by hand.
KNOWN_DEFECTS = {}
_WHERE = ("no slice yet: the question over several hosts says 'on this "
          "host' (`where` is the first target's label, :2574)")
for _r_ in ("/updateall host:all", "updateall host:all: press",
            "updateall host:all: pending emptied before the press"):
    KNOWN_DEFECTS[("multi", _r_)] = _WHERE
_UNPIN = ("no slice yet: /update's no-match reuses `unpin_not_found` "
          "(:1657), so it answers '… is not pinned', and the usage "
          "promises globs `_match_in` does not do")
for _setup_, _hs in (("multi", (None, "nas", "all")),
                     ("one", (None, "nas", "all", "zzz"))):
    for _c_ in ("web*", "nosuch"):
        for _h_ in _hs:
            KNOWN_DEFECTS[(_setup_, f"/update container:{_c_}"
                           + (f" host:{_h_}" if _h_ else ""))] = _UNPIN
    KNOWN_DEFECTS[(_setup_, "/update container:media host:nas")] = _UNPIN
for _setup_ in SETUPS:
    for _l in (5, 500, 0):
        KNOWN_DEFECTS[(_setup_, f"/logs container:web lines:{_l}")] = (
            "no slice yet: the heading says 'last 30 lines' whatever "
            "`lines` asked for (`logs_title` has the 30 baked in)")
for _h_ in ("nas", "all", "zzz"):
    KNOWN_DEFECTS[("multi", f"/restart host:{_h_}")] = (
        "no slice yet: with no container the host is ignored and "
        "Docksentry restarts itself (:1417)")
for _r_ in ("/stop container:web host:all", "/stop container:web* host:all",
            "stop host:all: press"):
    KNOWN_DEFECTS[("multi", _r_)] = (
        "no slice yet: the question lists `web`, `web`, `web` with no host "
        "tags, and skips dead and refused without a word")
for _cmd_ in ("start", "restart"):
    KNOWN_DEFECTS[("multi", f"/{_cmd_} container:web host:all")] = (
        "no slice yet: dead and refused hosts are skipped without a word")
for _cmd_ in ("note", "trustrunning", "askmajor", "setlink", "pin",
              "autoupdate", "protect", "cooldown"):
    KNOWN_DEFECTS[("multi", f"/{_cmd_} container:web host:all")] = (
        "no slice yet: the refused host answers 'not found', the dead one "
        "'could not list containers'")
KNOWN_DEFECTS[("multi", "/cooldown container:web seconds:3600 host:all")] = \
    KNOWN_DEFECTS[("multi", "/cooldown container:web host:all")]
for _h_ in ("dead", "refused", "flaky"):
    KNOWN_DEFECTS[("multi", f"/check host:{_h_}")] = (
        "no slice yet: '**Updates available**' heads a reply that holds "
        "only the failure line")

#: Rows another slice leans on as they are today (not wrong, but named).
PINNED_FOR = {}
for _setup_ in SETUPS:
    PINNED_FOR[(_setup_, "auto container host:all")] = (
        "A2: autocomplete with `host: all` lists the local host")
    PINNED_FOR[(_setup_, "/audit container:web")] = (
        "D14: /audit has no host option and reads every host")
for _r_ in ("/status host:all", "/status container:web host:all",
            "/logs container:web host:all", "/status host:zzz",
            "/status host:local", "/logs container:web host:local"):
    PINNED_FOR.setdefault(("one", _r_), (
        "A2: on one host `all` stays an unknown host"
        if "all" in _r_ else "A2: one-host rows stay as they are"))

#: A2: on multi, `host: all` on a read reaches exactly what no host
#: reaches. On main these rows answered "Unknown host `all`"; now each
#: must equal its no-host twin, apart from the step's label and the
#: `host` the audit entry records. Checked on top of the table, so a
#: regenerated table cannot quietly take the old answer back.
A2_TWINS = []
for _c_ in (None, "web", "web*", "nosuch"):
    _o = f" container:{_c_}" if _c_ else ""
    A2_TWINS.append(f"/status{_o} host:all")
    if _c_:
        A2_TWINS.append(f"/logs{_o} host:all")
for _cmd_ in ("check", "cleanup", "checkimages", "pin", "autoupdate",
              "protect", "cooldown", "trustrunning", "askmajor"):
    A2_TWINS.append(f"/{_cmd_} host:all")


def _sans_host(rec):
    """`rec` without what naming `host: all` legitimately changes."""
    rec = copy.deepcopy(rec)
    for step in rec.get("steps", []):
        step.pop("do", None)
    audit = []
    for line in rec.get("state", {}).get("audit", []):
        e = json.loads(line)
        (e.get("detail") or {}).pop("host", None)
        if e.get("detail") == {}:
            e.pop("detail")
        audit.append(json.dumps(e, sort_keys=True))
    if audit:
        rec["state"]["audit"] = audit
    return rec


def twin_checks(recs):
    """A2: each `host: all` read row against its no-host twin."""
    for name in A2_TWINS:
        twin = name[:-len(" host:all")]
        a, b = recs.get(("multi", name)), recs.get(("multi", twin))
        yield (f"[multi] {name} reaches what {twin or 'no host'} reaches",
               a is not None and b is not None
               and _sans_host(a) == _sans_host(b))


#: W3d: /status <container> with a dead or refused host. The plan read
#: the code as answering "not found" there. Measured on f713111 it does
#: not: the name fails to resolve on that host, the reply falls through
#: to the narrowed overview, and the overview names the host as
#: unreachable. These rows pin that beyond the table's fixed hosts —
#: a name living only on the broken host, a broken local daemon on one
#: host, ssh refusing on keys rather than on the port.
#: (label, setup, backend modes laid over it, what a refused host
#: prints or None, options, hosts the reply must name as unreachable)
W3D_ROWS = [
    ("web host:dead", "multi", {}, None,
     [("container", "web"), ("host", "dead")], ["dead"]),
    ("web host:refused", "multi", {}, None,
     [("container", "web"), ("host", "refused")], ["refused"]),
    ("nosuch, every host", "multi", {}, None,
     [("container", "nosuch")], ["dead", "refused"]),
    ("media, only on nas, nas refused", "multi", {"nas": "refused"}, None,
     [("container", "media")], ["nas", "dead", "refused"]),
    ("media host:nas, nas dead", "multi", {"nas": "dead"}, None,
     [("container", "media"), ("host", "nas")], ["nas"]),
    ("web host:refused, refused on keys", "multi", {},
     "Permission denied (publickey).",
     [("container", "web"), ("host", "refused")], ["refused"]),
    ("web, local daemon refused", "one", {"local": "refused"}, None,
     [("container", "web")], ["local"]),
    ("web, local daemon dead", "one", {"local": "dead"}, None,
     [("container", "web")], ["local"]),
]


def _w3d_run(setup, opts, modes=None, refused=None):
    """One /status with `modes` laid over the set-up and `refused` as
    what a refused host prints; both put back afterwards."""
    real = m.MatrixBackend.refused_stderr
    m.MODE_OVERRIDE.update(modes or {})
    if refused is not None:
        m.MatrixBackend.refused_stderr = refused
    try:
        return run_scenario(setup, [_c("status", *opts)])
    finally:
        m.MODE_OVERRIDE.clear()
        m.MatrixBackend.refused_stderr = real


def _w3d_handler_failed(out):
    """True when the handler raised and the dispatcher caught it. That
    still makes exactly one edit ("Something went wrong: …") and leaves
    `raised` unset, so `raised` alone does not see it."""
    return any(o.startswith("<log> Discord command error") for o in out) \
        or any(o.startswith("<edit> ") and "Something went wrong" in o
               for o in out)


def w3d_checks():
    """W3d: what /status <container> says about a dead or refused host."""
    for label, setup, modes, refused, opts, named in W3D_ROWS:
        rec, esc = _w3d_run(setup, opts, modes, refused)
        step = rec["steps"][0] if rec["steps"] else {}
        out = step.get("out", [])
        edits = [o for o in out if o.startswith("<edit> ")]
        text = "\n".join(edits)
        clean = not esc and not step.get("raised") \
            and not step.get("events") and len(edits) == 1 \
            and not _w3d_handler_failed(out)
        yield (f"[{setup}] W3d /status {label}: one answer, nothing raised "
               f"or escaped", clean)
        yield (f"[{setup}] W3d /status {label}: names "
               f"{', '.join(named)} as unreachable, once each",
               all(text.count(f"⚠ `{h}` unreachable: ") == 1
                   for h in named))
        yield (f"[{setup}] W3d /status {label}: never says 'not found'",
               bool(text) and "not found" not in text.lower())
        if refused:
            yield (f"[{setup}] W3d /status {label}: shows what ssh said",
                   refused in text)
    # Pinned as measured, not judged: found on a later host, the answer
    # is that host's detail, and the dead and refused hosts the lookup
    # passed on the way are not mentioned — the same first-host-wins rule
    # that shows local's `web` and says nothing about nas'. Telegram
    # (W3) adds the unreachable lines here; whether Discord should is
    # open, so a change to this row has to be made on purpose. Since W7d
    # the lookup gives each of them ten seconds (and remembers them).
    rec, esc = _w3d_run("multi", [("container", "cache")])
    step = rec["steps"][0] if rec["steps"] else {}
    edits = [o for o in step.get("out", []) if o.startswith("<edit> ")]
    yield ("[multi] W3d /status cache (only on flaky): flaky's detail, the "
           "dead and refused hosts before it unmentioned (pinned as measured)",
           not esc and len(edits) == 1
           and not _w3d_handler_failed(step.get("out", []))
           and edits[0].startswith("<edit> 🔎 **cache** @flaky")
           and "unreachable" not in edits[0]
           and step.get("hosts", {}).get("dead") == "ps -a --format t=10"
           and step.get("hosts", {}).get("refused") == "ps -a --format t=10")


#: W5d: Discord's bare /status listed through `get_running_containers`
#: — the update path's list, which leaves out pinned, excluded and
#: label-opted-out containers — and named a dead or refused host without
#: the SSH hint. Telegram's overview lists every running container and
#: carries the hint. Checked against Telegram's own /status in the same
#: world, not against a list written down here.
#: (label, setup, what `_w5d_world` lays over it, a container that must
#: be listed)
W5D_ROWS = [
    ("as built", "multi", {}, "docksentry"),
    ("as built", "one", {}, "docksentry"),
    ("db pinned", "multi", {"pin": "db"}, "db"),
    ("db pinned", "one", {"pin": "db"}, "db"),
    ("db excluded", "multi", {"env": {"EXCLUDE_CONTAINERS": "db"}}, "db"),
    ("db excluded", "one", {"env": {"EXCLUDE_CONTAINERS": "db"}}, "db"),
    ("db labelled docksentry.enable=false", "multi",
     {"labels": {"db": {"docksentry.enable": "false"}}}, "db"),
    ("db labelled docksentry.exclude=true", "one",
     {"labels": {"db": {"docksentry.exclude": "true"}}}, "db"),
    ("nas refused, flaky dead", "multi",
     {"modes": {"nas": "refused", "flaky": "dead"}}, "web"),
]
_W5D_LINE = re.compile(r"^(?:🟢|🔴|🟡|⚪|⏹) `([^`]+)`((?: @[\w.-]+)?)",
                       re.M)
_W5D_GONE = re.compile(r"`([\w.-]+)` unreachable")


def _w5d_edit_text():
    """The raw content of the last edit of the original response."""
    texts = [(b[3] or {}).get("content", "") for k, b in LOG
             if k == "rest" and b[1] == "PATCH"
             and b[2].endswith("/messages/@original")]
    return texts[-1] if texts else ""


def _w5d_world(setup, *, env=None, modes=None, refused=None, pin=None,
               version=None, labels=None):
    """Discord's and Telegram's bare /status, raw, in one world.

    `env` is laid over the matrix's env, `modes` and `refused` over the
    hosts as in W3d, `pin` is pinned through Discord first, `labels`
    (`{name: {label: value}}`) is laid over the local host's containers,
    `version` is what `image inspect` answers for the version label.
    Everything put
    back afterwards. Returns (discord text, telegram text, escapes,
    discord's recorded lines). A raise anywhere comes back as an empty
    answer with the exception in the escapes, so a check goes red
    instead of the procedure dying."""
    try:
        return _w5d_world_inner(setup, env=env, modes=modes,
                                refused=refused, pin=pin, version=version,
                                labels=labels)
    except Exception as e:                              # noqa: BLE001
        return "", "", [f"raised {type(e).__name__}: {e}"], []


def _w5d_world_inner(setup, *, env, modes, refused, pin, version, labels):
    import hosts as hostslib
    real_refused = m.MatrixBackend.refused_stderr
    real_answer = m.MatrixBackend._answer
    saved_env = dict(ENV)
    m.MODE_OVERRIDE.update(modes or {})
    if refused is not None:
        m.MatrixBackend.refused_stderr = refused
    if version is not None:
        def _answer(self, a, _real=real_answer):
            if a[:2] == ["image", "inspect"] and "--format" in a:
                return m._CP(a, 0, version + "\n", "")
            return _real(self, a)
        m.MatrixBackend._answer = _answer
    ENV.update(env or {})
    row = None
    sink = io.StringIO()
    try:
        hostslib.forget_unreachable()
        del m.ESCAPES[:]
        del LOG[:]
        with contextlib.redirect_stdout(sink), \
                contextlib.redirect_stderr(sink):
            row = Row(setup)
            for nm, lab in (labels or {}).items():
                row.backends["local"].containers[nm]["Config"][
                    "Labels"].update(lab)
            if pin:
                row.do(_c("pin", ("container", pin)))
                _join()
            del LOG[:]
            row.tg.start_time = CLOCK[0]    # set by run(), which never runs
            row.tg._handle_message(
                {"message_id": 1, "text": "/status",
                 "from": {"id": int(m.USER)}, "chat": {"id": int(m.CHAT)}},
                row.bot.checker, None)
            _join()
            tg = "\n".join(b for k, b in LOG if k == "telegram")
            hostslib.forget_unreachable()
            del LOG[:]
            row.cur = {}
            row.do(_c("status"))
            _join()
        dc = _w5d_edit_text()
        out = [row.line(e) for e in LOG]
        return dc, tg, list(m.ESCAPES), out
    finally:
        m.MODE_OVERRIDE.clear()
        m.MatrixBackend.refused_stderr = real_refused
        m.MatrixBackend._answer = real_answer
        ENV.clear()
        ENV.update(saved_env)
        if row is not None:
            if row.engine._update_lock.locked():
                row.engine._update_lock.release()
            shutil.rmtree(row.data, ignore_errors=True)


def w5d_checks():
    """W5d: both chats list the same containers and name the same hosts
    unreachable, with the SSH hint under the line."""
    for label, setup, lay, must in W5D_ROWS:
        dc, tg, esc, out = _w5d_world(setup, **lay)
        listed = _W5D_LINE.findall(dc)
        yield (f"[{setup}] W5d /status, {label}: one answer, nothing "
               f"raised or escaped",
               bool(dc) and not esc and not _w3d_handler_failed(out))
        yield (f"[{setup}] W5d /status, {label}: Discord lists the "
               f"containers Telegram lists, host by host",
               bool(listed) and listed == _W5D_LINE.findall(tg))
        yield (f"[{setup}] W5d /status, {label}: `{must}` is among them",
               (must, "") in listed)
        yield (f"[{setup}] W5d /status, {label}: the same hosts named "
               f"unreachable as on Telegram",
               _W5D_GONE.findall(dc) == _W5D_GONE.findall(tg))
    # The hint, as W3 checks it on Telegram: hostdiag reads this
    # machine's ~/.ssh, pointed at a fixed directory that does not exist.
    import hostdiag
    from i18n import get_translator
    denied = "user@192.0.2.30: Permission denied (publickey)."
    real_home = hostdiag._home_ssh
    hostdiag._home_ssh = lambda: m._NO_SSH_DIR
    try:
        dc, tg, esc, out = _w5d_world("multi", refused=denied)
    finally:
        hostdiag._home_ssh = real_home
    hint = get_translator("en")("hostdiag_ssh_no_dir", dir=m._NO_SSH_DIR)
    yield ("[multi] W5d /status: the SSH hint stands under the refused "
           "host's line", f"⚠ `refused` unreachable: {denied}\n{hint}" in dc)
    yield ("[multi] W5d /status: …the hint Telegram shows for it",
           hint in tg and dc.count(hint) == 1)
    yield ("[multi] W5d /status: the dead host is named once, without a "
           "hint of its own", dc.count("⚠ `dead` unreachable: ") == 1
           and dc.count(hint) == 1)
    # The version label (@NotRetarded's ollama) stays on every line now
    # that the inspect comes from the shared walk.
    dc, tg, esc, out = _w5d_world("multi", version="v9.9")
    listed = _W5D_LINE.findall(dc)
    yield ("[multi] W5d /status: every line still carries the image's "
           "version label", bool(listed) and dc.count(" (v9.9)") == len(listed))


#: W7d, written by hand before the change: Discord's reads and its
#: container autocomplete with the dead-host memory Telegram's reads got
#: in W7. Several steps on one bot, the clock frozen (run_scenario), so a
#: first failure's cooldown is the full 60 s. A read that finds a remote
#: host dead or refused leaves it in the memory; the next read within the
#: cooldown does not ask it — not its `ps`, not its check, not its
#: `ps -a` for a name — and names it with the words it gave and the
#: seconds left. The name lookup of /status <container> gives a remote
#: host ten seconds, like Telegram's, and what it learns is what the
#: overview after it says. Autocomplete gives every host three seconds
#: (Discord drops a later answer), skips a remembered host, and marks
#: nobody dead: a host slow for three seconds is not dead for a read
#: that waits ten. A check failure the daemon answered (flaky: its
#: registry) is not remembered. Writes ask anew; the local host is never
#: skipped.
_W7D_LATER = " (from the last try, not asked again for 60s)"
_W7D_DEAD_PS = ("Command '['docker', '-H', 'ssh://dead', 'ps', '-q']' "
                "timed out after 10 seconds")
_W7D_REFUSED = "ssh: connect to host 192.0.2.30 port 22: Connection refused"
_W7D_REFUSED_CHECK = "could not list containers (rc=255): " + _W7D_REFUSED


def _w7d_seq(setup, steps, modes=None):
    """run_scenario with `modes` laid over the hosts (put back after).
    Returns (step records, each step's raw edit text, escapes); a raise
    comes back as no steps with the exception in the escapes, so a check
    goes red instead of the procedure dying."""
    m.MODE_OVERRIDE.update(modes or {})
    try:
        rec, esc = run_scenario(setup, steps)
        return rec["steps"], list(STEP_TEXT), esc
    except Exception as e:                              # noqa: BLE001
        return [], [], [f"raised {type(e).__name__}: {e}"]
    finally:
        m.MODE_OVERRIDE.clear()


def _w7d_ok(steps, texts, esc, n):
    """`n` steps ran, nothing escaped, no handler raised."""
    return (len(steps) == n and len(texts) == n and not esc
            and not any(_w3d_handler_failed(s.get("out", []))
                        or s.get("raised") or s.get("events", []) and any(
                            e.startswith("thread") for e in s["events"])
                        for s in steps))


def _w7d_gone(host, words):
    return f"⚠ `{host}` unreachable: {words}"


def _w7d_failed(host, words):
    return f"⚠️ Host `{host}` could not be checked: {words}"


def _w7d_auto(steps, i):
    """Step `i`'s autocomplete answer, or None when it sent none."""
    if i >= len(steps):
        return None
    for o in steps[i].get("out", []):
        if o.startswith("<autocomplete>"):
            return o[len("<autocomplete>"):].strip()
    return None


def w7d_checks():
    """W7d: Discord reads skip a host found dead a moment ago."""
    def asked(step, host):
        return step.get("hosts", {}).get(host)

    def checked(step):
        return [e for e in step.get("events", []) if e.startswith("check_all@")]

    # /status, then /status: dead and refused are not asked again.
    steps, texts, esc = _w7d_seq("multi", [_c("status"), _c("status")])
    ok = _w7d_ok(steps, texts, esc, 2)
    want = texts[0].replace(
        _w7d_gone("dead", _W7D_DEAD_PS),
        _w7d_gone("dead", "no answer within 10 seconds" + _W7D_LATER)).replace(
        _w7d_gone("refused", _W7D_REFUSED),
        _w7d_gone("refused", _W7D_REFUSED + _W7D_LATER)) if ok else None
    yield ("[multi] W7d /status then /status: dead and refused named with "
           "their words and the seconds left, the rest as before",
           ok and want != texts[0] and texts[1] == want)
    yield ("[multi] W7d /status then /status: …neither is asked, every "
           "other host as the first time",
           ok and steps[1].get("hosts") == {
               k: v for k, v in steps[0].get("hosts", {}).items()
               if k not in ("dead", "refused")})

    # /status, then /check: the check does not wait on them either.
    steps, texts, esc = _w7d_seq("multi", [_c("status"), _c("check")])
    ok = _w7d_ok(steps, texts, esc, 2)
    yield ("[multi] W7d /status then /check: dead and refused say the "
           "remembered words, the clause whole",
           ok and _w7d_failed("dead", "no answer within 10 seconds"
                              + _W7D_LATER) in texts[1].split("\n")
           and _w7d_failed("refused", _W7D_REFUSED + _W7D_LATER)
           in texts[1].split("\n"))
    yield ("[multi] W7d /status then /check: …neither is asked or checked, "
           "the others are",
           ok and asked(steps[1], "dead") is None
           and asked(steps[1], "refused") is None
           and checked(steps[1]) == ["check_all@local", "check_all@nas",
                                     "check_all@flaky"])

    # /check, then /status: what the check learnt; flaky is asked.
    steps, texts, esc = _w7d_seq("multi", [_c("check"), _c("status")])
    ok = _w7d_ok(steps, texts, esc, 2)
    yield ("[multi] W7d /check then /status: the check's words, remembered",
           ok and _w7d_gone("dead", "no answer within 30 seconds"
                            + _W7D_LATER) in texts[1]
           and _w7d_gone("refused", _W7D_REFUSED_CHECK + _W7D_LATER)
           in texts[1])
    yield ("[multi] W7d /check then /status: …dead and refused not asked, "
           "flaky (its registry failed, not its daemon) asked",
           ok and asked(steps[1], "dead") is None
           and asked(steps[1], "refused") is None
           and (asked(steps[1], "flaky") or "").startswith("ps -q t=10"))

    # /check, then /check: the cut words, then the clause.
    steps, texts, esc = _w7d_seq("multi", [_c("check"), _c("check")])
    ok = _w7d_ok(steps, texts, esc, 2)
    lines = texts[1].split("\n") if ok else []
    flaky = [ln for ln in (texts[0].split("\n") if ok else [])
             if ln.startswith("⚠️ Host `flaky`")]
    # The cut is marked: "…port 22: Con (from the last try…" read the
    # stub as a word, since the clause follows it.
    yield ("[multi] W7d /check then /check: the words cut to 80 with an "
           "ellipsis at the cut, then the clause whole",
           ok and _w7d_failed("dead", "no answer within 30 seconds"
                              + _W7D_LATER) in lines
           and _w7d_failed("refused", _W7D_REFUSED_CHECK[:79] + "…"
                           + _W7D_LATER) in lines)
    yield ("[multi] W7d /check then /check: …dead and refused not asked, "
           "flaky checked again and said as the first time",
           ok and asked(steps[1], "dead") is None
           and asked(steps[1], "refused") is None
           and "check_all@flaky" in checked(steps[1])
           and len(flaky) == 1 and flaky[0] in lines)

    # /status <container>: the lookup's short probe, and its memory.
    for host, words, fresh in (
            ("dead", "no answer within 10 seconds", "dead"),
            ("refused", _W7D_REFUSED, "refused")):
        steps, texts, esc = _w7d_seq(
            "multi", [_c("status", ("container", "web"), ("host", host))])
        ok = _w7d_ok(steps, texts, esc, 1)
        yield (f"[multi] W7d /status container:web host:{host}: the lookup "
               f"asks once, ten seconds, and the overview says what it "
               f"learnt",
               ok and asked(steps[0], fresh) == "ps -a --format t=10"
               and texts[0] == _w7d_gone(host, words + _W7D_LATER))
        steps, texts, esc = _w7d_seq(
            "multi", [_c("status"),
                      _c("status", ("container", "web"), ("host", host))])
        ok = _w7d_ok(steps, texts, esc, 2)
        yield (f"[multi] W7d /status then /status container:web host:{host}:"
               f" not asked at all",
               ok and asked(steps[1], host) is None
               and texts[1].startswith(_w7d_gone(host, ""))
               and texts[1].endswith(_W7D_LATER))
    steps, texts, esc = _w7d_seq(
        "multi", [_c("status"), _c("status", ("container", "cache"))])
    ok = _w7d_ok(steps, texts, esc, 2)
    yield ("[multi] W7d /status then /status container:cache: flaky's "
           "detail, dead and refused not asked on the way",
           ok and texts[1].startswith("🔎 **cache** @flaky")
           and asked(steps[1], "dead") is None
           and asked(steps[1], "refused") is None
           and asked(steps[1], "nas") == "ps -a --format t=10")
    steps, texts, esc = _w7d_seq(
        "multi", [_c("status", ("container", "nosuch"))])
    ok = _w7d_ok(steps, texts, esc, 1)
    yield ("[multi] W7d /status container:nosuch: every remote host's "
           "lookup gets ten seconds, the local one its default; dead and "
           "refused are asked that once",
           ok and (asked(steps[0], "local") or "").startswith(
               "ps -a --format, ")
           and all((asked(steps[0], h) or "").startswith(
               "ps -a --format t=10, ps -q t=10") for h in ("nas", "flaky"))
           and all(asked(steps[0], h) == "ps -a --format t=10"
                   for h in ("dead", "refused")))

    # Autocomplete: three seconds, skip, and no memory of its own.
    _ac = ("auto", "logs", [("container", "", True), ("host", "dead", False)])
    steps, texts, esc = _w7d_seq("multi", [_ac, _ac])
    yield ("[multi] W7d autocomplete host:dead twice: three seconds each "
           "time, nothing suggested, nothing remembered",
           not esc and len(steps) == 2
           and all(asked(s, "dead") == "ps -a --format t=3" for s in steps)
           and _w7d_auto(steps, 0) == "" and _w7d_auto(steps, 1) == "")
    steps, texts, esc = _w7d_seq("multi", [_c("status"), _ac])
    yield ("[multi] W7d /status then autocomplete host:dead: not asked, "
           "nothing suggested, nothing logged",
           not esc and len(steps) == 2 and asked(steps[1], "dead") is None
           and _w7d_auto(steps, 1) == ""
           and not any(o.startswith("<log>") for o in steps[1].get("out", [])))
    for setup, host, on, want in (
            ("multi", "refused", "refused", ""),
            ("multi", "nas", "nas", "web, media"),
            ("multi", None, "local", "docksentry, web, webdav, db"),
            ("one", None, "local", "docksentry, web, webdav, db")):
        opts = [("container", "", True)] + (
            [("host", host, False)] if host else [])
        steps, texts, esc = _w7d_seq(setup, [("auto", "logs", opts)])
        yield (f"[{setup}] W7d autocomplete host:{host or '–'}: `ps -a` on "
               f"{on} with three seconds, the same suggestions",
               not esc and len(steps) == 1
               and asked(steps[0], on) == "ps -a --format t=3"
               and _w7d_auto(steps, 0) == want)

    # The local host is never skipped, not even on its own.
    steps, texts, esc = _w7d_seq("one", [_c("status"), _c("status")],
                                 modes={"local": "dead"})
    ok = _w7d_ok(steps, texts, esc, 2)
    yield ("[one] W7d local daemon dead, /status twice: asked both times",
           ok and all(asked(s, "local") == "ps -q t=10" for s in steps)
           and texts[0] == texts[1])

    # A lookup that reaches a host clears its record: nas failed once,
    # its cooldown ran out, the lookup found web on it — so the next
    # failure starts the backoff at 60 s again, not at 120.
    import hosts as hostslib
    left = []
    steps, texts, esc = _w7d_seq("multi", [
        ("call", lambda: hostslib.mark_unreachable("nas", "earlier")),
        ("call", lambda: HOSTS_CLOCK.__setitem__(0, HOSTS_CLOCK[0] + 61)),
        _c("status", ("container", "web"), ("host", "nas")),
        ("call", lambda: (hostslib.mark_unreachable("nas", "again"),
                          left.append(hostslib.unreachable_for("nas")[0])))])
    yield ("[multi] W7d /status container:web host:nas after nas's cooldown "
           "ran out: asked, found, and its backoff starts over",
           not esc and len(texts) == 4
           and texts[2].startswith("🔎 **web** @nas")
           and (asked(steps[2], "nas") or "").startswith("ps -a --format t=10")
           and left == [60.0])

    # A skipped host's check line: the words made a person's BEFORE the
    # cut, so a long host name cannot leave the reader Python's argv.
    import types
    import discord_bot
    from i18n import get_translator
    argv = ("Command '['docker', '-H', 'ssh://backup-server.internal."
            "example', 'ps', '--format', '{{.Names}}|{{.Image}}']' timed "
            "out after 30 seconds")
    try:
        said = discord_bot.DiscordBot._check_error(
            types.SimpleNamespace(t=get_translator("en")),
            hostslib.StillUnreachable("x", argv, 60))
    except Exception as e:                              # noqa: BLE001
        said = f"raised {type(e).__name__}: {e}"
    yield ("[multi] W7d /check, a skipped host remembered as a long argv: "
           "says the timeout, not the argv",
           said == "no answer within 30 seconds" + _W7D_LATER)

    # A write asks anew: /stop after a /status that found dead and refused
    # is the fresh /stop, call for call.
    _stop = _c("stop", ("container", "web"), ("host", "all"))
    steps, texts, esc = _w7d_seq("multi", [_c("status"), _stop])
    fresh = (_expected_for("multi", "/stop container:web host:all")
             or {}).get("steps", [{}])[0]
    yield ("[multi] W7d /status then /stop container:web host:all: the "
           "write asks every host, as a fresh /stop does",
           not esc and len(steps) == 2 and bool(fresh)
           and steps[1].get("hosts") == fresh.get("hosts")
           and steps[1].get("out") == fresh.get("out"))


#: Where each command lands, read against main at 8a74ba7
#: (app/discord_bot.py, `_dispatch` from :1344). Printed above its rows
#: by --snapshot, so a regenerated table keeps the review notes.
BRANCH_NOTES = {
    "/status": ":1361 → :1447. `_hosts_for` (:1172, read: no host = every "
               "host, `all` = every host on multi since A2 and unknown on "
               "one host, anything else unknown = 'unknown host'). A name "
               "resolves per host through `_resolve_container` (`ps -a`, "
               "10 s on a remote host and the backend default locally since "
               "W7d), first host that resolves wins; ambiguous or not found "
               "falls through to the overview narrowed by substring. The "
               "overview is `status_render.collect_overview` since W5d, the "
               "walk Telegram's /status takes (`ps -q`, `inspect`, 10 s "
               "each), plus `image inspect` per listed container for the "
               "version. Both read the dead-host memory since W7d: a remote "
               "host the lookup or an earlier read found dead or refused is "
               "not asked again and its line carries 'from the last try'.",
    "/check": ":1363 → :1529. Read targeting; busy lock → web_check_one_busy; "
              "`check_all()` per host, a raise is a 'could not be checked' "
              "line under the same heading. With the dead-host memory since "
              "W7d (`check_on_hosts`, use_cache).",
    "/updates": ":1365 → :1563, reads the pending file directly; no options.",
    "/pin": ":1367 → :1615. No container lists (read), a container sets "
            "(write: `_write_hosts_for`, :1183, no host = local, `all` = "
            "every host; on one host the option is ignored).",
    "/unpin": ":1369, the same handler; no container is the usage line.",
    "/note": ":1393 → :2281, write targeting.",
    "/setlink": ":1407 → :2160, write targeting; usage without a container.",
    "/cooldown": ":1375 → :1695, list/set split like /pin; `seconds` "
                 "parsed in the core (clamped there).",
    "/logs": ":1383 → :1789, read targeting; `lines` clamped 1..200 and "
             "passed as --tail, the heading is fixed.",
    "/update": ":1413 → :2486. Write targeting, matched against the "
               "pending list by prefix (`_match_in`, :1644), lock taken "
               "non-blocking, one batch per host under one claim. Busy "
               "since Q15: the containers go on the engine's queue "
               "(`claim_or_queue_many`) and the answer gives their place; "
               "the drainer runs them on the release, results through the "
               "broadcast.",
    "/updateall": ":1415 → :2547 asks first (approved set in the token); "
                  "the press re-reads the file and runs only what was "
                  "approved (:2583); on a held lock it queues exactly that "
                  "(Q15).",
    "/restart": ":1417 bare restarts Docksentry (go_down after the answer), "
                ":1422 with a container goes to lifecycle.",
    "/stop": ":1422 → :2654, plan first, then a confirmation; the press "
             "re-runs lifecycle.act on the raw argument.",
    "/start": ":1422 → :2654 lifecycle.act directly.",
    "/cleanup": ":1424 → :2723, every host by default (read-style), the "
                "lock guard per host.",
    "/checkimages": ":1426 → :2777, read targeting.",
    "/audit": ":1409 → :2239; no host option, so a name is looked for on "
              "every host.",
    "/restore": ":1411 → :1897, attachment checked by name and size, "
                "fetched, then a confirmation.",
    "/selfupdate": ":1401 → :2030, selfupdate.start on a thread; private "
                   "replies route the commentary through a DM.",
    "/lang": ":1405 → :2146, also writes the Telegram bot's translator.",
    "stop": "the press: `_on_component` (:1102) — cancel pops, unknown or "
            "wrong-action token is 'expired', owner gate fails closed before "
            "the TTL and the pop, then `_retire_buttons` edits the origin.",
    "updateall": "the press runs `_do_updateall` (:2583).",
    "restore": "the press runs `_run_restore`.",
    "press": "`_is_ours` (:910) drops anything not starting 'ds:'; a "
             "two-part id is 'unknown button'.",
    "autocomplete": ":836 authorised, then `_autocomplete` (:1282) on a "
                    "worker: host → registry names, container → `ps -a` on "
                    "the named host or local (nothing or `all` named → "
                    "local, since A2 by name rather than registry order), "
                    "3 s and a remembered dead host not asked since W7d.",
    "lock": "the update lock held from outside for one command; "
            "`release` gives it back through the door (the drainer runs "
            "what was queued), `unlock` raw (it stays queued).",
}


def _family(name):
    if name.startswith("/"):
        return name.split()[0]
    if name.startswith("auto "):
        return "autocomplete"
    if ":" in name:
        return name.split(":")[0].split(" ")[0]
    return name


def _expected_for(setup, name):
    return EXPECTED.get((setup, name))


# ── checks ─────────────────────────────────────────────────────────────
def trap_checks():
    """The trap through this entry path: a call past the stand-ins made
    inside `_dispatch` and swallowed there still lands in ESCAPES."""
    import discord_bot
    import subprocess
    results = []
    real = discord_bot.DiscordBot._cmd_help
    for kind, call in (
            ("subprocess.run", lambda: subprocess.run(["docker", "info"])),
            ("urlopen", lambda: urllib.request.urlopen(
                "https://discord.com/api/v10/x"))):
        def leaky(self, *a, _call=call, **k):
            try:
                _call()
            except Exception:                           # noqa: BLE001
                pass                                    # swallowed on purpose
            return real(self, *a, **k)
        discord_bot.DiscordBot._cmd_help = leaky
        try:
            rec, esc = run_scenario("one", [_c("help")])
        finally:
            discord_bot.DiscordBot._cmd_help = real
        results.append((f"the trap catches a swallowed {kind} inside "
                        f"_dispatch", len(esc) == 1
                        and esc[0].startswith(kind)))
    return results


#: Q15: a busy /update or confirmed /updateall is queued, and its result
#: arrives later — never as an edit of the answer that said "queued".
Q15_RELEASED = ("lock: /update container:web, then released",
                "lock: /update container:web host:all, then released",
                "updateall: lock taken before the press, then released",
                "updateall host:all: lock taken before the press, then "
                "released",
                "updateall: new pending, lock taken before the press, then "
                "released")


def q15_checks(recs):
    """On top of the table, so a regenerated one cannot quietly take it
    back: the answer says 'queued — position N', the release runs each
    queued container once and posts its card in the channel, edits no
    interaction, and nothing is left on the queue or the lock."""
    queued = re.compile(r"`([^`]+)` is queued — position (\d+)\.")
    for setup in SETUPS:
        for name in Q15_RELEASED:
            rec = recs.get((setup, name))
            if rec is None:
                yield f"[{setup}] Q15 {name}: the row ran", False
                continue
            steps = rec["steps"]
            rel = [st for st in steps if st["do"] == "release"]
            ask = [st for st in steps[:steps.index(rel[0])] if st.get("out")]
            said = queued.findall(ask[-1]["out"][-1]) if ask else []
            yield (f"[{setup}] Q15 {name}: answered queued, places 1..n",
                   bool(said) and [int(p) for _n, p in said]
                   == list(range(1, len(said) + 1)))
            out = rel[0].get("out", [])
            cards = [ln for ln in out if ln.startswith("[rest] <channel")]
            yield (f"[{setup}] Q15 {name}: the release runs each queued "
                   f"container once and posts its card",
                   bool(said)
                   and len(rel[0].get("events", [])) == len(said)
                   and len(cards) == len(said))
            yield (f"[{setup}] Q15 {name}: and edits no interaction",
                   not any(ln.startswith("<edit") for ln in out))
            st = rec["state"]
            yield (f"[{setup}] Q15 {name}: nothing left queued or locked",
                   "queued" not in st and "locked" not in st)


def check():
    failed = 0
    for label, ok in trap_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    steps_of = dict(all_scenarios())
    recs = {}
    for setup, name in all_rows():
        rec, esc = run_scenario(setup, steps_of[name])
        recs[(setup, name)] = rec
        want = _expected_for(setup, name)
        label = f"[{setup}] {name}"
        if (setup, name) in KNOWN_DEFECTS:
            label += f"  (known defect, {KNOWN_DEFECTS[(setup, name)]})"
        elif (setup, name) in PINNED_FOR:
            label += f"  (pinned for {PINNED_FOR[(setup, name)]})"
        ok = want is not None and rec == want and not esc
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if ok:
            continue
        failed += 1
        if want is None:
            print("      → no expectation for this row")
        if esc:
            print(f"      → escaped the stand-ins: {esc}")
        if want is None or rec == want:
            continue
        if rec["state"] != want["state"]:
            print(f"      → state: expected {want['state']!r}")
            print(f"      →   got      {rec['state']!r}")
        for i in range(max(len(rec["steps"]), len(want["steps"]))):
            got = rec["steps"][i] if i < len(rec["steps"]) else {}
            exp = want["steps"][i] if i < len(want["steps"]) else {}
            if got == exp:
                continue
            print(f"      → step {i + 1} ({(exp or got).get('do')}):")
            for k in ("out", "hosts", "events", "raised"):
                if got.get(k) != exp.get(k):
                    print(f"      →   {k}: expected {exp.get(k)!r}")
                    print(f"      →   {k}: got      {got.get(k)!r}")
    for label, ok in twin_checks(recs):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    for label, ok in q15_checks(recs):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    for label, ok in w3d_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    for label, ok in w5d_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    for label, ok in w7d_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    rows = set(all_rows())
    for key in sorted(k for k in EXPECTED if k not in rows):
        print(f"  FAIL  expectation without a row: {key!r}")
        failed += 1
    for table, what in ((KNOWN_DEFECTS, "known defect"),
                        (PINNED_FOR, "pinned row")):
        for key in sorted(k for k in table if k not in rows):
            print(f"  FAIL  {what} names no row: {key!r}")
            failed += 1
    for name in A2_TWINS:
        for row in (name, name[:-len(" host:all")]):
            if ("multi", row) not in rows:
                print(f"  FAIL  A2 twin names no row: {row!r}")
                failed += 1
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


def snapshot():
    """Print the EXPECTED table for the code under --app."""
    recs, bad = {}, []
    steps_of = dict(all_scenarios())
    for setup, name in all_rows():
        rec, esc = run_scenario(setup, steps_of[name])
        if esc:
            bad.append((setup, name, esc))
        recs[(setup, name)] = rec
    for setup, name, esc in bad:
        print(f"# ESCAPED in {setup} {name!r}: {esc}")
    print("EXPECTED = {")
    seen = set()
    for name, _ in all_scenarios():
        fam = _family(name)
        if fam not in seen:
            seen.add(fam)
            print(f"    # ── {fam} " + "─" * max(4, 60 - len(fam)))
            for line in textwrap.wrap(BRANCH_NOTES.get(fam, ""), 70):
                print(f"    # {line}")
        for setup in SETUPS:
            body = pprint.pformat(recs[(setup, name)], width=88,
                                  sort_dicts=False)
            body = "\n".join("    " + ln if i else ln
                             for i, ln in enumerate(body.splitlines()))
            print(f"    ({setup!r}, {name!r}): {body},")
    print("}")
    return 1 if bad else 0


def main():
    m._TMP_ROOT = tempfile.mkdtemp(prefix="dc-matrix-")
    try:
        if "--snapshot" in sys.argv:
            return snapshot()
        return check()
    finally:
        shutil.rmtree(m._TMP_ROOT, ignore_errors=True)


# ── expected, generated by --snapshot from main (8a74ba7); the 16 A2
# ── rows from core-split after A2, the 22 /status overview rows after
# ── W5d, the 29 /status, /check and autocomplete rows after W7d,
# ── the 4 busy rows and 18 new lock/updateall rows after Q15,
# ── reviewed ──
EXPECTED = {
    # ── /status ─────────────────────────────────────────────────────
    # :1361 → :1447. `_hosts_for` (:1172, read: no host = every host, `all`
    # = every host on multi since A2 and unknown on one host, anything else
    # unknown = 'unknown host'). A name resolves per host through
    # `_resolve_container` (`ps -a`, 10 s on a remote host and the backend
    # default locally since W7d), first host that resolves wins; ambiguous
    # or not found falls through to the overview narrowed by substring. The
    # overview is `status_render.collect_overview` since W5d, the walk
    # Telegram's /status takes (`ps -q`, `inspect`, 10 s each), plus `image
    # inspect` per listed container for the version. Both read the dead-host
    # memory since W7d: a remote host the lookup or an earlier read found
    # dead or refused is not asked again and its line carries 'from the last
    # try'.
    ('multi', '/status'): {'steps': [{'do': 'cmd /status',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?\n'
                        '🟢 `web` @nas — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `media` @nas — `jellyfin/jellyfin:10.9` — ⏱ ?\n'
                        "⚠ `dead` unreachable: Command '['docker', '-H', 'ssh://dead', "
                        "'ps', '-q']' timed out after 10 seconds…#20d73b0bc5  []"],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3',
                          'nas': 'ps -q t=10, inspect t=10, image inspect --format t=10 ×2',
                          'dead': 'ps -q t=10',
                          'refused': 'ps -q t=10',
                          'flaky': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "source": "discord"}']}},
    ('one', '/status'): {'steps': [{'do': 'cmd /status',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?  []'],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "source": "discord"}']}},
    ('multi', '/status host:nas'): {'steps': [{'do': "cmd /status host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🟢 `web` @nas — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `media` @nas — `jellyfin/jellyfin:10.9` — ⏱ ?  []'],
                'hosts': {'nas': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                 '×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/status host:nas'): {'steps': [{'do': "cmd /status host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/status host:all'): {'steps': [{'do': "cmd /status host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?\n'
                        '🟢 `web` @nas — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `media` @nas — `jellyfin/jellyfin:10.9` — ⏱ ?\n'
                        "⚠ `dead` unreachable: Command '['docker', '-H', 'ssh://dead', "
                        "'ps', '-q']' timed out after 10 seconds…#20d73b0bc5  []"],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3',
                          'nas': 'ps -q t=10, inspect t=10, image inspect --format t=10 ×2',
                          'dead': 'ps -q t=10',
                          'refused': 'ps -q t=10',
                          'flaky': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/status host:all'): {'steps': [{'do': "cmd /status host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/status host:zzz'): {'steps': [{'do': "cmd /status host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/status host:zzz'): {'steps': [{'do': "cmd /status host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/status container:web'): {'steps': [{'do': "cmd /status container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **web** — ✅ running (healthy) · ⏱ ?\n'
                        '\n'
                        '📦 `nginx:1.25`\n'
                        '     `251ad31786ba`\n'
                        '\n'
                        '🔁 unless-stopped  []'],
                'hosts': {'local': 'ps -a --format, inspect t=10, image inspect --format '
                                   't=10, stats --no-stream --format t=20, inspect --size '
                                   '--format t=15, image inspect --format t=15'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/status container:web'): {'steps': [{'do': "cmd /status container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **web** — ✅ running (healthy) · ⏱ ?\n'
                        '\n'
                        '📦 `nginx:1.25`\n'
                        '     `251ad31786ba`\n'
                        '\n'
                        '🔁 unless-stopped  []'],
                'hosts': {'local': 'ps -a --format, inspect t=10, image inspect --format '
                                   't=10, stats --no-stream --format t=20, inspect --size '
                                   '--format t=15, image inspect --format t=15'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/status container:web host:nas'): {'steps': [{'do': "cmd /status container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **web** @nas — ✅ running (healthy) · ⏱ ?\n'
                        '\n'
                        '📦 `nginx:1.25`\n'
                        '     `251ad31786ba`\n'
                        '\n'
                        '🔁 unless-stopped  []'],
                'hosts': {'nas': 'ps -a --format t=10, inspect t=10, image inspect '
                                 '--format t=10, stats --no-stream --format t=20, inspect '
                                 '--size --format t=15, image inspect --format t=15'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', '/status container:web host:nas'): {'steps': [{'do': "cmd /status container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/status container:web host:all'): {'steps': [{'do': "cmd /status container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **web** — ✅ running (healthy) · ⏱ ?\n'
                        '\n'
                        '📦 `nginx:1.25`\n'
                        '     `251ad31786ba`\n'
                        '\n'
                        '🔁 unless-stopped  []'],
                'hosts': {'local': 'ps -a --format, inspect t=10, image inspect --format '
                                   't=10, stats --no-stream --format t=20, inspect --size '
                                   '--format t=15, image inspect --format t=15'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/status container:web host:all'): {'steps': [{'do': "cmd /status container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/status container:web host:zzz'): {'steps': [{'do': "cmd /status container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/status container:web host:zzz'): {'steps': [{'do': "cmd /status container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/status container:web*'): {'steps': [{'do': "cmd /status container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ `dead` unreachable: no answer within 10 seconds (from '
                        'the last try, not asked again for 60s)\n'
                        '⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: '
                        'Connection refused (from the last try, not asked again for 60s)  '
                        '[]'],
                'hosts': {'local': 'ps -a --format, ps -q t=10, inspect t=10',
                          'nas': 'ps -a --format t=10, ps -q t=10, inspect t=10',
                          'dead': 'ps -a --format t=10',
                          'refused': 'ps -a --format t=10',
                          'flaky': 'ps -a --format t=10, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/status container:web*'): {'steps': [{'do': "cmd /status container:'web*'",
                'out': ['<ack deferred ephemeral>', '<edit> No containers running.  []'],
                'hosts': {'local': 'ps -a --format, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/status container:web* host:nas'): {'steps': [{'do': "cmd /status container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> No containers running.  []'],
                'hosts': {'nas': 'ps -a --format t=10, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/status container:web* host:nas'): {'steps': [{'do': "cmd /status container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/status container:web* host:all'): {'steps': [{'do': "cmd /status container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ `dead` unreachable: no answer within 10 seconds (from '
                        'the last try, not asked again for 60s)\n'
                        '⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: '
                        'Connection refused (from the last try, not asked again for 60s)  '
                        '[]'],
                'hosts': {'local': 'ps -a --format, ps -q t=10, inspect t=10',
                          'nas': 'ps -a --format t=10, ps -q t=10, inspect t=10',
                          'dead': 'ps -a --format t=10',
                          'refused': 'ps -a --format t=10',
                          'flaky': 'ps -a --format t=10, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/status container:web* host:all'): {'steps': [{'do': "cmd /status container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/status container:web* host:zzz'): {'steps': [{'do': "cmd /status container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/status container:web* host:zzz'): {'steps': [{'do': "cmd /status container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/status container:nosuch'): {'steps': [{'do': "cmd /status container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ `dead` unreachable: no answer within 10 seconds (from '
                        'the last try, not asked again for 60s)\n'
                        '⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: '
                        'Connection refused (from the last try, not asked again for 60s)  '
                        '[]'],
                'hosts': {'local': 'ps -a --format, ps -q t=10, inspect t=10',
                          'nas': 'ps -a --format t=10, ps -q t=10, inspect t=10',
                          'dead': 'ps -a --format t=10',
                          'refused': 'ps -a --format t=10',
                          'flaky': 'ps -a --format t=10, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/status container:nosuch'): {'steps': [{'do': "cmd /status container:'nosuch'",
                'out': ['<ack deferred ephemeral>', '<edit> No containers running.  []'],
                'hosts': {'local': 'ps -a --format, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/status container:nosuch host:nas'): {'steps': [{'do': "cmd /status container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> No containers running.  []'],
                'hosts': {'nas': 'ps -a --format t=10, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/status container:nosuch host:nas'): {'steps': [{'do': "cmd /status container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/status container:nosuch host:all'): {'steps': [{'do': "cmd /status container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ `dead` unreachable: no answer within 10 seconds (from '
                        'the last try, not asked again for 60s)\n'
                        '⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: '
                        'Connection refused (from the last try, not asked again for 60s)  '
                        '[]'],
                'hosts': {'local': 'ps -a --format, ps -q t=10, inspect t=10',
                          'nas': 'ps -a --format t=10, ps -q t=10, inspect t=10',
                          'dead': 'ps -a --format t=10',
                          'refused': 'ps -a --format t=10',
                          'flaky': 'ps -a --format t=10, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/status container:nosuch host:all'): {'steps': [{'do': "cmd /status container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/status container:nosuch host:zzz'): {'steps': [{'do': "cmd /status container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/status container:nosuch host:zzz'): {'steps': [{'do': "cmd /status container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /backup ─────────────────────────────────────────────────────
    ('multi', '/backup'): {'steps': [{'do': 'cmd /backup',
                'out': ['<ack deferred ephemeral>',
                        "<upload webhooks> docksentry-backup-<stamp>.json ['ask_major', "
                        "'autoupdate', 'docksentry_version', 'generated_at', 'groups', "
                        "'hosts', 'instance', 'links', 'notes', 'pinned', "
                        "'schema_version', 'settings', 'update_windows'] 📦 Docksentry "
                        'backup — restore via Web UI → Settings → Import.']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/backup", "actor": "u7", "source": "discord"}'],
               'files': {'backups/docksentry-backup-<stamp>.json': '{"ask_major": [], '
                                                                   '"autoupdate": [], '
                                                                   '"docksentry_version": '
                                                                   '"{VERSION}", '
                                                                   '"generated_at": '
                                                                   '"<ts>", "groups": {}, '
                                                                   '"hosts": ["local"], '
                                                                   '"instance": "", '
                                                                   '"links": {}, "notes": '
                                                                   '{}, "pinned": [], '
                                                                   '"schema_version": 1, '
                                                                   '"settings": {}, '
                                                                   '"update_windows": '
                                                                   '{}}'}}},
    ('one', '/backup'): {'steps': [{'do': 'cmd /backup',
                'out': ['<ack deferred ephemeral>',
                        "<upload webhooks> docksentry-backup-<stamp>.json ['ask_major', "
                        "'autoupdate', 'docksentry_version', 'generated_at', 'groups', "
                        "'hosts', 'instance', 'links', 'notes', 'pinned', "
                        "'schema_version', 'settings', 'update_windows'] 📦 Docksentry "
                        'backup — restore via Web UI → Settings → Import.']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/backup", "actor": "u7", "source": "discord"}'],
               'files': {'backups/docksentry-backup-<stamp>.json': '{"ask_major": [], '
                                                                   '"autoupdate": [], '
                                                                   '"docksentry_version": '
                                                                   '"{VERSION}", '
                                                                   '"generated_at": '
                                                                   '"<ts>", "groups": {}, '
                                                                   '"hosts": ["local"], '
                                                                   '"instance": "", '
                                                                   '"links": {}, "notes": '
                                                                   '{}, "pinned": [], '
                                                                   '"schema_version": 1, '
                                                                   '"settings": {}, '
                                                                   '"update_windows": '
                                                                   '{}}'}}},
    # ── /help ───────────────────────────────────────────────────────
    ('multi', '/help'): {'steps': [{'do': 'cmd /help',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Docksentry commands**\n'
                        '\n'
                        '`/askmajor` — Ask before applying a major update\n'
                        '`/audit` — Audit a container, or Docksentry itself when you name '
                        'none\n'
                        '`/autoupdate` — Toggle auto-update for a container\n'
                        '`/backup` — Send a backup of settings, groups and pins as a file\n'
                        '`/changelog` — What is new in versions a…#6ea20378c7  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/help", "actor": "u7", "source": "discord"}']}},
    ('one', '/help'): {'steps': [{'do': 'cmd /help',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Docksentry commands**\n'
                        '\n'
                        '`/askmajor` — Ask before applying a major update\n'
                        '`/audit` — Audit a container, or Docksentry itself when you name '
                        'none\n'
                        '`/autoupdate` — Toggle auto-update for a container\n'
                        '`/backup` — Send a backup of settings, groups and pins as a file\n'
                        '`/changelog` — What is new in versions a…#6ea20378c7  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/help", "actor": "u7", "source": "discord"}']}},
    # ── /note ───────────────────────────────────────────────────────
    # :1393 → :2281, write targeting.
    ('multi', '/note'): {'steps': [{'do': 'cmd /note',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/note <container> <text>` — leave the text off to '
                        'clear it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "source": "discord"}']}},
    ('one', '/note'): {'steps': [{'do': 'cmd /note',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/note <container> <text>` — leave the text off to '
                        'clear it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "source": "discord"}']}},
    ('multi', '/note host:nas'): {'steps': [{'do': "cmd /note host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/note <container> <text>` — leave the text off to '
                        'clear it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/note host:nas'): {'steps': [{'do': "cmd /note host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/note <container> <text>` — leave the text off to '
                        'clear it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/note host:all'): {'steps': [{'do': "cmd /note host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/note <container> <text>` — leave the text off to '
                        'clear it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/note host:all'): {'steps': [{'do': "cmd /note host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/note <container> <text>` — leave the text off to '
                        'clear it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/note host:zzz'): {'steps': [{'do': "cmd /note host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/note <container> <text>` — leave the text off to '
                        'clear it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/note host:zzz'): {'steps': [{'do': "cmd /note host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/note <container> <text>` — leave the text off to '
                        'clear it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/note container:web'): {'steps': [{'do': "cmd /note container:'web'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note cleared on `web`.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'container_notes.json': '{}'}}},
    ('one', '/note container:web'): {'steps': [{'do': "cmd /note container:'web'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note cleared on `web`.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'container_notes.json': '{}'}}},
    ('multi', '/note container:web host:nas'): {'steps': [{'do': "cmd /note container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📝 Note cleared on `web`. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'container_notes.json': '{}'}}},
    ('one', '/note container:web host:nas'): {'steps': [{'do': "cmd /note container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note cleared on `web`.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'container_notes.json': '{}'}}},
    ('multi', '/note container:web host:all'): {'steps': [{'do': "cmd /note container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📝 Note cleared on `web`.\n'
                        '📝 Note cleared on `web`. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web` not found. @refused\n'
                        '📝 Note cleared on `web`. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'container_notes.json': '{}'}}},
    ('one', '/note container:web host:all'): {'steps': [{'do': "cmd /note container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note cleared on `web`.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'container_notes.json': '{}'}}},
    ('multi', '/note container:web host:zzz'): {'steps': [{'do': "cmd /note container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/note container:web host:zzz'): {'steps': [{'do': "cmd /note container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note cleared on `web`.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}'],
               'files': {'container_notes.json': '{}'}}},
    ('multi', '/note container:web*'): {'steps': [{'do': "cmd /note container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/note container:web*'): {'steps': [{'do': "cmd /note container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/note container:web* host:nas'): {'steps': [{'do': "cmd /note container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/note container:web* host:nas'): {'steps': [{'do': "cmd /note container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/note container:web* host:all'): {'steps': [{'do': "cmd /note container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.\n'
                        'Container `web*` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web*` not found. @refused\n'
                        'Container `web*` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/note container:web* host:all'): {'steps': [{'do': "cmd /note container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/note container:web* host:zzz'): {'steps': [{'do': "cmd /note container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/note container:web* host:zzz'): {'steps': [{'do': "cmd /note container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/note container:nosuch'): {'steps': [{'do': "cmd /note container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/note container:nosuch'): {'steps': [{'do': "cmd /note container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/note container:nosuch host:nas'): {'steps': [{'do': "cmd /note container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/note container:nosuch host:nas'): {'steps': [{'do': "cmd /note container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/note container:nosuch host:all'): {'steps': [{'do': "cmd /note container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.\n'
                        'Container `nosuch` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `nosuch` not found. @refused\n'
                        'Container `nosuch` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/note container:nosuch host:all'): {'steps': [{'do': "cmd /note container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/note container:nosuch host:zzz'): {'steps': [{'do': "cmd /note container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/note container:nosuch host:zzz'): {'steps': [{'do': "cmd /note container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /trustrunning ───────────────────────────────────────────────
    ('multi', '/trustrunning'): {'steps': [{'do': 'cmd /trustrunning',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🩺 Nothing is set to accept running-but-unhealthy.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "source": '
                         '"discord"}']}},
    ('one', '/trustrunning'): {'steps': [{'do': 'cmd /trustrunning',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🩺 Nothing is set to accept running-but-unhealthy.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "source": '
                         '"discord"}']}},
    ('multi', '/trustrunning host:nas'): {'steps': [{'do': "cmd /trustrunning host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🩺 Nothing is set to accept running-but-unhealthy. @nas  '
                        '[]']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('one', '/trustrunning host:nas'): {'steps': [{'do': "cmd /trustrunning host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('multi', '/trustrunning host:all'): {'steps': [{'do': "cmd /trustrunning host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🩺 Nothing is set to accept running-but-unhealthy.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', '/trustrunning host:all'): {'steps': [{'do': "cmd /trustrunning host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', '/trustrunning host:zzz'): {'steps': [{'do': "cmd /trustrunning host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('one', '/trustrunning host:zzz'): {'steps': [{'do': "cmd /trustrunning host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('multi', '/trustrunning container:web'): {'steps': [{'do': "cmd /trustrunning container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a running-but-unhealthy result will be accepted '
                        'after an update.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web"}, "source": "discord"}'],
               'files': {'trust_running_containers.json': '["web"]'}}},
    ('one', '/trustrunning container:web'): {'steps': [{'do': "cmd /trustrunning container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a running-but-unhealthy result will be accepted '
                        'after an update.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web"}, "source": "discord"}'],
               'files': {'trust_running_containers.json': '["web"]'}}},
    ('multi', '/trustrunning container:web host:nas'): {'steps': [{'do': "cmd /trustrunning container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a running-but-unhealthy result will be accepted '
                        'after an update. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web", "host": "nas"}, "source": "discord"}'],
               'files': {'trust_running_containers.json': '["nas/web"]'}}},
    ('one', '/trustrunning container:web host:nas'): {'steps': [{'do': "cmd /trustrunning container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a running-but-unhealthy result will be accepted '
                        'after an update.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web", "host": "nas"}, "source": "discord"}'],
               'files': {'trust_running_containers.json': '["web"]'}}},
    ('multi', '/trustrunning container:web host:all'): {'steps': [{'do': "cmd /trustrunning container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a running-but-unhealthy result will be accepted '
                        'after an update.\n'
                        '✅ `web`: a running-but-unhealthy result will be accepted after an '
                        'update. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web` not found. @refused\n'
                        '✅ `web`: a running-but-unhealthy result will be accepted after an '
                        'update. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web", "host": "all"}, "source": "discord"}'],
               'files': {'trust_running_containers.json': '["web", "nas/web", '
                                                          '"flaky/web"]'}}},
    ('one', '/trustrunning container:web host:all'): {'steps': [{'do': "cmd /trustrunning container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a running-but-unhealthy result will be accepted '
                        'after an update.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web", "host": "all"}, "source": "discord"}'],
               'files': {'trust_running_containers.json': '["web"]'}}},
    ('multi', '/trustrunning container:web host:zzz'): {'steps': [{'do': "cmd /trustrunning container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/trustrunning container:web host:zzz'): {'steps': [{'do': "cmd /trustrunning container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a running-but-unhealthy result will be accepted '
                        'after an update.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web", "host": "zzz"}, "source": "discord"}'],
               'files': {'trust_running_containers.json': '["web"]'}}},
    ('multi', '/trustrunning container:web*'): {'steps': [{'do': "cmd /trustrunning container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web*"}, "source": "discord"}']}},
    ('one', '/trustrunning container:web*'): {'steps': [{'do': "cmd /trustrunning container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web*"}, "source": "discord"}']}},
    ('multi', '/trustrunning container:web* host:nas'): {'steps': [{'do': "cmd /trustrunning container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/trustrunning container:web* host:nas'): {'steps': [{'do': "cmd /trustrunning container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/trustrunning container:web* host:all'): {'steps': [{'do': "cmd /trustrunning container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.\n'
                        'Container `web*` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web*` not found. @refused\n'
                        'Container `web*` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/trustrunning container:web* host:all'): {'steps': [{'do': "cmd /trustrunning container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/trustrunning container:web* host:zzz'): {'steps': [{'do': "cmd /trustrunning container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/trustrunning container:web* host:zzz'): {'steps': [{'do': "cmd /trustrunning container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/trustrunning container:nosuch'): {'steps': [{'do': "cmd /trustrunning container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "nosuch"}, "source": "discord"}']}},
    ('one', '/trustrunning container:nosuch'): {'steps': [{'do': "cmd /trustrunning container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "nosuch"}, "source": "discord"}']}},
    ('multi', '/trustrunning container:nosuch host:nas'): {'steps': [{'do': "cmd /trustrunning container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/trustrunning container:nosuch host:nas'): {'steps': [{'do': "cmd /trustrunning container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/trustrunning container:nosuch host:all'): {'steps': [{'do': "cmd /trustrunning container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.\n'
                        'Container `nosuch` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `nosuch` not found. @refused\n'
                        'Container `nosuch` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/trustrunning container:nosuch host:all'): {'steps': [{'do': "cmd /trustrunning container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/trustrunning container:nosuch host:zzz'): {'steps': [{'do': "cmd /trustrunning container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/trustrunning container:nosuch host:zzz'): {'steps': [{'do': "cmd /trustrunning container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/trustrunning", "actor": "u7", "detail": '
                         '{"container": "nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /askmajor ───────────────────────────────────────────────────
    ('multi', '/askmajor'): {'steps': [{'do': 'cmd /askmajor',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ No container asks before a major update.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "source": "discord"}']}},
    ('one', '/askmajor'): {'steps': [{'do': 'cmd /askmajor',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ No container asks before a major update.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "source": "discord"}']}},
    ('multi', '/askmajor host:nas'): {'steps': [{'do': "cmd /askmajor host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ No container asks before a major update. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('one', '/askmajor host:nas'): {'steps': [{'do': "cmd /askmajor host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('multi', '/askmajor host:all'): {'steps': [{'do': "cmd /askmajor host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ No container asks before a major update.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', '/askmajor host:all'): {'steps': [{'do': "cmd /askmajor host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', '/askmajor host:zzz'): {'steps': [{'do': "cmd /askmajor host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('one', '/askmajor host:zzz'): {'steps': [{'do': "cmd /askmajor host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('multi', '/askmajor container:web'): {'steps': [{'do': "cmd /askmajor container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a major version jump will ask first.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'ask_before_major.json': '["web"]'}}},
    ('one', '/askmajor container:web'): {'steps': [{'do': "cmd /askmajor container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a major version jump will ask first.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'ask_before_major.json': '["web"]'}}},
    ('multi', '/askmajor container:web host:nas'): {'steps': [{'do': "cmd /askmajor container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a major version jump will ask first. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'ask_before_major.json': '["nas/web"]'}}},
    ('one', '/askmajor container:web host:nas'): {'steps': [{'do': "cmd /askmajor container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a major version jump will ask first.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'ask_before_major.json': '["web"]'}}},
    ('multi', '/askmajor container:web host:all'): {'steps': [{'do': "cmd /askmajor container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a major version jump will ask first.\n'
                        '✅ `web`: a major version jump will ask first. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web` not found. @refused\n'
                        '✅ `web`: a major version jump will ask first. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'ask_before_major.json': '["web", "nas/web", "flaky/web"]'}}},
    ('one', '/askmajor container:web host:all'): {'steps': [{'do': "cmd /askmajor container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a major version jump will ask first.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'ask_before_major.json': '["web"]'}}},
    ('multi', '/askmajor container:web host:zzz'): {'steps': [{'do': "cmd /askmajor container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/askmajor container:web host:zzz'): {'steps': [{'do': "cmd /askmajor container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: a major version jump will ask first.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}'],
               'files': {'ask_before_major.json': '["web"]'}}},
    ('multi', '/askmajor container:web*'): {'steps': [{'do': "cmd /askmajor container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/askmajor container:web*'): {'steps': [{'do': "cmd /askmajor container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/askmajor container:web* host:nas'): {'steps': [{'do': "cmd /askmajor container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/askmajor container:web* host:nas'): {'steps': [{'do': "cmd /askmajor container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/askmajor container:web* host:all'): {'steps': [{'do': "cmd /askmajor container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.\n'
                        'Container `web*` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web*` not found. @refused\n'
                        'Container `web*` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/askmajor container:web* host:all'): {'steps': [{'do': "cmd /askmajor container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/askmajor container:web* host:zzz'): {'steps': [{'do': "cmd /askmajor container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/askmajor container:web* host:zzz'): {'steps': [{'do': "cmd /askmajor container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/askmajor container:nosuch'): {'steps': [{'do': "cmd /askmajor container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/askmajor container:nosuch'): {'steps': [{'do': "cmd /askmajor container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/askmajor container:nosuch host:nas'): {'steps': [{'do': "cmd /askmajor container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/askmajor container:nosuch host:nas'): {'steps': [{'do': "cmd /askmajor container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/askmajor container:nosuch host:all'): {'steps': [{'do': "cmd /askmajor container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.\n'
                        'Container `nosuch` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `nosuch` not found. @refused\n'
                        'Container `nosuch` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/askmajor container:nosuch host:all'): {'steps': [{'do': "cmd /askmajor container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/askmajor container:nosuch host:zzz'): {'steps': [{'do': "cmd /askmajor container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/askmajor container:nosuch host:zzz'): {'steps': [{'do': "cmd /askmajor container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/askmajor", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /testchannel ────────────────────────────────────────────────
    ('multi', '/testchannel'): {'steps': [{'do': 'cmd /testchannel',
                'out': ['<ack deferred ephemeral>',
                        '<telegram> 🔔 Test notification — if you can read this on a '
                        'channel, that channel works.',
                        '[rest] <channel chan-1> 🔔 Test notification — if you can read '
                        'this on a channel, that channel works.',
                        '<edit> 🔔 Sent to every channel that is switched on.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/testchannel", "actor": "u7", "source": "discord"}']}},
    ('one', '/testchannel'): {'steps': [{'do': 'cmd /testchannel',
                'out': ['<ack deferred ephemeral>',
                        '<telegram> 🔔 Test notification — if you can read this on a '
                        'channel, that channel works.',
                        '[rest] <channel chan-1> 🔔 Test notification — if you can read '
                        'this on a channel, that channel works.',
                        '<edit> 🔔 Sent to every channel that is switched on.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/testchannel", "actor": "u7", "source": "discord"}']}},
    # ── /changelog ──────────────────────────────────────────────────
    ('multi', '/changelog'): {'steps': [{'do': 'cmd /changelog',
                'out': ['<ack deferred ephemeral>',
                        "<edit> ❌ Couldn't fetch the changelog: offline (stand-in)  []"],
                'events': ['changelog.fetch']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/changelog", "actor": "u7", "source": "discord"}']}},
    ('one', '/changelog'): {'steps': [{'do': 'cmd /changelog',
                'out': ['<ack deferred ephemeral>',
                        "<edit> ❌ Couldn't fetch the changelog: offline (stand-in)  []"],
                'events': ['changelog.fetch']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/changelog", "actor": "u7", "source": "discord"}']}},
    # ── /selfupdate ─────────────────────────────────────────────────
    # :1401 → :2030, selfupdate.start on a thread; private replies route the
    # commentary through a DM.
    ('multi', '/selfupdate'): {'steps': [{'do': 'cmd /selfupdate',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⬆️ Self-update started — I will send you a direct message '
                        'when it finishes, and restart if there is something to apply.  '
                        '[]'],
                'events': ['selfupdate.run target=None']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/selfupdate", "actor": "u7", "source": "discord"}']}},
    ('one', '/selfupdate'): {'steps': [{'do': 'cmd /selfupdate',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⬆️ Self-update started — I will send you a direct message '
                        'when it finishes, and restart if there is something to apply.  '
                        '[]'],
                'events': ['selfupdate.run target=None']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/selfupdate", "actor": "u7", "source": "discord"}']}},
    # ── /debug ──────────────────────────────────────────────────────
    ('multi', '/debug'): {'steps': [{'do': 'cmd /debug',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔍 Debug logging **on**. It shows every registry call and '
                        'the per-container bookkeeping of a scan.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/debug", "actor": "u7", "source": "discord"}'],
               'files': {'settings.json': '{"debug": true}'}}},
    ('one', '/debug'): {'steps': [{'do': 'cmd /debug',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔍 Debug logging **on**. It shows every registry call and '
                        'the per-container bookkeeping of a scan.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/debug", "actor": "u7", "source": "discord"}'],
               'files': {'settings.json': '{"debug": true}'}}},
    # ── /lang ───────────────────────────────────────────────────────
    # :1405 → :2146, also writes the Telegram bot's translator.
    ('multi', '/lang'): {'steps': [{'do': 'cmd /lang',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ Unknown language ``. Available: ar, de, en, es, fr, hi, '
                        'it, ja, ko, nl, pl, pt, ru, tr, uk, zh  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/lang", "actor": "u7", "source": "discord"}']}},
    ('one', '/lang'): {'steps': [{'do': 'cmd /lang',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ Unknown language ``. Available: ar, de, en, es, fr, hi, '
                        'it, ja, ko, nl, pl, pt, ru, tr, uk, zh  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/lang", "actor": "u7", "source": "discord"}']}},
    # ── /setlink ────────────────────────────────────────────────────
    # :1407 → :2160, write targeting; usage without a container.
    ('multi', '/setlink'): {'steps': [{'do': 'cmd /setlink',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/setlink container_name https://github.com/...` — '
                        'or `/setlink container_name` to clear.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "source": "discord"}']}},
    ('one', '/setlink'): {'steps': [{'do': 'cmd /setlink',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/setlink container_name https://github.com/...` — '
                        'or `/setlink container_name` to clear.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "source": "discord"}']}},
    ('multi', '/setlink host:nas'): {'steps': [{'do': "cmd /setlink host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/setlink container_name https://github.com/...` — '
                        'or `/setlink container_name` to clear.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/setlink host:nas'): {'steps': [{'do': "cmd /setlink host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/setlink container_name https://github.com/...` — '
                        'or `/setlink container_name` to clear.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/setlink host:all'): {'steps': [{'do': "cmd /setlink host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/setlink container_name https://github.com/...` — '
                        'or `/setlink container_name` to clear.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/setlink host:all'): {'steps': [{'do': "cmd /setlink host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/setlink container_name https://github.com/...` — '
                        'or `/setlink container_name` to clear.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/setlink host:zzz'): {'steps': [{'do': "cmd /setlink host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/setlink container_name https://github.com/...` — '
                        'or `/setlink container_name` to clear.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/setlink host:zzz'): {'steps': [{'do': "cmd /setlink host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/setlink container_name https://github.com/...` — '
                        'or `/setlink container_name` to clear.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/setlink container:web'): {'steps': [{'do': "cmd /setlink container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link cleared — OCI labels will be used again.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'container_links.json': '{}'}}},
    ('one', '/setlink container:web'): {'steps': [{'do': "cmd /setlink container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link cleared — OCI labels will be used again.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'container_links.json': '{}'}}},
    ('multi', '/setlink container:web host:nas'): {'steps': [{'do': "cmd /setlink container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link cleared — OCI labels will be used again. '
                        '@nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'container_links.json': '{}'}}},
    ('one', '/setlink container:web host:nas'): {'steps': [{'do': "cmd /setlink container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link cleared — OCI labels will be used again.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'container_links.json': '{}'}}},
    ('multi', '/setlink container:web host:all'): {'steps': [{'do': "cmd /setlink container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link cleared — OCI labels will be used again.\n'
                        '🔗 `web` link cleared — OCI labels will be used again. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web` not found. @refused\n'
                        '🔗 `web` link cleared — OCI labels will be used again. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'container_links.json': '{}'}}},
    ('one', '/setlink container:web host:all'): {'steps': [{'do': "cmd /setlink container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link cleared — OCI labels will be used again.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'container_links.json': '{}'}}},
    ('multi', '/setlink container:web host:zzz'): {'steps': [{'do': "cmd /setlink container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/setlink container:web host:zzz'): {'steps': [{'do': "cmd /setlink container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link cleared — OCI labels will be used again.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}'],
               'files': {'container_links.json': '{}'}}},
    ('multi', '/setlink container:web*'): {'steps': [{'do': "cmd /setlink container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/setlink container:web*'): {'steps': [{'do': "cmd /setlink container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/setlink container:web* host:nas'): {'steps': [{'do': "cmd /setlink container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/setlink container:web* host:nas'): {'steps': [{'do': "cmd /setlink container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/setlink container:web* host:all'): {'steps': [{'do': "cmd /setlink container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.\n'
                        'Container `web*` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web*` not found. @refused\n'
                        'Container `web*` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/setlink container:web* host:all'): {'steps': [{'do': "cmd /setlink container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/setlink container:web* host:zzz'): {'steps': [{'do': "cmd /setlink container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/setlink container:web* host:zzz'): {'steps': [{'do': "cmd /setlink container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/setlink container:nosuch'): {'steps': [{'do': "cmd /setlink container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/setlink container:nosuch'): {'steps': [{'do': "cmd /setlink container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/setlink container:nosuch host:nas'): {'steps': [{'do': "cmd /setlink container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/setlink container:nosuch host:nas'): {'steps': [{'do': "cmd /setlink container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/setlink container:nosuch host:all'): {'steps': [{'do': "cmd /setlink container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.\n'
                        'Container `nosuch` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `nosuch` not found. @refused\n'
                        'Container `nosuch` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/setlink container:nosuch host:all'): {'steps': [{'do': "cmd /setlink container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/setlink container:nosuch host:zzz'): {'steps': [{'do': "cmd /setlink container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/setlink container:nosuch host:zzz'): {'steps': [{'do': "cmd /setlink container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /audit ──────────────────────────────────────────────────────
    # :1409 → :2239; no host option, so a name is looked for on every host.
    ('multi', '/audit'): {'steps': [{'do': 'cmd /audit',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔍 **Docksentry, looking at itself**\n'
                        '0 Compose container(s): 0 readable, 0 behind a wrong mount, 0 '
                        'with nothing mounted.\n'
                        '• I am container `docksentry`\n'
                        '• 0 mount(s) of my own  []'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect t=10, ps -a -q t=15, '
                                   'inspect t=30, inspect --format ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/audit", "actor": "u7", "source": "discord"}']}},
    ('one', '/audit'): {'steps': [{'do': 'cmd /audit',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔍 **Docksentry, looking at itself**\n'
                        '0 Compose container(s): 0 readable, 0 behind a wrong mount, 0 '
                        'with nothing mounted.\n'
                        '• I am container `docksentry`\n'
                        '• 0 mount(s) of my own  []'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect t=10, ps -a -q t=15, '
                                   'inspect t=30, inspect --format ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/audit", "actor": "u7", "source": "discord"}']}},
    ('multi', '/audit container:web'): {'steps': [{'do': "cmd /audit container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: clean — every non-default HostConfig and Config '
                        'field is restored on recreate.  []'],
                'hosts': {'local': 'ps -a --format, inspect t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/audit", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/audit container:web'): {'steps': [{'do': "cmd /audit container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web`: clean — every non-default HostConfig and Config '
                        'field is restored on recreate.  []'],
                'hosts': {'local': 'ps -a --format, inspect t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/audit", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/audit container:web*'): {'steps': [{'do': "cmd /audit container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/audit", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/audit container:web*'): {'steps': [{'do': "cmd /audit container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/audit", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/audit container:nosuch'): {'steps': [{'do': "cmd /audit container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/audit", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/audit container:nosuch'): {'steps': [{'do': "cmd /audit container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/audit", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    # ── /restore ────────────────────────────────────────────────────
    # :1411 → :1897, attachment checked by name and size, fetched, then a
    # confirmation.
    ('multi', '/restore'): {'steps': [{'do': 'cmd /restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ That file is not JSON, so it is not a Docksentry '
                        'backup.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', '/restore'): {'steps': [{'do': 'cmd /restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ That file is not JSON, so it is not a Docksentry '
                        'backup.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    # ── /check ──────────────────────────────────────────────────────
    # :1363 → :1529. Read targeting; busy lock → web_check_one_busy;
    # `check_all()` per host, a raise is a 'could not be checked' line under
    # the same heading. With the dead-host memory since W7d
    # (`check_on_hosts`, use_cache).
    ('multi', '/check'): {'steps': [{'do': 'cmd /check',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25\n'
                        "⚠️ Host `dead` could not be checked: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '--format', '{{.Names}}|{{.Image}}\n"
                        '⚠️ Host `refused` could not be checked: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Con\n'
                        '⚠️ Host `flaky` could not be checked: registry did not answer in '
                        '15s  []'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=5',
                          'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                 'inspect --format ×2',
                          'dead': 'ps --format',
                          'refused': 'ps --format'},
                'events': ['check_all@local',
                           'check_all@nas',
                           'check_all@dead',
                           'check_all@refused',
                           'check_all@flaky']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "source": "discord"}']}},
    ('one', '/check'): {'steps': [{'do': 'cmd /check',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n• `web` — nginx:1.25  []'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ['check_all@local']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "source": "discord"}']}},
    ('multi', '/check host:nas'): {'steps': [{'do': "cmd /check host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n• `web` @nas — nginx:1.25  []'],
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                 'inspect --format ×2'},
                'events': ['check_all@nas']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/check host:nas'): {'steps': [{'do': "cmd /check host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/check host:all'): {'steps': [{'do': "cmd /check host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25\n'
                        "⚠️ Host `dead` could not be checked: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '--format', '{{.Names}}|{{.Image}}\n"
                        '⚠️ Host `refused` could not be checked: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Con\n'
                        '⚠️ Host `flaky` could not be checked: registry did not answer in '
                        '15s  []'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=5',
                          'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                 'inspect --format ×2',
                          'dead': 'ps --format',
                          'refused': 'ps --format'},
                'events': ['check_all@local',
                           'check_all@nas',
                           'check_all@dead',
                           'check_all@refused',
                           'check_all@flaky']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/check host:all'): {'steps': [{'do': "cmd /check host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/check host:zzz'): {'steps': [{'do': "cmd /check host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/check host:zzz'): {'steps': [{'do': "cmd /check host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    # ── /updates ────────────────────────────────────────────────────
    # :1365 → :1563, reads the pending file directly; no options.
    ('multi', '/updates'): {'steps': [{'do': 'cmd /updates',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Pending updates**\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/updates", "actor": "u7", "source": "discord"}']}},
    ('one', '/updates'): {'steps': [{'do': 'cmd /updates',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Pending updates**\n• `web` — nginx:1.25  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/updates", "actor": "u7", "source": "discord"}']}},
    # ── /hosts ──────────────────────────────────────────────────────
    ('multi', '/hosts'): {'steps': [{'do': 'cmd /hosts',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Managed hosts**\n'
                        '• `local` — local\n'
                        '• `nas` — ssh://nas\n'
                        '• `dead` — ssh://dead\n'
                        '• `refused` — ssh://refused\n'
                        '• `flaky` — ssh://flaky  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/hosts", "actor": "u7", "source": "discord"}']}},
    ('one', '/hosts'): {'steps': [{'do': 'cmd /hosts',
                'out': ['<ack deferred ephemeral>',
                        '<edit> This instance manages a single host.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/hosts", "actor": "u7", "source": "discord"}']}},
    # ── /pin ────────────────────────────────────────────────────────
    # :1367 → :1615. No container lists (read), a container sets (write:
    # `_write_hosts_for`, :1183, no host = local, `all` = every host; on one
    # host the option is ignored).
    ('multi', '/pin'): {'steps': [{'do': 'cmd /pin',
                'out': ['<ack deferred ephemeral>', '<edit> 📌 No pinned containers.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "source": "discord"}']}},
    ('one', '/pin'): {'steps': [{'do': 'cmd /pin',
                'out': ['<ack deferred ephemeral>', '<edit> 📌 No pinned containers.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "source": "discord"}']}},
    ('multi', '/pin host:nas'): {'steps': [{'do': "cmd /pin host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 No pinned containers. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/pin host:nas'): {'steps': [{'do': "cmd /pin host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/pin host:all'): {'steps': [{'do': "cmd /pin host:'all'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 No pinned containers.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/pin host:all'): {'steps': [{'do': "cmd /pin host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/pin host:zzz'): {'steps': [{'do': "cmd /pin host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/pin host:zzz'): {'steps': [{'do': "cmd /pin host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/pin container:web'): {'steps': [{'do': "cmd /pin container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` pinned — will be skipped during updates.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'pinned_containers.json': '["web"]'}}},
    ('one', '/pin container:web'): {'steps': [{'do': "cmd /pin container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` pinned — will be skipped during updates.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'pinned_containers.json': '["web"]'}}},
    ('multi', '/pin container:web host:nas'): {'steps': [{'do': "cmd /pin container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` pinned — will be skipped during updates. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": "web", '
                         '"host": "nas"}, "source": "discord"}'],
               'files': {'pinned_containers.json': '["nas/web"]'}}},
    ('one', '/pin container:web host:nas'): {'steps': [{'do': "cmd /pin container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` pinned — will be skipped during updates.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": "web", '
                         '"host": "nas"}, "source": "discord"}'],
               'files': {'pinned_containers.json': '["web"]'}}},
    ('multi', '/pin container:web host:all'): {'steps': [{'do': "cmd /pin container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` pinned — will be skipped during updates.\n'
                        '📌 `web` pinned — will be skipped during updates. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web` not found. @refused\n'
                        '📌 `web` pinned — will be skipped during updates. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": "web", '
                         '"host": "all"}, "source": "discord"}'],
               'files': {'pinned_containers.json': '["web", "nas/web", "flaky/web"]'}}},
    ('one', '/pin container:web host:all'): {'steps': [{'do': "cmd /pin container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` pinned — will be skipped during updates.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": "web", '
                         '"host": "all"}, "source": "discord"}'],
               'files': {'pinned_containers.json': '["web"]'}}},
    ('multi', '/pin container:web host:zzz'): {'steps': [{'do': "cmd /pin container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": "web", '
                         '"host": "zzz"}, "source": "discord"}']}},
    ('one', '/pin container:web host:zzz'): {'steps': [{'do': "cmd /pin container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` pinned — will be skipped during updates.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": "web", '
                         '"host": "zzz"}, "source": "discord"}'],
               'files': {'pinned_containers.json': '["web"]'}}},
    ('multi', '/pin container:web*'): {'steps': [{'do': "cmd /pin container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/pin container:web*'): {'steps': [{'do': "cmd /pin container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/pin container:web* host:nas'): {'steps': [{'do': "cmd /pin container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/pin container:web* host:nas'): {'steps': [{'do': "cmd /pin container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/pin container:web* host:all'): {'steps': [{'do': "cmd /pin container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.\n'
                        'Container `web*` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web*` not found. @refused\n'
                        'Container `web*` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/pin container:web* host:all'): {'steps': [{'do': "cmd /pin container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/pin container:web* host:zzz'): {'steps': [{'do': "cmd /pin container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/pin container:web* host:zzz'): {'steps': [{'do': "cmd /pin container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/pin container:nosuch'): {'steps': [{'do': "cmd /pin container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/pin container:nosuch'): {'steps': [{'do': "cmd /pin container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/pin container:nosuch host:nas'): {'steps': [{'do': "cmd /pin container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/pin container:nosuch host:nas'): {'steps': [{'do': "cmd /pin container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/pin container:nosuch host:all'): {'steps': [{'do': "cmd /pin container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.\n'
                        'Container `nosuch` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `nosuch` not found. @refused\n'
                        'Container `nosuch` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/pin container:nosuch host:all'): {'steps': [{'do': "cmd /pin container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/pin container:nosuch host:zzz'): {'steps': [{'do': "cmd /pin container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/pin container:nosuch host:zzz'): {'steps': [{'do': "cmd /pin container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/pin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /unpin ──────────────────────────────────────────────────────
    # :1369, the same handler; no container is the usage line.
    ('multi', '/unpin'): {'steps': [{'do': 'cmd /unpin',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/unpin <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "source": "discord"}']}},
    ('one', '/unpin'): {'steps': [{'do': 'cmd /unpin',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/unpin <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "source": "discord"}']}},
    ('multi', '/unpin host:nas'): {'steps': [{'do': "cmd /unpin host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/unpin <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/unpin host:nas'): {'steps': [{'do': "cmd /unpin host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/unpin <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/unpin host:all'): {'steps': [{'do': "cmd /unpin host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/unpin <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/unpin host:all'): {'steps': [{'do': "cmd /unpin host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/unpin <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/unpin host:zzz'): {'steps': [{'do': "cmd /unpin host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/unpin <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/unpin host:zzz'): {'steps': [{'do': "cmd /unpin host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/unpin <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/unpin container:web'): {'steps': [{'do': "cmd /unpin container:'web'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web` is not pinned.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/unpin container:web'): {'steps': [{'do': "cmd /unpin container:'web'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/unpin container:web host:nas'): {'steps': [{'do': "cmd /unpin container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` is not pinned. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', '/unpin container:web host:nas'): {'steps': [{'do': "cmd /unpin container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/unpin container:web host:all'): {'steps': [{'do': "cmd /unpin container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web` is not pinned.\n'
                        '📌 `web` is not pinned. @nas\n'
                        '📌 `web` is not pinned. @dead\n'
                        '📌 `web` is not pinned. @refused\n'
                        '📌 `web` is not pinned. @flaky  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/unpin container:web host:all'): {'steps': [{'do': "cmd /unpin container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/unpin container:web host:zzz'): {'steps': [{'do': "cmd /unpin container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/unpin container:web host:zzz'): {'steps': [{'do': "cmd /unpin container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/unpin container:web*'): {'steps': [{'do': "cmd /unpin container:'web*'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/unpin container:web*'): {'steps': [{'do': "cmd /unpin container:'web*'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/unpin container:web* host:nas'): {'steps': [{'do': "cmd /unpin container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web*` is not pinned. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/unpin container:web* host:nas'): {'steps': [{'do': "cmd /unpin container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/unpin container:web* host:all'): {'steps': [{'do': "cmd /unpin container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `web*` is not pinned.\n'
                        '📌 `web*` is not pinned. @nas\n'
                        '📌 `web*` is not pinned. @dead\n'
                        '📌 `web*` is not pinned. @refused\n'
                        '📌 `web*` is not pinned. @flaky  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/unpin container:web* host:all'): {'steps': [{'do': "cmd /unpin container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/unpin container:web* host:zzz'): {'steps': [{'do': "cmd /unpin container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/unpin container:web* host:zzz'): {'steps': [{'do': "cmd /unpin container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>', '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/unpin container:nosuch'): {'steps': [{'do': "cmd /unpin container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/unpin container:nosuch'): {'steps': [{'do': "cmd /unpin container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/unpin container:nosuch host:nas'): {'steps': [{'do': "cmd /unpin container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `nosuch` is not pinned. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/unpin container:nosuch host:nas'): {'steps': [{'do': "cmd /unpin container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/unpin container:nosuch host:all'): {'steps': [{'do': "cmd /unpin container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `nosuch` is not pinned.\n'
                        '📌 `nosuch` is not pinned. @nas\n'
                        '📌 `nosuch` is not pinned. @dead\n'
                        '📌 `nosuch` is not pinned. @refused\n'
                        '📌 `nosuch` is not pinned. @flaky  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/unpin container:nosuch host:all'): {'steps': [{'do': "cmd /unpin container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/unpin container:nosuch host:zzz'): {'steps': [{'do': "cmd /unpin container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/unpin container:nosuch host:zzz'): {'steps': [{'do': "cmd /unpin container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/unpin", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /autoupdate ─────────────────────────────────────────────────
    ('multi', '/autoupdate'): {'steps': [{'do': 'cmd /autoupdate',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ No containers set to auto-update.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "source": "discord"}']}},
    ('one', '/autoupdate'): {'steps': [{'do': 'cmd /autoupdate',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ No containers set to auto-update.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "source": "discord"}']}},
    ('multi', '/autoupdate host:nas'): {'steps': [{'do': "cmd /autoupdate host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ No containers set to auto-update. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('one', '/autoupdate host:nas'): {'steps': [{'do': "cmd /autoupdate host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('multi', '/autoupdate host:all'): {'steps': [{'do': "cmd /autoupdate host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ No containers set to auto-update.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', '/autoupdate host:all'): {'steps': [{'do': "cmd /autoupdate host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', '/autoupdate host:zzz'): {'steps': [{'do': "cmd /autoupdate host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('one', '/autoupdate host:zzz'): {'steps': [{'do': "cmd /autoupdate host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('multi', '/autoupdate container:web'): {'steps': [{'do': "cmd /autoupdate container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ `web` — auto-update enabled.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'autoupdate_containers.json': '["web"]'}}},
    ('one', '/autoupdate container:web'): {'steps': [{'do': "cmd /autoupdate container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ `web` — auto-update enabled.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'autoupdate_containers.json': '["web"]'}}},
    ('multi', '/autoupdate container:web host:nas'): {'steps': [{'do': "cmd /autoupdate container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ `web` — auto-update enabled. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'autoupdate_containers.json': '["nas/web"]'}}},
    ('one', '/autoupdate container:web host:nas'): {'steps': [{'do': "cmd /autoupdate container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ `web` — auto-update enabled.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'autoupdate_containers.json': '["web"]'}}},
    ('multi', '/autoupdate container:web host:all'): {'steps': [{'do': "cmd /autoupdate container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ `web` — auto-update enabled.\n'
                        '⚡ `web` — auto-update enabled. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web` not found. @refused\n'
                        '⚡ `web` — auto-update enabled. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'autoupdate_containers.json': '["web", "nas/web", "flaky/web"]'}}},
    ('one', '/autoupdate container:web host:all'): {'steps': [{'do': "cmd /autoupdate container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ `web` — auto-update enabled.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'autoupdate_containers.json': '["web"]'}}},
    ('multi', '/autoupdate container:web host:zzz'): {'steps': [{'do': "cmd /autoupdate container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/autoupdate container:web host:zzz'): {'steps': [{'do': "cmd /autoupdate container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚡ `web` — auto-update enabled.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}'],
               'files': {'autoupdate_containers.json': '["web"]'}}},
    ('multi', '/autoupdate container:web*'): {'steps': [{'do': "cmd /autoupdate container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/autoupdate container:web*'): {'steps': [{'do': "cmd /autoupdate container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/autoupdate container:web* host:nas'): {'steps': [{'do': "cmd /autoupdate container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/autoupdate container:web* host:nas'): {'steps': [{'do': "cmd /autoupdate container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/autoupdate container:web* host:all'): {'steps': [{'do': "cmd /autoupdate container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.\n'
                        'Container `web*` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web*` not found. @refused\n'
                        'Container `web*` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/autoupdate container:web* host:all'): {'steps': [{'do': "cmd /autoupdate container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/autoupdate container:web* host:zzz'): {'steps': [{'do': "cmd /autoupdate container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/autoupdate container:web* host:zzz'): {'steps': [{'do': "cmd /autoupdate container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/autoupdate container:nosuch'): {'steps': [{'do': "cmd /autoupdate container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/autoupdate container:nosuch'): {'steps': [{'do': "cmd /autoupdate container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/autoupdate container:nosuch host:nas'): {'steps': [{'do': "cmd /autoupdate container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/autoupdate container:nosuch host:nas'): {'steps': [{'do': "cmd /autoupdate container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/autoupdate container:nosuch host:all'): {'steps': [{'do': "cmd /autoupdate container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.\n'
                        'Container `nosuch` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `nosuch` not found. @refused\n'
                        'Container `nosuch` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/autoupdate container:nosuch host:all'): {'steps': [{'do': "cmd /autoupdate container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/autoupdate container:nosuch host:zzz'): {'steps': [{'do': "cmd /autoupdate container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/autoupdate container:nosuch host:zzz'): {'steps': [{'do': "cmd /autoupdate container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/autoupdate", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /protect ────────────────────────────────────────────────────
    ('multi', '/protect'): {'steps': [{'do': 'cmd /protect',
                'out': ['<ack deferred ephemeral>',
                        '<edit> No stop-protected containers. `/protect <name>` to add '
                        'one.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "source": "discord"}']}},
    ('one', '/protect'): {'steps': [{'do': 'cmd /protect',
                'out': ['<ack deferred ephemeral>',
                        '<edit> No stop-protected containers. `/protect <name>` to add '
                        'one.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "source": "discord"}']}},
    ('multi', '/protect host:nas'): {'steps': [{'do': "cmd /protect host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> No stop-protected containers. `/protect <name>` to add '
                        'one. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/protect host:nas'): {'steps': [{'do': "cmd /protect host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/protect host:all'): {'steps': [{'do': "cmd /protect host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> No stop-protected containers. `/protect <name>` to add '
                        'one.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/protect host:all'): {'steps': [{'do': "cmd /protect host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/protect host:zzz'): {'steps': [{'do': "cmd /protect host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/protect host:zzz'): {'steps': [{'do': "cmd /protect host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/protect container:web'): {'steps': [{'do': "cmd /protect container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🛡 `web` is now protected from Stop. Restart still works.  '
                        '[]'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'protect_stop_containers.json': '["web"]'}}},
    ('one', '/protect container:web'): {'steps': [{'do': "cmd /protect container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🛡 `web` is now protected from Stop. Restart still works.  '
                        '[]'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}'],
               'files': {'protect_stop_containers.json': '["web"]'}}},
    ('multi', '/protect container:web host:nas'): {'steps': [{'do': "cmd /protect container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🛡 `web` is now protected from Stop. Restart still works. '
                        '@nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'protect_stop_containers.json': '["nas/web"]'}}},
    ('one', '/protect container:web host:nas'): {'steps': [{'do': "cmd /protect container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🛡 `web` is now protected from Stop. Restart still works.  '
                        '[]'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}'],
               'files': {'protect_stop_containers.json': '["web"]'}}},
    ('multi', '/protect container:web host:all'): {'steps': [{'do': "cmd /protect container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🛡 `web` is now protected from Stop. Restart still works.\n'
                        '🛡 `web` is now protected from Stop. Restart still works. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web` not found. @refused\n'
                        '🛡 `web` is now protected from Stop. Restart still works. @flaky  '
                        '[]'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'protect_stop_containers.json': '["web", "nas/web", '
                                                         '"flaky/web"]'}}},
    ('one', '/protect container:web host:all'): {'steps': [{'do': "cmd /protect container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🛡 `web` is now protected from Stop. Restart still works.  '
                        '[]'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}'],
               'files': {'protect_stop_containers.json': '["web"]'}}},
    ('multi', '/protect container:web host:zzz'): {'steps': [{'do': "cmd /protect container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/protect container:web host:zzz'): {'steps': [{'do': "cmd /protect container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🛡 `web` is now protected from Stop. Restart still works.  '
                        '[]'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}'],
               'files': {'protect_stop_containers.json': '["web"]'}}},
    ('multi', '/protect container:web*'): {'steps': [{'do': "cmd /protect container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/protect container:web*'): {'steps': [{'do': "cmd /protect container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/protect container:web* host:nas'): {'steps': [{'do': "cmd /protect container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/protect container:web* host:nas'): {'steps': [{'do': "cmd /protect container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/protect container:web* host:all'): {'steps': [{'do': "cmd /protect container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.\n'
                        'Container `web*` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web*` not found. @refused\n'
                        'Container `web*` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/protect container:web* host:all'): {'steps': [{'do': "cmd /protect container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/protect container:web* host:zzz'): {'steps': [{'do': "cmd /protect container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/protect container:web* host:zzz'): {'steps': [{'do': "cmd /protect container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/protect container:nosuch'): {'steps': [{'do': "cmd /protect container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/protect container:nosuch'): {'steps': [{'do': "cmd /protect container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/protect container:nosuch host:nas'): {'steps': [{'do': "cmd /protect container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/protect container:nosuch host:nas'): {'steps': [{'do': "cmd /protect container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/protect container:nosuch host:all'): {'steps': [{'do': "cmd /protect container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.\n'
                        'Container `nosuch` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `nosuch` not found. @refused\n'
                        'Container `nosuch` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/protect container:nosuch host:all'): {'steps': [{'do': "cmd /protect container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/protect container:nosuch host:zzz'): {'steps': [{'do': "cmd /protect container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/protect container:nosuch host:zzz'): {'steps': [{'do': "cmd /protect container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/protect", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /cooldown ───────────────────────────────────────────────────
    # :1375 → :1695, list/set split like /pin; `seconds` parsed in the core
    # (clamped there).
    ('multi', '/cooldown'): {'steps': [{'do': 'cmd /cooldown',
                'out': ['<ack deferred ephemeral>',
                        '<edit> No per-container update cooldowns set. `/cooldown <name> '
                        '<seconds>` to add one.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "source": "discord"}']}},
    ('one', '/cooldown'): {'steps': [{'do': 'cmd /cooldown',
                'out': ['<ack deferred ephemeral>',
                        '<edit> No per-container update cooldowns set. `/cooldown <name> '
                        '<seconds>` to add one.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "source": "discord"}']}},
    ('multi', '/cooldown host:nas'): {'steps': [{'do': "cmd /cooldown host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> No per-container update cooldowns set. `/cooldown <name> '
                        '<seconds>` to add one. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('one', '/cooldown host:nas'): {'steps': [{'do': "cmd /cooldown host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('multi', '/cooldown host:all'): {'steps': [{'do': "cmd /cooldown host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> No per-container update cooldowns set. `/cooldown <name> '
                        '<seconds>` to add one.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', '/cooldown host:all'): {'steps': [{'do': "cmd /cooldown host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', '/cooldown host:zzz'): {'steps': [{'do': "cmd /cooldown host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('one', '/cooldown host:zzz'): {'steps': [{'do': "cmd /cooldown host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web'): {'steps': [{'do': "cmd /cooldown container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> `web` update cooldown: **0s**. `/cooldown web <seconds>` '
                        'to change (0 clears).  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/cooldown container:web'): {'steps': [{'do': "cmd /cooldown container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> `web` update cooldown: **0s**. `/cooldown web <seconds>` '
                        'to change (0 clears).  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web host:nas'): {'steps': [{'do': "cmd /cooldown container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> `web` update cooldown: **0s**. `/cooldown web <seconds>` '
                        'to change (0 clears). @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', '/cooldown container:web host:nas'): {'steps': [{'do': "cmd /cooldown container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> `web` update cooldown: **0s**. `/cooldown web <seconds>` '
                        'to change (0 clears).  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web host:all'): {'steps': [{'do': "cmd /cooldown container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> `web` update cooldown: **0s**. `/cooldown web <seconds>` '
                        'to change (0 clears).\n'
                        '`web` update cooldown: **0s**. `/cooldown web <seconds>` to '
                        'change (0 clears). @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web` not found. @refused\n'
                        '`web` update cooldown: **0s**. `/cooldown web <seconds>` to '
                        'change (0 clears). @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/cooldown container:web host:all'): {'steps': [{'do': "cmd /cooldown container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> `web` update cooldown: **0s**. `/cooldown web <seconds>` '
                        'to change (0 clears).  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web host:zzz'): {'steps': [{'do': "cmd /cooldown container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/cooldown container:web host:zzz'): {'steps': [{'do': "cmd /cooldown container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> `web` update cooldown: **0s**. `/cooldown web <seconds>` '
                        'to change (0 clears).  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web*'): {'steps': [{'do': "cmd /cooldown container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/cooldown container:web*'): {'steps': [{'do': "cmd /cooldown container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web* host:nas'): {'steps': [{'do': "cmd /cooldown container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/cooldown container:web* host:nas'): {'steps': [{'do': "cmd /cooldown container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web* host:all'): {'steps': [{'do': "cmd /cooldown container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.\n'
                        'Container `web*` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `web*` not found. @refused\n'
                        'Container `web*` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/cooldown container:web* host:all'): {'steps': [{'do': "cmd /cooldown container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web* host:zzz'): {'steps': [{'do': "cmd /cooldown container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/cooldown container:web* host:zzz'): {'steps': [{'do': "cmd /cooldown container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/cooldown container:nosuch'): {'steps': [{'do': "cmd /cooldown container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/cooldown container:nosuch'): {'steps': [{'do': "cmd /cooldown container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/cooldown container:nosuch host:nas'): {'steps': [{'do': "cmd /cooldown container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/cooldown container:nosuch host:nas'): {'steps': [{'do': "cmd /cooldown container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/cooldown container:nosuch host:all'): {'steps': [{'do': "cmd /cooldown container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.\n'
                        'Container `nosuch` not found. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead\n"
                        'Container `nosuch` not found. @refused\n'
                        'Container `nosuch` not found. @flaky  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/cooldown container:nosuch host:all'): {'steps': [{'do': "cmd /cooldown container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/cooldown container:nosuch host:zzz'): {'steps': [{'do': "cmd /cooldown container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/cooldown container:nosuch host:zzz'): {'steps': [{'do': "cmd /cooldown container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /maintenance ────────────────────────────────────────────────
    ('multi', '/maintenance'): {'steps': [{'do': 'cmd /maintenance',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **off** — updates run on schedule.  '
                        '[]']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "source": "discord"}']}},
    ('one', '/maintenance'): {'steps': [{'do': 'cmd /maintenance',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **off** — updates run on schedule.  '
                        '[]']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "source": "discord"}']}},
    # ── /history ────────────────────────────────────────────────────
    ('multi', '/history'): {'steps': [{'do': 'cmd /history',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 No updates recorded yet.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/history", "actor": "u7", "source": "discord"}']}},
    ('one', '/history'): {'steps': [{'do': 'cmd /history',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 No updates recorded yet.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/history", "actor": "u7", "source": "discord"}']}},
    ('multi', '/history container:web'): {'steps': [{'do': "cmd /history container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 No updates recorded yet.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/history", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/history container:web'): {'steps': [{'do': "cmd /history container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 No updates recorded yet.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/history", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/history container:web*'): {'steps': [{'do': "cmd /history container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 No updates recorded yet.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/history", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/history container:web*'): {'steps': [{'do': "cmd /history container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 No updates recorded yet.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/history", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/history container:nosuch'): {'steps': [{'do': "cmd /history container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 No updates recorded yet.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/history", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/history container:nosuch'): {'steps': [{'do': "cmd /history container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 No updates recorded yet.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/history", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    # ── /events ─────────────────────────────────────────────────────
    ('multi', '/events'): {'steps': [{'do': 'cmd /events',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🩺 No container events recorded yet. Health flips, non-zero '
                        'exits, OOM kills and crash-restarts will show up here.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/events", "actor": "u7", "source": "discord"}']}},
    ('one', '/events'): {'steps': [{'do': 'cmd /events',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🩺 No container events recorded yet. Health flips, non-zero '
                        'exits, OOM kills and crash-restarts will show up here.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/events", "actor": "u7", "source": "discord"}']}},
    # ── /logs ───────────────────────────────────────────────────────
    # :1383 → :1789, read targeting; `lines` clamped 1..200 and passed as
    # --tail, the heading is fixed.
    ('multi', '/logs'): {'steps': [{'do': 'cmd /logs',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/logs container_name`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "source": "discord"}']}},
    ('one', '/logs'): {'steps': [{'do': 'cmd /logs',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/logs container_name`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "source": "discord"}']}},
    ('multi', '/logs host:nas'): {'steps': [{'do': "cmd /logs host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/logs container_name`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/logs host:nas'): {'steps': [{'do': "cmd /logs host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/logs container_name`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/logs host:all'): {'steps': [{'do': "cmd /logs host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/logs container_name`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/logs host:all'): {'steps': [{'do': "cmd /logs host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/logs container_name`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/logs host:zzz'): {'steps': [{'do': "cmd /logs host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/logs container_name`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/logs host:zzz'): {'steps': [{'do': "cmd /logs host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/logs container_name`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/logs container:web'): {'steps': [{'do': "cmd /logs container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 30 t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/logs container:web'): {'steps': [{'do': "cmd /logs container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 30 t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/logs container:web host:nas'): {'steps': [{'do': "cmd /logs container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines): @nas\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'nas': 'ps -a --format, logs --tail 30 t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', '/logs container:web host:nas'): {'steps': [{'do': "cmd /logs container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/logs container:web host:all'): {'steps': [{'do': "cmd /logs container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 30 t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/logs container:web host:all'): {'steps': [{'do': "cmd /logs container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/logs container:web host:zzz'): {'steps': [{'do': "cmd /logs container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/logs container:web host:zzz'): {'steps': [{'do': "cmd /logs container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/logs container:web*'): {'steps': [{'do': "cmd /logs container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/logs container:web*'): {'steps': [{'do': "cmd /logs container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/logs container:web* host:nas'): {'steps': [{'do': "cmd /logs container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/logs container:web* host:nas'): {'steps': [{'do': "cmd /logs container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/logs container:web* host:all'): {'steps': [{'do': "cmd /logs container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `web*` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/logs container:web* host:all'): {'steps': [{'do': "cmd /logs container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/logs container:web* host:zzz'): {'steps': [{'do': "cmd /logs container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/logs container:web* host:zzz'): {'steps': [{'do': "cmd /logs container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/logs container:nosuch'): {'steps': [{'do': "cmd /logs container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/logs container:nosuch'): {'steps': [{'do': "cmd /logs container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/logs container:nosuch host:nas'): {'steps': [{'do': "cmd /logs container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found. @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/logs container:nosuch host:nas'): {'steps': [{'do': "cmd /logs container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/logs container:nosuch host:all'): {'steps': [{'do': "cmd /logs container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/logs container:nosuch host:all'): {'steps': [{'do': "cmd /logs container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/logs container:nosuch host:zzz'): {'steps': [{'do': "cmd /logs container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/logs container:nosuch host:zzz'): {'steps': [{'do': "cmd /logs container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /groups ─────────────────────────────────────────────────────
    ('multi', '/groups'): {'steps': [{'do': 'cmd /groups',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Container groups**\n'
                        'No container groups configured. @local\n'
                        'No container groups configured. @nas\n'
                        'No container groups configured. @dead\n'
                        'No container groups configured. @refused\n'
                        'No container groups configured. @flaky  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/groups", "actor": "u7", "source": "discord"}']}},
    ('one', '/groups'): {'steps': [{'do': 'cmd /groups',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Container groups**\n'
                        'No container groups configured.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/groups", "actor": "u7", "source": "discord"}']}},
    # ── /settings ───────────────────────────────────────────────────
    ('multi', '/settings'): {'steps': [{'do': 'cmd /settings',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Settings**\n'
                        '🗓️ Schedule: `0 18 * * *`\n'
                        '🌍 Language: `en`\n'
                        '🔄 Auto-selfupdate: off\n'
                        '🔍 Debug: off\n'
                        '🚫 Excluded: `-`\n'
                        '📌 Pinned: `-`\n'
                        '⚡ Auto-update: `-`\n'
                        '🖧 Hosts: `local, nas, dead, refused, flaky`\n'
                        '🔧 Maintenance: off  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/settings", "actor": "u7", "source": "discord"}']}},
    ('one', '/settings'): {'steps': [{'do': 'cmd /settings',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Settings**\n'
                        '🗓️ Schedule: `0 18 * * *`\n'
                        '🌍 Language: `en`\n'
                        '🔄 Auto-selfupdate: off\n'
                        '🔍 Debug: off\n'
                        '🚫 Excluded: `-`\n'
                        '📌 Pinned: `-`\n'
                        '⚡ Auto-update: `-`\n'
                        '🔧 Maintenance: off  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "u7", "source": "discord"}']}},
    # ── /update ─────────────────────────────────────────────────────
    # :1413 → :2486. Write targeting, matched against the pending list by
    # prefix (`_match_in`, :1644), lock taken non-blocking, one batch per
    # host under one claim. Busy since Q15: the containers go on the
    # engine's queue (`claim_or_queue_many`) and the answer gives their
    # place; the drainer runs them on the release, results through the
    # broadcast.
    ('multi', '/update'): {'steps': [{'do': 'cmd /update',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Usage: `/update <name>` or `/update <glob>` (e.g. `/update '
                        'gluetun-*`). Updates only matching containers that have a pending '
                        'update.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "source": "discord"}']}},
    ('one', '/update'): {'steps': [{'do': 'cmd /update',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Usage: `/update <name>` or `/update <glob>` (e.g. `/update '
                        'gluetun-*`). Updates only matching containers that have a pending '
                        'update.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "source": "discord"}']}},
    ('multi', '/update host:nas'): {'steps': [{'do': "cmd /update host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Usage: `/update <name>` or `/update <glob>` (e.g. `/update '
                        'gluetun-*`). Updates only matching containers that have a pending '
                        'update.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/update host:nas'): {'steps': [{'do': "cmd /update host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Usage: `/update <name>` or `/update <glob>` (e.g. `/update '
                        'gluetun-*`). Updates only matching containers that have a pending '
                        'update.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/update host:all'): {'steps': [{'do': "cmd /update host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Usage: `/update <name>` or `/update <glob>` (e.g. `/update '
                        'gluetun-*`). Updates only matching containers that have a pending '
                        'update.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/update host:all'): {'steps': [{'do': "cmd /update host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Usage: `/update <name>` or `/update <glob>` (e.g. `/update '
                        'gluetun-*`). Updates only matching containers that have a pending '
                        'update.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/update host:zzz'): {'steps': [{'do': "cmd /update host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Usage: `/update <name>` or `/update <glob>` (e.g. `/update '
                        'gluetun-*`). Updates only matching containers that have a pending '
                        'update.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/update host:zzz'): {'steps': [{'do': "cmd /update host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Usage: `/update <name>` or `/update <glob>` (e.g. `/update '
                        'gluetun-*`). Updates only matching containers that have a pending '
                        'update.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/update container:web'): {'steps': [{'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/update container:web'): {'steps': [{'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/update container:web host:nas'): {'steps': [{'do': "cmd /update container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)  []'],
                'hosts': {'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@nas:web']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', '/update container:web host:nas'): {'steps': [{'do': "cmd /update container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/update container:web host:all'): {'steps': [{'do': "cmd /update container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '⚠️ No pending updates found. @dead\n'
                        '⚠️ No pending updates found. @refused\n'
                        '⚠️ No pending updates found. @flaky\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/update container:web host:all'): {'steps': [{'do': "cmd /update container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/update container:web host:zzz'): {'steps': [{'do': "cmd /update container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/update container:web host:zzz'): {'steps': [{'do': "cmd /update container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/update container:web*'): {'steps': [{'do': "cmd /update container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/update container:web*'): {'steps': [{'do': "cmd /update container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/update container:web* host:nas'): {'steps': [{'do': "cmd /update container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `web*` is not pinned. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/update container:web* host:nas'): {'steps': [{'do': "cmd /update container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/update container:web* host:all'): {'steps': [{'do': "cmd /update container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `web*` is not pinned.\n'
                        '📌 `web*` is not pinned. @nas\n'
                        '⚠️ No pending updates found. @dead\n'
                        '⚠️ No pending updates found. @refused\n'
                        '⚠️ No pending updates found. @flaky  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/update container:web* host:all'): {'steps': [{'do': "cmd /update container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/update container:web* host:zzz'): {'steps': [{'do': "cmd /update container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/update container:web* host:zzz'): {'steps': [{'do': "cmd /update container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `web*` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/update container:nosuch'): {'steps': [{'do': "cmd /update container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/update container:nosuch'): {'steps': [{'do': "cmd /update container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/update container:nosuch host:nas'): {'steps': [{'do': "cmd /update container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `nosuch` is not pinned. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/update container:nosuch host:nas'): {'steps': [{'do': "cmd /update container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/update container:nosuch host:all'): {'steps': [{'do': "cmd /update container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `nosuch` is not pinned.\n'
                        '📌 `nosuch` is not pinned. @nas\n'
                        '⚠️ No pending updates found. @dead\n'
                        '⚠️ No pending updates found. @refused\n'
                        '⚠️ No pending updates found. @flaky  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/update container:nosuch host:all'): {'steps': [{'do': "cmd /update container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/update container:nosuch host:zzz'): {'steps': [{'do': "cmd /update container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/update container:nosuch host:zzz'): {'steps': [{'do': "cmd /update container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `nosuch` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /updateall ──────────────────────────────────────────────────
    # :1415 → :2547 asks first (approved set in the token); the press re-
    # reads the file and runs only what was approved (:2583); on a held lock
    # it queues exactly that (Q15).
    ('multi', '/updateall'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', '/updateall'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']}],
     'state': {'pending': ['web@local'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('multi', '/updateall host:nas'): {'steps': [{'do': "cmd /updateall host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on @nas?\n'
                        '• `web` @nas — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('one', '/updateall host:nas'): {'steps': [{'do': "cmd /updateall host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']}],
     'state': {'pending': ['web@local'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('multi', '/updateall host:all'): {'steps': [{'do': "cmd /updateall host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **2** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 2 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', '/updateall host:all'): {'steps': [{'do': "cmd /updateall host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']}],
     'state': {'pending': ['web@local'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', '/updateall host:zzz'): {'steps': [{'do': "cmd /updateall host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('one', '/updateall host:zzz'): {'steps': [{'do': "cmd /updateall host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']}],
     'state': {'pending': ['web@local'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    # ── /restart ────────────────────────────────────────────────────
    # :1417 bare restarts Docksentry (go_down after the answer), :1422 with
    # a container goes to lifecycle.
    ('multi', '/restart'): {'steps': [{'do': 'cmd /restart',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('one', '/restart'): {'steps': [{'do': 'cmd /restart',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('multi', '/restart host:nas'): {'steps': [{'do': "cmd /restart host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('one', '/restart host:nas'): {'steps': [{'do': "cmd /restart host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('multi', '/restart host:all'): {'steps': [{'do': "cmd /restart host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('one', '/restart host:all'): {'steps': [{'do': "cmd /restart host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('multi', '/restart host:zzz'): {'steps': [{'do': "cmd /restart host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('one', '/restart host:zzz'): {'steps': [{'do': "cmd /restart host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('multi', '/restart container:web'): {'steps': [{'do': "cmd /restart container:'web'",
                'out': ['<ack deferred ephemeral>', '<edit> 🔁 `web` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/restart container:web'): {'steps': [{'do': "cmd /restart container:'web'",
                'out': ['<ack deferred ephemeral>', '<edit> 🔁 `web` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/restart container:web host:nas'): {'steps': [{'do': "cmd /restart container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> 🔁 `web` restarted. @nas  []'],
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps -a --format, inspect --format t=5, restart --time '
                                 't=120'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', '/restart container:web host:nas'): {'steps': [{'do': "cmd /restart container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> 🔁 `web` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/restart container:web host:all'): {'steps': [{'do': "cmd /restart container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔁 `web` restarted.\n'
                        '🔁 `web` restarted. @nas\n'
                        '🔁 `web` restarted. @flaky  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120, inspect --format t=5 ×2',
                          'nas': 'ps -a --format, inspect --format t=5, restart --time '
                                 't=120',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format, inspect --format t=5, restart --time '
                                   't=120'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/restart container:web host:all'): {'steps': [{'do': "cmd /restart container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>', '<edit> 🔁 `web` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/restart container:web host:zzz'): {'steps': [{'do': "cmd /restart container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/restart container:web host:zzz'): {'steps': [{'do': "cmd /restart container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>', '<edit> 🔁 `web` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/restart container:web*'): {'steps': [{'do': "cmd /restart container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **restart — 2 match(es) for** `web*`**:**\n'
                        '🔁 `web` restarted.\n'
                        '🔁 `webdav` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120, inspect --format t=5, restart --time '
                                   't=120'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/restart container:web*'): {'steps': [{'do': "cmd /restart container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **restart — 2 match(es) for** `web*`**:**\n'
                        '🔁 `web` restarted.\n'
                        '🔁 `webdav` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120, inspect --format t=5, restart --time '
                                   't=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/restart container:web* host:nas'): {'steps': [{'do': "cmd /restart container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **restart — 1 match(es) for** `web*`**:** @nas\n'
                        '🔁 `web` restarted.  []'],
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps -a --format, inspect --format t=5, restart --time '
                                 't=120'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/restart container:web* host:nas'): {'steps': [{'do': "cmd /restart container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **restart — 2 match(es) for** `web*`**:**\n'
                        '🔁 `web` restarted.\n'
                        '🔁 `webdav` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120, inspect --format t=5, restart --time '
                                   't=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/restart container:web* host:all'): {'steps': [{'do': "cmd /restart container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **restart — 2 match(es) for** `web*`**:**\n'
                        '🔁 `web` restarted.\n'
                        '🔁 `webdav` restarted.\n'
                        '**restart — 1 match(es) for** `web*`**:** @nas\n'
                        '🔁 `web` restarted.\n'
                        '**restart — 1 match(es) for** `web*`**:** @flaky\n'
                        '🔁 `web` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120, inspect --format t=5, restart --time '
                                   't=120, inspect --format t=5 ×2',
                          'nas': 'ps -a --format, inspect --format t=5, restart --time '
                                 't=120',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format, inspect --format t=5, restart --time '
                                   't=120'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/restart container:web* host:all'): {'steps': [{'do': "cmd /restart container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **restart — 2 match(es) for** `web*`**:**\n'
                        '🔁 `web` restarted.\n'
                        '🔁 `webdav` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120, inspect --format t=5, restart --time '
                                   't=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/restart container:web* host:zzz'): {'steps': [{'do': "cmd /restart container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/restart container:web* host:zzz'): {'steps': [{'do': "cmd /restart container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **restart — 2 match(es) for** `web*`**:**\n'
                        '🔁 `web` restarted.\n'
                        '🔁 `webdav` restarted.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120, inspect --format t=5, restart --time '
                                   't=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/restart container:nosuch'): {'steps': [{'do': "cmd /restart container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/restart container:nosuch'): {'steps': [{'do': "cmd /restart container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/restart container:nosuch host:nas'): {'steps': [{'do': "cmd /restart container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/restart container:nosuch host:nas'): {'steps': [{'do': "cmd /restart container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/restart container:nosuch host:all'): {'steps': [{'do': "cmd /restart container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/restart container:nosuch host:all'): {'steps': [{'do': "cmd /restart container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/restart container:nosuch host:zzz'): {'steps': [{'do': "cmd /restart container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/restart container:nosuch host:zzz'): {'steps': [{'do': "cmd /restart container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /stop ───────────────────────────────────────────────────────
    # :1422 → :2654, plan first, then a confirmation; the press re-runs
    # lifecycle.act on the raw argument.
    ('multi', '/stop'): {'steps': [{'do': 'cmd /stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/stop <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "source": "discord"}']}},
    ('one', '/stop'): {'steps': [{'do': 'cmd /stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/stop <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "source": "discord"}']}},
    ('multi', '/stop host:nas'): {'steps': [{'do': "cmd /stop host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/stop <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/stop host:nas'): {'steps': [{'do': "cmd /stop host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/stop <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/stop host:all'): {'steps': [{'do': "cmd /stop host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/stop <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/stop host:all'): {'steps': [{'do': "cmd /stop host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/stop <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/stop host:zzz'): {'steps': [{'do': "cmd /stop host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/stop <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/stop host:zzz'): {'steps': [{'do': "cmd /stop host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/stop <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/stop container:web'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/stop container:web'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/stop container:web host:nas'): {'steps': [{'do': "cmd /stop container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', '/stop container:web host:nas'): {'steps': [{'do': "cmd /stop container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/stop container:web host:all'): {'steps': [{'do': "cmd /stop container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 3 containers matching `web`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `web`, `web`  [Stop 3 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5 ×2',
                          'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/stop container:web host:all'): {'steps': [{'do': "cmd /stop container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/stop container:web host:zzz'): {'steps': [{'do': "cmd /stop container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/stop container:web host:zzz'): {'steps': [{'do': "cmd /stop container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/stop container:web*'): {'steps': [{'do': "cmd /stop container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 2 containers matching `web*`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `webdav`  [Stop 2 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, inspect --format '
                                   't=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/stop container:web*'): {'steps': [{'do': "cmd /stop container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 2 containers matching `web*`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `webdav`  [Stop 2 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, inspect --format '
                                   't=10'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/stop container:web* host:nas'): {'steps': [{'do': "cmd /stop container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/stop container:web* host:nas'): {'steps': [{'do': "cmd /stop container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 2 containers matching `web*`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `webdav`  [Stop 2 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, inspect --format '
                                   't=10'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/stop container:web* host:all'): {'steps': [{'do': "cmd /stop container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 4 containers matching `web*`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `webdav`, `web`, `web`  [Stop 4 containers→ds:stop:<tok1> '
                        '| Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5 ×2',
                          'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/stop container:web* host:all'): {'steps': [{'do': "cmd /stop container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 2 containers matching `web*`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `webdav`  [Stop 2 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, inspect --format '
                                   't=10'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/stop container:web* host:zzz'): {'steps': [{'do': "cmd /stop container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/stop container:web* host:zzz'): {'steps': [{'do': "cmd /stop container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 2 containers matching `web*`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `webdav`  [Stop 2 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, inspect --format '
                                   't=10'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/stop container:nosuch'): {'steps': [{'do': "cmd /stop container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/stop container:nosuch'): {'steps': [{'do': "cmd /stop container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/stop container:nosuch host:nas'): {'steps': [{'do': "cmd /stop container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/stop container:nosuch host:nas'): {'steps': [{'do': "cmd /stop container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/stop container:nosuch host:all'): {'steps': [{'do': "cmd /stop container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/stop container:nosuch host:all'): {'steps': [{'do': "cmd /stop container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/stop container:nosuch host:zzz'): {'steps': [{'do': "cmd /stop container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/stop container:nosuch host:zzz'): {'steps': [{'do': "cmd /stop container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /start ──────────────────────────────────────────────────────
    # :1422 → :2654 lifecycle.act directly.
    ('multi', '/start'): {'steps': [{'do': 'cmd /start',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/start <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "source": "discord"}']}},
    ('one', '/start'): {'steps': [{'do': 'cmd /start',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/start <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "source": "discord"}']}},
    ('multi', '/start host:nas'): {'steps': [{'do': "cmd /start host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/start <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/start host:nas'): {'steps': [{'do': "cmd /start host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/start <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/start host:all'): {'steps': [{'do': "cmd /start host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/start <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/start host:all'): {'steps': [{'do': "cmd /start host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/start <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/start host:zzz'): {'steps': [{'do': "cmd /start host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/start <container>`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/start host:zzz'): {'steps': [{'do': "cmd /start host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Usage: `/start <container>`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('multi', '/start container:web'): {'steps': [{'do': "cmd /start container:'web'",
                'out': ['<ack deferred ephemeral>', '<edit> ▶️ `web` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', '/start container:web'): {'steps': [{'do': "cmd /start container:'web'",
                'out': ['<ack deferred ephemeral>', '<edit> ▶️ `web` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/start container:web host:nas'): {'steps': [{'do': "cmd /start container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> ▶️ `web` started. @nas  []'],
                'hosts': {'nas': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', '/start container:web host:nas'): {'steps': [{'do': "cmd /start container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> ▶️ `web` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/start container:web host:all'): {'steps': [{'do': "cmd /start container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ▶️ `web` started.\n'
                        '▶️ `web` started. @nas\n'
                        '▶️ `web` started. @flaky  []'],
                'hosts': {'local': 'ps -a --format, start t=30',
                          'nas': 'ps -a --format, start t=30',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/start container:web host:all'): {'steps': [{'do': "cmd /start container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>', '<edit> ▶️ `web` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/start container:web host:zzz'): {'steps': [{'do': "cmd /start container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/start container:web host:zzz'): {'steps': [{'do': "cmd /start container:'web' host:'zzz'",
                'out': ['<ack deferred ephemeral>', '<edit> ▶️ `web` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/start container:web*'): {'steps': [{'do': "cmd /start container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **start — 2 match(es) for** `web*`**:**\n'
                        '▶️ `web` started.\n'
                        '▶️ `webdav` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30 ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', '/start container:web*'): {'steps': [{'do': "cmd /start container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **start — 2 match(es) for** `web*`**:**\n'
                        '▶️ `web` started.\n'
                        '▶️ `webdav` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', '/start container:web* host:nas'): {'steps': [{'do': "cmd /start container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **start — 1 match(es) for** `web*`**:** @nas\n'
                        '▶️ `web` started.  []'],
                'hosts': {'nas': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('one', '/start container:web* host:nas'): {'steps': [{'do': "cmd /start container:'web*' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **start — 2 match(es) for** `web*`**:**\n'
                        '▶️ `web` started.\n'
                        '▶️ `webdav` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/start container:web* host:all'): {'steps': [{'do': "cmd /start container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **start — 2 match(es) for** `web*`**:**\n'
                        '▶️ `web` started.\n'
                        '▶️ `webdav` started.\n'
                        '**start — 1 match(es) for** `web*`**:** @nas\n'
                        '▶️ `web` started.\n'
                        '**start — 1 match(es) for** `web*`**:** @flaky\n'
                        '▶️ `web` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30 ×2',
                          'nas': 'ps -a --format, start t=30',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('one', '/start container:web* host:all'): {'steps': [{'do': "cmd /start container:'web*' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **start — 2 match(es) for** `web*`**:**\n'
                        '▶️ `web` started.\n'
                        '▶️ `webdav` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "all"}, "source": "discord"}']}},
    ('multi', '/start container:web* host:zzz'): {'steps': [{'do': "cmd /start container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/start container:web* host:zzz'): {'steps': [{'do': "cmd /start container:'web*' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **start — 2 match(es) for** `web*`**:**\n'
                        '▶️ `web` started.\n'
                        '▶️ `webdav` started.  []'],
                'hosts': {'local': 'ps -a --format, start t=30 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"web*", "host": "zzz"}, "source": "discord"}']}},
    ('multi', '/start container:nosuch'): {'steps': [{'do': "cmd /start container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('one', '/start container:nosuch'): {'steps': [{'do': "cmd /start container:'nosuch'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"nosuch"}, "source": "discord"}']}},
    ('multi', '/start container:nosuch host:nas'): {'steps': [{'do': "cmd /start container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('one', '/start container:nosuch host:nas'): {'steps': [{'do': "cmd /start container:'nosuch' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/start container:nosuch host:all'): {'steps': [{'do': "cmd /start container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('one', '/start container:nosuch host:all'): {'steps': [{'do': "cmd /start container:'nosuch' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "all"}, "source": "discord"}']}},
    ('multi', '/start container:nosuch host:zzz'): {'steps': [{'do': "cmd /start container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    ('one', '/start container:nosuch host:zzz'): {'steps': [{'do': "cmd /start container:'nosuch' host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Container `nosuch` not found.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/start", "actor": "u7", "detail": {"container": '
                         '"nosuch", "host": "zzz"}, "source": "discord"}']}},
    # ── /cleanup ────────────────────────────────────────────────────
    # :1424 → :2723, every host by default (read-style), the lock guard per
    # host.
    ('multi', '/cleanup'): {'steps': [{'do': 'cmd /cleanup',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ✅ Total reclaimed space: 0 B\n'
                        '✅ Total reclaimed space: 0 B @nas\n'
                        "❌ Cleanup error: Command '['docker', '-H', 'ssh://dead', 'image', "
                        "'prune', '-a', '--force', '--filter', 'until=24h']' timed out "
                        'after 180 seconds @dead\n'
                        '❌ Cleanup failed: ssh: connect to host 192.0.2.30 port 22: '
                        'Connection refused @refused\n'
                        '✅ Total reclaimed space: 0 B @flaky  []'],
                'hosts': {'local': 'image prune -a --force --filter t=180',
                          'nas': 'image prune -a --force --filter t=180',
                          'dead': 'image prune -a --force --filter t=180',
                          'refused': 'image prune -a --force --filter t=180',
                          'flaky': 'image prune -a --force --filter t=180'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "source": "discord"}']}},
    ('one', '/cleanup'): {'steps': [{'do': 'cmd /cleanup',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ✅ Total reclaimed space: 0 B  []'],
                'hosts': {'local': 'image prune -a --force --filter t=180'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "source": "discord"}']}},
    ('multi', '/cleanup host:nas'): {'steps': [{'do': "cmd /cleanup host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ✅ Total reclaimed space: 0 B @nas  []'],
                'hosts': {'nas': 'image prune -a --force --filter t=180'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('one', '/cleanup host:nas'): {'steps': [{'do': "cmd /cleanup host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "detail": {"host": "nas"}, '
                         '"source": "discord"}']}},
    ('multi', '/cleanup host:all'): {'steps': [{'do': "cmd /cleanup host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ✅ Total reclaimed space: 0 B\n'
                        '✅ Total reclaimed space: 0 B @nas\n'
                        "❌ Cleanup error: Command '['docker', '-H', 'ssh://dead', 'image', "
                        "'prune', '-a', '--force', '--filter', 'until=24h']' timed out "
                        'after 180 seconds @dead\n'
                        '❌ Cleanup failed: ssh: connect to host 192.0.2.30 port 22: '
                        'Connection refused @refused\n'
                        '✅ Total reclaimed space: 0 B @flaky  []'],
                'hosts': {'local': 'image prune -a --force --filter t=180',
                          'nas': 'image prune -a --force --filter t=180',
                          'dead': 'image prune -a --force --filter t=180',
                          'refused': 'image prune -a --force --filter t=180',
                          'flaky': 'image prune -a --force --filter t=180'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('one', '/cleanup host:all'): {'steps': [{'do': "cmd /cleanup host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "detail": {"host": "all"}, '
                         '"source": "discord"}']}},
    ('multi', '/cleanup host:zzz'): {'steps': [{'do': "cmd /cleanup host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    ('one', '/cleanup host:zzz'): {'steps': [{'do': "cmd /cleanup host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "detail": {"host": "zzz"}, '
                         '"source": "discord"}']}},
    # ── /checkimages ────────────────────────────────────────────────
    # :1426 → :2777, read targeting.
    ('multi', '/checkimages'): {'steps': [{'do': 'cmd /checkimages',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Reclaimable image space**\n'
                        '🧹 @local — nothing to reclaim.\n'
                        '🧹 @nas — nothing to reclaim.\n'
                        "⚠️ Host `dead` could not be checked: Command '['docker', '-H', "
                        "'ssh://dead', 'system', 'df', '--format', '{{json .}}'\n"
                        '⚠️ Host `refused` could not be checked: ssh: connect to host '
                        '192.0.2.30 port 22: Connection refused\n'
                        '🧹 @flaky — nothing to reclaim.  []'],
                'hosts': {'local': 'system df --format t=15, images --filter --format t=20',
                          'nas': 'system df --format t=15, images --filter --format t=20',
                          'dead': 'system df --format t=15',
                          'refused': 'system df --format t=15',
                          'flaky': 'system df --format t=15, images --filter --format '
                                   't=20'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/checkimages", "actor": "u7", "source": "discord"}']}},
    ('one', '/checkimages'): {'steps': [{'do': 'cmd /checkimages',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Reclaimable image space**\n🧹 — nothing to reclaim.  []'],
                'hosts': {'local': 'system df --format t=15, images --filter --format '
                                   't=20'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/checkimages", "actor": "u7", "source": "discord"}']}},
    ('multi', '/checkimages host:nas'): {'steps': [{'do': "cmd /checkimages host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Reclaimable image space**\n'
                        '🧹 @nas — nothing to reclaim.  []'],
                'hosts': {'nas': 'system df --format t=15, images --filter --format '
                                 't=20'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/checkimages", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('one', '/checkimages host:nas'): {'steps': [{'do': "cmd /checkimages host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `nas`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/checkimages", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('multi', '/checkimages host:all'): {'steps': [{'do': "cmd /checkimages host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Reclaimable image space**\n'
                        '🧹 @local — nothing to reclaim.\n'
                        '🧹 @nas — nothing to reclaim.\n'
                        "⚠️ Host `dead` could not be checked: Command '['docker', '-H', "
                        "'ssh://dead', 'system', 'df', '--format', '{{json .}}'\n"
                        '⚠️ Host `refused` could not be checked: ssh: connect to host '
                        '192.0.2.30 port 22: Connection refused\n'
                        '🧹 @flaky — nothing to reclaim.  []'],
                'hosts': {'local': 'system df --format t=15, images --filter --format t=20',
                          'nas': 'system df --format t=15, images --filter --format t=20',
                          'dead': 'system df --format t=15',
                          'refused': 'system df --format t=15',
                          'flaky': 'system df --format t=15, images --filter --format '
                                   't=20'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/checkimages", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', '/checkimages host:all'): {'steps': [{'do': "cmd /checkimages host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `all`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/checkimages", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', '/checkimages host:zzz'): {'steps': [{'do': "cmd /checkimages host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`, `nas`, '
                        '`dead`, `refused`, `flaky`  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/checkimages", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('one', '/checkimages host:zzz'): {'steps': [{'do': "cmd /checkimages host:'zzz'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `zzz`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/checkimages", "actor": "u7", "detail": {"host": '
                         '"zzz"}, "source": "discord"}']}},
    ('multi', "/note container:web text:'a  b'"): {'steps': [{'do': "cmd /note container:'web' text:'a  b'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note on `web`: a  b  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "text": "a  b"}, "source": "discord"}'],
               'files': {'container_notes.json': '{"web": "a  b"}'}}},
    ('one', "/note container:web text:'a  b'"): {'steps': [{'do': "cmd /note container:'web' text:'a  b'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note on `web`: a  b  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "text": "a  b"}, "source": "discord"}'],
               'files': {'container_notes.json': '{"web": "a  b"}'}}},
    ('multi', "/note container:web text:'x' host:nas"): {'steps': [{'do': "cmd /note container:'web' text:'x' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note on `web`: x @nas  []'],
                'hosts': {'nas': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas", "text": "x"}, "source": "discord"}'],
               'files': {'container_notes.json': '{"nas/web": "x"}'}}},
    ('one', "/note container:web text:'x' host:nas"): {'steps': [{'do': "cmd /note container:'web' text:'x' host:'nas'",
                'out': ['<ack deferred ephemeral>', '<edit> 📝 Note on `web`: x  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/note", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas", "text": "x"}, "source": "discord"}'],
               'files': {'container_notes.json': '{"web": "x"}'}}},
    ('multi', '/setlink container:web url:https'): {'steps': [{'do': "cmd /setlink container:'web' url:'https://example.org/x'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link set to https://example.org/x  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "url": "https://example.org/x"}, "source": "discord"}'],
               'files': {'container_links.json': '{"web": "https://example.org/x"}'}}},
    ('one', '/setlink container:web url:https'): {'steps': [{'do': "cmd /setlink container:'web' url:'https://example.org/x'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 `web` link set to https://example.org/x  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "url": "https://example.org/x"}, "source": "discord"}'],
               'files': {'container_links.json': '{"web": "https://example.org/x"}'}}},
    ('multi', '/setlink container:web url:notaurl'): {'steps': [{'do': "cmd /setlink container:'web' url:'notaurl'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 URL must start with `http://` or `https://`.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "url": "notaurl"}, "source": "discord"}']}},
    ('one', '/setlink container:web url:notaurl'): {'steps': [{'do': "cmd /setlink container:'web' url:'notaurl'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔗 URL must start with `http://` or `https://`.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/setlink", "actor": "u7", "detail": {"container": '
                         '"web", "url": "notaurl"}, "source": "discord"}']}},
    ('multi', '/cooldown container:web seconds:3600'): {'steps': [{'do': "cmd /cooldown container:'web' seconds:3600",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web` update cooldown set to **600s** — Docksentry will '
                        'pause that long after recreating it before the next container.  '
                        '[]'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "seconds": "3600"}, "source": "discord"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'}}},
    ('one', '/cooldown container:web seconds:3600'): {'steps': [{'do': "cmd /cooldown container:'web' seconds:3600",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web` update cooldown set to **600s** — Docksentry will '
                        'pause that long after recreating it before the next container.  '
                        '[]'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "seconds": "3600"}, "source": "discord"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'}}},
    ('multi', '/cooldown container:web seconds:0'): {'steps': [{'do': "cmd /cooldown container:'web' seconds:0",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web` update cooldown cleared.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "seconds": "0"}, "source": "discord"}'],
               'files': {'update_cooldowns.json': '{}'}}},
    ('one', '/cooldown container:web seconds:0'): {'steps': [{'do': "cmd /cooldown container:'web' seconds:0",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web` update cooldown cleared.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "seconds": "0"}, "source": "discord"}'],
               'files': {'update_cooldowns.json': '{}'}}},
    ('multi', '/cooldown container:web seconds:-5'): {'steps': [{'do': "cmd /cooldown container:'web' seconds:-5",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web` update cooldown cleared.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "seconds": "-5"}, "source": "discord"}'],
               'files': {'update_cooldowns.json': '{}'}}},
    ('one', '/cooldown container:web seconds:-5'): {'steps': [{'do': "cmd /cooldown container:'web' seconds:-5",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web` update cooldown cleared.  []'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "seconds": "-5"}, "source": "discord"}'],
               'files': {'update_cooldowns.json': '{}'}}},
    ('multi', '/cooldown container:web seconds:3600 host:all'): {'steps': [{'do': "cmd /cooldown container:'web' seconds:3600 host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web` update cooldown set to **600s** — Docksentry will '
                        'pause that long after recreating it before the next container.\n'
                        '✅ `web` update cooldown set to **600s** — Docksentry will pause '
                        'that long after recreating it before the next container. @nas\n'
                        "Could not list containers: Command '['docker', '-H', "
                        '…#e1d1864335  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all", "seconds": "3600"}, "source": "discord"}'],
               'files': {'update_cooldowns.json': '{"flaky/web": 600, "nas/web": 600, '
                                                  '"web": 600}'}}},
    ('one', '/cooldown container:web seconds:3600 host:all'): {'steps': [{'do': "cmd /cooldown container:'web' seconds:3600 host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ `web` update cooldown set to **600s** — Docksentry will '
                        'pause that long after recreating it before the next container.  '
                        '[]'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cooldown", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all", "seconds": "3600"}, "source": "discord"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'}}},
    ('multi', '/logs container:web lines:5'): {'steps': [{'do': "cmd /logs container:'web' lines:5",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 5 t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "lines": "5"}, "source": "discord"}']}},
    ('one', '/logs container:web lines:5'): {'steps': [{'do': "cmd /logs container:'web' lines:5",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 5 t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "lines": "5"}, "source": "discord"}']}},
    ('multi', '/logs container:web lines:500'): {'steps': [{'do': "cmd /logs container:'web' lines:500",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 200 t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "lines": "500"}, "source": "discord"}']}},
    ('one', '/logs container:web lines:500'): {'steps': [{'do': "cmd /logs container:'web' lines:500",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 200 t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "lines": "500"}, "source": "discord"}']}},
    ('multi', '/logs container:web lines:0'): {'steps': [{'do': "cmd /logs container:'web' lines:0",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 30 t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "lines": "0"}, "source": "discord"}']}},
    ('one', '/logs container:web lines:0'): {'steps': [{'do': "cmd /logs container:'web' lines:0",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 30 t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "lines": "0"}, "source": "discord"}']}},
    ('multi', '/logs container:web host:local'): {'steps': [{'do': "cmd /logs container:'web' host:'local'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 30 t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "host": "local"}, "source": "discord"}']}},
    ('one', '/logs container:web host:local'): {'steps': [{'do': "cmd /logs container:'web' host:'local'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📋 **Logs for** `web` (last 30 lines):\n'
                        '```\n'
                        'line one\n'
                        'line two\n'
                        '```  []'],
                'hosts': {'local': 'ps -a --format, logs --tail 30 t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/logs", "actor": "u7", "detail": {"container": '
                         '"web", "host": "local"}, "source": "discord"}']}},
    ('multi', '/status host:local'): {'steps': [{'do': "cmd /status host:'local'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?  []'],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": '
                         '"local"}, "source": "discord"}']}},
    ('one', '/status host:local'): {'steps': [{'do': "cmd /status host:'local'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?  []'],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": '
                         '"local"}, "source": "discord"}']}},
    ('multi', '/status container:web host:local'): {'steps': [{'do': "cmd /status container:'web' host:'local'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **web** — ✅ running (healthy) · ⏱ ?\n'
                        '\n'
                        '📦 `nginx:1.25`\n'
                        '     `251ad31786ba`\n'
                        '\n'
                        '🔁 unless-stopped  []'],
                'hosts': {'local': 'ps -a --format, inspect t=10, image inspect --format '
                                   't=10, stats --no-stream --format t=20, inspect --size '
                                   '--format t=15, image inspect --format t=15'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "local"}, "source": "discord"}']}},
    ('one', '/status container:web host:local'): {'steps': [{'do': "cmd /status container:'web' host:'local'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **web** — ✅ running (healthy) · ⏱ ?\n'
                        '\n'
                        '📦 `nginx:1.25`\n'
                        '     `251ad31786ba`\n'
                        '\n'
                        '🔁 unless-stopped  []'],
                'hosts': {'local': 'ps -a --format, inspect t=10, image inspect --format '
                                   't=10, stats --no-stream --format t=20, inspect --size '
                                   '--format t=15, image inspect --format t=15'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "local"}, "source": "discord"}']}},
    ('multi', '/status container:media'): {'steps': [{'do': "cmd /status container:'media'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **media** @nas — ✅ running · ⏱ ?\n'
                        '\n'
                        '📦 `jellyfin/jellyfin:10.9`\n'
                        '     `67c91a7b0610`\n'
                        '\n'
                        '🔁 unless-stopped  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format t=10, inspect t=10, image inspect '
                                 '--format t=10, stats --no-stream --format t=20, inspect '
                                 '--size --format t=15, image inspect --format t=15'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"media"}, "source": "discord"}']}},
    ('one', '/status container:media'): {'steps': [{'do': "cmd /status container:'media'",
                'out': ['<ack deferred ephemeral>', '<edit> No containers running.  []'],
                'hosts': {'local': 'ps -a --format, ps -q t=10, inspect t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"media"}, "source": "discord"}']}},
    ('multi', '/status container:we'): {'steps': [{'do': "cmd /status container:'we'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **web** @nas — ✅ running (healthy) · ⏱ ?\n'
                        '\n'
                        '📦 `nginx:1.25`\n'
                        '     `251ad31786ba`\n'
                        '\n'
                        '🔁 unless-stopped  []'],
                'hosts': {'local': 'ps -a --format',
                          'nas': 'ps -a --format t=10, inspect t=10, image inspect '
                                 '--format t=10, stats --no-stream --format t=20, inspect '
                                 '--size --format t=15, image inspect --format t=15'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"we"}, "source": "discord"}']}},
    ('one', '/status container:we'): {'steps': [{'do': "cmd /status container:'we'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🟢 `web` — `nginx:1.25` — ⏱ ?  []'],
                'hosts': {'local': 'ps -a --format, ps -q t=10, inspect t=10, image '
                                   'inspect --format t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"we"}, "source": "discord"}']}},
    ('multi', '/status host:dead'): {'steps': [{'do': "cmd /status host:'dead'",
                'out': ['<ack deferred ephemeral>',
                        "<edit> ⚠ `dead` unreachable: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-q']' timed out after 10 seconds  []"],
                'hosts': {'dead': 'ps -q t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": "dead"}, '
                         '"source": "discord"}']}},
    ('one', '/status host:dead'): {'steps': [{'do': "cmd /status host:'dead'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `dead`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": "dead"}, '
                         '"source": "discord"}']}},
    ('multi', '/status host:refused'): {'steps': [{'do': "cmd /status host:'refused'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 '
                        'port 22: Connection refused  []'],
                'hosts': {'refused': 'ps -q t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": '
                         '"refused"}, "source": "discord"}']}},
    ('one', '/status host:refused'): {'steps': [{'do': "cmd /status host:'refused'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `refused`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"host": '
                         '"refused"}, "source": "discord"}']}},
    ('multi', '/status container:web host:dead'): {'steps': [{'do': "cmd /status container:'web' host:'dead'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ `dead` unreachable: no answer within 10 seconds (from '
                        'the last try, not asked again for 60s)  []'],
                'hosts': {'dead': 'ps -a --format t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "dead"}, "source": "discord"}']}},
    ('one', '/status container:web host:dead'): {'steps': [{'do': "cmd /status container:'web' host:'dead'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `dead`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "dead"}, "source": "discord"}']}},
    ('multi', '/status container:web host:refused'): {'steps': [{'do': "cmd /status container:'web' host:'refused'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 '
                        'port 22: Connection refused (from the last try, not asked again '
                        'for 60s)  []'],
                'hosts': {'refused': 'ps -a --format t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "refused"}, "source": "discord"}']}},
    ('one', '/status container:web host:refused'): {'steps': [{'do': "cmd /status container:'web' host:'refused'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `refused`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "detail": {"container": '
                         '"web", "host": "refused"}, "source": "discord"}']}},
    ('multi', '/check host:dead'): {'steps': [{'do': "cmd /check host:'dead'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n'
                        "⚠️ Host `dead` could not be checked: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '--format', '{{.Names}}|{{.Image}}  []"],
                'hosts': {'dead': 'ps --format'},
                'events': ['check_all@dead']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "dead"}, '
                         '"source": "discord"}']}},
    ('one', '/check host:dead'): {'steps': [{'do': "cmd /check host:'dead'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `dead`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "dead"}, '
                         '"source": "discord"}']}},
    ('multi', '/check host:refused'): {'steps': [{'do': "cmd /check host:'refused'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n'
                        '⚠️ Host `refused` could not be checked: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Con  []'],
                'hosts': {'refused': 'ps --format'},
                'events': ['check_all@refused']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": '
                         '"refused"}, "source": "discord"}']}},
    ('one', '/check host:refused'): {'steps': [{'do': "cmd /check host:'refused'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `refused`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": '
                         '"refused"}, "source": "discord"}']}},
    ('multi', '/check host:flaky'): {'steps': [{'do': "cmd /check host:'flaky'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n'
                        '⚠️ Host `flaky` could not be checked: registry did not answer in '
                        '15s  []'],
                'events': ['check_all@flaky']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "flaky"}, '
                         '"source": "discord"}']}},
    ('one', '/check host:flaky'): {'steps': [{'do': "cmd /check host:'flaky'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ❌ Unknown host `flaky`. Managed hosts: `local`  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "detail": {"host": "flaky"}, '
                         '"source": "discord"}']}},
    ('multi', '/update container:media host:nas'): {'steps': [{'do': "cmd /update container:'media' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `media` is not pinned. @nas  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"media", "host": "nas"}, "source": "discord"}']}},
    ('one', '/update container:media host:nas'): {'steps': [{'do': "cmd /update container:'media' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> 📌 `media` is not pinned.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"media", "host": "nas"}, "source": "discord"}']}},
    ('multi', '/selfupdate version:previous'): {'steps': [{'do': "cmd /selfupdate version:'previous'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⬆️ Self-update started — I will send you a direct message '
                        'when it finishes, and restart if there is something to apply.  '
                        '[]'],
                'events': ["selfupdate.run target='previous'"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/selfupdate", "actor": "u7", "detail": {"version": '
                         '"previous"}, "source": "discord"}']}},
    ('one', '/selfupdate version:previous'): {'steps': [{'do': "cmd /selfupdate version:'previous'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⬆️ Self-update started — I will send you a direct message '
                        'when it finishes, and restart if there is something to apply.  '
                        '[]'],
                'events': ["selfupdate.run target='previous'"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/selfupdate", "actor": "u7", "detail": {"version": '
                         '"previous"}, "source": "discord"}']}},
    ('multi', '/selfupdate version:2.0.0'): {'steps': [{'do': "cmd /selfupdate version:'2.0.0'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⬆️ Self-update started — I will send you a direct message '
                        'when it finishes, and restart if there is something to apply.  '
                        '[]'],
                'events': ["selfupdate.run target='2.0.0'"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/selfupdate", "actor": "u7", "detail": {"version": '
                         '"2.0.0"}, "source": "discord"}']}},
    ('one', '/selfupdate version:2.0.0'): {'steps': [{'do': "cmd /selfupdate version:'2.0.0'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⬆️ Self-update started — I will send you a direct message '
                        'when it finishes, and restart if there is something to apply.  '
                        '[]'],
                'events': ["selfupdate.run target='2.0.0'"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/selfupdate", "actor": "u7", "detail": {"version": '
                         '"2.0.0"}, "source": "discord"}']}},
    ('multi', '/lang code:de'): {'steps': [{'do': "cmd /lang code:'de'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ Sprache auf **Deutsch** geändert.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/lang", "actor": "u7", "detail": {"code": "de"}, '
                         '"source": "discord"}'],
               'files': {'settings.json': '{"language": "de"}'}}},
    ('one', '/lang code:de'): {'steps': [{'do': "cmd /lang code:'de'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ Sprache auf **Deutsch** geändert.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/lang", "actor": "u7", "detail": {"code": "de"}, '
                         '"source": "discord"}'],
               'files': {'settings.json': '{"language": "de"}'}}},
    ('multi', '/lang code:xx'): {'steps': [{'do': "cmd /lang code:'xx'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ Unknown language `xx`. Available: ar, de, en, es, fr, '
                        'hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/lang", "actor": "u7", "detail": {"code": "xx"}, '
                         '"source": "discord"}']}},
    ('one', '/lang code:xx'): {'steps': [{'do': "cmd /lang code:'xx'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ Unknown language `xx`. Available: ar, de, en, es, fr, '
                        'hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/lang", "actor": "u7", "detail": {"code": "xx"}, '
                         '"source": "discord"}']}},
    ('multi', "/lang code:' DE '"): {'steps': [{'do': "cmd /lang code:' DE '",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ Sprache auf **Deutsch** geändert.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/lang", "actor": "u7", "detail": {"code": " DE "}, '
                         '"source": "discord"}'],
               'files': {'settings.json': '{"language": "de"}'}}},
    ('one', "/lang code:' DE '"): {'steps': [{'do': "cmd /lang code:' DE '",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ✅ Sprache auf **Deutsch** geändert.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/lang", "actor": "u7", "detail": {"code": " DE "}, '
                         '"source": "discord"}'],
               'files': {'settings.json': '{"language": "de"}'}}},
    ('multi', '/maintenance duration:on'): {'steps': [{'do': "cmd /maintenance duration:'on'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Could not read `on` as a duration. Try `2h`, `30m`, `1d`, '
                        '`forever` or `off`.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"on"}, "source": "discord"}']}},
    ('one', '/maintenance duration:on'): {'steps': [{'do': "cmd /maintenance duration:'on'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Could not read `on` as a duration. Try `2h`, `30m`, `1d`, '
                        '`forever` or `off`.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"on"}, "source": "discord"}']}},
    ('multi', '/maintenance duration:forever'): {'steps': [{'do': "cmd /maintenance duration:'forever'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **on** until you turn it off.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"forever"}, "source": "discord"}'],
               'files': {'maintenance.json': '{"until": "forever"}'}}},
    ('one', '/maintenance duration:forever'): {'steps': [{'do': "cmd /maintenance duration:'forever'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **on** until you turn it off.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"forever"}, "source": "discord"}'],
               'files': {'maintenance.json': '{"until": "forever"}'}}},
    ('multi', '/maintenance duration:off'): {'steps': [{'do': "cmd /maintenance duration:'off'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **off** — updates run on schedule.  '
                        '[]']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"off"}, "source": "discord"}'],
               'files': {'maintenance.json': '{}'}}},
    ('one', '/maintenance duration:off'): {'steps': [{'do': "cmd /maintenance duration:'off'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **off** — updates run on schedule.  '
                        '[]']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"off"}, "source": "discord"}'],
               'files': {'maintenance.json': '{}'}}},
    ('multi', '/maintenance duration:abc'): {'steps': [{'do': "cmd /maintenance duration:'abc'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Could not read `abc` as a duration. Try `2h`, `30m`, `1d`, '
                        '`forever` or `off`.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"abc"}, "source": "discord"}']}},
    ('one', '/maintenance duration:abc'): {'steps': [{'do': "cmd /maintenance duration:'abc'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Could not read `abc` as a duration. Try `2h`, `30m`, `1d`, '
                        '`forever` or `off`.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"abc"}, "source": "discord"}']}},
    ('multi', '/maintenance duration:2h, then bare'): {'steps': [{'do': "cmd /maintenance duration:'2h'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **on** until <hh:mm>.  []']},
               {'do': 'cmd /maintenance',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **on** — 1h 59m remaining.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"2h"}, "source": "discord"}',
                         '{"action": "/maintenance", "actor": "u7", "source": "discord"}'],
               'files': {'maintenance.json': '{"until": "<ts>"}'}}},
    ('one', '/maintenance duration:2h, then bare'): {'steps': [{'do': "cmd /maintenance duration:'2h'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **on** until <hh:mm>.  []']},
               {'do': 'cmd /maintenance',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔧 Maintenance mode is **on** — 1h 59m remaining.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/maintenance", "actor": "u7", "detail": {"duration": '
                         '"2h"}, "source": "discord"}',
                         '{"action": "/maintenance", "actor": "u7", "source": "discord"}'],
               'files': {'maintenance.json': '{"until": "<ts>"}'}}},
    ('multi', '/hosts check:ssh://x'): {'steps': [{'do': "cmd /hosts check:'ssh://x'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **Pre-flight for** `ssh://x`\n'
                        '• Nothing answered at `ssh://x` — stand-in: not dialled  []'],
                'events': ["RemoteBackend('ssh://x')", "hostcheck.preflight('ssh://x')"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/hosts", "actor": "u7", "detail": {"check": '
                         '"ssh://x"}, "source": "discord"}']}},
    ('one', '/hosts check:ssh://x'): {'steps': [{'do': "cmd /hosts check:'ssh://x'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔎 **Pre-flight for** `ssh://x`\n'
                        '• Nothing answered at `ssh://x` — stand-in: not dialled  []'],
                'events': ["RemoteBackend('ssh://x')", "hostcheck.preflight('ssh://x')"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/hosts", "actor": "u7", "detail": {"check": '
                         '"ssh://x"}, "source": "discord"}']}},
    ('multi', '/debug twice'): {'steps': [{'do': 'cmd /debug',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔍 Debug logging **on**. It shows every registry call and '
                        'the per-container bookkeeping of a scan.  []']},
               {'do': 'cmd /debug',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔍 Debug logging **off**. It shows every registry call and '
                        'the per-container bookkeeping of a scan.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/debug", "actor": "u7", "source": "discord"}',
                         '{"action": "/debug", "actor": "u7", "source": "discord"}'],
               'files': {'settings.json': '{}'}}},
    ('one', '/debug twice'): {'steps': [{'do': 'cmd /debug',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔍 Debug logging **on**. It shows every registry call and '
                        'the per-container bookkeeping of a scan.  []']},
               {'do': 'cmd /debug',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔍 Debug logging **off**. It shows every registry call and '
                        'the per-container bookkeeping of a scan.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/debug", "actor": "u7", "source": "discord"}',
                         '{"action": "/debug", "actor": "u7", "source": "discord"}'],
               'files': {'settings.json': '{}'}}},
    ('multi', '/restore .txt'): {'steps': [{'do': "cmd /restore filename:'backup.txt' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ That file is not JSON, so it is not a Docksentry '
                        'backup.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', '/restore .txt'): {'steps': [{'do': "cmd /restore filename:'backup.txt' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ That file is not JSON, so it is not a Docksentry '
                        'backup.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('multi', '/restore too large'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:3145728 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ 3.0 MB is too large for a backup file. Nothing was '
                        'downloaded. The limit is 2 MB.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', '/restore too large'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:3145728 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ 3.0 MB is too large for a backup file. Nothing was '
                        'downloaded. The limit is 2 MB.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('multi', '/restore not json'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:16 "
                      "url:'https://cdn.discordapp.invalid/attachments/text.json'",
                'out': ['<ack deferred ephemeral>',
                        '<log> Discord: attachment was not JSON: Expecting value: line 1 '
                        'column 1 (char 0)',
                        '<edit> ⚠️ That file is not JSON, so it is not a Docksentry '
                        'backup. `Expecting value: line 1 column 1 (char 0)`  []'],
                'events': ['fetch text.json']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', '/restore not json'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:16 "
                      "url:'https://cdn.discordapp.invalid/attachments/text.json'",
                'out': ['<ack deferred ephemeral>',
                        '<log> Discord: attachment was not JSON: Expecting value: line 1 '
                        'column 1 (char 0)',
                        '<edit> ⚠️ That file is not JSON, so it is not a Docksentry '
                        'backup. `Expecting value: line 1 column 1 (char 0)`  []'],
                'events': ['fetch text.json']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('multi', '/restore not a backup'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:6 "
                      "url:'https://cdn.discordapp.invalid/attachments/list.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ That is JSON, but not a Docksentry backup — no '
                        '`schema_version` in it.  []'],
                'events': ['fetch list.json']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', '/restore not a backup'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:6 "
                      "url:'https://cdn.discordapp.invalid/attachments/list.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ That is JSON, but not a Docksentry backup — no '
                        '`schema_version` in it.  []'],
                'events': ['fetch list.json']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('multi', '/restore 404'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/gone.json'",
                'out': ['<ack deferred ephemeral>',
                        '<log> Discord: attachment fetch returned 404: '
                        'https://cdn.discordapp.invalid/attachments/gone.json',
                        '<edit> ⚠️ The chat would not hand me that file — it answered '
                        '**HTTP 404**. Try attaching it again.  []'],
                'events': ['fetch gone.json']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', '/restore 404'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/gone.json'",
                'out': ['<ack deferred ephemeral>',
                        '<log> Discord: attachment fetch returned 404: '
                        'https://cdn.discordapp.invalid/attachments/gone.json',
                        '<edit> ⚠️ The chat would not hand me that file — it answered '
                        '**HTTP 404**. Try attaching it again.  []'],
                'events': ['fetch gone.json']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    # ── unknown command ─────────────────────────────────────────────
    ('multi', 'unknown command'): {'steps': [{'do': 'cmd /nosuchcmd',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Unknown command `nosuchcmd`.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/nosuchcmd", "actor": "u7", "source": "discord"}']}},
    ('one', 'unknown command'): {'steps': [{'do': 'cmd /nosuchcmd',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Unknown command `nosuchcmd`.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/nosuchcmd", "actor": "u7", "source": "discord"}']}},
    # ── stranger user ───────────────────────────────────────────────
    ('multi', 'stranger user'): {'steps': [{'do': "cmd /status {'user': '9'}"}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'stranger user'): {'steps': [{'do': "cmd /status {'user': '9'}"}], 'state': {'pending': ['web@local']}},
    # ── other guild ─────────────────────────────────────────────────
    ('multi', 'other guild'): {'steps': [{'do': "cmd /status {'guild': '666'}"}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'other guild'): {'steps': [{'do': "cmd /status {'guild': '666'}"}], 'state': {'pending': ['web@local']}},
    # ── not an interaction ──────────────────────────────────────────
    ('multi', 'not an interaction'): {'steps': [{'do': 'event MESSAGE_CREATE 2'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'not an interaction'): {'steps': [{'do': 'event MESSAGE_CREATE 2'}], 'state': {'pending': ['web@local']}},
    # ── modal submit ────────────────────────────────────────────────
    ('multi', 'modal submit'): {'steps': [{'do': 'event INTERACTION_CREATE 5'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'modal submit'): {'steps': [{'do': 'event INTERACTION_CREATE 5'}], 'state': {'pending': ['web@local']}},
    # ── lock ────────────────────────────────────────────────────────
    # the update lock held from outside for one command; `release` gives it
    # back through the door (the drainer runs what was queued), `unlock` raw
    # (it stays queued).
    ('multi', 'lock: /update container:web'): {'steps': [{'do': 'lock'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queued': ['web'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'lock: /update container:web'): {'steps': [{'do': 'lock'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'queued': ['web'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'lock: /update container:web, then released'): {'steps': [{'do': 'lock'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'lock: /update container:web, then released'): {'steps': [{'do': 'lock'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<telegram> ✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': [],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'lock: /update container:web twice'): {'steps': [{'do': 'lock'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is already in the queue (waiting 0s) — it runs when its '
                        'turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queued': ['web'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}',
                         '{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'lock: /update container:web twice'): {'steps': [{'do': 'lock'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is already in the queue (waiting 0s) — it runs when its '
                        'turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'queued': ['web'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}',
                         '{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'lock: /update container:web host:all, then released'): {'steps': [{'do': 'lock'},
               {'do': "cmd /update container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        '🕒 `web @nas` is queued — position 2. It runs when its turn '
                        'comes.\n'
                        'The result is posted in <#chan-1>, not here.\n'
                        '⚠️ No pending updates found. @dead\n'
                        '⚠️ No pending updates found. @refused\n'
                        '⚠️ No pending updates found. @flaky  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<telegram> ⏳ Updating `web @nas`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)',
                        '<telegram> ✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format',
                          'nas': 'inspect --format t=10 ×2, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web']}],
     'state': {'pending': [],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', 'lock: /update container:web host:all, then released'): {'steps': [{'do': 'lock'},
               {'do': "cmd /update container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<telegram> ✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': [],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', 'lock: /update container:web, queue full'): {'steps': [{'do': 'lock'},
               {'do': 'fill 0'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '⚠️ The update queue is full (50) — this request was not queued; '
                        'anything already waiting keeps its place. Run the command again '
                        'once it has worked through.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queued': ['q0…q49 (50)'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'lock: /update container:web, queue full'): {'steps': [{'do': 'lock'},
               {'do': 'fill 0'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '⚠️ The update queue is full (50) — this request was not queued; '
                        'anything already waiting keeps its place. Run the command again '
                        'once it has worked through.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'queued': ['q0…q49 (50)'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'lock: /update container:web host:all, queue nearly full'): {'steps': [{'do': 'lock'},
               {'do': 'fill 1'},
               {'do': "cmd /update container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '⚠️ The update queue is full (50) — this request was not queued; '
                        'anything already waiting keeps its place. Run the command again '
                        'once it has worked through.\n'
                        '⚠️ No pending updates found. @dead\n'
                        '⚠️ No pending updates found. @refused\n'
                        '⚠️ No pending updates found. @flaky  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queued': ['q0…q48 (49)'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', 'lock: /update container:web host:all, queue nearly full'): {'steps': [{'do': 'lock'},
               {'do': 'fill 1'},
               {'do': "cmd /update container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> **Update**\n'
                        '🕒 `web` is queued — position 50. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'queued': ['q0…q48 (49)', 'web'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', '/update container:web host:all, three hosts pending'): {'steps': [{'do': 'add_pending web nginx:1.25 flaky'},
               {'do': "cmd /update container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '⚠️ No pending updates found. @dead\n'
                        '⚠️ No pending updates found. @refused\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @flaky: '
                        'updated (stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format',
                          'flaky': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web',
                           'update_container@nas:web',
                           'update_container@flaky:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', '/update container:web host:all, three hosts pending'): {'steps': [{'do': 'add_pending web nginx:1.25 flaky'},
               {'do': "cmd /update container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@flaky'],
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', 'lock: /check'): {'steps': [{'do': 'lock'},
               {'do': 'cmd /check',
                'out': ['<ack deferred ephemeral>',
                        '<edit> An update is running right now — try again in a moment  '
                        '[]']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "source": "discord"}']}},
    ('one', 'lock: /check'): {'steps': [{'do': 'lock'},
               {'do': 'cmd /check',
                'out': ['<ack deferred ephemeral>',
                        '<edit> An update is running right now — try again in a moment  '
                        '[]']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "source": "discord"}']}},
    ('multi', 'lock: /cleanup'): {'steps': [{'do': 'lock'},
               {'do': 'cmd /cleanup',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        "<edit> ⏳ Updates in progress — cleanup skipped so it can't delete "
                        'a freshly pulled image. Try again once they finish.\n'
                        "⏳ Updates in progress — cleanup skipped so it can't delete a "
                        'freshly pulled image. Try again once they finish. @nas\n'
                        "⏳ Updates in progress — cleanup skipped so it can't delete a "
                        'freshly pull…#f615d4a3c8  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "source": "discord"}']}},
    ('one', 'lock: /cleanup'): {'steps': [{'do': 'lock'},
               {'do': 'cmd /cleanup',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        "<edit> ⏳ Updates in progress — cleanup skipped so it can't delete "
                        'a freshly pulled image. Try again once they finish.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/cleanup", "actor": "u7", "source": "discord"}']}},
    ('multi', 'lock: /stop container:web'): {'steps': [{'do': 'lock'},
               {'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        "<edit> ⏳ Updates in progress — stop refused so it can't interfere "
                        'with a running update. Try again once they finish.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'lock: /stop container:web'): {'steps': [{'do': 'lock'},
               {'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        "<edit> ⏳ Updates in progress — stop refused so it can't interfere "
                        'with a running update. Try again once they finish.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'lock: /restart container:web'): {'steps': [{'do': 'lock'},
               {'do': "cmd /restart container:'web'",
                'out': ['<ack deferred ephemeral>',
                        "<edit> ⏳ Updates in progress — restart refused so it can't "
                        'interfere with a running update. Try again once they finish.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'lock: /restart container:web'): {'steps': [{'do': 'lock'},
               {'do': "cmd /restart container:'web'",
                'out': ['<ack deferred ephemeral>',
                        "<edit> ⏳ Updates in progress — restart refused so it can't "
                        'interfere with a running update. Try again once they finish.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'lock: /selfupdate, then /update releases it'): {'steps': [{'do': 'lock'},
               {'do': 'cmd /selfupdate',
                'out': ['<ack deferred ephemeral>',
                        '[rest] <dm-open 7>',
                        '[rest] <channel dm-7> ⏳ Container updates in progress — '
                        'self-update queued, it will start automatically once they finish.',
                        '<edit> ⬆️ Self-update started — I will send you a direct message '
                        'when it finishes, and restart if there is something to apply.  '
                        '[]']},
               {'do': 'unlock'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> 🚀 Container updates finished — starting the queued '
                        'self-update...',
                        '[rest] <channel chan-1> 🚀 Container updates finished — starting '
                        'the queued self-update...',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web', 'selfupdate.run target=None']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/selfupdate", "actor": "u7", "source": "discord"}',
                         '{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'lock: /selfupdate, then /update releases it'): {'steps': [{'do': 'lock'},
               {'do': 'cmd /selfupdate',
                'out': ['<ack deferred ephemeral>',
                        '[rest] <dm-open 7>',
                        '[rest] <channel dm-7> ⏳ Container updates in progress — '
                        'self-update queued, it will start automatically once they finish.',
                        '<edit> ⬆️ Self-update started — I will send you a direct message '
                        'when it finishes, and restart if there is something to apply.  '
                        '[]']},
               {'do': 'unlock'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> 🚀 Container updates finished — starting the queued '
                        'self-update...',
                        '[rest] <channel chan-1> 🚀 Container updates finished — starting '
                        'the queued self-update...',
                        '<edit> **Update**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web', 'selfupdate.run target=None']}],
     'state': {'pending': None,
               'audit': ['{"action": "/selfupdate", "actor": "u7", "source": "discord"}',
                         '{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', '/status twice'): {'steps': [{'do': 'cmd /status',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?\n'
                        '🟢 `web` @nas — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `media` @nas — `jellyfin/jellyfin:10.9` — ⏱ ?\n'
                        "⚠ `dead` unreachable: Command '['docker', '-H', 'ssh://dead', "
                        "'ps', '-q']' timed out after 10 seconds…#20d73b0bc5  []"],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3',
                          'nas': 'ps -q t=10, inspect t=10, image inspect --format t=10 ×2',
                          'dead': 'ps -q t=10',
                          'refused': 'ps -q t=10',
                          'flaky': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×2'}},
               {'do': 'cmd /status',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?\n'
                        '🟢 `web` @nas — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `media` @nas — `jellyfin/jellyfin:10.9` — ⏱ ?\n'
                        '⚠ `dead` unreachable: no answer within 10 seconds (from the last '
                        'try, not asked again for 60s)\n'
                        '⚠ `ref…#e77fa6c0d5  []'],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3',
                          'nas': 'ps -q t=10, inspect t=10, image inspect --format t=10 ×2',
                          'flaky': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/status", "actor": "u7", "source": "discord"}',
                         '{"action": "/status", "actor": "u7", "source": "discord"}']}},
    ('one', '/status twice'): {'steps': [{'do': 'cmd /status',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?  []'],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3'}},
               {'do': 'cmd /status',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚪ `docksentry` — `ghcr.io/example/docksentry:latest` — ⏱ '
                        '?\n'
                        '🟢 `web` — `nginx:1.25` — ⏱ ?\n'
                        '⚪ `db` — `postgres:16` — ⏱ ?  []'],
                'hosts': {'local': 'ps -q t=10, inspect t=10, image inspect --format t=10 '
                                   '×3'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/status", "actor": "u7", "source": "discord"}',
                         '{"action": "/status", "actor": "u7", "source": "discord"}']}},
    ('multi', '/check twice'): {'steps': [{'do': 'cmd /check',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25\n'
                        "⚠️ Host `dead` could not be checked: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '--format', '{{.Names}}|{{.Image}}\n"
                        '⚠️ Host `refused` could not be checked: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Con\n'
                        '⚠️ Host `flaky` could not be checked: registry did not answer in '
                        '15s  []'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=5',
                          'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                 'inspect --format ×2',
                          'dead': 'ps --format',
                          'refused': 'ps --format'},
                'events': ['check_all@local',
                           'check_all@nas',
                           'check_all@dead',
                           'check_all@refused',
                           'check_all@flaky']},
               {'do': 'cmd /check',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25\n'
                        '⚠️ Host `dead` could not be checked: no answer within 30 seconds '
                        '(from the last try, not asked again for 60s)\n'
                        '⚠️ Host `refused` could not be checked: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Co… '
                        '…#3c4ace7fa8  []'],
                'hosts': {'local': 'ps --format, inspect t=60, inspect --format ×2',
                          'nas': 'ps --format, inspect t=60, inspect --format ×2'},
                'events': ['check_all@local', 'check_all@nas', 'check_all@flaky']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/check", "actor": "u7", "source": "discord"}',
                         '{"action": "/check", "actor": "u7", "source": "discord"}']}},
    ('one', '/check twice'): {'steps': [{'do': 'cmd /check',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n• `web` — nginx:1.25  []'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ['check_all@local']},
               {'do': 'cmd /check',
                'out': ['<ack deferred ephemeral>',
                        '<edit> **Updates available**\n• `web` — nginx:1.25  []'],
                'hosts': {'local': 'ps --format, inspect t=60, inspect --format ×2'},
                'events': ['check_all@local']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/check", "actor": "u7", "source": "discord"}',
                         '{"action": "/check", "actor": "u7", "source": "discord"}']}},
    # ── pending ─────────────────────────────────────────────────────
    ('multi', 'pending emptied: /update container:web'): {'steps': [{'do': 'drop_pending'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠️ No pending updates found.  []']}],
     'state': {'pending': None,
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'pending emptied: /update container:web'): {'steps': [{'do': 'drop_pending'},
               {'do': "cmd /update container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠️ No pending updates found.  []']}],
     'state': {'pending': None,
               'audit': ['{"action": "/update", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'pending emptied: /updates'): {'steps': [{'do': 'drop_pending'},
               {'do': 'cmd /updates',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ No pending updates found.  []']}],
     'state': {'pending': None,
               'audit': ['{"action": "/updates", "actor": "u7", "source": "discord"}']}},
    ('one', 'pending emptied: /updates'): {'steps': [{'do': 'drop_pending'},
               {'do': 'cmd /updates',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠️ No pending updates found.  []']}],
     'state': {'pending': None,
               'audit': ['{"action": "/updates", "actor": "u7", "source": "discord"}']}},
    # ── stop ────────────────────────────────────────────────────────
    # the press: `_on_component` (:1102) — cancel pops, unknown or wrong-
    # action token is 'expired', owner gate fails closed before the TTL and
    # the pop, then `_retire_buttons` edits the origin.
    ('multi', 'stop: press, press again'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'stop: press, press again'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'stop host:nas: press'): {'steps': [{'do': "cmd /stop container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped. @nas  []'],
                'hosts': {'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10, inspect --format t=5, stop --time t=90, inspect '
                                 '--format t=5'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('one', 'stop host:nas: press'): {'steps': [{'do': "cmd /stop container:'web' host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "nas"}, "source": "discord"}']}},
    ('multi', 'stop host:all: press'): {'steps': [{'do': "cmd /stop container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 3 containers matching `web`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `web`, `web`  [Stop 3 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5 ×2',
                          'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.\n'
                        '🟥 `web` stopped. @nas\n'
                        '🟥 `web` stopped. @flaky  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5',
                          'nas': 'ps -a --format, inspect --format t=5, inspect --format '
                                 't=10, inspect --format t=5, stop --time t=90, inspect '
                                 '--format t=5',
                          'dead': 'ps -a --format',
                          'refused': 'ps -a --format',
                          'flaky': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('one', 'stop host:all: press'): {'steps': [{'do': "cmd /stop container:'web' host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web", "host": "all"}, "source": "discord"}']}},
    ('multi', 'stop web*: press'): {'steps': [{'do': "cmd /stop container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 2 containers matching `web*`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `webdav`  [Stop 2 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, inspect --format '
                                   't=10'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **stop — 2 match(es) for** `web*`**:**\n'
                        '🟥 `web` stopped.\n'
                        '🟥 `webdav` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5 ×2, inspect --format t=10, inspect '
                                   '--format t=5, stop --time t=90, inspect --format '
                                   't=5'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('one', 'stop web*: press'): {'steps': [{'do': "cmd /stop container:'web*'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop 2 containers matching `web*`?\n'
                        'They stay down until something starts them again.\n'
                        '`web`, `webdav`  [Stop 2 containers→ds:stop:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, inspect --format '
                                   't=10'}},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **stop — 2 match(es) for** `web*`**:**\n'
                        '🟥 `web` stopped.\n'
                        '🟥 `webdav` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5 ×2, inspect --format t=10, inspect '
                                   '--format t=5, stop --time t=90, inspect --format '
                                   't=5'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web*"}, "source": "discord"}']}},
    ('multi', 'stop: foreign press, then owner'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': "press stop {'user': '8'}",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Only the person who ran the command can confirm it.  []']},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'stop: foreign press, then owner'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': "press stop {'user': '8'}",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Only the person who ran the command can confirm it.  []']},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'stop: stranger press'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': "press stop {'user': '9'}"}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'stop: stranger press'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': "press stop {'user': '9'}"}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'stop: expired'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'age'},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'stop: expired'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'age'},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'stop: cancel, then press'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'press cancel',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Cancelled — nothing was changed.  []']},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'stop: cancel, then press'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'press cancel',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Cancelled — nothing was changed.  []']},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'stop: pressed as updateall'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'press updateall {} stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'stop: pressed as updateall'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'press updateall {} stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('multi', 'stop: two offers, the first pressed'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': "cmd /stop container:'db'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `db`? It stays down until something starts it '
                        'again.  [Stop db→ds:stop:<tok2> | Cancel→ds:cancel:<tok2>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10'}},
               {'do': 'raw_press ds:stop:<tok1>',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}',
                         '{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"db"}, "source": "discord"}']}},
    ('one', 'stop: two offers, the first pressed'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': "cmd /stop container:'db'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `db`? It stays down until something starts it '
                        'again.  [Stop db→ds:stop:<tok2> | Cancel→ds:cancel:<tok2>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10'}},
               {'do': 'raw_press ds:stop:<tok1>',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> 🟥 `web` stopped.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5, inspect --format '
                                   't=10, inspect --format t=5, stop --time t=90, inspect '
                                   '--format t=5'}}],
     'state': {'pending': ['web@local'],
               'confirmations': ['stop'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}',
                         '{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"db"}, "source": "discord"}']}},
    ('multi', 'stop: lock taken before the press'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'lock'},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        "<edit> ⏳ Updates in progress — stop refused so it can't interfere "
                        'with a running update. Try again once they finish.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    ('one', 'stop: lock taken before the press'): {'steps': [{'do': "cmd /stop container:'web'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⚠ Stop `web`? It stays down until something starts it '
                        'again.  [Stop web→ds:stop:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}},
               {'do': 'lock'},
               {'do': 'press stop',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        "<edit> ⏳ Updates in progress — stop refused so it can't interfere "
                        'with a running update. Try again once they finish.  []'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/stop", "actor": "u7", "detail": {"container": '
                         '"web"}, "source": "discord"}']}},
    # ── updateall ───────────────────────────────────────────────────
    # the press runs `_do_updateall` (:2583).
    ('multi', 'updateall: press, press again'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update all**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', 'updateall: press, press again'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update all**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': None,
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('multi', 'updateall host:nas: press'): {'steps': [{'do': "cmd /updateall host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on @nas?\n'
                        '• `web` @nas — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update all**\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)  []'],
                'hosts': {'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@nas:web']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('one', 'updateall host:nas: press'): {'steps': [{'do': "cmd /updateall host:'nas'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update all**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"nas"}, "source": "discord"}']}},
    ('multi', 'updateall host:all: press'): {'steps': [{'do': "cmd /updateall host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **2** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 2 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update all**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', 'updateall host:all: press'): {'steps': [{'do': "cmd /updateall host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update all**\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', 'updateall: foreign press'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': "press updateall {'user': '8'}",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Only the person who ran the command can confirm it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', 'updateall: foreign press'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': "press updateall {'user': '8'}",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> Only the person who ran the command can confirm it.  []']}],
     'state': {'pending': ['web@local'],
               'confirmations': ['updateall'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('multi', 'updateall: expired'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'age'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', 'updateall: expired'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'age'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('multi', 'updateall: cancel'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'press cancel',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Cancelled — nothing was changed.  []']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', 'updateall: cancel'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'press cancel',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Cancelled — nothing was changed.  []']},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('multi', 'updateall host:all: pending emptied before the press'): {'steps': [{'do': "cmd /updateall host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **2** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 2 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'drop_pending'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> Nothing left to update — every container you approved has '
                        'already been updated.  []']}],
     'state': {'pending': None,
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', 'updateall host:all: pending emptied before the press'): {'steps': [{'do': "cmd /updateall host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'drop_pending'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> Nothing left to update — every container you approved has '
                        'already been updated.  []']}],
     'state': {'pending': None,
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', 'updateall: new entry before the press'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'add_pending db postgres:16 local'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update all**\n'
                        'ℹ 1 newly pending container(s) appeared after you were asked and '
                        'were NOT updated — run `/updateall` again for those.\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@nas', 'db@local'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', 'updateall: new entry before the press'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'add_pending db postgres:16 local'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<edit> **Update all**\n'
                        'ℹ 1 newly pending container(s) appeared after you were asked and '
                        'were NOT updated — run `/updateall` again for those.\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)  []'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['db@local'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('multi', 'updateall: lock taken before the press'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'lock'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **Update all**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queued': ['web'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', 'updateall: lock taken before the press'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'lock'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **Update all**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'queued': ['web'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('multi', 'updateall: lock taken before the press, then released'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'lock'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **Update all**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', 'updateall: lock taken before the press, then released'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'lock'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **Update all**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<telegram> ✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': [],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('multi', 'updateall host:all: lock taken before the press, then released'): {'steps': [{'do': "cmd /updateall host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **2** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '• `web` @nas — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 2 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'lock'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **Update all**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        '🕒 `web @nas` is queued — position 2. It runs when its turn '
                        'comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<telegram> ⏳ Updating `web @nas`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)',
                        '<telegram> ✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format',
                          'nas': 'inspect --format t=10 ×2, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web']}],
     'state': {'pending': [],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('one', 'updateall host:all: lock taken before the press, then released'): {'steps': [{'do': "cmd /updateall host:'all'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'lock'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **Update all**\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<telegram> ✅ All updates completed.'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': [],
               'audit': ['{"action": "/updateall", "actor": "u7", "detail": {"host": '
                         '"all"}, "source": "discord"}']}},
    ('multi', 'updateall: new pending, lock taken before the press, then released'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'add_pending db postgres:16 local'},
               {'do': 'lock'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **Update all**\n'
                        'ℹ 1 newly pending container(s) appeared after you were asked and '
                        'were NOT updated — run `/updateall` again for those.\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@nas', 'db@local'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    ('one', 'updateall: new pending, lock taken before the press, then released'): {'steps': [{'do': 'cmd /updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit> ⚠ Update **1** container(s) on this host?\n'
                        '• `web` — nginx:1.25\n'
                        '\n'
                        'Each one is pulled and recreated. Press the button to go ahead.  '
                        '[Update 1 container(s)→ds:updateall:<tok1> | '
                        'Cancel→ds:cancel:<tok1>]']},
               {'do': 'add_pending db postgres:16 local'},
               {'do': 'lock'},
               {'do': 'press updateall',
                'out': ['<ack deferred ephemeral>',
                        '<edit> ⏳ Working on it — this can take a few minutes.\n'
                        "If it runs past Discord's reply window I will post the result in "
                        'the channel instead.',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> **Update all**\n'
                        'ℹ 1 newly pending container(s) appeared after you were asked and '
                        'were NOT updated — run `/updateall` again for those.\n'
                        '🕒 `web` is queued — position 1. It runs when its turn comes.\n'
                        'The result is posted in <#chan-1>, not here.  []']},
               {'do': 'release',
                'out': ['<telegram> ⏳ Updating `web`...',
                        '[rest] <channel chan-1>   embeds=[{"title": "✅ Update OK: web", '
                        '"description": '
                        '"[**web**](https://github.com/example/web/releases/latest) '
                        '(`nginx:1.25`)\\nupdated (stand-in)"}]',
                        '<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10 ×2, inspect --format t=5, '
                                   'inspect --format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['db@local'],
               'audit': ['{"action": "/updateall", "actor": "u7", "source": "discord"}']}},
    # ── restore ─────────────────────────────────────────────────────
    # the press runs `_run_restore`.
    ('multi', 'restore: press, press again'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📥 **A backup arrived**\n'
                        '\n'
                        '`backup.json`\n'
                        'From: **matrix** · <ts> · vx\n'
                        'Contains: pinned, notes\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on **this** instance. Nothing has been changed '
                        'yet.  [♻️ Restore→ds:restore:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'events': ['fetch backup.json']},
               {'do': 'press restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> ✅ Restored: pinned, notes\n'
                        '\n'
                        'Some settings only take effect after a restart — `/restart` with '
                        'no container does it.  []']},
               {'do': 'press restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}'],
               'files': {'container_notes.json': '{"web": "hello"}',
                         'pinned_containers.json': '["db"]'}}},
    ('one', 'restore: press, press again'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📥 **A backup arrived**\n'
                        '\n'
                        '`backup.json`\n'
                        'From: **matrix** · <ts> · vx\n'
                        'Contains: pinned, notes\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on **this** instance. Nothing has been changed '
                        'yet.  [♻️ Restore→ds:restore:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'events': ['fetch backup.json']},
               {'do': 'press restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit itok1> ⏳ Confirmed — working on it…  []',
                        '<edit> ✅ Restored: pinned, notes\n'
                        '\n'
                        'Some settings only take effect after a restart — `/restart` with '
                        'no container does it.  []']},
               {'do': 'press restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}'],
               'files': {'container_notes.json': '{"web": "hello"}',
                         'pinned_containers.json': '["db"]'}}},
    ('multi', 'restore: foreign press'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📥 **A backup arrived**\n'
                        '\n'
                        '`backup.json`\n'
                        'From: **matrix** · <ts> · vx\n'
                        'Contains: pinned, notes\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on **this** instance. Nothing has been changed '
                        'yet.  [♻️ Restore→ds:restore:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'events': ['fetch backup.json']},
               {'do': "press restore {'user': '8'}",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Only the person who ran the command can confirm it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'confirmations': ['restore'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', 'restore: foreign press'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📥 **A backup arrived**\n'
                        '\n'
                        '`backup.json`\n'
                        'From: **matrix** · <ts> · vx\n'
                        'Contains: pinned, notes\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on **this** instance. Nothing has been changed '
                        'yet.  [♻️ Restore→ds:restore:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'events': ['fetch backup.json']},
               {'do': "press restore {'user': '8'}",
                'out': ['<ack deferred ephemeral>',
                        '<edit> Only the person who ran the command can confirm it.  []']}],
     'state': {'pending': ['web@local'],
               'confirmations': ['restore'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('multi', 'restore: expired'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📥 **A backup arrived**\n'
                        '\n'
                        '`backup.json`\n'
                        'From: **matrix** · <ts> · vx\n'
                        'Contains: pinned, notes\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on **this** instance. Nothing has been changed '
                        'yet.  [♻️ Restore→ds:restore:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'events': ['fetch backup.json']},
               {'do': 'age'},
               {'do': 'press restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', 'restore: expired'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📥 **A backup arrived**\n'
                        '\n'
                        '`backup.json`\n'
                        'From: **matrix** · <ts> · vx\n'
                        'Contains: pinned, notes\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on **this** instance. Nothing has been changed '
                        'yet.  [♻️ Restore→ds:restore:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'events': ['fetch backup.json']},
               {'do': 'age'},
               {'do': 'press restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('multi', 'restore: cancel'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📥 **A backup arrived**\n'
                        '\n'
                        '`backup.json`\n'
                        'From: **matrix** · <ts> · vx\n'
                        'Contains: pinned, notes\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on **this** instance. Nothing has been changed '
                        'yet.  [♻️ Restore→ds:restore:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'events': ['fetch backup.json']},
               {'do': 'press cancel',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Cancelled — nothing was changed.  []']},
               {'do': 'press restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    ('one', 'restore: cancel'): {'steps': [{'do': "cmd /restore filename:'backup.json' size:300 "
                      "url:'https://cdn.discordapp.invalid/attachments/backup.json'",
                'out': ['<ack deferred ephemeral>',
                        '<edit> 📥 **A backup arrived**\n'
                        '\n'
                        '`backup.json`\n'
                        'From: **matrix** · <ts> · vx\n'
                        'Contains: pinned, notes\n'
                        '\n'
                        'Restoring overwrites the settings, groups, pins, notes, links and '
                        'update windows on **this** instance. Nothing has been changed '
                        'yet.  [♻️ Restore→ds:restore:<tok1> | Cancel→ds:cancel:<tok1>]'],
                'events': ['fetch backup.json']},
               {'do': 'press cancel',
                'out': ['<ack deferred ephemeral>',
                        '<edit> Cancelled — nothing was changed.  []']},
               {'do': 'press restore',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restore", "actor": "u7", "detail": {"file": '
                         '"att1"}, "source": "discord"}']}},
    # ── press ───────────────────────────────────────────────────────
    # `_is_ours` (:910) drops anything not starting 'ds:'; a two-part id is
    # 'unknown button'.
    ('multi', 'press: unknown token'): {'steps': [{'do': 'raw_press ds:stop:0000000000000000',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'press: unknown token'): {'steps': [{'do': 'raw_press ds:stop:0000000000000000',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local']}},
    ('multi', 'press: unknown action'): {'steps': [{'do': 'raw_press ds:bogus:0000000000000000',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'press: unknown action'): {'steps': [{'do': 'raw_press ds:bogus:0000000000000000',
                'out': ['<ack deferred ephemeral>',
                        '<edit> That confirmation has expired or was already used — run '
                        'the command again if you still want it.  []']}],
     'state': {'pending': ['web@local']}},
    ('multi', 'press: two-part ds id'): {'steps': [{'do': 'raw_press ds:stop',
                'out': ['<ack deferred ephemeral>',
                        "<edit> I don't recognise that button.  []"]}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'press: two-part ds id'): {'steps': [{'do': 'raw_press ds:stop',
                'out': ['<ack deferred ephemeral>',
                        "<edit> I don't recognise that button.  []"]}],
     'state': {'pending': ['web@local']}},
    ('multi', "press: another bot's button"): {'steps': [{'do': 'raw_press other:stop:x'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', "press: another bot's button"): {'steps': [{'do': 'raw_press other:stop:x'}], 'state': {'pending': ['web@local']}},
    # ── restart self ────────────────────────────────────────────────
    ('multi', 'restart self'): {'steps': [{'do': 'cmd /restart',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/restart", "actor": "u7", "source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    ('one', 'restart self'): {'steps': [{'do': 'cmd /restart',
                'out': ['<ack deferred ephemeral>',
                        '<edit> 🔄 Restarting — back in a moment. (restart policy: '
                        '`unless-stopped`)  []'],
                'hosts': {'local': 'inspect --format t=5 ×2, inspect --format t=15'},
                'events': ['selfrestart.go_down']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/restart", "actor": "u7", "source": "discord"}'],
               'files': {'restart_request.json': '{"by": "discord", "ts": <epoch>}'}}},
    # ── autocomplete ────────────────────────────────────────────────
    # :836 authorised, then `_autocomplete` (:1282) on a worker: host →
    # registry names, container → `ps -a` on the named host or local
    # (nothing or `all` named → local, since A2 by name rather than registry
    # order), 3 s and a remembered dead host not asked since W7d.
    ('multi', "auto host ''"): {'steps': [{'do': "auto /status host:''*",
                'out': ['<autocomplete> local, nas, dead, refused, flaky']}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', "auto host ''"): {'steps': [{'do': "auto /status host:''*", 'out': ['<autocomplete> local']}],
     'state': {'pending': ['web@local']}},
    ('multi', "auto host 'na'"): {'steps': [{'do': "auto /status host:'na'*", 'out': ['<autocomplete> nas']}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', "auto host 'na'"): {'steps': [{'do': "auto /status host:'na'*", 'out': ['<autocomplete> ']}],
     'state': {'pending': ['web@local']}},
    ('multi', "auto container ''"): {'steps': [{'do': "auto /logs container:''*",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', "auto container ''"): {'steps': [{'do': "auto /logs container:''*",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local']}},
    ('multi', "auto container 'we'"): {'steps': [{'do': "auto /logs container:'we'*",
                'out': ['<autocomplete> web, webdav'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', "auto container 'we'"): {'steps': [{'do': "auto /logs container:'we'*",
                'out': ['<autocomplete> web, webdav'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local']}},
    ('multi', 'auto container host:nas'): {'steps': [{'do': "auto /logs container:''* host:'nas'",
                'out': ['<autocomplete> web, media'],
                'hosts': {'nas': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'auto container host:nas'): {'steps': [{'do': "auto /logs container:''* host:'nas'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local']}},
    ('multi', 'auto container host:all'): {'steps': [{'do': "auto /logs container:''* host:'all'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'auto container host:all'): {'steps': [{'do': "auto /logs container:''* host:'all'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local']}},
    ('multi', 'auto container host:zzz'): {'steps': [{'do': "auto /logs container:''* host:'zzz'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'auto container host:zzz'): {'steps': [{'do': "auto /logs container:''* host:'zzz'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local']}},
    ('multi', 'auto container host:dead'): {'steps': [{'do': "auto /logs container:''* host:'dead'",
                'out': ["<log> Discord autocomplete failed: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' timed out "
                        'after 3 seconds',
                        '<autocomplete> '],
                'hosts': {'dead': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'auto container host:dead'): {'steps': [{'do': "auto /logs container:''* host:'dead'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local']}},
    ('multi', 'auto container host:refused'): {'steps': [{'do': "auto /logs container:''* host:'refused'",
                'out': ['<autocomplete> '],
                'hosts': {'refused': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'auto container host:refused'): {'steps': [{'do': "auto /logs container:''* host:'refused'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local']}},
    ('multi', 'auto container host:dead twice'): {'steps': [{'do': "auto /logs container:''* host:'dead'",
                'out': ["<log> Discord autocomplete failed: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' timed out "
                        'after 3 seconds',
                        '<autocomplete> '],
                'hosts': {'dead': 'ps -a --format t=3'}},
               {'do': "auto /logs container:''* host:'dead'",
                'out': ["<log> Discord autocomplete failed: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' timed out "
                        'after 3 seconds',
                        '<autocomplete> '],
                'hosts': {'dead': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'auto container host:dead twice'): {'steps': [{'do': "auto /logs container:''* host:'dead'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}},
               {'do': "auto /logs container:''* host:'dead'",
                'out': ['<autocomplete> docksentry, web, webdav, db'],
                'hosts': {'local': 'ps -a --format t=3'}}],
     'state': {'pending': ['web@local']}},
    ('multi', 'auto nothing focused'): {'steps': [{'do': "auto /logs container:'we'"}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'auto nothing focused'): {'steps': [{'do': "auto /logs container:'we'"}], 'state': {'pending': ['web@local']}},
    ('multi', 'auto stranger'): {'steps': [{'do': "auto /status host:''* {'user': '9'}"}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'auto stranger'): {'steps': [{'do': "auto /status host:''* {'user': '9'}"}],
     'state': {'pending': ['web@local']}},
}
# ── end of the generated table ──

if __name__ == "__main__":
    sys.exit(main())
