#!/usr/bin/env python3
"""Web action matrix: what every state-changing request answers today, through the real `do_POST` and `do_GET`.

The core split moves the Web UI's update clicks onto the shared queue
(Q14, Q14b), its check walk onto the core walk (W6w, W7w), its cleanup
onto every host (WB3) and gives the V2 Check button a POST (WB1). Each
older procedure drives one endpoint with its own fakes; this one drives
every state-changing path the handler has against a real `TelegramBot`,
a real `UpdateEngine` and the Telegram matrices' hosts, and compares
what came back with a table taken from main before anything moved.

Three set-ups, the same rows in each:

  * `multi`    — the Telegram matrices' five-host registry: `local` and
                 `nas` answer, `dead` raises TimeoutExpired on every
                 call, `refused` returns rc=255 with ssh's "Connection
                 refused", `flaky` lists its containers but its checker's
                 `check_all` raises. Telegram configured;
  * `one`      — a registry holding only the local host (main.py always
                 hands the Web UI a registry). Telegram configured;
  * `headless` — the same one-host registry with no BOT_TOKEN/CHAT_ID,
                 so `bot.enabled` is False: a Web-UI-only install.

Every request enters where the HTTP server would hand it over: a
handler instance per request with real headers (`http.client.
HTTPMessage`), a body on `rfile` and a `wfile` the response is written
into, then `do_POST` / `do_GET`. So the password check (Basic auth with
WEB_PASSWORD, or a session cookie from a real `/login`), the CSRF check
on Origin/Referer, the audit seam and the backup seam all run for real,
and the response is read back off the bytes the handler wrote (status,
headers apart from Server/Date/Content-Length, body). A row is a short
scenario, because a toggle or a login only means something next to the
request before or after it.

Per step the table holds the response, what was sent in order (Telegram
`sendMessage` texts, webhook payloads, the process's own log lines,
which for several endpoints are the only answer there is), which host
backends were called and with what (argv verb, flags, timeout), and the
environment events. Threads a request starts run for real and are joined
before the next step. After the last step: the pending file, the update
lock, the check lock, a queued self-update, the live session count,
the audit records (without their timestamp), the language the Telegram
bot speaks if it is not English, and the JSON state files in the data
dir (backups by name only).

What is real: everything the Telegram matrices list as real (it is
their fixture, imported: Config from a scrubbed environment, the
stores, the registry, the checkers apart from their registry half),
plus `create_handler` and everything under it, a `TelegramBot` and an
`UpdateEngine` wired as main.py wires them (AuditLog, Notifier,
Broadcast, selfupdate.Context), the server's `SessionStore` and
`AuditLog` as `WebUI.start` builds them, the Notifier and its webhook
plugin down to the HTTP request, and `TelegramBot.send_message` down to
`api_call`.

Stand-ins, on top of the Telegram matrices' (their header lists them):

  * the Telegram transport: `api_call` records instead of sending (so
    the channel switch, `enabled` and the bot label still decide what
    reaches it);
  * the webhook endpoint: `urlopen` answers three fixed URLs under
    https://hook.invalid/ (200, 404, 503) and stays the trap for
    everything else; WEBHOOK_URL points at the 200 one;
  * `restart_discord`, the callback main.py hands the handler, records
    and answers "running";
  * the headless self-update check's bare `docker pull <own image>` and
    `docker inspect --format {{.Id}}||{{.Created}} <own image>` (it calls
    subprocess itself, not the backend): the pull succeeds and the id is
    the running one, so the answer is "already up to date";
  * the socket: `rfile`/`wfile` are byte buffers.

`subprocess.run`, `Popen`, `urlopen` and the socket connects stay the
Telegram matrices' trap: any of those past the stand-ins turns its row
red (the list is in test_telegram_dispatch_matrix's docstring). The
first checks drive that trap on purpose through this entry path.

`python3 scripts/test_web_action_matrix.py --snapshot [--app DIR]`
prints a fresh expectation table from DIR's app code (default this
tree's). The committed table was generated from main (8a74ba7) and then
read row by row against the branch each one lands in.
"""

import base64
import contextlib
import http.client
import io
import json
import os
import pprint
import re
import shutil
import sys
import tempfile
import textwrap
import threading
import types
import urllib.error
import urllib.parse
import urllib.request

import test_telegram_dispatch_matrix as m

PASSWORD = "secret"
HOST = "ds.local:8080"
ORIGIN = "http://" + HOST
HOOK = "https://hook.invalid/"
ENV = {"WEB_UI": "true", "WEB_PASSWORD": PASSWORD, "WEBHOOK_URL": HOOK + "ok"}
SETUPS = ("multi", "one", "headless")

LOG = []          # (kind, payload) in the order things happened
_LOG_LOCK = threading.Lock()


# ── the webhook endpoint ───────────────────────────────────────────────
class _Resp:
    status = 200

    def read(self, *a):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_hook(trap):
    def urlopen(req, *a, **kw):
        url = getattr(req, "full_url", req)
        if isinstance(url, str) and url.startswith(HOOK):
            what = url[len(HOOK):]
            try:
                body = json.loads((getattr(req, "data", b"") or b"{}"))
            except ValueError:
                body = {"raw": str(getattr(req, "data", ""))}
            with _LOG_LOCK:
                LOG.append(("webhook", (what, body)))
            if what == "404":
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            if what == "503":
                raise urllib.error.HTTPError(url, 503, "Unavailable", {},
                                             None)
            return _Resp()
        return trap(req, *a, **kw)
    return urlopen


class _Lines(io.TextIOBase):
    """stdout as log lines: every complete line lands in LOG."""

    def __init__(self):
        self.buf = ""

    def write(self, s):
        with _LOG_LOCK:
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                if line.strip():
                    LOG.append(("log", line))
        return len(s)

    def flush(self):
        pass


def _make_pull(trap):
    """The headless self-update check pulls its own image with a bare
    `docker pull` (telegram_bot `_check_selfupdate_auto_locked`), outside
    the backend. These two argv answer "already up to date": the pull
    succeeds and the image id is the running container's. Anything else
    stays the trap."""
    import subprocess

    def run(args, *a, **kw):
        argv = [str(x) for x in args] if isinstance(args, (list, tuple)) \
            else args
        if isinstance(argv, list) and argv[:2] == ["docker", "pull"]:
            m.EVENTS.append(f"docker pull {argv[2]}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if isinstance(argv, list) and argv[:4] == [
                "docker", "inspect", "--format", "{{.Id}}||{{.Created}}"]:
            m.EVENTS.append(f"docker inspect {argv[4]}")
            own = m.LOCAL[0].containers.get("docksentry") or {}
            return subprocess.CompletedProcess(
                argv, 0, f"{own.get('Image', '')}||2026-01-01T00:00:00Z\n",
                "")
        return trap(args, *a, **kw)
    return run


_PATCHED = False


def _patch():
    global _PATCHED
    m._patch_environment()
    if _PATCHED:
        return
    _PATCHED = True
    import subprocess
    urllib.request.urlopen = _make_hook(urllib.request.urlopen)
    subprocess.run = _make_pull(subprocess.run)


# ── one row ────────────────────────────────────────────────────────────
class Row:
    def __init__(self, setup):
        _patch()
        from audit import AuditLog
        from broadcast import Broadcast
        from notifier import Notifier
        from telegram_bot import TelegramBot
        from update_engine import UpdateEngine
        from webauth import SessionStore
        import selfupdate
        import web_ui

        self.setup = setup
        env = dict(ENV)
        if setup == "headless":
            env.update({"BOT_TOKEN": "", "CHAT_ID": ""})
        world = "multi" if setup == "multi" else "one"
        (cfg, store, managed, registry, self.backends,
         self.data) = m.build_world(world, env)
        self.cfg, self.store = cfg, store
        # As main.py wires them.
        engine = UpdateEngine(cfg, store, hosts=registry)
        bot = TelegramBot(cfg, store, engine, hosts=registry)
        bot.backend = self.backends["local"]
        bot.start_time = 0
        bot.api_call = self._api_call
        bot.audit = AuditLog(cfg)
        notifier = Notifier(cfg)
        bot.notifier = notifier
        seam = Broadcast(telegram=bot, notifier=notifier)
        bot.broadcast = seam
        engine.broadcast = seam
        bot.selfupdate_ctx = selfupdate.Context(engine, cfg, seam.tell)
        self.bot, self.engine = bot, engine
        checker = registry.local.checker
        self.handler = web_ui.create_handler(
            cfg, checker, bot, store, cfg.web_password or None,
            registry.local.backend, registry,
            restart_discord=self._restart_discord)
        # As WebUI.start builds the server's side.
        self.server = types.SimpleNamespace(
            debug=False, audit=AuditLog(cfg), token_seen={},
            sessions=SessionStore(
                idle_seconds=int(cfg.web_session_hours) * 3600,
                max_seconds=int(cfg.web_session_max_days) * 86400))
        m._seed_pending(cfg, world)
        self.english = bot.t("update_result")
        self.cookie = ""
        self.tokens = {}            # session token → "<sessionN>"

    # ── transport ──
    @staticmethod
    def _api_call(method, data=None, *a, **k):
        data = data or {}
        if method == "sendMessage":
            kb = data.get("reply_markup")
            if isinstance(kb, str):
                kb = json.loads(kb)
            n = sum(len(r) for r in (kb or {}).get("inline_keyboard", []))
            with _LOG_LOCK:
                LOG.append(("telegram", (data.get("text", ""), n)))
        else:
            with _LOG_LOCK:
                LOG.append(("telegram", (f"<api {method}>", 0)))
        return {"ok": True, "result": {"message_id": 1}}

    @staticmethod
    def _restart_discord():
        with _LOG_LOCK:
            LOG.append(("restart_discord", None))
        return True, "", ""

    # ── requests ──
    def request(self, method, path, body=b"", opts=None):
        opts = dict(opts or {})
        hdrs = {"Host": HOST, "Accept": "text/html,application/xhtml+xml"}
        origin = opts.get("origin", ORIGIN)
        if origin is not None:
            hdrs["Origin"] = origin
        referer = opts.get("referer", "/")
        if referer is not None:
            hdrs["Referer"] = ORIGIN + referer
        auth = opts.get("auth", "basic")
        if auth == "basic":
            auth = ("admin", PASSWORD)
        elif auth == "wrong":
            auth = ("admin", "nope")
        if isinstance(auth, tuple):
            hdrs["Authorization"] = "Basic " + base64.b64encode(
                f"{auth[0]}:{auth[1]}".encode()).decode()
        elif auth == "cookie":
            hdrs["Cookie"] = f"ds_session={self.cookie}"
        if opts.get("fetch"):
            hdrs["Sec-Fetch-Mode"] = "cors"
        if method == "POST":
            hdrs["Content-Type"] = opts.get(
                "ctype", "application/x-www-form-urlencoded")
            hdrs["Content-Length"] = str(opts.get("length", len(body)))
        msg = http.client.HTTPMessage()
        for k, v in hdrs.items():
            msg[k] = v
        h = self.handler.__new__(self.handler)
        h.headers = msg
        h.path = path
        h.command = method
        h.request_version = "HTTP/1.1"
        h.requestline = f"{method} {path} HTTP/1.1"
        h.client_address = ("192.0.2.9", 50000)
        h.server = self.server
        h.close_connection = True
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        getattr(h, "do_" + method)()
        return h.wfile.getvalue()

    def do(self, step):
        kind, args = step[0], step[1:]
        if kind == "post":
            path, form, opts = args
            body = form if isinstance(form, bytes) else \
                urllib.parse.urlencode(form).encode()
            return self.request("POST", path, body, opts)
        if kind == "get":
            return self.request("GET", args[0], b"", args[1])
        if kind == "lock":
            self.engine._update_lock.acquire()
        elif kind == "unlock":
            self.engine._update_lock.release()
        elif kind == "check_lock":
            import web_ui
            web_ui._CHECK_LOCK.acquire()
        elif kind == "check_unlock":
            import web_ui
            web_ui._CHECK_LOCK.release()
        elif kind == "config":
            setattr(self.cfg, args[0], args[1])
        elif kind == "add_pending":
            from container_store import atomic_write_json
            atomic_write_json(self.cfg.pending_file, self._pending_raw() + [
                {"name": args[0], "image": args[1], "host": args[2],
                 "old_version": "1.0.0", "new_version": "1.1.0"}])
        elif kind == "major":
            self.bot._store_for(args[0]).add_pending_major(args[1], {
                "image": "nginx:1.25", "old_version": "1.25.0",
                "new_version": "2.0.0", "compose": {}})
        elif kind == "store":
            getattr(self.bot._store_for(args[0]), args[1])(*args[2:])
        elif kind == "corrupt_pins":
            with open(self.cfg.pinned_file, "w", encoding="utf-8") as f:
                f.write('["db"')
        else:
            raise ValueError(f"unknown step {kind!r}")
        return None

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
        s = re.sub(r"scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+",
                   "<scrypt>", s)
        s = re.sub(r"\d{4}-\d{2}-\d{2}[T _]\d{2}[:\-]?\d{2}([:\-]?\d{2})?"
                   r"(\.\d+)?", "<ts>", s)
        s = re.sub(r"\b\d{8}-\d{6}\b", "<stamp>", s)
        s = re.sub(r"\buntil \d{2}:\d{2}\b", "until <hh:mm>", s)
        s = re.sub(r"\b1\d{9}(\.\d+)?\b", "<epoch>", s)
        return m._short(s)

    def response(self, raw):
        if raw is None:
            return None
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ", 2)[1] if lines and lines[0] else "?"
        parts = [status]
        ctype = ""
        for line in lines[1:]:
            k, _, v = line.partition(": ")
            if k in ("Server", "Date", "Content-Length"):
                continue
            if k == "Set-Cookie":
                mt = re.match(r"ds_session=([^;]*)", v)
                if mt and mt.group(1):
                    tok = mt.group(1)
                    self.cookie = tok
                    if tok not in self.tokens:
                        self.tokens[tok] = f"<session{len(self.tokens) + 1}>"
            if k == "Content-Type":
                ctype = v
            parts.append(f"{k}: {self.norm(v)}")
        if body:
            if "json" in ctype:
                try:
                    shown = json.dumps(json.loads(body), sort_keys=True,
                                       ensure_ascii=False)
                except ValueError:
                    shown = body.decode("utf-8", "replace")
            else:
                shown = body.decode("utf-8", "replace")
            parts.append("body: " + self.norm(shown))
        return " | ".join(parts)

    def line(self, entry):
        kind, body = entry
        if kind == "telegram":
            text, kb = body
            return ("<telegram> " + self.norm(text)
                    + (f"  [{kb} buttons]" if kb else ""))
        if kind == "webhook":
            what, payload = body
            p = {k: v for k, v in payload.items() if k != "source"}
            return f"<webhook {what}> " + self.norm(
                json.dumps(p, sort_keys=True, ensure_ascii=False))
        if kind == "restart_discord":
            return "<restart_discord>"
        return "<log> " + self.norm(body)

    def state(self):
        import web_ui
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
                    audit.append(self.norm(json.dumps(e, sort_keys=True,
                                                      ensure_ascii=False)))
        files, backups = {}, 0
        skip = {os.path.abspath(pf), os.path.abspath(apath or "-")}
        for root, _dirs, names in os.walk(self.data):
            for n in sorted(names):
                p = os.path.join(root, n)
                rel = os.path.relpath(p, self.data)
                if "backup" in rel:
                    backups += 1
                    continue
                if os.path.abspath(p) in skip or not n.endswith(".json"):
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f)
                    if n == "maintenance.json":
                        data = _maintenance_window(data)
                    body = json.dumps(data, sort_keys=True,
                                      ensure_ascii=False)
                except (OSError, ValueError):
                    body = "<unreadable>"
                files[self.norm(rel)] = self.norm(body)
        said = self.bot.t("update_result")
        out = {"pending": pending,
               "locked": self.engine._update_lock.locked(),
               "check_locked": web_ui._CHECK_LOCK.locked(),
               "queued_selfupdate": self.engine._queued_selfupdate,
               "sessions": len(self.server.sessions._sessions),
               "telegram_says": None if said == self.english else said,
               "audit": audit, "files": files, "backups": backups}
        return {k: v for k, v in out.items()
                if v not in (None, False, [], {}, 0) or k == "pending"}


def _maintenance_window(data):
    """`until` as hours from now (one decimal): the timestamp itself moves
    with the clock, the length of the window is what a request set."""
    import datetime as _dt
    until = data.get("until") if isinstance(data, dict) else None
    if isinstance(until, str) and until != "forever":
        try:
            end = _dt.datetime.fromisoformat(until)
            now = _dt.datetime.now(end.tzinfo)
            data = dict(data, until="now+%.1fh"
                        % ((end - now).total_seconds() / 3600))
        except ValueError:
            pass
    return data


def _join():
    hung, i = [], 0
    while i < len(m.STARTED):                       # threads may start threads
        m.STARTED[i].join(20)
        if m.STARTED[i].is_alive():
            hung.append(m.STARTED[i].name)
        i += 1
    del m.STARTED[:]
    return hung


#: The dead-host memory's clock while a row runs.
HOSTS_CLOCK = [4242.0]


class _FrozenClock:
    """`hosts.time` while a row runs: the dead-host memory's cooldown
    does not tick, so "not asked again for 60s" is 60 whatever the
    machine's load (W7w, as T4 since W7d)."""

    @staticmethod
    def monotonic():
        return HOSTS_CLOCK[0]


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
    import web_ui
    hostslib.forget_unreachable()
    m.EVENTS.clear()
    del m.ESCAPES[:]
    del m.STARTED[:]
    del m.THREAD_ERRORS[:]
    del LOG[:]
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        row = Row(setup)
    m.EVENTS.clear()                # building leaves nothing worth pinning
    for b in row.backends.values():
        del b.calls[:]
    _join()
    del LOG[:]
    out_steps = []
    for step in steps:
        mark = {n: len(b.calls) for n, b in row.backends.items()}
        m.EVENTS.clear()
        del m.THREAD_ERRORS[:]
        del LOG[:]
        raised, raw = None, None
        lines = _Lines()
        with contextlib.redirect_stdout(lines), \
                contextlib.redirect_stderr(sink):
            try:
                raw = row.do(step)
            except Exception as e:                  # noqa: BLE001
                raised = type(e).__name__
            hung = _join()
            if lines.buf.strip():
                LOG.append(("log", lines.buf))
        rec = {"do": _label_step(step)}
        resp = row.response(raw)
        out = [row.line(e) for e in LOG]
        touched = {n: m._calls_summary(b.calls[mark[n]:])
                   for n, b in row.backends.items()
                   if len(b.calls) > mark[n]}
        events = [row.norm(e) for e in m.EVENTS] + list(m.THREAD_ERRORS) \
            + [f"thread did not finish: {h}" for h in hung]
        for k, v in (("http", resp), ("out", out), ("hosts", touched),
                     ("events", events), ("raised", raised)):
            if v:
                rec[k] = v
        out_steps.append(rec)
    state = row.state()
    if row.engine._update_lock.locked():
        row.engine._update_lock.release()
    if web_ui._CHECK_LOCK.locked():
        web_ui._CHECK_LOCK.release()
    escapes = list(m.ESCAPES)
    shutil.rmtree(row.data, ignore_errors=True)
    return {"steps": out_steps, "state": state}, escapes


def _label_step(step):
    kind, args = step[0], step[1:]
    if kind == "post":
        path, form, opts = args
        if isinstance(form, bytes):
            shown = f"<{len(form)} bytes>"
        else:
            shown = "&".join(f"{k}={v}" for k, v in form)
        extra = " ".join(f"{k}={v!r}" for k, v in sorted(opts.items()))
        return " ".join(x for x in ("POST", path, shown, extra) if x)
    if kind == "get":
        extra = " ".join(f"{k}={v!r}" for k, v in sorted(args[1].items()))
        return " ".join(x for x in ("GET", args[0], extra) if x)
    return " ".join([kind] + [x if isinstance(x, str) else repr(x)
                              for x in args])


# ── the rows ───────────────────────────────────────────────────────────
def P(path, *form, **opts):
    """A POST step; `form` is (key, value) pairs or one bytes body."""
    if len(form) == 1 and isinstance(form[0], bytes):
        return ("post", path, form[0], opts)
    return ("post", path, list(form), opts)


def G(path, **opts):
    return ("get", path, opts)


def _multipart(payload, filename="docksentry-backup.json"):
    b = "matrixboundary"
    return (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/json"
            f"\r\n\r\n").encode() + payload + f"\r\n--{b}--\r\n".encode()


_MP = "multipart/form-data; boundary=matrixboundary"


def _bundle(**extra):
    import backup
    return json.dumps(dict({
        "schema_version": backup.SCHEMA_VERSION, "instance": "matrix",
        "generated_at": "2026-11-19T10:00:00", "docksentry_version": "x",
        "pinned": ["db"], "notes": {"web": "hello"}}, **extra)).encode()


LOGIN = (("username", "admin"), ("password", PASSWORD))
NAMES = ("web", "nas/web", "zzz/web", "")


def _rows():
    rows = []

    def add(name, *steps):
        rows.append((name, list(steps)))

    # ── the gates ──
    add("gate: no credentials", P("/api/pin", ("name", "web"), auth=None))
    add("gate: no credentials, a page", P("/settings", auth=None))
    add("gate: no credentials, a fetch",
        P("/api/check_one", ("name", "web"), auth=None, fetch=True))
    add("gate: wrong password", P("/api/pin", ("name", "web"), auth="wrong"))
    add("gate: cross-origin", P("/api/pin", ("name", "web"),
                                origin="http://evil.example"))
    add("gate: Referer only", P("/api/pin", ("name", "web"), origin=None))
    add("gate: neither Origin nor Referer",
        P("/api/pin", ("name", "web"), origin=None, referer=None))
    add("gate: unknown POST path", P("/api/nosuch", ("name", "web")))
    add("POST /api/check", P("/api/check"))
    add("gate: GET /api/check, no credentials", G("/api/check", auth=None))
    # ── /login, /logout ──
    add("login: ok", P("/login", *LOGIN, ("next", "/settings"), auth=None))
    add("login: wrong password", P("/login", ("username", "admin"),
                                   ("password", "nope"), auth=None))
    add("login: cross-origin", P("/login", *LOGIN, auth=None,
                                 origin="http://evil.example"))
    add("login: next off-site", P("/login", *LOGIN,
                                  ("next", "https://evil.example/"),
                                  auth=None))
    add("login, cookie, logout, cookie",
        P("/login", *LOGIN, auth=None),
        P("/api/pin", ("name", "web"), auth="cookie"),
        G("/logout", auth="cookie"),
        P("/api/pin", ("name", "db"), auth="cookie"))
    add("logout without a session", G("/logout", auth=None))
    # ── /settings ──
    add("settings: empty form", P("/settings"))
    add("settings: cron with four fields", P(
        "/settings", ("cron_schedule", "0 4 * *"), ("language", "de")))
    add("settings: cron minute 61", P("/settings",
                                      ("cron_schedule", "61 * * * *")))
    add("settings: typical", P(
        "/settings", ("language", "de"), ("debug", "on"),
        ("cron_schedule", "0 4 * * *"), ("cleanup_grace_hours", "48"),
        ("disk_warn_percent", "150"), ("quiet_hours_start", "25:00"),
        ("quiet_hours_end", "7:30"), ("exclude_containers", " a, b ,,"),
        ("status_view", "list"), ("monitor_interval_seconds", "5"),
        ("weekly_report_hour", "x")))
    add("settings: password change, then the old and the new one",
        P("/login", *LOGIN, auth=None),
        P("/settings", ("web_password", "newpw")),
        P("/api/pin", ("name", "web")),
        P("/api/pin", ("name", "web"), auth=("admin", "newpw")),
        P("/api/pin", ("name", "db"), auth="cookie"))
    add("settings: username set, then two names",
        P("/settings", ("web_username", "alice")),
        P("/api/pin", ("name", "web")),
        P("/api/pin", ("name", "web"), auth=("alice", PASSWORD)))
    # ── /connections ──
    add("connections: bad webhook", P("/connections",
                                      ("webhook_url", "ftp://x")))
    add("connections: bad discord webhook", P(
        "/connections", ("discord_webhook", "https://example.org/hook")))
    add("connections: typical", P(
        "/connections", ("conn_page", "1"),
        ("telegram_allowed_users", " 7, 8 ,"), ("bot_label", "x" * 40),
        ("smtp_port", "99999"), ("smtp_tls", "bogus"),
        ("ntfy_topic", " t "), ("smtp_password", "pw")))
    add("connections: discord credentials changed", P(
        "/connections", ("discord_bot_token", "tok"),
        ("discord_app_id", "1")))
    add("connections: telegram switched off, then an update",
        P("/connections", ("conn_page", "1"),
          ("channel_telegram_enabled_shown", "1")),
        P("/api/update", ("name", "web")))
    # ── /api/update ──
    for n in ("web", "nas/web", "", "db", "zzz/web"):
        add(f"update: name={n}", P("/api/update", ("name", n)))
    add("update: dead/web pending",
        ("add_pending", "web", "nginx:1.25", "dead"),
        P("/api/update", ("name", "dead/web")))
    add("update: lock held", ("lock",), P("/api/update", ("name", "web")),
        ("unlock",))
    add("update: pin list unreadable", ("corrupt_pins",),
        P("/api/update", ("name", "web")))
    add("update: twice", P("/api/update", ("name", "web")),
        P("/api/update", ("name", "web")))
    # A self-update queued while the lock was held is handed over by the
    # next update that releases it — here the Web worker. The raw unlock
    # in between drains nothing, so only the worker's hand-over can run it.
    add("update: hands over a queued self-update", ("lock",),
        P("/api/selfupdate"), ("unlock",), P("/api/update", ("name", "web")))
    # ── /api/bulk ──
    for a in ("pin", "unpin", "autoupdate_on", "autoupdate_off"):
        add(f"bulk: {a} web,nas/web,zzz/web", P(
            "/api/bulk", ("action", a), ("names", "web,nas/web,zzz/web")))
    add("bulk: autoupdate_on docksentry,nas/docksentry", P(
        "/api/bulk", ("action", "autoupdate_on"),
        ("names", "docksentry,nas/docksentry")))
    add("bulk: pin, repeated names", P("/api/bulk", ("action", "pin"),
                                       ("names", "web"), ("names", "db")))
    add("bulk: pin, then unpin one",
        P("/api/bulk", ("action", "pin"), ("names", "web,db")),
        P("/api/bulk", ("action", "unpin"), ("names", "web")))
    add("bulk: autoupdate_on, then off for one",
        P("/api/bulk", ("action", "autoupdate_on"), ("names", "web,db")),
        P("/api/bulk", ("action", "autoupdate_off"), ("names", "db")))
    add("bulk: update web,nas/web", P("/api/bulk", ("action", "update"),
                                      ("names", "web,nas/web")))
    add("bulk: update web,db (db not pending)", P(
        "/api/bulk", ("action", "update"), ("names", "web,db")))
    add("bulk: update, lock held", ("lock",), P(
        "/api/bulk", ("action", "update"), ("names", "web")), ("unlock",))
    add("bulk: update, pin list unreadable", ("corrupt_pins",), P(
        "/api/bulk", ("action", "update"), ("names", "web")))
    add("bulk: bogus action", P("/api/bulk", ("action", "bogus"),
                                ("names", "web")))
    add("bulk: no names", P("/api/bulk", ("action", "pin")))
    add("bulk: no action", P("/api/bulk", ("names", "web")))
    # ── /api/check_one ──
    for n in ("web", "nas/web", "docksentry", "zzz/web", "", "dead/web",
              "refused/web", "flaky/web", "nosuch"):
        add(f"check_one: name={n}", P("/api/check_one", ("name", n)))
    add("check_one: update lock held", ("lock",),
        P("/api/check_one", ("name", "web")), ("unlock",))
    add("check_one: check lock held", ("check_lock",),
        P("/api/check_one", ("name", "web")), ("check_unlock",))
    add("check_one: pin list unreadable", ("corrupt_pins",),
        P("/api/check_one", ("name", "web")))
    add("check_one: debug on", ("config", "debug", True),
        P("/api/check_one", ("name", "web")))
    # ── the per-container toggles ──
    for ep in ("pin", "unpin", "autoupdate", "protect", "trust_running",
               "ask_major"):
        for n in NAMES:
            add(f"{ep}: name={n}", P(f"/api/{ep}", ("name", n)))
        if ep == "pin":
            add("pin: name=nosuch", P("/api/pin", ("name", "nosuch")))
        elif ep == "unpin":
            add("unpin: after pin", P("/api/pin", ("name", "web")),
                P("/api/unpin", ("name", "web")))
        else:
            add(f"{ep}: twice", P(f"/api/{ep}", ("name", "web")),
                P(f"/api/{ep}", ("name", "web")))
        if ep == "autoupdate":
            add("autoupdate: name=docksentry", P("/api/autoupdate",
                                                 ("name", "docksentry")))
            add("autoupdate: name=nas/docksentry", P(
                "/api/autoupdate", ("name", "nas/docksentry")))
    # ── /api/cooldown ──
    for n, s in (("web", "3600"), ("web", "abc"), ("nas/web", "60"),
                 ("zzz/web", "60"), ("web", "-5"), ("web", "99999999"),
                 ("", "60")):
        add(f"cooldown: name={n} seconds={s}", P(
            "/api/cooldown", ("name", n), ("seconds", s)))
    add("cooldown: 3600, then abc",
        P("/api/cooldown", ("name", "web"), ("seconds", "3600")),
        P("/api/cooldown", ("name", "web"), ("seconds", "abc")))
    add("cooldown: no seconds field", P("/api/cooldown", ("name", "web")))
    # ── /api/note ──
    add("note: name=web", P("/api/note", ("name", "web"), ("note", "a  b"),
                            referer="/container/web"))
    add("note: name=nas/web", P("/api/note", ("name", "nas/web"),
                                ("note", "x")))
    add("note: name=zzz/web", P("/api/note", ("name", "zzz/web"),
                                ("note", "x")))
    add("note: set, then cleared",
        P("/api/note", ("name", "web"), ("note", "x")),
        P("/api/note", ("name", "web"), ("note", "")))
    add("note: no Referer", P("/api/note", ("name", "web"), ("note", "x"),
                              referer=None))
    # ── /api/link ──
    for n, u in (("web", "https://example.org/x"), ("web", "notaurl"),
                 ("web", "javascript:alert(1)"), ("web", ""),
                 ("nas/web", "https://example.org/n"),
                 ("zzz/web", "https://example.org/z")):
        add(f"link: name={n} url={u}", P("/api/link", ("name", n),
                                         ("url", u),
                                         referer="/container/web"))
    # ── /api/lifecycle ──
    for a in ("start", "stop", "restart"):
        add(f"lifecycle: {a} web", P("/api/lifecycle", ("name", "web"),
                                     ("action", a),
                                     referer="/container/web"))
    for n, a in (("docksentry", "stop"), ("docksentry", "restart"),
                 ("nas/web", "restart"), ("dead/web", "stop"),
                 ("refused/web", "stop"), ("webdav", "start"),
                 ("nosuch", "stop"), ("zzz/web", "restart"),
                 ("web", "bogus"), ("web", ""), ("we", "stop")):
        add(f"lifecycle: {a} {n}", P("/api/lifecycle", ("name", n),
                                     ("action", a)))
    add("lifecycle: stop web, protected",
        ("store", "local", "toggle_protect_stop", "web"),
        P("/api/lifecycle", ("name", "web"), ("action", "stop")))
    add("lifecycle: stop web, lock held", ("lock",),
        P("/api/lifecycle", ("name", "web"), ("action", "stop")),
        ("unlock",))
    # ── /api/cleanup, /api/selfupdate ──
    add("cleanup", P("/api/cleanup"))
    add("cleanup: lock held", ("lock",), P("/api/cleanup"), ("unlock",))
    add("selfupdate", P("/api/selfupdate"))
    add("selfupdate: lock held", ("lock",), P("/api/selfupdate"),
        ("unlock",))
    # ── /api/major_confirm ──
    for h, n in (("local", "web"), ("nas", "web")):
        key = n if h == "local" else f"{h}/{n}"
        for a in ("confirm", "reject"):
            add(f"major: {a} {key}", ("major", h, n),
                P("/api/major_confirm", ("name", key), ("action", a)))
    add("major: confirm web, lock held", ("major", "local", "web"),
        ("lock",), P("/api/major_confirm", ("name", "web"),
                     ("action", "confirm")), ("unlock",))
    add("major: confirm web, nothing deferred", P(
        "/api/major_confirm", ("name", "web"), ("action", "confirm")))
    add("major: confirm web, pin list unreadable", ("major", "local", "web"),
        ("corrupt_pins",), P("/api/major_confirm", ("name", "web"),
                             ("action", "confirm")))
    add("major: bogus action", ("major", "local", "web"), P(
        "/api/major_confirm", ("name", "web"), ("action", "bogus")))
    add("major: confirm zzz/web", P("/api/major_confirm",
                                    ("name", "zzz/web"),
                                    ("action", "confirm")))
    # ── /api/maintenance ──
    for form in ((("action", "forever"),), (("action", "hours"),
                                             ("hours", "4")),
                 (("action", "hours"), ("hours", "abc")),
                 (("action", "hours"), ("hours", "0")),
                 (("action", "hours"), ("hours", "9999")), ()):
        add("maintenance: " + ("&".join(f"{k}={v}" for k, v in form)
                               or "empty form"),
            P("/api/maintenance", *form, referer="/settings"))
    add("maintenance: forever, then off",
        P("/api/maintenance", ("action", "forever")),
        P("/api/maintenance", ("action", "off")))
    # ── /api/window ──
    add("window: save web", P("/api/window", ("name", "web"),
                              ("action", "save"), ("start", "02:00"),
                              ("end", "04:00"), ("weekdays", "1"),
                              ("weekdays", "3"), ("weekdays", "x")))
    add("window: save web, bad time", P("/api/window", ("name", "web"),
                                        ("start", "2:00"),
                                        ("end", "04:00")))
    add("window: save, then delete",
        P("/api/window", ("name", "web"), ("start", "02:00"),
          ("end", "04:00")),
        P("/api/window", ("name", "web"), ("action", "delete")))
    add("window: save nas/web", P("/api/window", ("name", "nas/web"),
                                  ("start", "02:00"), ("end", "04:00")))
    add("window: save zzz/web", P("/api/window", ("name", "zzz/web"),
                                  ("start", "02:00"), ("end", "04:00")))
    add("window: no name", P("/api/window", ("start", "02:00"),
                             ("end", "04:00")))
    # ── groups ──
    stack = (("name", "My Stack!"), ("containers", "web"),
             ("containers", "db"), ("wait_seconds", "10"),
             ("restart_dependents", "on"))
    add("group_save: new", P("/api/group_save", *stack))
    add("group_save: new, from /settings", P("/api/group_save", *stack,
                                             referer="/settings"))
    add("group_save: same name twice", P("/api/group_save", *stack),
        P("/api/group_save", *stack))
    add("group_save: edit", P("/api/group_save", *stack),
        P("/api/group_save", ("group_id", "my-stack"), ("name", "Renamed"),
          ("containers", "db")))
    add("group_save: no containers", P("/api/group_save",
                                       ("name", "Empty")))
    add("group_delete", P("/api/group_save", *stack),
        P("/api/group_delete", ("group_id", "my-stack")))
    add("group_delete: unknown", P("/api/group_delete", ("group_id", "x"),
                                   referer="/settings"))
    for d in ("up", "down", "sideways"):
        add(f"group_reorder: db {d}", P("/api/group_save", *stack),
            P("/api/group_reorder", ("group_id", "my-stack"),
              ("container", "db"), ("direction", d)))
    add("groups_import_batch", P("/api/group_save", *stack), P(
        "/api/groups_import_batch",
        ("stacks", json.dumps({"name": "my stack!", "containers": ["web"],
                               "restart_dependents": False})),
        ("stacks", json.dumps({"name": "media", "containers": ["plex", ""],
                               "wait_seconds": 5})),
        ("stacks", "{not json"),
        ("stacks", json.dumps({"name": "", "containers": ["x"]}))))
    add("group_reorder_batch", P("/api/group_save", *stack), P(
        "/api/group_reorder_batch", ("group_id", "my-stack"),
        ("containers", "db"), ("containers", "ghost")))
    add("group_reorder_batch: unknown", P(
        "/api/group_reorder_batch", ("group_id", "x"), ("containers", "db")))
    # ── /api/test_channel, /api/test_webhook ──
    for n in ("nosuch", "telegram", "ntfy", "webhook"):
        add(f"test_channel: {n}", P("/api/test_channel", ("name", n)))
    for code in ("404", "503"):
        add(f"test_channel: webhook answers {code}",
            ("config", "webhook_url", HOOK + code),
            P("/api/test_channel", ("name", "webhook")))
    for kind, url in (("bogus", HOOK + "ok"), ("webhook", ""),
                      ("webhook", HOOK + "ok"), ("discord", HOOK + "ok"),
                      ("webhook", HOOK + "404")):
        add(f"test_webhook: kind={kind} url={url}", P(
            "/api/test_webhook", ("kind", kind), ("url", url)))
    # ── /api/backup_import ──
    add("backup_import: a backup", P("/api/backup_import",
                                     _multipart(_bundle()), ctype=_MP))
    add("backup_import: plain JSON body", P(
        "/api/backup_import", _bundle(), ctype="application/json"))
    add("backup_import: not JSON", P("/api/backup_import",
                                     _multipart(b"this is not json"),
                                     ctype=_MP))
    add("backup_import: a JSON list", P("/api/backup_import",
                                        _multipart(b"[1, 2]"), ctype=_MP))
    add("backup_import: no schema_version", P(
        "/api/backup_import", _multipart(b'{"settings": {"debug": true}}'),
        ctype=_MP))
    add("backup_import: declared too large", P(
        "/api/backup_import", _multipart(_bundle()), ctype=_MP,
        length=3 * 1024 * 1024))
    add("backup_import: body over the cap", P(
        "/api/backup_import", b" " * (2 * 1024 * 1024 + 10),
        ctype="application/json"))
    # ── /api/wizard, /api/ui_mode, /api/env_adopt ──
    add("wizard: passwords differ", P(
        "/api/wizard", ("web_password", "a"), ("web_password_confirm", "b")))
    add("wizard: no password, all auto", P(
        "/api/wizard", ("no_password", "1"), ("language", "de"),
        ("cron_schedule", "0 5 * * *"), ("auto_mode", "all"),
        ("webhook_url", "ftp://x"),
        ("discord_webhook", "https://example.org/x")))
    add("wizard: password and channels", P(
        "/api/wizard", ("web_password", "pw"), ("web_password_confirm", "pw"),
        ("web_username", " bob "), ("language", "xx"),
        ("webhook_url", HOOK + "ok"), ("auto_mode", "picky")))
    for mode in ("simple", "bogus"):
        add(f"ui_mode: {mode}", P("/api/ui_mode", ("mode", mode),
                                  referer="/settings"))
    add("env_adopt: language after saving de",
        P("/settings", ("language", "de")),
        P("/api/env_adopt", ("key", "language"), referer="/settings"))
    add("env_adopt: nosuch", P("/api/env_adopt", ("key", "nosuch")))
    add("env_adopt: no key, no Referer", P("/api/env_adopt", referer=None))
    # ── the state-changing GETs ──
    add("GET /api/check", G("/api/check"))
    add("GET /api/check: update lock held", ("lock",), G("/api/check"),
        ("unlock",))
    add("GET /api/check: check lock held", ("check_lock",),
        G("/api/check"), ("check_unlock",))
    add("GET /api/check twice", G("/api/check"), G("/api/check"))
    add("GET /api/check: cross-origin", G("/api/check",
                                          origin="http://evil.example"))
    add("GET /api/wizard_skip", G("/api/wizard_skip"))
    add("GET /api/wizard_skip: no password",
        ("config", "web_password", ""), G("/api/wizard_skip", auth=None))
    return rows


def all_scenarios():
    seen, out = set(), []
    for name, steps in _rows():
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
#: Rows another slice leans on as they are today (not wrong, but named).
PINNED_FOR = {}


def _mark(table, rows, why, setups=SETUPS):
    for _s in setups:
        for _r in rows:
            table[(_s, _r)] = why


_mark(KNOWN_DEFECTS, ["POST /api/check"],
      "WB1: the V2 Check button posts here and gets a 404")
_mark(KNOWN_DEFECTS, ["cleanup", "cleanup: lock held"],
      "WB3: the page says 'Settings saved.', and on several hosts only "
      "the local one is pruned")
_mark(KNOWN_DEFECTS, ["update: lock held", "bulk: update, lock held"],
      "Q14: busy is thrown away (single: a banner, bulk: Telegram only)")
_mark(KNOWN_DEFECTS, ["update: name=db", "update: twice"],
      "Q14b: 'started' for a key that is not pending, then nothing")
_mark(KNOWN_DEFECTS, ["lifecycle: stop refused/web"],
      "no slice yet: the refused host answers 'not found', in the log only",
      ("multi",))
_mark(KNOWN_DEFECTS, ["check_one: name=zzz/web"],
      "no slice yet: an unknown host answers 'missing name'")
_mark(KNOWN_DEFECTS, ["check_one: name=nas/web", "check_one: name=dead/web",
                      "check_one: name=refused/web",
                      "check_one: name=flaky/web"],
      "no slice yet: an unknown host answers 'missing name'",
      ("one", "headless"))
_mark(KNOWN_DEFECTS, ["check_one: name=nosuch"],
      "no slice yet: a container that does not exist reads as up to date")
_mark(KNOWN_DEFECTS, ["settings: cron minute 61"],
      "no slice yet: the cron check parses fields but not their range, "
      "so a schedule that never fires is saved")
_mark(KNOWN_DEFECTS, ["login, cookie, logout, cookie"],
      "no slice yet: a signed-in session is audited as 'web', not as the "
      "user (`_audit_actor` reads Basic auth only)")
_mark(KNOWN_DEFECTS, ["lifecycle: stop docksentry",
                      "lifecycle: restart docksentry",
                      "lifecycle: stop web, protected",
                      "lifecycle: stop web, lock held"],
      "no slice yet: the refusal goes to the log, the browser is just "
      "redirected")
_mark(KNOWN_DEFECTS, ["cooldown: name=web seconds=abc",
                      "cooldown: 3600, then abc"],
      "no slice yet: a value that is not a number clears the cooldown "
      "(the chats answer cooldown_bad_value)")
_mark(KNOWN_DEFECTS, ["selfupdate", "selfupdate: lock held"],
      "no slice yet: the page says 'Settings saved.' whatever happened")

_mark(PINNED_FOR, ["GET /api/check: cross-origin"],
      "WB1: the POST it adds goes behind the CSRF check; this GET stays")
_mark(PINNED_FOR, ["update: name=zzz/web"],
      "Q14b: an unknown host is 'started', then a log line")
_mark(PINNED_FOR, ["update: name=nas/web"],
      "Q14b: an unknown host is 'started', then a log line",
      ("one", "headless"))
_mark(PINNED_FOR, ["update: name=web", "bulk: update web,nas/web"],
      "Q3/Q14: one claim of the update lock runs the whole batch")
_mark(PINNED_FOR, ["major: confirm web, lock held"],
      "Q16 (decided): confirming a major stays 'busy', it is not queued")
_mark(PINNED_FOR, ["selfupdate: lock held"],
      "T6: selfupdate.start queues itself behind a held lock")
# Headless queues nothing (the self-update check just skips), so there
# the row only shows that the update itself still runs.
_mark(PINNED_FOR, ["update: hands over a queued self-update"],
      "Q14: the Web worker releases the lock, then runs the queued "
      "self-update", ("multi", "one"))

#: Where each request lands, read against main at 8a74ba7
#: (app/web_ui.py unless named). Printed above its rows by --snapshot,
#: so a regenerated table keeps the review notes.
BRANCH_NOTES = {
    "gate": "do_POST :2257 — /login first, then `_check_auth` (:938, Basic "
            "or session; no password = open unless configured before), "
            "then `_check_csrf` (:1010, Origin or Referer host == Host), "
            "then the audit (:2214) and backup seams, then the path. GETs "
            "get `_check_auth` only. An unknown path is the 404 at :3537.",
    "POST /api/check": "no POST branch: the 404 at :3537 (the GET is :2172).",
    "login": ":2261 → `_do_login` (:1756): CSRF-checked, no auth; a "
             "failure sleeps 0.5 s and answers 401; `next` must be local.",
    "logout": ":1867 → `_do_logout` (:1809), before the auth gate.",
    "settings": ":2297. Cron validated first (`_validate_cron`), then about "
                "35 fields parsed and clamped inline, absent checkboxes "
                "read as off, `bot.t` follows `language`, a new password "
                "clears the sessions; save_persistent writes what differs "
                "from the environment.",
    "connections": ":2504. Webhooks validated first; `conn_page` marks a "
                   "real form submission (switches and checkboxes); a "
                   "changed Discord credential calls `restart_discord`.",
    "update": ":2699 pre-gate (empty → gone, pin list → pinlist, lock → "
              "busy), else a thread → `_run_web_update_batch` (:7150): "
              "per host from that host's pending slice, unknown host "
              "printed and skipped, one non-blocking claim, the engine "
              "batch per host, Telegram only when `bot.enabled`.",
    "bulk": ":3516, no pre-gate except the pin list; a thread → `_api_bulk` "
            "(:7444). update = `_run_web_update_batch`; the flags go "
            "straight to each host's store, unknown host printed.",
    "check_one": ":2726 → `_api_check_one` (:7317), synchronous JSON. "
                 "`_action_target` None → 'missing name'; the update lock "
                 "and `_CHECK_LOCK` → busy; our own name asks "
                 "`has_selfupdate_available`, anything else `check_all(only)`.",
    "pin": ":2746, `_action_target` (:1357) then the host store's `pin`; "
           "no existence check, no reply, redirect '/'.",
    "unpin": ":2756, like /api/pin.",
    "autoupdate": ":2764, `toggle_auto`, refused for our own container on "
                  "the local host only.",
    "protect": ":3478, `toggle_protect_stop`, back to the container page "
               "for a local target (`_back_to_container`, :1392).",
    "trust_running": ":3470, `toggle_trust_running`, redirect '/'.",
    "ask_major": ":3462, `toggle_ask_before_major`, redirect '/'.",
    "cooldown": ":3486, the raw string to the store's `set_cooldown` "
                "(container_store.py:409), which clamps and reads a "
                "non-number as 0.",
    "note": ":2831, `set_note`, back to the Referer path.",
    "link": ":2994, `set_link`'s boolean becomes ?link=saved|cleared|"
            "rejected on the Referer path; nothing for an unknown host.",
    "lifecycle": ":2784 → lifecycle.act with the target's backend, checker "
                 "and store; the outcome is printed (bot.t), the browser "
                 "goes back to the Referer path.",
    "cleanup": ":2825 → `_api_cleanup` (:7384): `bot.cleanup_guarded` "
               "(telegram_bot.py:2776) with the local checker; Telegram "
               "only when enabled, the plugins under cleanup_manual_prefix.",
    "selfupdate": ":2828 → `_api_selfupdate` (:7409): `_handle_selfupdate` "
                  "with Telegram, `check_selfupdate_auto` without.",
    "major": ":3495. confirm → a thread on `bot._confirm_major_update` "
             "(telegram_bot.py:2078) with the raw key; reject → the host "
             "store's `remove_pending_major`.",
    "maintenance": ":3026. off / forever / hours clamped to 0..720 "
                   "(default 1 when the field is missing).",
    "window": ":3441. save needs HH:MM twice; delete clears; no target "
              "means nothing happens.",
    "group_save": ":3291, the global store; a new name gets a slug, a "
                  "taken one -2.",
    "group_delete": ":3329.",
    "group_reorder": ":3339, up/down only.",
    "groups_import_batch": ":3351, same name (case-blind) merges, bad "
                           "JSON and empty entries skipped; JSON answer.",
    "group_reorder_batch": ":3402, unknown members dropped, missing ones "
                           "appended; JSON answer.",
    "test_channel": ":2842, the saved settings of one channel, isolated; "
                    "the verdict is read off what the channel printed.",
    "test_webhook": ":2941, the typed URL; ok unless something raises.",
    "backup_import": ":3148. Declared length over cap + 8 KB → 413, body "
                     "over cap → 413, not JSON / no schema_version → 400, "
                     "else backup.restore.",
    "wizard": ":3082. Password or a deliberate no_password, then language, "
              "cron, valid webhooks only, auto_mode=all saves every local "
              "container, ui_mode=simple, setup done.",
    "ui_mode": ":3051.",
    "env_adopt": ":3062 → config.adopt_env_value.",
    "GET /api/check": ":2172 → `_api_check` (:7270) on a thread: skipped "
                      "while updating or checking, else every host's "
                      "check_all, a raise printed per host, then "
                      "notify_updates. A remote host found dead a moment "
                      "ago is skipped with its remembered words (W7w). No "
                      "CSRF check on a GET.",
    "GET /api/wizard_skip": ":2175, only with a password set.",
}


def _family(name):
    if name.startswith(("GET ", "POST ")):
        return " ".join(name.split()[:2]).rstrip(":")
    return re.split(r"[:, ]", name)[0]


def _expected_for(setup, name):
    return EXPECTED.get((setup, name))


# ── checks ─────────────────────────────────────────────────────────────
def trap_checks():
    """The trap through this entry path: a call past the stand-ins made
    inside `do_POST` and swallowed there still lands in ESCAPES."""
    import subprocess
    import web_ui
    results = []
    real = web_ui._validate_webhook_url
    for kind, call in (
            ("subprocess.run", lambda: subprocess.run(["docker", "info"])),
            ("urlopen", lambda: urllib.request.urlopen(
                "https://api.telegram.org/x"))):
        def leaky(*a, _call=call, **k):
            try:
                _call()
            except Exception:                           # noqa: BLE001
                pass                                    # swallowed on purpose
            return real(*a, **k)
        web_ui._validate_webhook_url = leaky
        try:
            rec, esc = run_scenario("one", [P("/connections",
                                              ("webhook_url", ""))])
        finally:
            web_ui._validate_webhook_url = real
        results.append((f"the trap catches a swallowed {kind} inside "
                        f"do_POST", len(esc) == 1
                        and esc[0].startswith(kind)))
    return results


def check():
    failed = 0
    for label, ok in trap_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    steps_of = dict(all_scenarios())
    for setup, name in all_rows():
        rec, esc = run_scenario(setup, steps_of[name])
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
            for k in ("http", "out", "hosts", "events", "raised"):
                if got.get(k) != exp.get(k):
                    print(f"      →   {k}: expected {exp.get(k)!r}")
                    print(f"      →   {k}: got      {got.get(k)!r}")
    rows = set(all_rows())
    for key in sorted(k for k in EXPECTED if k not in rows):
        print(f"  FAIL  expectation without a row: {key!r}")
        failed += 1
    for table, what in ((KNOWN_DEFECTS, "known defect"),
                        (PINNED_FOR, "pinned row")):
        for key in sorted(k for k in table if k not in rows):
            print(f"  FAIL  {what} names no row: {key!r}")
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
    m._TMP_ROOT = tempfile.mkdtemp(prefix="web-matrix-")
    try:
        if "--snapshot" in sys.argv:
            return snapshot()
        return check()
    finally:
        shutil.rmtree(m._TMP_ROOT, ignore_errors=True)


# ── expected, generated by --snapshot from main (8a74ba7), reviewed ──
EXPECTED = {
    # ── gate ────────────────────────────────────────────────────────
    # do_POST :2257 — /login first, then `_check_auth` (:938, Basic or
    # session; no password = open unless configured before), then
    # `_check_csrf` (:1010, Origin or Referer host == Host), then the audit
    # (:2214) and backup seams, then the path. GETs get `_check_auth` only.
    # An unknown path is the 404 at :3537.
    ('multi', 'gate: no credentials'): {'steps': [{'do': 'POST /api/pin name=web auth=None',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'gate: no credentials'): {'steps': [{'do': 'POST /api/pin name=web auth=None',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'gate: no credentials'): {'steps': [{'do': 'POST /api/pin name=web auth=None',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'gate: no credentials, a page'): {'steps': [{'do': 'POST /settings auth=None',
                'http': '302 | Location: /login?next=/settings'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'gate: no credentials, a page'): {'steps': [{'do': 'POST /settings auth=None',
                'http': '302 | Location: /login?next=/settings'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'gate: no credentials, a page'): {'steps': [{'do': 'POST /settings auth=None',
                'http': '302 | Location: /login?next=/settings'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'gate: no credentials, a fetch'): {'steps': [{'do': 'POST /api/check_one name=web auth=None fetch=True',
                'http': '401 | Content-Type: text/html; charset=utf-8 | body: <h1>401 - '
                        'Login required</h1>'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'gate: no credentials, a fetch'): {'steps': [{'do': 'POST /api/check_one name=web auth=None fetch=True',
                'http': '401 | Content-Type: text/html; charset=utf-8 | body: <h1>401 - '
                        'Login required</h1>'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'gate: no credentials, a fetch'): {'steps': [{'do': 'POST /api/check_one name=web auth=None fetch=True',
                'http': '401 | Content-Type: text/html; charset=utf-8 | body: <h1>401 - '
                        'Login required</h1>'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'gate: wrong password'): {'steps': [{'do': "POST /api/pin name=web auth='wrong'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'gate: wrong password'): {'steps': [{'do': "POST /api/pin name=web auth='wrong'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'gate: wrong password'): {'steps': [{'do': "POST /api/pin name=web auth='wrong'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'gate: cross-origin'): {'steps': [{'do': "POST /api/pin name=web origin='http://evil.example'",
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'gate: cross-origin'): {'steps': [{'do': "POST /api/pin name=web origin='http://evil.example'",
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'gate: cross-origin'): {'steps': [{'do': "POST /api/pin name=web origin='http://evil.example'",
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'gate: Referer only'): {'steps': [{'do': 'POST /api/pin name=web origin=None', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('one', 'gate: Referer only'): {'steps': [{'do': 'POST /api/pin name=web origin=None', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'gate: Referer only'): {'steps': [{'do': 'POST /api/pin name=web origin=None', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'gate: neither Origin nor Referer'): {'steps': [{'do': 'POST /api/pin name=web origin=None referer=None',
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'gate: neither Origin nor Referer'): {'steps': [{'do': 'POST /api/pin name=web origin=None referer=None',
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'gate: neither Origin nor Referer'): {'steps': [{'do': 'POST /api/pin name=web origin=None referer=None',
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'gate: unknown POST path'): {'steps': [{'do': 'POST /api/nosuch name=web',
                'http': '404 | Content-Type: text/html; charset=utf-8 | body: '
                        '<h1>404</h1>'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/nosuch", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'gate: unknown POST path'): {'steps': [{'do': 'POST /api/nosuch name=web',
                'http': '404 | Content-Type: text/html; charset=utf-8 | body: '
                        '<h1>404</h1>'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/nosuch", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'gate: unknown POST path'): {'steps': [{'do': 'POST /api/nosuch name=web',
                'http': '404 | Content-Type: text/html; charset=utf-8 | body: '
                        '<h1>404</h1>'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/nosuch", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    # ── POST /api/check ─────────────────────────────────────────────
    # no POST branch: the 404 at :3537 (the GET is :2172).
    ('multi', 'POST /api/check'): {'steps': [{'do': 'POST /api/check',
                'http': '404 | Content-Type: text/html; charset=utf-8 | body: '
                        '<h1>404</h1>'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'POST /api/check'): {'steps': [{'do': 'POST /api/check',
                'http': '404 | Content-Type: text/html; charset=utf-8 | body: '
                        '<h1>404</h1>'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'POST /api/check'): {'steps': [{'do': 'POST /api/check',
                'http': '404 | Content-Type: text/html; charset=utf-8 | body: '
                        '<h1>404</h1>'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('multi', 'gate: GET /api/check, no credentials'): {'steps': [{'do': 'GET /api/check auth=None',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'gate: GET /api/check, no credentials'): {'steps': [{'do': 'GET /api/check auth=None',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'gate: GET /api/check, no credentials'): {'steps': [{'do': 'GET /api/check auth=None',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local']}},
    # ── login ───────────────────────────────────────────────────────
    # :2261 → `_do_login` (:1756): CSRF-checked, no auth; a failure sleeps
    # 0.5 s and answers 401; `next` must be local.
    ('multi', 'login: ok'): {'steps': [{'do': 'POST /login username=admin&password=secret&next=/settings auth=None',
                'http': '302 | Location: /settings | Set-Cookie: ds_session=<session1>; '
                        'Path=/; HttpOnly; SameSite=Lax; Max-Age=28800'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'sessions': 1,
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}']}},
    ('one', 'login: ok'): {'steps': [{'do': 'POST /login username=admin&password=secret&next=/settings auth=None',
                'http': '302 | Location: /settings | Set-Cookie: ds_session=<session1>; '
                        'Path=/; HttpOnly; SameSite=Lax; Max-Age=28800'}],
     'state': {'pending': ['web@local'],
               'sessions': 1,
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}']}},
    ('headless', 'login: ok'): {'steps': [{'do': 'POST /login username=admin&password=secret&next=/settings auth=None',
                'http': '302 | Location: /settings | Set-Cookie: ds_session=<session1>; '
                        'Path=/; HttpOnly; SameSite=Lax; Max-Age=28800'}],
     'state': {'pending': ['web@local'],
               'sessions': 1,
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}']}},
    ('multi', 'login: wrong password'): {'steps': [{'do': 'POST /login username=admin&password=nope auth=None',
                'http': '401 | Content-Type: text/html; charset=utf-8 | Cache-Control: '
                        'no-store | body: <!DOCTYPE html>\n'
                        '<html lang="en"><head>\n'
                        '<meta charset="utf-8">\n'
                        '<meta name="viewport" '
                        'content="width=device-width,initial-scale=1">\n'
                        '<title>Sign in · Docksentry</title>\n'
                        '<script>\n'
                        '// Same pre-paint theme resolution as every other page. Without '
                        'it the\n'
                        '// login page carried a bare data-theme="auto", which '
                        '…#bd0883037a'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "failed"}']}},
    ('one', 'login: wrong password'): {'steps': [{'do': 'POST /login username=admin&password=nope auth=None',
                'http': '401 | Content-Type: text/html; charset=utf-8 | Cache-Control: '
                        'no-store | body: <!DOCTYPE html>\n'
                        '<html lang="en"><head>\n'
                        '<meta charset="utf-8">\n'
                        '<meta name="viewport" '
                        'content="width=device-width,initial-scale=1">\n'
                        '<title>Sign in · Docksentry</title>\n'
                        '<script>\n'
                        '// Same pre-paint theme resolution as every other page. Without '
                        'it the\n'
                        '// login page carried a bare data-theme="auto", which '
                        '…#bd0883037a'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "failed"}']}},
    ('headless', 'login: wrong password'): {'steps': [{'do': 'POST /login username=admin&password=nope auth=None',
                'http': '401 | Content-Type: text/html; charset=utf-8 | Cache-Control: '
                        'no-store | body: <!DOCTYPE html>\n'
                        '<html lang="en"><head>\n'
                        '<meta charset="utf-8">\n'
                        '<meta name="viewport" '
                        'content="width=device-width,initial-scale=1">\n'
                        '<title>Sign in · Docksentry</title>\n'
                        '<script>\n'
                        '// Same pre-paint theme resolution as every other page. Without '
                        'it the\n'
                        '// login page carried a bare data-theme="auto", which '
                        '…#bd0883037a'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "failed"}']}},
    ('multi', 'login: cross-origin'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None '
                      "origin='http://evil.example'",
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'login: cross-origin'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None '
                      "origin='http://evil.example'",
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'login: cross-origin'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None '
                      "origin='http://evil.example'",
                'http': '403 | Content-Type: text/html; charset=utf-8 | body: <h1>403 - '
                        'CSRF check failed</h1>'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'login: next off-site'): {'steps': [{'do': 'POST /login '
                      'username=admin&password=secret&next=https://evil.example/ auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'sessions': 1,
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}']}},
    ('one', 'login: next off-site'): {'steps': [{'do': 'POST /login '
                      'username=admin&password=secret&next=https://evil.example/ auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'}],
     'state': {'pending': ['web@local'],
               'sessions': 1,
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}']}},
    ('headless', 'login: next off-site'): {'steps': [{'do': 'POST /login '
                      'username=admin&password=secret&next=https://evil.example/ auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'}],
     'state': {'pending': ['web@local'],
               'sessions': 1,
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}']}},
    ('multi', 'login, cookie, logout, cookie'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'},
               {'do': "POST /api/pin name=web auth='cookie'", 'http': '303 | Location: /'},
               {'do': "GET /logout auth='cookie'",
                'http': '302 | Location: /login | Set-Cookie: ds_session=; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=0'},
               {'do': "POST /api/pin name=db auth='cookie'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}',
                         '{"action": "/api/pin", "actor": "web", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('one', 'login, cookie, logout, cookie'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'},
               {'do': "POST /api/pin name=web auth='cookie'", 'http': '303 | Location: /'},
               {'do': "GET /logout auth='cookie'",
                'http': '302 | Location: /login | Set-Cookie: ds_session=; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=0'},
               {'do': "POST /api/pin name=db auth='cookie'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}',
                         '{"action": "/api/pin", "actor": "web", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'login, cookie, logout, cookie'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'},
               {'do': "POST /api/pin name=web auth='cookie'", 'http': '303 | Location: /'},
               {'do': "GET /logout auth='cookie'",
                'http': '302 | Location: /login | Set-Cookie: ds_session=; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=0'},
               {'do': "POST /api/pin name=db auth='cookie'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}',
                         '{"action": "/api/pin", "actor": "web", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    # ── logout ──────────────────────────────────────────────────────
    # :1867 → `_do_logout` (:1809), before the auth gate.
    ('multi', 'logout without a session'): {'steps': [{'do': 'GET /logout auth=None',
                'http': '302 | Location: /login | Set-Cookie: ds_session=; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=0'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'logout without a session'): {'steps': [{'do': 'GET /logout auth=None',
                'http': '302 | Location: /login | Set-Cookie: ds_session=; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=0'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'logout without a session'): {'steps': [{'do': 'GET /logout auth=None',
                'http': '302 | Location: /login | Set-Cookie: ds_session=; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=0'}],
     'state': {'pending': ['web@local']}},
    # ── settings ────────────────────────────────────────────────────
    # :2297. Cron validated first (`_validate_cron`), then about 35 fields
    # parsed and clamped inline, absent checkboxes read as off, `bot.t`
    # follows `language`, a new password clears the sessions;
    # save_persistent writes what differs from the environment.
    ('multi', 'settings: empty form'): {'steps': [{'do': 'POST /settings', 'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/settings", "actor": "admin", "source": "web"}'],
               'files': {'settings.json': '{"monitor_enabled": false}'},
               'backups': 1}},
    ('one', 'settings: empty form'): {'steps': [{'do': 'POST /settings', 'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "admin", "source": "web"}'],
               'files': {'settings.json': '{"monitor_enabled": false}'},
               'backups': 1}},
    ('headless', 'settings: empty form'): {'steps': [{'do': 'POST /settings', 'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "admin", "source": "web"}'],
               'files': {'settings.json': '{"monitor_enabled": false}'},
               'backups': 1}},
    ('multi', 'settings: cron with four fields'): {'steps': [{'do': 'POST /settings cron_schedule=0 4 * *&language=de',
                'http': '303 | Location: '
                        '/settings?error=Cron%20schedule%3A%20need%205%20space-separated%20fields%2C%20got%204'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cron_schedule": "0 4 * *", "language": "de"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'settings: cron with four fields'): {'steps': [{'do': 'POST /settings cron_schedule=0 4 * *&language=de',
                'http': '303 | Location: '
                        '/settings?error=Cron%20schedule%3A%20need%205%20space-separated%20fields%2C%20got%204'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cron_schedule": "0 4 * *", "language": "de"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'settings: cron with four fields'): {'steps': [{'do': 'POST /settings cron_schedule=0 4 * *&language=de',
                'http': '303 | Location: '
                        '/settings?error=Cron%20schedule%3A%20need%205%20space-separated%20fields%2C%20got%204'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cron_schedule": "0 4 * *", "language": "de"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'settings: cron minute 61'): {'steps': [{'do': 'POST /settings cron_schedule=61 * * * *',
                'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cron_schedule": "61 * * * *"}, "source": "web"}'],
               'files': {'settings.json': '{"cron_schedule": "61 * * * *", '
                                          '"monitor_enabled": false}'},
               'backups': 1}},
    ('one', 'settings: cron minute 61'): {'steps': [{'do': 'POST /settings cron_schedule=61 * * * *',
                'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cron_schedule": "61 * * * *"}, "source": "web"}'],
               'files': {'settings.json': '{"cron_schedule": "61 * * * *", '
                                          '"monitor_enabled": false}'},
               'backups': 1}},
    ('headless', 'settings: cron minute 61'): {'steps': [{'do': 'POST /settings cron_schedule=61 * * * *',
                'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cron_schedule": "61 * * * *"}, "source": "web"}'],
               'files': {'settings.json': '{"cron_schedule": "61 * * * *", '
                                          '"monitor_enabled": false}'},
               'backups': 1}},
    ('multi', 'settings: typical'): {'steps': [{'do': 'POST /settings language=de&debug=on&cron_schedule=0 4 * * '
                      '*&cleanup_grace_hours=48&disk_warn_percent=150&quiet_hours_start=25:00&quiet_hours_end=7:30&exclude_containers= '
                      'a, b '
                      ',,&status_view=list&monitor_interval_seconds=5&weekly_report_hour=x',
                'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'telegram_says': '*Update-Ergebnis:*',
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cleanup_grace_hours": "48", "cron_schedule": "0 4 * * *", '
                         '"debug": "on", "disk_warn_percent": "150", "exclude_containers": '
                         '" a, b ,,", "language": "de", "monitor_interval_seconds": "5", '
                         '"quiet_hours_end": "7:30", "quiet_hours_start": "25:00", '
                         '"status_view": "list", "weekly_report_hour": "x"}, "source": '
                         '"web"}'],
               'files': {'settings.json': '{"cleanup_grace_hours": 48, "cron_schedule": "0 '
                                          '4 * * *", "debug": true, "disk_warn_percent": '
                                          '100, "exclude_containers": ["a", "b"], '
                                          '"language": "de", "monitor_enabled": false, '
                                          '"monitor_interval_seconds": 15, '
                                          '"quiet_hours_end": "07:30", "status_view": '
                                          '"list"}'},
               'backups': 1}},
    ('one', 'settings: typical'): {'steps': [{'do': 'POST /settings language=de&debug=on&cron_schedule=0 4 * * '
                      '*&cleanup_grace_hours=48&disk_warn_percent=150&quiet_hours_start=25:00&quiet_hours_end=7:30&exclude_containers= '
                      'a, b '
                      ',,&status_view=list&monitor_interval_seconds=5&weekly_report_hour=x',
                'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local'],
               'telegram_says': '*Update-Ergebnis:*',
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cleanup_grace_hours": "48", "cron_schedule": "0 4 * * *", '
                         '"debug": "on", "disk_warn_percent": "150", "exclude_containers": '
                         '" a, b ,,", "language": "de", "monitor_interval_seconds": "5", '
                         '"quiet_hours_end": "7:30", "quiet_hours_start": "25:00", '
                         '"status_view": "list", "weekly_report_hour": "x"}, "source": '
                         '"web"}'],
               'files': {'settings.json': '{"cleanup_grace_hours": 48, "cron_schedule": "0 '
                                          '4 * * *", "debug": true, "disk_warn_percent": '
                                          '100, "exclude_containers": ["a", "b"], '
                                          '"language": "de", "monitor_enabled": false, '
                                          '"monitor_interval_seconds": 15, '
                                          '"quiet_hours_end": "07:30", "status_view": '
                                          '"list"}'},
               'backups': 1}},
    ('headless', 'settings: typical'): {'steps': [{'do': 'POST /settings language=de&debug=on&cron_schedule=0 4 * * '
                      '*&cleanup_grace_hours=48&disk_warn_percent=150&quiet_hours_start=25:00&quiet_hours_end=7:30&exclude_containers= '
                      'a, b '
                      ',,&status_view=list&monitor_interval_seconds=5&weekly_report_hour=x',
                'http': '303 | Location: /settings?saved=1'}],
     'state': {'pending': ['web@local'],
               'telegram_says': '*Update-Ergebnis:*',
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"cleanup_grace_hours": "48", "cron_schedule": "0 4 * * *", '
                         '"debug": "on", "disk_warn_percent": "150", "exclude_containers": '
                         '" a, b ,,", "language": "de", "monitor_interval_seconds": "5", '
                         '"quiet_hours_end": "7:30", "quiet_hours_start": "25:00", '
                         '"status_view": "list", "weekly_report_hour": "x"}, "source": '
                         '"web"}'],
               'files': {'settings.json': '{"cleanup_grace_hours": 48, "cron_schedule": "0 '
                                          '4 * * *", "debug": true, "disk_warn_percent": '
                                          '100, "exclude_containers": ["a", "b"], '
                                          '"language": "de", "monitor_enabled": false, '
                                          '"monitor_interval_seconds": 15, '
                                          '"quiet_hours_end": "07:30", "status_view": '
                                          '"list"}'},
               'backups': 1}},
    ('multi', 'settings: password change, then the old and the new one'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'},
               {'do': 'POST /settings web_password=newpw',
                'http': '303 | Location: /settings?saved=1'},
               {'do': 'POST /api/pin name=web',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'},
               {'do': "POST /api/pin name=web auth=('admin', 'newpw')",
                'http': '303 | Location: /'},
               {'do': "POST /api/pin name=db auth='cookie'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}',
                         '{"action": "/settings", "actor": "admin", "detail": '
                         '{"web_password": "<redacted:5>"}, "source": "web"}',
                         '{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]',
                         'settings.json': '{"monitor_enabled": false, "web_password": '
                                          '"<scrypt>"}'},
               'backups': 1}},
    ('one', 'settings: password change, then the old and the new one'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'},
               {'do': 'POST /settings web_password=newpw',
                'http': '303 | Location: /settings?saved=1'},
               {'do': 'POST /api/pin name=web',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'},
               {'do': "POST /api/pin name=web auth=('admin', 'newpw')",
                'http': '303 | Location: /'},
               {'do': "POST /api/pin name=db auth='cookie'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}',
                         '{"action": "/settings", "actor": "admin", "detail": '
                         '{"web_password": "<redacted:5>"}, "source": "web"}',
                         '{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]',
                         'settings.json': '{"monitor_enabled": false, "web_password": '
                                          '"<scrypt>"}'},
               'backups': 1}},
    ('headless', 'settings: password change, then the old and the new one'): {'steps': [{'do': 'POST /login username=admin&password=secret auth=None',
                'http': '302 | Location: / | Set-Cookie: ds_session=<session1>; Path=/; '
                        'HttpOnly; SameSite=Lax; Max-Age=28800'},
               {'do': 'POST /settings web_password=newpw',
                'http': '303 | Location: /settings?saved=1'},
               {'do': 'POST /api/pin name=web',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'},
               {'do': "POST /api/pin name=web auth=('admin', 'newpw')",
                'http': '303 | Location: /'},
               {'do': "POST /api/pin name=db auth='cookie'",
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/login", "actor": "admin", "source": "web", '
                         '"target": "ok"}',
                         '{"action": "/settings", "actor": "admin", "detail": '
                         '{"web_password": "<redacted:5>"}, "source": "web"}',
                         '{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]',
                         'settings.json': '{"monitor_enabled": false, "web_password": '
                                          '"<scrypt>"}'},
               'backups': 1}},
    ('multi', 'settings: username set, then two names'): {'steps': [{'do': 'POST /settings web_username=alice',
                'http': '303 | Location: /settings?saved=1'},
               {'do': 'POST /api/pin name=web',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'},
               {'do': "POST /api/pin name=web auth=('alice', 'secret')",
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"web_username": "alice"}, "source": "web"}',
                         '{"action": "/api/pin", "actor": "alice", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]',
                         'settings.json': '{"monitor_enabled": false, "web_username": '
                                          '"alice"}'},
               'backups': 1}},
    ('one', 'settings: username set, then two names'): {'steps': [{'do': 'POST /settings web_username=alice',
                'http': '303 | Location: /settings?saved=1'},
               {'do': 'POST /api/pin name=web',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'},
               {'do': "POST /api/pin name=web auth=('alice', 'secret')",
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"web_username": "alice"}, "source": "web"}',
                         '{"action": "/api/pin", "actor": "alice", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]',
                         'settings.json': '{"monitor_enabled": false, "web_username": '
                                          '"alice"}'},
               'backups': 1}},
    ('headless', 'settings: username set, then two names'): {'steps': [{'do': 'POST /settings web_username=alice',
                'http': '303 | Location: /settings?saved=1'},
               {'do': 'POST /api/pin name=web',
                'http': '401 | WWW-Authenticate: Basic realm="Docksentry" | Content-Type: '
                        'text/html; charset=utf-8 | body: <h1>401 - Login required</h1>'},
               {'do': "POST /api/pin name=web auth=('alice', 'secret')",
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/settings", "actor": "admin", "detail": '
                         '{"web_username": "alice"}, "source": "web"}',
                         '{"action": "/api/pin", "actor": "alice", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]',
                         'settings.json': '{"monitor_enabled": false, "web_username": '
                                          '"alice"}'},
               'backups': 1}},
    # ── connections ─────────────────────────────────────────────────
    # :2504. Webhooks validated first; `conn_page` marks a real form
    # submission (switches and checkboxes); a changed Discord credential
    # calls `restart_discord`.
    ('multi', 'connections: bad webhook'): {'steps': [{'do': 'POST /connections webhook_url=ftp://x',
                'http': '303 | Location: '
                        '/connections?error=Webhook%20URL%3A%20Only%20http%3A//%20and%20https%3A//%20URLs%20are%20allowed%20%28got%20%27ftp%27%29'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"webhook_url": "<redacted:7>"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'connections: bad webhook'): {'steps': [{'do': 'POST /connections webhook_url=ftp://x',
                'http': '303 | Location: '
                        '/connections?error=Webhook%20URL%3A%20Only%20http%3A//%20and%20https%3A//%20URLs%20are%20allowed%20%28got%20%27ftp%27%29'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"webhook_url": "<redacted:7>"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'connections: bad webhook'): {'steps': [{'do': 'POST /connections webhook_url=ftp://x',
                'http': '303 | Location: '
                        '/connections?error=Webhook%20URL%3A%20Only%20http%3A//%20and%20https%3A//%20URLs%20are%20allowed%20%28got%20%27ftp%27%29'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"webhook_url": "<redacted:7>"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'connections: bad discord webhook'): {'steps': [{'do': 'POST /connections discord_webhook=https://example.org/hook',
                'http': '303 | Location: '
                        '/connections?error=Discord%20webhook%3A%20Discord%20webhook%20host%20must%20be%20discord.com%20%28got%20example.org%29'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"discord_webhook": "<redacted:24>"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'connections: bad discord webhook'): {'steps': [{'do': 'POST /connections discord_webhook=https://example.org/hook',
                'http': '303 | Location: '
                        '/connections?error=Discord%20webhook%3A%20Discord%20webhook%20host%20must%20be%20discord.com%20%28got%20example.org%29'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"discord_webhook": "<redacted:24>"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'connections: bad discord webhook'): {'steps': [{'do': 'POST /connections discord_webhook=https://example.org/hook',
                'http': '303 | Location: '
                        '/connections?error=Discord%20webhook%3A%20Discord%20webhook%20host%20must%20be%20discord.com%20%28got%20example.org%29'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"discord_webhook": "<redacted:24>"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'connections: typical'): {'steps': [{'do': 'POST /connections conn_page=1&telegram_allowed_users= 7, 8 '
                      ',&bot_label=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&smtp_port=99999&smtp_tls=bogus&ntfy_topic= '
                      't &smtp_password=pw',
                'http': '303 | Location: /connections?saved=1'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"bot_label": "<redacted:40>", "conn_page": "1", "ntfy_topic": " '
                         't ", "smtp_password": "<redacted:2>", "smtp_port": "99999", '
                         '"smtp_tls": "bogus", "telegram_allowed_users": " 7, 8 ,"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"bot_label": '
                                          '"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", '
                                          '"ntfy_topic": "t", "smtp_password": "pw", '
                                          '"smtp_port": 65535, "smtp_tls_verify": false, '
                                          '"telegram_allowed_users": ["7", "8"]}'},
               'backups': 1}},
    ('one', 'connections: typical'): {'steps': [{'do': 'POST /connections conn_page=1&telegram_allowed_users= 7, 8 '
                      ',&bot_label=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&smtp_port=99999&smtp_tls=bogus&ntfy_topic= '
                      't &smtp_password=pw',
                'http': '303 | Location: /connections?saved=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"bot_label": "<redacted:40>", "conn_page": "1", "ntfy_topic": " '
                         't ", "smtp_password": "<redacted:2>", "smtp_port": "99999", '
                         '"smtp_tls": "bogus", "telegram_allowed_users": " 7, 8 ,"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"bot_label": '
                                          '"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", '
                                          '"ntfy_topic": "t", "smtp_password": "pw", '
                                          '"smtp_port": 65535, "smtp_tls_verify": false, '
                                          '"telegram_allowed_users": ["7", "8"]}'},
               'backups': 1}},
    ('headless', 'connections: typical'): {'steps': [{'do': 'POST /connections conn_page=1&telegram_allowed_users= 7, 8 '
                      ',&bot_label=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&smtp_port=99999&smtp_tls=bogus&ntfy_topic= '
                      't &smtp_password=pw',
                'http': '303 | Location: /connections?saved=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"bot_label": "<redacted:40>", "conn_page": "1", "ntfy_topic": " '
                         't ", "smtp_password": "<redacted:2>", "smtp_port": "99999", '
                         '"smtp_tls": "bogus", "telegram_allowed_users": " 7, 8 ,"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"bot_label": '
                                          '"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", '
                                          '"ntfy_topic": "t", "smtp_password": "pw", '
                                          '"smtp_port": 65535, "smtp_tls_verify": false, '
                                          '"telegram_allowed_users": ["7", "8"]}'},
               'backups': 1}},
    ('multi', 'connections: discord credentials changed'): {'steps': [{'do': 'POST /connections discord_bot_token=tok&discord_app_id=1',
                'http': '303 | Location: /connections?saved=1&discord=ok',
                'out': ['<restart_discord>']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"discord_app_id": "1", "discord_bot_token": "<redacted:3>"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"discord_app_id": "1", "discord_bot_token": '
                                          '"tok"}'},
               'backups': 1}},
    ('one', 'connections: discord credentials changed'): {'steps': [{'do': 'POST /connections discord_bot_token=tok&discord_app_id=1',
                'http': '303 | Location: /connections?saved=1&discord=ok',
                'out': ['<restart_discord>']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"discord_app_id": "1", "discord_bot_token": "<redacted:3>"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"discord_app_id": "1", "discord_bot_token": '
                                          '"tok"}'},
               'backups': 1}},
    ('headless', 'connections: discord credentials changed'): {'steps': [{'do': 'POST /connections discord_bot_token=tok&discord_app_id=1',
                'http': '303 | Location: /connections?saved=1&discord=ok',
                'out': ['<restart_discord>']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"discord_app_id": "1", "discord_bot_token": "<redacted:3>"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"discord_app_id": "1", "discord_bot_token": '
                                          '"tok"}'},
               'backups': 1}},
    ('multi', 'connections: telegram switched off, then an update'): {'steps': [{'do': 'POST /connections conn_page=1&channel_telegram_enabled_shown=1',
                'http': '303 | Location: /connections?saved=1'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"channel_telegram_enabled_shown": "1", "conn_page": "1"}, '
                         '"source": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'settings.json': '{"channel_telegram_enabled": false, '
                                          '"smtp_tls_verify": false}'},
               'backups': 1}},
    ('one', 'connections: telegram switched off, then an update'): {'steps': [{'do': 'POST /connections conn_page=1&channel_telegram_enabled_shown=1',
                'http': '303 | Location: /connections?saved=1'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"channel_telegram_enabled_shown": "1", "conn_page": "1"}, '
                         '"source": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'settings.json': '{"channel_telegram_enabled": false, '
                                          '"smtp_tls_verify": false}'},
               'backups': 1}},
    ('headless', 'connections: telegram switched off, then an update'): {'steps': [{'do': 'POST /connections conn_page=1&channel_telegram_enabled_shown=1',
                'http': '303 | Location: /connections?saved=1'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/connections", "actor": "admin", "detail": '
                         '{"channel_telegram_enabled_shown": "1", "conn_page": "1"}, '
                         '"source": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'settings.json': '{"channel_telegram_enabled": false, '
                                          '"smtp_tls_verify": false}'},
               'backups': 1}},
    # ── update ──────────────────────────────────────────────────────
    # :2699 pre-gate (empty → gone, pin list → pinlist, lock → busy), else a
    # thread → `_run_web_update_batch` (:7150): per host from that host's
    # pending slice, unknown host printed and skipped, one non-blocking
    # claim, the engine batch per host, Telegram only when `bot.enabled`.
    ('multi', 'update: name=web'): {'steps': [{'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'update: name=web'): {'steps': [{'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'update: name=web'): {'steps': [{'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'update: name=nas/web'): {'steps': [{'do': 'POST /api/update name=nas/web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)'],
                'hosts': {'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@nas:web']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('one', 'update: name=nas/web'): {'steps': [{'do': 'POST /api/update name=nas/web',
                'http': '303 | Location: /?update=started',
                'out': ["<log> Web UI update: unknown host 'nas' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'update: name=nas/web'): {'steps': [{'do': 'POST /api/update name=nas/web',
                'http': '303 | Location: /?update=started',
                'out': ["<log> Web UI update: unknown host 'nas' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'update: name='): {'steps': [{'do': 'POST /api/update name=', 'http': '303 | Location: /?update=gone'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/update", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'update: name='): {'steps': [{'do': 'POST /api/update name=', 'http': '303 | Location: /?update=gone'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'update: name='): {'steps': [{'do': 'POST /api/update name=', 'http': '303 | Location: /?update=gone'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('multi', 'update: name=db'): {'steps': [{'do': 'POST /api/update name=db',
                'http': '303 | Location: /?update=started'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"db"}, "source": "web", "target": "db"}'],
               'backups': 1}},
    ('one', 'update: name=db'): {'steps': [{'do': 'POST /api/update name=db',
                'http': '303 | Location: /?update=started'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"db"}, "source": "web", "target": "db"}'],
               'backups': 1}},
    ('headless', 'update: name=db'): {'steps': [{'do': 'POST /api/update name=db',
                'http': '303 | Location: /?update=started'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"db"}, "source": "web", "target": "db"}'],
               'backups': 1}},
    ('multi', 'update: name=zzz/web'): {'steps': [{'do': 'POST /api/update name=zzz/web',
                'http': '303 | Location: /?update=started',
                'out': ["<log> Web UI update: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'update: name=zzz/web'): {'steps': [{'do': 'POST /api/update name=zzz/web',
                'http': '303 | Location: /?update=started',
                'out': ["<log> Web UI update: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'update: name=zzz/web'): {'steps': [{'do': 'POST /api/update name=zzz/web',
                'http': '303 | Location: /?update=started',
                'out': ["<log> Web UI update: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'update: dead/web pending'): {'steps': [{'do': 'add_pending web nginx:1.25 dead'},
               {'do': 'POST /api/update name=dead/web',
                'http': '303 | Location: /?update=started',
                'out': ["<log> Web UI update error: Command '['docker', '-H', "
                        "'ssh://dead', 'inspect', '--format', "
                        "'{{.HostConfig.NetworkMode}}', 'web']' timed out after 30 "
                        'seconds'],
                'hosts': {'dead': 'inspect --format t=10, inspect --format t=5 ×2, inspect '
                                  '--format'}}],
     'state': {'pending': ['web@local', 'web@nas', 'web@dead'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"dead/web"}, "source": "web", "target": "dead/web"}'],
               'backups': 1}},
    ('one', 'update: dead/web pending'): {'steps': [{'do': 'add_pending web nginx:1.25 dead'},
               {'do': 'POST /api/update name=dead/web',
                'http': '303 | Location: /?update=started',
                'out': ["<log> Web UI update: unknown host 'dead' — skipped"]}],
     'state': {'pending': ['web@local', 'web@dead'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"dead/web"}, "source": "web", "target": "dead/web"}'],
               'backups': 1}},
    ('headless', 'update: dead/web pending'): {'steps': [{'do': 'add_pending web nginx:1.25 dead'},
               {'do': 'POST /api/update name=dead/web',
                'http': '303 | Location: /?update=started',
                'out': ["<log> Web UI update: unknown host 'dead' — skipped"]}],
     'state': {'pending': ['web@local', 'web@dead'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"dead/web"}, "source": "web", "target": "dead/web"}'],
               'backups': 1}},
    ('multi', 'update: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/update name=web', 'http': '303 | Location: /?update=busy'},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'update: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/update name=web', 'http': '303 | Location: /?update=busy'},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'update: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/update name=web', 'http': '303 | Location: /?update=busy'},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'update: pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('one', 'update: pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('headless', 'update: pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('multi', 'update: twice'): {'steps': [{'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started'}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'update: twice'): {'steps': [{'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started'}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'update: twice'): {'steps': [{'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started'}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'update: hands over a queued self-update'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<telegram> ⏳ Container updates in progress — self-update queued, '
                        'it will start automatically once they finish.']},
               {'do': 'unlock'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<telegram> 🚀 Container updates finished — starting the queued '
                        'self-update...',
                        '<webhook ok> {"event": "message", "text": "🚀 Container updates '
                        'finished — starting the queued self-update..."}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web', 'selfupdate.run target=None']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'update: hands over a queued self-update'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<telegram> ⏳ Container updates in progress — self-update queued, '
                        'it will start automatically once they finish.']},
               {'do': 'unlock'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<telegram> 🚀 Container updates finished — starting the queued '
                        'self-update...',
                        '<webhook ok> {"event": "message", "text": "🚀 Container updates '
                        'finished — starting the queued self-update..."}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web', 'selfupdate.run target=None']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'update: hands over a queued self-update'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<log> Selfupdate check: skipped — another update flow is running.',
                        '<log> Web UI selfupdate (headless): no update applied — see lines '
                        'above for the reason']},
               {'do': 'unlock'},
               {'do': 'POST /api/update name=web',
                'http': '303 | Location: /?update=started',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": "web"}',
                         '{"action": "/api/update", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    # ── bulk ────────────────────────────────────────────────────────
    # :3516, no pre-gate except the pin list; a thread → `_api_bulk`
    # (:7444). update = `_run_web_update_batch`; the flags go straight to
    # each host's store, unknown host printed.
    ('multi', 'bulk: pin web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web,nas/web,zzz/web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["web", "nas/web"]'},
               'backups': 1}},
    ('one', 'bulk: pin web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped",
                        "<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web,nas/web,zzz/web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'bulk: pin web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped",
                        "<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web,nas/web,zzz/web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'bulk: unpin web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=unpin&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"unpin", "names": "web,nas/web,zzz/web"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'bulk: unpin web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=unpin&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped",
                        "<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"unpin", "names": "web,nas/web,zzz/web"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'bulk: unpin web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=unpin&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped",
                        "<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"unpin", "names": "web,nas/web,zzz/web"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'bulk: autoupdate_on web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'zzz' — skipped"],
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "web,nas/web,zzz/web"}, "source": '
                         '"web"}'],
               'files': {'autoupdate_containers.json': '["web", "nas/web"]'},
               'backups': 1}},
    ('one', 'bulk: autoupdate_on web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped",
                        "<log> Web UI bulk: unknown host 'zzz' — skipped"],
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "web,nas/web,zzz/web"}, "source": '
                         '"web"}'],
               'files': {'autoupdate_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'bulk: autoupdate_on web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped",
                        "<log> Web UI bulk: unknown host 'zzz' — skipped"],
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "web,nas/web,zzz/web"}, "source": '
                         '"web"}'],
               'files': {'autoupdate_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'bulk: autoupdate_off web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_off&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_off", "names": "web,nas/web,zzz/web"}, "source": '
                         '"web"}'],
               'files': {'autoupdate_containers.json': '[]'},
               'backups': 1}},
    ('one', 'bulk: autoupdate_off web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_off&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped",
                        "<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_off", "names": "web,nas/web,zzz/web"}, "source": '
                         '"web"}'],
               'files': {'autoupdate_containers.json': '[]'},
               'backups': 1}},
    ('headless', 'bulk: autoupdate_off web,nas/web,zzz/web'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_off&names=web,nas/web,zzz/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped",
                        "<log> Web UI bulk: unknown host 'zzz' — skipped"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_off", "names": "web,nas/web,zzz/web"}, "source": '
                         '"web"}'],
               'files': {'autoupdate_containers.json': '[]'},
               'backups': 1}},
    ('multi', 'bulk: autoupdate_on docksentry,nas/docksentry'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=docksentry,nas/docksentry',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "docksentry,nas/docksentry"}, '
                         '"source": "web"}'],
               'files': {'autoupdate_containers.json': '["nas/docksentry"]'},
               'backups': 1}},
    ('one', 'bulk: autoupdate_on docksentry,nas/docksentry'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=docksentry,nas/docksentry',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped"],
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "docksentry,nas/docksentry"}, '
                         '"source": "web"}'],
               'files': {'autoupdate_containers.json': '[]'},
               'backups': 1}},
    ('headless', 'bulk: autoupdate_on docksentry,nas/docksentry'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=docksentry,nas/docksentry',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown host 'nas' — skipped"],
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "docksentry,nas/docksentry"}, '
                         '"source": "web"}'],
               'files': {'autoupdate_containers.json': '[]'},
               'backups': 1}},
    ('multi', 'bulk: pin, repeated names'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web&names=db',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["web", "db"]'},
               'backups': 1}},
    ('one', 'bulk: pin, repeated names'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web&names=db',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["web", "db"]'},
               'backups': 1}},
    ('headless', 'bulk: pin, repeated names'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web&names=db',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["web", "db"]'},
               'backups': 1}},
    ('multi', 'bulk: pin, then unpin one'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web,db',
                'http': '303 | Location: /'},
               {'do': 'POST /api/bulk action=unpin&names=web',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web,db"}, "source": "web"}',
                         '{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"unpin", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('one', 'bulk: pin, then unpin one'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web,db',
                'http': '303 | Location: /'},
               {'do': 'POST /api/bulk action=unpin&names=web',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web,db"}, "source": "web"}',
                         '{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"unpin", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('headless', 'bulk: pin, then unpin one'): {'steps': [{'do': 'POST /api/bulk action=pin&names=web,db',
                'http': '303 | Location: /'},
               {'do': 'POST /api/bulk action=unpin&names=web',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin", "names": "web,db"}, "source": "web"}',
                         '{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"unpin", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('multi', 'bulk: autoupdate_on, then off for one'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=web,db',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}},
               {'do': 'POST /api/bulk action=autoupdate_off&names=db',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "web,db"}, "source": "web"}',
                         '{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_off", "names": "db"}, "source": "web"}'],
               'files': {'autoupdate_containers.json': '["web"]'},
               'backups': 1}},
    ('one', 'bulk: autoupdate_on, then off for one'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=web,db',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}},
               {'do': 'POST /api/bulk action=autoupdate_off&names=db',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "web,db"}, "source": "web"}',
                         '{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_off", "names": "db"}, "source": "web"}'],
               'files': {'autoupdate_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'bulk: autoupdate_on, then off for one'): {'steps': [{'do': 'POST /api/bulk action=autoupdate_on&names=web,db',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}},
               {'do': 'POST /api/bulk action=autoupdate_off&names=db',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_on", "names": "web,db"}, "source": "web"}',
                         '{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"autoupdate_off", "names": "db"}, "source": "web"}'],
               'files': {'autoupdate_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'bulk: update web,nas/web'): {'steps': [{'do': 'POST /api/bulk action=update&names=web,nas/web',
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)\n'
                        '✅ [web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format',
                          'nas': 'inspect --format t=10, inspect --format t=5, inspect '
                                 '--format'},
                'events': ['update_container@local:web', 'update_container@nas:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web,nas/web"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'bulk: update web,nas/web'): {'steps': [{'do': 'POST /api/bulk action=update&names=web,nas/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI update: unknown host 'nas' — skipped",
                        '<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web,nas/web"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'bulk: update web,nas/web'): {'steps': [{'do': 'POST /api/bulk action=update&names=web,nas/web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI update: unknown host 'nas' — skipped",
                        '<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web,nas/web"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'bulk: update web,db (db not pending)'): {'steps': [{'do': 'POST /api/bulk action=update&names=web,db',
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web,db"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'bulk: update web,db (db not pending)'): {'steps': [{'do': 'POST /api/bulk action=update&names=web,db',
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}',
                        '<telegram> *Update Result:*\n'
                        '\n'
                        '✅ [web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web,db"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'bulk: update web,db (db not pending)'): {'steps': [{'do': 'POST /api/bulk action=update&names=web,db',
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5, inspect '
                                   '--format'},
                'events': ['update_container@local:web']}],
     'state': {'pending': None,
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web,db"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'bulk: update, lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/bulk action=update&names=web',
                'http': '303 | Location: /',
                'out': ['<telegram> ⚠️ Update already in progress...']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'bulk: update, lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/bulk action=update&names=web',
                'http': '303 | Location: /',
                'out': ['<telegram> ⚠️ Update already in progress...']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'bulk: update, lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/bulk action=update&names=web',
                'http': '303 | Location: /'},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'bulk: update, pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/bulk action=update&names=web',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('one', 'bulk: update, pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/bulk action=update&names=web',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('headless', 'bulk: update, pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/bulk action=update&names=web',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"update", "names": "web"}, "source": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('multi', 'bulk: bogus action'): {'steps': [{'do': 'POST /api/bulk action=bogus&names=web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown action 'bogus'"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"bogus", "names": "web"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'bulk: bogus action'): {'steps': [{'do': 'POST /api/bulk action=bogus&names=web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown action 'bogus'"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"bogus", "names": "web"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'bulk: bogus action'): {'steps': [{'do': 'POST /api/bulk action=bogus&names=web',
                'http': '303 | Location: /',
                'out': ["<log> Web UI bulk: unknown action 'bogus'"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"bogus", "names": "web"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'bulk: no names'): {'steps': [{'do': 'POST /api/bulk action=pin', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'bulk: no names'): {'steps': [{'do': 'POST /api/bulk action=pin', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'bulk: no names'): {'steps': [{'do': 'POST /api/bulk action=pin', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"action": '
                         '"pin"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'bulk: no action'): {'steps': [{'do': 'POST /api/bulk names=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"names": '
                         '"web"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'bulk: no action'): {'steps': [{'do': 'POST /api/bulk names=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"names": '
                         '"web"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'bulk: no action'): {'steps': [{'do': 'POST /api/bulk names=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/bulk", "actor": "admin", "detail": {"names": '
                         '"web"}, "source": "web"}'],
               'backups': 1}},
    # ── check_one ───────────────────────────────────────────────────
    # :2726 → `_api_check_one` (:7317), synchronous JSON. `_action_target`
    # None → 'missing name'; the update lock and `_CHECK_LOCK` → busy; our
    # own name asks `has_selfupdate_available`, anything else
    # `check_all(only)`.
    ('multi', 'check_one: name=web'): {'steps': [{'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"found": true, '
                        '"name": "web", "ok": true, "selfupdate": false}',
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'check_one: name=web'): {'steps': [{'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"found": true, '
                        '"name": "web", "ok": true, "selfupdate": false}',
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'check_one: name=web'): {'steps': [{'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"found": true, '
                        '"name": "web", "ok": true, "selfupdate": false}',
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'check_one: name=nas/web'): {'steps': [{'do': 'POST /api/check_one name=nas/web',
                'http': '200 | Content-Type: application/json | body: {"found": true, '
                        '"name": "web", "ok": true, "selfupdate": false}',
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                 'inspect --format ×2'},
                'events': ["check_all@nas only=['web']"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('one', 'check_one: name=nas/web'): {'steps': [{'do': 'POST /api/check_one name=nas/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'check_one: name=nas/web'): {'steps': [{'do': 'POST /api/check_one name=nas/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'check_one: name=docksentry'): {'steps': [{'do': 'POST /api/check_one name=docksentry',
                'http': '200 | Content-Type: application/json | body: {"found": false, '
                        '"name": "docksentry", "ok": true, "selfupdate": true}',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "docksentry"}, "source": "web", "target": '
                         '"docksentry"}'],
               'backups': 1}},
    ('one', 'check_one: name=docksentry'): {'steps': [{'do': 'POST /api/check_one name=docksentry',
                'http': '200 | Content-Type: application/json | body: {"found": false, '
                        '"name": "docksentry", "ok": true, "selfupdate": true}',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "docksentry"}, "source": "web", "target": '
                         '"docksentry"}'],
               'backups': 1}},
    ('headless', 'check_one: name=docksentry'): {'steps': [{'do': 'POST /api/check_one name=docksentry',
                'http': '200 | Content-Type: application/json | body: {"found": false, '
                        '"name": "docksentry", "ok": true, "selfupdate": true}',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "docksentry"}, "source": "web", "target": '
                         '"docksentry"}'],
               'backups': 1}},
    ('multi', 'check_one: name=zzz/web'): {'steps': [{'do': 'POST /api/check_one name=zzz/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'check_one: name=zzz/web'): {'steps': [{'do': 'POST /api/check_one name=zzz/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'check_one: name=zzz/web'): {'steps': [{'do': 'POST /api/check_one name=zzz/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'check_one: name='): {'steps': [{'do': 'POST /api/check_one name=',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'check_one: name='): {'steps': [{'do': 'POST /api/check_one name=',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'check_one: name='): {'steps': [{'do': 'POST /api/check_one name=',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('multi', 'check_one: name=dead/web'): {'steps': [{'do': 'POST /api/check_one name=dead/web',
                'http': '200 | Content-Type: application/json | body: {"error": "Command '
                        "'['docker', '-H', 'ssh://dead', 'ps', '--format', "
                        '\'{{.Names}}|{{.Image}}\']\' timed out after 30 seconds", "ok": '
                        'false}',
                'out': ["<log> Web UI single check error: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '--format', '{{.Names}}|{{.Image}}']' timed "
                        'out after 30 seconds'],
                'hosts': {'dead': 'ps --format'},
                'events': ["check_all@dead only=['web']"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "dead/web"}, "source": "web", "target": "dead/web"}'],
               'backups': 1}},
    ('one', 'check_one: name=dead/web'): {'steps': [{'do': 'POST /api/check_one name=dead/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "dead/web"}, "source": "web", "target": "dead/web"}'],
               'backups': 1}},
    ('headless', 'check_one: name=dead/web'): {'steps': [{'do': 'POST /api/check_one name=dead/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "dead/web"}, "source": "web", "target": "dead/web"}'],
               'backups': 1}},
    ('multi', 'check_one: name=refused/web'): {'steps': [{'do': 'POST /api/check_one name=refused/web',
                'http': '200 | Content-Type: application/json | body: {"error": "could not '
                        'list containers (rc=255): ssh: connect to host 192.0.2.30 port '
                        '22: Connection refused", "ok": false}',
                'out': ['<log> Web UI single check error: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Connection '
                        'refused'],
                'hosts': {'refused': 'ps --format'},
                'events': ["check_all@refused only=['web']"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "refused/web"}, "source": "web", "target": '
                         '"refused/web"}'],
               'backups': 1}},
    ('one', 'check_one: name=refused/web'): {'steps': [{'do': 'POST /api/check_one name=refused/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "refused/web"}, "source": "web", "target": '
                         '"refused/web"}'],
               'backups': 1}},
    ('headless', 'check_one: name=refused/web'): {'steps': [{'do': 'POST /api/check_one name=refused/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "refused/web"}, "source": "web", "target": '
                         '"refused/web"}'],
               'backups': 1}},
    ('multi', 'check_one: name=flaky/web'): {'steps': [{'do': 'POST /api/check_one name=flaky/web',
                'http': '200 | Content-Type: application/json | body: {"error": "registry '
                        'did not answer in 15s", "ok": false}',
                'out': ['<log> Web UI single check error: registry did not answer in 15s'],
                'events': ["check_all@flaky only=['web']"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "flaky/web"}, "source": "web", "target": "flaky/web"}'],
               'backups': 1}},
    ('one', 'check_one: name=flaky/web'): {'steps': [{'do': 'POST /api/check_one name=flaky/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "flaky/web"}, "source": "web", "target": "flaky/web"}'],
               'backups': 1}},
    ('headless', 'check_one: name=flaky/web'): {'steps': [{'do': 'POST /api/check_one name=flaky/web',
                'http': '200 | Content-Type: application/json | body: {"error": "missing '
                        'name", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "flaky/web"}, "source": "web", "target": "flaky/web"}'],
               'backups': 1}},
    ('multi', 'check_one: name=nosuch'): {'steps': [{'do': 'POST /api/check_one name=nosuch',
                'http': '200 | Content-Type: application/json | body: {"found": false, '
                        '"name": "nosuch", "ok": true, "selfupdate": false}',
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['nosuch']"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "nosuch"}, "source": "web", "target": "nosuch"}'],
               'backups': 1}},
    ('one', 'check_one: name=nosuch'): {'steps': [{'do': 'POST /api/check_one name=nosuch',
                'http': '200 | Content-Type: application/json | body: {"found": false, '
                        '"name": "nosuch", "ok": true, "selfupdate": false}',
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['nosuch']"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "nosuch"}, "source": "web", "target": "nosuch"}'],
               'backups': 1}},
    ('headless', 'check_one: name=nosuch'): {'steps': [{'do': 'POST /api/check_one name=nosuch',
                'http': '200 | Content-Type: application/json | body: {"found": false, '
                        '"name": "nosuch", "ok": true, "selfupdate": false}',
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['nosuch']"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "nosuch"}, "source": "web", "target": "nosuch"}'],
               'backups': 1}},
    ('multi', 'check_one: update lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"busy": true, '
                        '"error": "update running", "ok": false}'},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'check_one: update lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"busy": true, '
                        '"error": "update running", "ok": false}'},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'check_one: update lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"busy": true, '
                        '"error": "update running", "ok": false}'},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'check_one: check lock held'): {'steps': [{'do': 'check_lock'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"busy": true, '
                        '"error": "check running", "ok": false}'},
               {'do': 'check_unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'check_one: check lock held'): {'steps': [{'do': 'check_lock'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"busy": true, '
                        '"error": "check running", "ok": false}'},
               {'do': 'check_unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'check_one: check lock held'): {'steps': [{'do': 'check_lock'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"busy": true, '
                        '"error": "check running", "ok": false}'},
               {'do': 'check_unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'check_one: pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"error": "the pin '
                        'list could not be read, so nothing was checked", "ok": false}',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.'],
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('one', 'check_one: pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"error": "the pin '
                        'list could not be read, so nothing was checked", "ok": false}',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.'],
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('headless', 'check_one: pin list unreadable'): {'steps': [{'do': 'corrupt_pins'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"error": "the pin '
                        'list could not be read, so nothing was checked", "ok": false}',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.'],
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('multi', 'check_one: debug on'): {'steps': [{'do': 'config debug True'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"debug": ["  '
                        'Skipped (self): docksentry"], "found": true, "name": "web", "ok": '
                        'true, "selfupdate": false}',
                'out': ['<log>   Skipped (self): docksentry'],
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'check_one: debug on'): {'steps': [{'do': 'config debug True'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"debug": ["  '
                        'Skipped (self): docksentry"], "found": true, "name": "web", "ok": '
                        'true, "selfupdate": false}',
                'out': ['<log>   Skipped (self): docksentry'],
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'check_one: debug on'): {'steps': [{'do': 'config debug True'},
               {'do': 'POST /api/check_one name=web',
                'http': '200 | Content-Type: application/json | body: {"debug": ["  '
                        'Skipped (self): docksentry"], "found": true, "name": "web", "ok": '
                        'true, "selfupdate": false}',
                'out': ['<log>   Skipped (self): docksentry'],
                'hosts': {'local': 'inspect --format t=5 ×2, ps --format, inspect t=60, '
                                   'inspect --format ×2'},
                'events': ["check_all@local only=['web']"]}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/check_one", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    # ── pin ─────────────────────────────────────────────────────────
    # :2746, `_action_target` (:1357) then the host store's `pin`; no
    # existence check, no reply, redirect '/'.
    ('multi', 'pin: name=web'): {'steps': [{'do': 'POST /api/pin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('one', 'pin: name=web'): {'steps': [{'do': 'POST /api/pin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'pin: name=web'): {'steps': [{'do': 'POST /api/pin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'pin: name=nas/web'): {'steps': [{'do': 'POST /api/pin name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'files': {'pinned_containers.json': '["nas/web"]'},
               'backups': 1}},
    ('one', 'pin: name=nas/web'): {'steps': [{'do': 'POST /api/pin name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'pin: name=nas/web'): {'steps': [{'do': 'POST /api/pin name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'pin: name=zzz/web'): {'steps': [{'do': 'POST /api/pin name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'pin: name=zzz/web'): {'steps': [{'do': 'POST /api/pin name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'pin: name=zzz/web'): {'steps': [{'do': 'POST /api/pin name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'pin: name='): {'steps': [{'do': 'POST /api/pin name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'pin: name='): {'steps': [{'do': 'POST /api/pin name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'pin: name='): {'steps': [{'do': 'POST /api/pin name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('multi', 'pin: name=nosuch'): {'steps': [{'do': 'POST /api/pin name=nosuch', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"nosuch"}, "source": "web", "target": "nosuch"}'],
               'files': {'pinned_containers.json': '["nosuch"]'},
               'backups': 1}},
    ('one', 'pin: name=nosuch'): {'steps': [{'do': 'POST /api/pin name=nosuch', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"nosuch"}, "source": "web", "target": "nosuch"}'],
               'files': {'pinned_containers.json': '["nosuch"]'},
               'backups': 1}},
    ('headless', 'pin: name=nosuch'): {'steps': [{'do': 'POST /api/pin name=nosuch', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"nosuch"}, "source": "web", "target": "nosuch"}'],
               'files': {'pinned_containers.json': '["nosuch"]'},
               'backups': 1}},
    # ── unpin ───────────────────────────────────────────────────────
    # :2756, like /api/pin.
    ('multi', 'unpin: name=web'): {'steps': [{'do': 'POST /api/unpin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'unpin: name=web'): {'steps': [{'do': 'POST /api/unpin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'unpin: name=web'): {'steps': [{'do': 'POST /api/unpin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'unpin: name=nas/web'): {'steps': [{'do': 'POST /api/unpin name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('one', 'unpin: name=nas/web'): {'steps': [{'do': 'POST /api/unpin name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'unpin: name=nas/web'): {'steps': [{'do': 'POST /api/unpin name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'unpin: name=zzz/web'): {'steps': [{'do': 'POST /api/unpin name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'unpin: name=zzz/web'): {'steps': [{'do': 'POST /api/unpin name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'unpin: name=zzz/web'): {'steps': [{'do': 'POST /api/unpin name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'unpin: name='): {'steps': [{'do': 'POST /api/unpin name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'unpin: name='): {'steps': [{'do': 'POST /api/unpin name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'unpin: name='): {'steps': [{'do': 'POST /api/unpin name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/unpin", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('multi', 'unpin: after pin'): {'steps': [{'do': 'POST /api/pin name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/unpin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '[]'},
               'backups': 1}},
    ('one', 'unpin: after pin'): {'steps': [{'do': 'POST /api/pin name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/unpin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '[]'},
               'backups': 1}},
    ('headless', 'unpin: after pin'): {'steps': [{'do': 'POST /api/pin name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/unpin name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/pin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/unpin", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'pinned_containers.json': '[]'},
               'backups': 1}},
    # ── autoupdate ──────────────────────────────────────────────────
    # :2764, `toggle_auto`, refused for our own container on the local host
    # only.
    ('multi', 'autoupdate: name=web'): {'steps': [{'do': 'POST /api/autoupdate name=web',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'autoupdate_containers.json': '["web"]'},
               'backups': 1}},
    ('one', 'autoupdate: name=web'): {'steps': [{'do': 'POST /api/autoupdate name=web',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'autoupdate_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'autoupdate: name=web'): {'steps': [{'do': 'POST /api/autoupdate name=web',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'autoupdate_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'autoupdate: name=nas/web'): {'steps': [{'do': 'POST /api/autoupdate name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'files': {'autoupdate_containers.json': '["nas/web"]'},
               'backups': 1}},
    ('one', 'autoupdate: name=nas/web'): {'steps': [{'do': 'POST /api/autoupdate name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'autoupdate: name=nas/web'): {'steps': [{'do': 'POST /api/autoupdate name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'autoupdate: name=zzz/web'): {'steps': [{'do': 'POST /api/autoupdate name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'autoupdate: name=zzz/web'): {'steps': [{'do': 'POST /api/autoupdate name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'autoupdate: name=zzz/web'): {'steps': [{'do': 'POST /api/autoupdate name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'autoupdate: name='): {'steps': [{'do': 'POST /api/autoupdate name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'autoupdate: name='): {'steps': [{'do': 'POST /api/autoupdate name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'autoupdate: name='): {'steps': [{'do': 'POST /api/autoupdate name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'autoupdate: twice'): {'steps': [{'do': 'POST /api/autoupdate name=web',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}},
               {'do': 'POST /api/autoupdate name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'autoupdate_containers.json': '[]'},
               'backups': 1}},
    ('one', 'autoupdate: twice'): {'steps': [{'do': 'POST /api/autoupdate name=web',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}},
               {'do': 'POST /api/autoupdate name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'autoupdate_containers.json': '[]'},
               'backups': 1}},
    ('headless', 'autoupdate: twice'): {'steps': [{'do': 'POST /api/autoupdate name=web',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}},
               {'do': 'POST /api/autoupdate name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'autoupdate_containers.json': '[]'},
               'backups': 1}},
    ('multi', 'autoupdate: name=docksentry'): {'steps': [{'do': 'POST /api/autoupdate name=docksentry',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "docksentry"}, "source": "web", "target": '
                         '"docksentry"}'],
               'backups': 1}},
    ('one', 'autoupdate: name=docksentry'): {'steps': [{'do': 'POST /api/autoupdate name=docksentry',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "docksentry"}, "source": "web", "target": '
                         '"docksentry"}'],
               'backups': 1}},
    ('headless', 'autoupdate: name=docksentry'): {'steps': [{'do': 'POST /api/autoupdate name=docksentry',
                'http': '303 | Location: /',
                'hosts': {'local': 'inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "docksentry"}, "source": "web", "target": '
                         '"docksentry"}'],
               'backups': 1}},
    ('multi', 'autoupdate: name=nas/docksentry'): {'steps': [{'do': 'POST /api/autoupdate name=nas/docksentry',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "nas/docksentry"}, "source": "web", "target": '
                         '"nas/docksentry"}'],
               'files': {'autoupdate_containers.json': '["nas/docksentry"]'},
               'backups': 1}},
    ('one', 'autoupdate: name=nas/docksentry'): {'steps': [{'do': 'POST /api/autoupdate name=nas/docksentry',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "nas/docksentry"}, "source": "web", "target": '
                         '"nas/docksentry"}'],
               'backups': 1}},
    ('headless', 'autoupdate: name=nas/docksentry'): {'steps': [{'do': 'POST /api/autoupdate name=nas/docksentry',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/autoupdate", "actor": "admin", "detail": '
                         '{"name": "nas/docksentry"}, "source": "web", "target": '
                         '"nas/docksentry"}'],
               'backups': 1}},
    # ── protect ─────────────────────────────────────────────────────
    # :3478, `toggle_protect_stop`, back to the container page for a local
    # target (`_back_to_container`, :1392).
    ('multi', 'protect: name=web'): {'steps': [{'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'protect_stop_containers.json': '["web"]'},
               'backups': 1}},
    ('one', 'protect: name=web'): {'steps': [{'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'protect_stop_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'protect: name=web'): {'steps': [{'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'protect_stop_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'protect: name=nas/web'): {'steps': [{'do': 'POST /api/protect name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'files': {'protect_stop_containers.json': '["nas/web"]'},
               'backups': 1}},
    ('one', 'protect: name=nas/web'): {'steps': [{'do': 'POST /api/protect name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'protect: name=nas/web'): {'steps': [{'do': 'POST /api/protect name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'protect: name=zzz/web'): {'steps': [{'do': 'POST /api/protect name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'protect: name=zzz/web'): {'steps': [{'do': 'POST /api/protect name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'protect: name=zzz/web'): {'steps': [{'do': 'POST /api/protect name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'protect: name='): {'steps': [{'do': 'POST /api/protect name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'protect: name='): {'steps': [{'do': 'POST /api/protect name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'protect: name='): {'steps': [{'do': 'POST /api/protect name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('multi', 'protect: twice'): {'steps': [{'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'},
               {'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'protect_stop_containers.json': '[]'},
               'backups': 1}},
    ('one', 'protect: twice'): {'steps': [{'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'},
               {'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'protect_stop_containers.json': '[]'},
               'backups': 1}},
    ('headless', 'protect: twice'): {'steps': [{'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'},
               {'do': 'POST /api/protect name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/protect", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'protect_stop_containers.json': '[]'},
               'backups': 1}},
    # ── trust_running ───────────────────────────────────────────────
    # :3470, `toggle_trust_running`, redirect '/'.
    ('multi', 'trust_running: name=web'): {'steps': [{'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'trust_running_containers.json': '["web"]'},
               'backups': 1}},
    ('one', 'trust_running: name=web'): {'steps': [{'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'trust_running_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'trust_running: name=web'): {'steps': [{'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'trust_running_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'trust_running: name=nas/web'): {'steps': [{'do': 'POST /api/trust_running name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'files': {'trust_running_containers.json': '["nas/web"]'},
               'backups': 1}},
    ('one', 'trust_running: name=nas/web'): {'steps': [{'do': 'POST /api/trust_running name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'trust_running: name=nas/web'): {'steps': [{'do': 'POST /api/trust_running name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'trust_running: name=zzz/web'): {'steps': [{'do': 'POST /api/trust_running name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'trust_running: name=zzz/web'): {'steps': [{'do': 'POST /api/trust_running name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'trust_running: name=zzz/web'): {'steps': [{'do': 'POST /api/trust_running name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'trust_running: name='): {'steps': [{'do': 'POST /api/trust_running name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'trust_running: name='): {'steps': [{'do': 'POST /api/trust_running name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'trust_running: name='): {'steps': [{'do': 'POST /api/trust_running name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'trust_running: twice'): {'steps': [{'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'trust_running_containers.json': '[]'},
               'backups': 1}},
    ('one', 'trust_running: twice'): {'steps': [{'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'trust_running_containers.json': '[]'},
               'backups': 1}},
    ('headless', 'trust_running: twice'): {'steps': [{'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/trust_running name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/trust_running", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'trust_running_containers.json': '[]'},
               'backups': 1}},
    # ── ask_major ───────────────────────────────────────────────────
    # :3462, `toggle_ask_before_major`, redirect '/'.
    ('multi', 'ask_major: name=web'): {'steps': [{'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'ask_before_major.json': '["web"]'},
               'backups': 1}},
    ('one', 'ask_major: name=web'): {'steps': [{'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'ask_before_major.json': '["web"]'},
               'backups': 1}},
    ('headless', 'ask_major: name=web'): {'steps': [{'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'ask_before_major.json': '["web"]'},
               'backups': 1}},
    ('multi', 'ask_major: name=nas/web'): {'steps': [{'do': 'POST /api/ask_major name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'files': {'ask_before_major.json': '["nas/web"]'},
               'backups': 1}},
    ('one', 'ask_major: name=nas/web'): {'steps': [{'do': 'POST /api/ask_major name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'ask_major: name=nas/web'): {'steps': [{'do': 'POST /api/ask_major name=nas/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "nas/web"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'ask_major: name=zzz/web'): {'steps': [{'do': 'POST /api/ask_major name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'ask_major: name=zzz/web'): {'steps': [{'do': 'POST /api/ask_major name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'ask_major: name=zzz/web'): {'steps': [{'do': 'POST /api/ask_major name=zzz/web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "zzz/web"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'ask_major: name='): {'steps': [{'do': 'POST /api/ask_major name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'ask_major: name='): {'steps': [{'do': 'POST /api/ask_major name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'ask_major: name='): {'steps': [{'do': 'POST /api/ask_major name=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('multi', 'ask_major: twice'): {'steps': [{'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'ask_before_major.json': '[]'},
               'backups': 1}},
    ('one', 'ask_major: twice'): {'steps': [{'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'ask_before_major.json': '[]'},
               'backups': 1}},
    ('headless', 'ask_major: twice'): {'steps': [{'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'},
               {'do': 'POST /api/ask_major name=web', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}',
                         '{"action": "/api/ask_major", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'files': {'ask_before_major.json': '[]'},
               'backups': 1}},
    # ── cooldown ────────────────────────────────────────────────────
    # :3486, the raw string to the store's `set_cooldown`
    # (container_store.py:409), which clamps and reads a non-number as 0.
    ('multi', 'cooldown: name=web seconds=3600'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=3600',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "3600"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'},
               'backups': 1}},
    ('one', 'cooldown: name=web seconds=3600'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=3600',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "3600"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'},
               'backups': 1}},
    ('headless', 'cooldown: name=web seconds=3600'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=3600',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "3600"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'},
               'backups': 1}},
    ('multi', 'cooldown: name=web seconds=abc'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=abc',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "abc"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('one', 'cooldown: name=web seconds=abc'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=abc',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "abc"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('headless', 'cooldown: name=web seconds=abc'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=abc',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "abc"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('multi', 'cooldown: name=nas/web seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=nas/web&seconds=60',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"nas/web", "seconds": "60"}, "source": "web", "target": '
                         '"nas/web"}'],
               'files': {'update_cooldowns.json': '{"nas/web": 60}'},
               'backups': 1}},
    ('one', 'cooldown: name=nas/web seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=nas/web&seconds=60',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"nas/web", "seconds": "60"}, "source": "web", "target": '
                         '"nas/web"}'],
               'backups': 1}},
    ('headless', 'cooldown: name=nas/web seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=nas/web&seconds=60',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"nas/web", "seconds": "60"}, "source": "web", "target": '
                         '"nas/web"}'],
               'backups': 1}},
    ('multi', 'cooldown: name=zzz/web seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=zzz/web&seconds=60',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "seconds": "60"}, "source": "web", "target": '
                         '"zzz/web"}'],
               'backups': 1}},
    ('one', 'cooldown: name=zzz/web seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=zzz/web&seconds=60',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "seconds": "60"}, "source": "web", "target": '
                         '"zzz/web"}'],
               'backups': 1}},
    ('headless', 'cooldown: name=zzz/web seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=zzz/web&seconds=60',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "seconds": "60"}, "source": "web", "target": '
                         '"zzz/web"}'],
               'backups': 1}},
    ('multi', 'cooldown: name=web seconds=-5'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=-5',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "-5"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('one', 'cooldown: name=web seconds=-5'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=-5',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "-5"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('headless', 'cooldown: name=web seconds=-5'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=-5',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "-5"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('multi', 'cooldown: name=web seconds=99999999'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=99999999',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "99999999"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'},
               'backups': 1}},
    ('one', 'cooldown: name=web seconds=99999999'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=99999999',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "99999999"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'},
               'backups': 1}},
    ('headless', 'cooldown: name=web seconds=99999999'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=99999999',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "99999999"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'update_cooldowns.json': '{"web": 600}'},
               'backups': 1}},
    ('multi', 'cooldown: name= seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=&seconds=60', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": '
                         '{"seconds": "60"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'cooldown: name= seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=&seconds=60', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": '
                         '{"seconds": "60"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'cooldown: name= seconds=60'): {'steps': [{'do': 'POST /api/cooldown name=&seconds=60', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": '
                         '{"seconds": "60"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'cooldown: 3600, then abc'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=3600',
                'http': '303 | Location: /container/web'},
               {'do': 'POST /api/cooldown name=web&seconds=abc',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "3600"}, "source": "web", "target": "web"}',
                         '{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "abc"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('one', 'cooldown: 3600, then abc'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=3600',
                'http': '303 | Location: /container/web'},
               {'do': 'POST /api/cooldown name=web&seconds=abc',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "3600"}, "source": "web", "target": "web"}',
                         '{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "abc"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('headless', 'cooldown: 3600, then abc'): {'steps': [{'do': 'POST /api/cooldown name=web&seconds=3600',
                'http': '303 | Location: /container/web'},
               {'do': 'POST /api/cooldown name=web&seconds=abc',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "3600"}, "source": "web", "target": "web"}',
                         '{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web", "seconds": "abc"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('multi', 'cooldown: no seconds field'): {'steps': [{'do': 'POST /api/cooldown name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('one', 'cooldown: no seconds field'): {'steps': [{'do': 'POST /api/cooldown name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    ('headless', 'cooldown: no seconds field'): {'steps': [{'do': 'POST /api/cooldown name=web',
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cooldown", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'update_cooldowns.json': '{}'},
               'backups': 1}},
    # ── note ────────────────────────────────────────────────────────
    # :2831, `set_note`, back to the Referer path.
    ('multi', 'note: name=web'): {'steps': [{'do': "POST /api/note name=web&note=a  b referer='/container/web'",
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "a  b"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{"web": "a  b"}'},
               'backups': 1}},
    ('one', 'note: name=web'): {'steps': [{'do': "POST /api/note name=web&note=a  b referer='/container/web'",
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "a  b"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{"web": "a  b"}'},
               'backups': 1}},
    ('headless', 'note: name=web'): {'steps': [{'do': "POST /api/note name=web&note=a  b referer='/container/web'",
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "a  b"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{"web": "a  b"}'},
               'backups': 1}},
    ('multi', 'note: name=nas/web'): {'steps': [{'do': 'POST /api/note name=nas/web&note=x', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"nas/web", "note": "x"}, "source": "web", "target": "nas/web"}'],
               'files': {'container_notes.json': '{"nas/web": "x"}'},
               'backups': 1}},
    ('one', 'note: name=nas/web'): {'steps': [{'do': 'POST /api/note name=nas/web&note=x', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"nas/web", "note": "x"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'note: name=nas/web'): {'steps': [{'do': 'POST /api/note name=nas/web&note=x', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"nas/web", "note": "x"}, "source": "web", "target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'note: name=zzz/web'): {'steps': [{'do': 'POST /api/note name=zzz/web&note=x', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "note": "x"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'note: name=zzz/web'): {'steps': [{'do': 'POST /api/note name=zzz/web&note=x', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "note": "x"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'note: name=zzz/web'): {'steps': [{'do': 'POST /api/note name=zzz/web&note=x', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "note": "x"}, "source": "web", "target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'note: set, then cleared'): {'steps': [{'do': 'POST /api/note name=web&note=x', 'http': '303 | Location: /'},
               {'do': 'POST /api/note name=web&note=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "x"}, "source": "web", "target": "web"}',
                         '{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{}'},
               'backups': 1}},
    ('one', 'note: set, then cleared'): {'steps': [{'do': 'POST /api/note name=web&note=x', 'http': '303 | Location: /'},
               {'do': 'POST /api/note name=web&note=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "x"}, "source": "web", "target": "web"}',
                         '{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{}'},
               'backups': 1}},
    ('headless', 'note: set, then cleared'): {'steps': [{'do': 'POST /api/note name=web&note=x', 'http': '303 | Location: /'},
               {'do': 'POST /api/note name=web&note=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "x"}, "source": "web", "target": "web"}',
                         '{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{}'},
               'backups': 1}},
    ('multi', 'note: no Referer'): {'steps': [{'do': 'POST /api/note name=web&note=x referer=None',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "x"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{"web": "x"}'},
               'backups': 1}},
    ('one', 'note: no Referer'): {'steps': [{'do': 'POST /api/note name=web&note=x referer=None',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "x"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{"web": "x"}'},
               'backups': 1}},
    ('headless', 'note: no Referer'): {'steps': [{'do': 'POST /api/note name=web&note=x referer=None',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/note", "actor": "admin", "detail": {"name": '
                         '"web", "note": "x"}, "source": "web", "target": "web"}'],
               'files': {'container_notes.json': '{"web": "x"}'},
               'backups': 1}},
    # ── link ────────────────────────────────────────────────────────
    # :2994, `set_link`'s boolean becomes ?link=saved|cleared|rejected on
    # the Referer path; nothing for an unknown host.
    ('multi', 'link: name=web url=https://example.org/x'): {'steps': [{'do': 'POST /api/link name=web&url=https://example.org/x '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web?link=saved'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "https://example.org/x"}, "source": "web", '
                         '"target": "web"}'],
               'files': {'container_links.json': '{"web": "https://example.org/x"}'},
               'backups': 1}},
    ('one', 'link: name=web url=https://example.org/x'): {'steps': [{'do': 'POST /api/link name=web&url=https://example.org/x '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web?link=saved'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "https://example.org/x"}, "source": "web", '
                         '"target": "web"}'],
               'files': {'container_links.json': '{"web": "https://example.org/x"}'},
               'backups': 1}},
    ('headless', 'link: name=web url=https://example.org/x'): {'steps': [{'do': 'POST /api/link name=web&url=https://example.org/x '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web?link=saved'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "https://example.org/x"}, "source": "web", '
                         '"target": "web"}'],
               'files': {'container_links.json': '{"web": "https://example.org/x"}'},
               'backups': 1}},
    ('multi', 'link: name=web url=notaurl'): {'steps': [{'do': "POST /api/link name=web&url=notaurl referer='/container/web'",
                'http': '303 | Location: /container/web?link=rejected'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "notaurl"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'link: name=web url=notaurl'): {'steps': [{'do': "POST /api/link name=web&url=notaurl referer='/container/web'",
                'http': '303 | Location: /container/web?link=rejected'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "notaurl"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'link: name=web url=notaurl'): {'steps': [{'do': "POST /api/link name=web&url=notaurl referer='/container/web'",
                'http': '303 | Location: /container/web?link=rejected'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "notaurl"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'link: name=web url=javascript:alert(1)'): {'steps': [{'do': 'POST /api/link name=web&url=javascript:alert(1) '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web?link=rejected'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "javascript:alert(1)"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'link: name=web url=javascript:alert(1)'): {'steps': [{'do': 'POST /api/link name=web&url=javascript:alert(1) '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web?link=rejected'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "javascript:alert(1)"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'link: name=web url=javascript:alert(1)'): {'steps': [{'do': 'POST /api/link name=web&url=javascript:alert(1) '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web?link=rejected'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web", "url": "javascript:alert(1)"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'link: name=web url='): {'steps': [{'do': "POST /api/link name=web&url= referer='/container/web'",
                'http': '303 | Location: /container/web?link=cleared'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'container_links.json': '{}'},
               'backups': 1}},
    ('one', 'link: name=web url='): {'steps': [{'do': "POST /api/link name=web&url= referer='/container/web'",
                'http': '303 | Location: /container/web?link=cleared'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'container_links.json': '{}'},
               'backups': 1}},
    ('headless', 'link: name=web url='): {'steps': [{'do': "POST /api/link name=web&url= referer='/container/web'",
                'http': '303 | Location: /container/web?link=cleared'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"web"}, "source": "web", "target": "web"}'],
               'files': {'container_links.json': '{}'},
               'backups': 1}},
    ('multi', 'link: name=nas/web url=https://example.org/n'): {'steps': [{'do': 'POST /api/link name=nas/web&url=https://example.org/n '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web?link=saved'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"nas/web", "url": "https://example.org/n"}, "source": "web", '
                         '"target": "nas/web"}'],
               'files': {'container_links.json': '{"nas/web": "https://example.org/n"}'},
               'backups': 1}},
    ('one', 'link: name=nas/web url=https://example.org/n'): {'steps': [{'do': 'POST /api/link name=nas/web&url=https://example.org/n '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"nas/web", "url": "https://example.org/n"}, "source": "web", '
                         '"target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'link: name=nas/web url=https://example.org/n'): {'steps': [{'do': 'POST /api/link name=nas/web&url=https://example.org/n '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"nas/web", "url": "https://example.org/n"}, "source": "web", '
                         '"target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'link: name=zzz/web url=https://example.org/z'): {'steps': [{'do': 'POST /api/link name=zzz/web&url=https://example.org/z '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "url": "https://example.org/z"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'link: name=zzz/web url=https://example.org/z'): {'steps': [{'do': 'POST /api/link name=zzz/web&url=https://example.org/z '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "url": "https://example.org/z"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'link: name=zzz/web url=https://example.org/z'): {'steps': [{'do': 'POST /api/link name=zzz/web&url=https://example.org/z '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/link", "actor": "admin", "detail": {"name": '
                         '"zzz/web", "url": "https://example.org/z"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    # ── lifecycle ───────────────────────────────────────────────────
    # :2784 → lifecycle.act with the target's backend, checker and store;
    # the outcome is printed (bot.t), the browser goes back to the Referer
    # path.
    ('multi', 'lifecycle: start web'): {'steps': [{'do': "POST /api/lifecycle name=web&action=start referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log> Lifecycle start web: ▶️ `web` started.'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "start", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'lifecycle: start web'): {'steps': [{'do': "POST /api/lifecycle name=web&action=start referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log> Lifecycle start web: ▶️ `web` started.'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "start", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: start web'): {'steps': [{'do': "POST /api/lifecycle name=web&action=start referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log> Lifecycle start web: ▶️ `web` started.'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "start", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'lifecycle: stop web'): {'steps': [{'do': "POST /api/lifecycle name=web&action=stop referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log>   Stop web: effective_stop=60s, subprocess=90s',
                        '<log> Lifecycle stop web: 🟥 `web` stopped.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, stop --time t=90, '
                                   'inspect --format t=5'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'lifecycle: stop web'): {'steps': [{'do': "POST /api/lifecycle name=web&action=stop referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log>   Stop web: effective_stop=60s, subprocess=90s',
                        '<log> Lifecycle stop web: 🟥 `web` stopped.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, stop --time t=90, '
                                   'inspect --format t=5'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: stop web'): {'steps': [{'do': "POST /api/lifecycle name=web&action=stop referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log>   Stop web: effective_stop=60s, subprocess=90s',
                        '<log> Lifecycle stop web: 🟥 `web` stopped.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10, inspect --format t=5, stop --time t=90, '
                                   'inspect --format t=5'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'lifecycle: restart web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=restart '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log> Lifecycle restart web: 🔁 `web` restarted.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'lifecycle: restart web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=restart '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log> Lifecycle restart web: 🔁 `web` restarted.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: restart web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=restart '
                      "referer='/container/web'",
                'http': '303 | Location: /container/web',
                'out': ['<log> Lifecycle restart web: 🔁 `web` restarted.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, restart '
                                   '--time t=120'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'lifecycle: stop docksentry'): {'steps': [{'do': 'POST /api/lifecycle name=docksentry&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop docksentry: 🛡 Refused to stop `docksentry` — '
                        "that's me. Use /selfupdate for self-updates."],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "docksentry"}, "source": "web", '
                         '"target": "docksentry"}'],
               'backups': 1}},
    ('one', 'lifecycle: stop docksentry'): {'steps': [{'do': 'POST /api/lifecycle name=docksentry&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop docksentry: 🛡 Refused to stop `docksentry` — '
                        "that's me. Use /selfupdate for self-updates."],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "docksentry"}, "source": "web", '
                         '"target": "docksentry"}'],
               'backups': 1}},
    ('headless', 'lifecycle: stop docksentry'): {'steps': [{'do': 'POST /api/lifecycle name=docksentry&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop docksentry: 🛡 Refused to stop `docksentry` — '
                        "that's me. Use /selfupdate for self-updates."],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "docksentry"}, "source": "web", '
                         '"target": "docksentry"}'],
               'backups': 1}},
    ('multi', 'lifecycle: restart docksentry'): {'steps': [{'do': 'POST /api/lifecycle name=docksentry&action=restart',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle restart docksentry: 🛡 Refused to restart '
                        "`docksentry` — that's me. Use /selfupdate for self-updates."],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "docksentry"}, "source": "web", '
                         '"target": "docksentry"}'],
               'backups': 1}},
    ('one', 'lifecycle: restart docksentry'): {'steps': [{'do': 'POST /api/lifecycle name=docksentry&action=restart',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle restart docksentry: 🛡 Refused to restart '
                        "`docksentry` — that's me. Use /selfupdate for self-updates."],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "docksentry"}, "source": "web", '
                         '"target": "docksentry"}'],
               'backups': 1}},
    ('headless', 'lifecycle: restart docksentry'): {'steps': [{'do': 'POST /api/lifecycle name=docksentry&action=restart',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle restart docksentry: 🛡 Refused to restart '
                        "`docksentry` — that's me. Use /selfupdate for self-updates."],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "docksentry"}, "source": "web", '
                         '"target": "docksentry"}'],
               'backups': 1}},
    ('multi', 'lifecycle: restart nas/web'): {'steps': [{'do': 'POST /api/lifecycle name=nas/web&action=restart',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle restart web: 🔁 `web` restarted.'],
                'hosts': {'local': 'inspect --format t=5',
                          'nas': 'ps -a --format, inspect --format t=5, restart --time '
                                 't=120'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'backups': 1}},
    ('one', 'lifecycle: restart nas/web'): {'steps': [{'do': 'POST /api/lifecycle name=nas/web&action=restart',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: restart nas/web'): {'steps': [{'do': 'POST /api/lifecycle name=nas/web&action=restart',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'lifecycle: stop dead/web'): {'steps': [{'do': 'POST /api/lifecycle name=dead/web&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop web: Could not list containers: Command '
                        "'['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', "
                        "'{{.Names}}']' t"],
                'hosts': {'dead': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "dead/web"}, "source": "web", '
                         '"target": "dead/web"}'],
               'backups': 1}},
    ('one', 'lifecycle: stop dead/web'): {'steps': [{'do': 'POST /api/lifecycle name=dead/web&action=stop',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "dead/web"}, "source": "web", '
                         '"target": "dead/web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: stop dead/web'): {'steps': [{'do': 'POST /api/lifecycle name=dead/web&action=stop',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "dead/web"}, "source": "web", '
                         '"target": "dead/web"}'],
               'backups': 1}},
    ('multi', 'lifecycle: stop refused/web'): {'steps': [{'do': 'POST /api/lifecycle name=refused/web&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop web: Container `web` not found.'],
                'hosts': {'refused': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "refused/web"}, "source": "web", '
                         '"target": "refused/web"}'],
               'backups': 1}},
    ('one', 'lifecycle: stop refused/web'): {'steps': [{'do': 'POST /api/lifecycle name=refused/web&action=stop',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "refused/web"}, "source": "web", '
                         '"target": "refused/web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: stop refused/web'): {'steps': [{'do': 'POST /api/lifecycle name=refused/web&action=stop',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "refused/web"}, "source": "web", '
                         '"target": "refused/web"}'],
               'backups': 1}},
    ('multi', 'lifecycle: start webdav'): {'steps': [{'do': 'POST /api/lifecycle name=webdav&action=start',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle start webdav: ▶️ `webdav` started.'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "start", "name": "webdav"}, "source": "web", '
                         '"target": "webdav"}'],
               'backups': 1}},
    ('one', 'lifecycle: start webdav'): {'steps': [{'do': 'POST /api/lifecycle name=webdav&action=start',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle start webdav: ▶️ `webdav` started.'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "start", "name": "webdav"}, "source": "web", '
                         '"target": "webdav"}'],
               'backups': 1}},
    ('headless', 'lifecycle: start webdav'): {'steps': [{'do': 'POST /api/lifecycle name=webdav&action=start',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle start webdav: ▶️ `webdav` started.'],
                'hosts': {'local': 'ps -a --format, start t=30'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "start", "name": "webdav"}, "source": "web", '
                         '"target": "webdav"}'],
               'backups': 1}},
    ('multi', 'lifecycle: stop nosuch'): {'steps': [{'do': 'POST /api/lifecycle name=nosuch&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop nosuch: Container `nosuch` not found.'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "nosuch"}, "source": "web", "target": '
                         '"nosuch"}'],
               'backups': 1}},
    ('one', 'lifecycle: stop nosuch'): {'steps': [{'do': 'POST /api/lifecycle name=nosuch&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop nosuch: Container `nosuch` not found.'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "nosuch"}, "source": "web", "target": '
                         '"nosuch"}'],
               'backups': 1}},
    ('headless', 'lifecycle: stop nosuch'): {'steps': [{'do': 'POST /api/lifecycle name=nosuch&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop nosuch: Container `nosuch` not found.'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "nosuch"}, "source": "web", "target": '
                         '"nosuch"}'],
               'backups': 1}},
    ('multi', 'lifecycle: restart zzz/web'): {'steps': [{'do': 'POST /api/lifecycle name=zzz/web&action=restart',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "zzz/web"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'lifecycle: restart zzz/web'): {'steps': [{'do': 'POST /api/lifecycle name=zzz/web&action=restart',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "zzz/web"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: restart zzz/web'): {'steps': [{'do': 'POST /api/lifecycle name=zzz/web&action=restart',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "restart", "name": "zzz/web"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'lifecycle: bogus web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=bogus',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "bogus", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'lifecycle: bogus web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=bogus',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "bogus", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: bogus web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=bogus',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "bogus", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'lifecycle:  web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('one', 'lifecycle:  web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('headless', 'lifecycle:  web'): {'steps': [{'do': 'POST /api/lifecycle name=web&action=', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"name": "web"}, "source": "web", "target": "web"}'],
               'backups': 1}},
    ('multi', 'lifecycle: stop we'): {'steps': [{'do': 'POST /api/lifecycle name=we&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop we: Multiple matches: `web`, `webdav`. Be '
                        'more specific.'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "we"}, "source": "web", "target": '
                         '"we"}'],
               'backups': 1}},
    ('one', 'lifecycle: stop we'): {'steps': [{'do': 'POST /api/lifecycle name=we&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop we: Multiple matches: `web`, `webdav`. Be '
                        'more specific.'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "we"}, "source": "web", "target": '
                         '"we"}'],
               'backups': 1}},
    ('headless', 'lifecycle: stop we'): {'steps': [{'do': 'POST /api/lifecycle name=we&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop we: Multiple matches: `web`, `webdav`. Be '
                        'more specific.'],
                'hosts': {'local': 'ps -a --format'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "we"}, "source": "web", "target": '
                         '"we"}'],
               'backups': 1}},
    ('multi', 'lifecycle: stop web, protected'): {'steps': [{'do': 'store local toggle_protect_stop web'},
               {'do': 'POST /api/lifecycle name=web&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop web: 🛡 `web` is protected from Stop. Remove '
                        'the protection first (container detail page in the Web UI, or '
                        '`/protect web`). Restart still works.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'protect_stop_containers.json': '["web"]'},
               'backups': 1}},
    ('one', 'lifecycle: stop web, protected'): {'steps': [{'do': 'store local toggle_protect_stop web'},
               {'do': 'POST /api/lifecycle name=web&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop web: 🛡 `web` is protected from Stop. Remove '
                        'the protection first (container detail page in the Web UI, or '
                        '`/protect web`). Restart still works.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'protect_stop_containers.json': '["web"]'},
               'backups': 1}},
    ('headless', 'lifecycle: stop web, protected'): {'steps': [{'do': 'store local toggle_protect_stop web'},
               {'do': 'POST /api/lifecycle name=web&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop web: 🛡 `web` is protected from Stop. Remove '
                        'the protection first (container detail page in the Web UI, or '
                        '`/protect web`). Restart still works.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2, inspect '
                                   '--format t=10'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'protect_stop_containers.json': '["web"]'},
               'backups': 1}},
    ('multi', 'lifecycle: stop web, lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/lifecycle name=web&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop web: ⏳ Updates in progress — stop refused so '
                        "it can't interfere with a running update. Try again once they "
                        'finish.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'lifecycle: stop web, lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/lifecycle name=web&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop web: ⏳ Updates in progress — stop refused so '
                        "it can't interfere with a running update. Try again once they "
                        'finish.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'lifecycle: stop web, lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/lifecycle name=web&action=stop',
                'http': '303 | Location: /',
                'out': ['<log> Lifecycle stop web: ⏳ Updates in progress — stop refused so '
                        "it can't interfere with a running update. Try again once they "
                        'finish.'],
                'hosts': {'local': 'ps -a --format, inspect --format t=5 ×2'}},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/lifecycle", "actor": "admin", "detail": '
                         '{"action": "stop", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    # ── cleanup ─────────────────────────────────────────────────────
    # :2825 → `_api_cleanup` (:7384): `bot.cleanup_guarded`
    # (telegram_bot.py:2776) with the local checker; Telegram only when
    # enabled, the plugins under cleanup_manual_prefix.
    ('multi', 'cleanup'): {'steps': [{'do': 'POST /api/cleanup',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<telegram> ✅ Total reclaimed space: 0 B',
                        '<webhook ok> {"event": "message", "text": "🧹 Cleanup: ✅ Total '
                        'reclaimed space: 0 B"}',
                        '<log> Cleanup: ✅ Total reclaimed space: 0 B'],
                'hosts': {'local': 'image prune -a --force --filter t=180'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cleanup", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'cleanup'): {'steps': [{'do': 'POST /api/cleanup',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<telegram> ✅ Total reclaimed space: 0 B',
                        '<webhook ok> {"event": "message", "text": "🧹 Cleanup: ✅ Total '
                        'reclaimed space: 0 B"}',
                        '<log> Cleanup: ✅ Total reclaimed space: 0 B'],
                'hosts': {'local': 'image prune -a --force --filter t=180'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cleanup", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'cleanup'): {'steps': [{'do': 'POST /api/cleanup',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<webhook ok> {"event": "message", "text": "🧹 Cleanup: ✅ Total '
                        'reclaimed space: 0 B"}',
                        '<log> Cleanup: ✅ Total reclaimed space: 0 B'],
                'hosts': {'local': 'image prune -a --force --filter t=180'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cleanup", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('multi', 'cleanup: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/cleanup',
                'http': '303 | Location: /settings?saved=1',
                'out': ["<telegram> ⏳ Updates in progress — cleanup skipped so it can't "
                        'delete a freshly pulled image. Try again once they finish.',
                        '<log> Cleanup skipped: ⏳ Updates in progress — cleanup skipped so '
                        "it can't delete a freshly pulled image. Try again once they "
                        'finish.']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/cleanup", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'cleanup: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/cleanup',
                'http': '303 | Location: /settings?saved=1',
                'out': ["<telegram> ⏳ Updates in progress — cleanup skipped so it can't "
                        'delete a freshly pulled image. Try again once they finish.',
                        '<log> Cleanup skipped: ⏳ Updates in progress — cleanup skipped so '
                        "it can't delete a freshly pulled image. Try again once they "
                        'finish.']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cleanup", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'cleanup: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/cleanup',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<log> Cleanup skipped: ⏳ Updates in progress — cleanup skipped so '
                        "it can't delete a freshly pulled image. Try again once they "
                        'finish.']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/cleanup", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    # ── selfupdate ──────────────────────────────────────────────────
    # :2828 → `_api_selfupdate` (:7409): `_handle_selfupdate` with Telegram,
    # `check_selfupdate_auto` without.
    ('multi', 'selfupdate'): {'steps': [{'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'events': ['selfupdate.run target=None']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'selfupdate'): {'steps': [{'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'events': ['selfupdate.run target=None']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'selfupdate'): {'steps': [{'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<log> Selfupdate check: pulling ghcr.io/example/docksentry:latest '
                        '(running sha256:df7fd47fc51e, created 2026-01-01)...',
                        '<log> Selfupdate check: already up to date (sha256:df7fd47fc51e).',
                        '<log> Web UI selfupdate (headless): no update applied — see lines '
                        'above for the reason'],
                'hosts': {'local': 'inspect --format t=5, inspect t=10'},
                'events': ['docker pull ghcr.io/example/docksentry:latest',
                           'docker inspect ghcr.io/example/docksentry:latest']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'selfupdate: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<telegram> ⏳ Container updates in progress — self-update queued, '
                        'it will start automatically once they finish.']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'queued_selfupdate': (None,),
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'selfupdate: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<telegram> ⏳ Container updates in progress — self-update queued, '
                        'it will start automatically once they finish.']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'queued_selfupdate': (None,),
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'selfupdate: lock held'): {'steps': [{'do': 'lock'},
               {'do': 'POST /api/selfupdate',
                'http': '303 | Location: /settings?saved=1',
                'out': ['<log> Selfupdate check: skipped — another update flow is running.',
                        '<log> Web UI selfupdate (headless): no update applied — see lines '
                        'above for the reason']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/selfupdate", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    # ── major ───────────────────────────────────────────────────────
    # :3495. confirm → a thread on `bot._confirm_major_update`
    # (telegram_bot.py:2078) with the raw key; reject → the host store's
    # `remove_pending_major`.
    ('multi', 'major: confirm web'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /',
                'out': ['<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{}'},
               'backups': 1}},
    ('one', 'major: confirm web'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /',
                'out': ['<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest): updated '
                        '(stand-in)',
                        '<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{}'},
               'backups': 1}},
    ('headless', 'major: confirm web'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'local': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@local:web']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{}'},
               'backups': 1}},
    ('multi', 'major: reject web'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=reject',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "reject", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{}'},
               'backups': 1}},
    ('one', 'major: reject web'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=reject',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "reject", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{}'},
               'backups': 1}},
    ('headless', 'major: reject web'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=reject',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "reject", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{}'},
               'backups': 1}},
    ('multi', 'major: confirm nas/web'): {'steps': [{'do': 'major nas web'},
               {'do': 'POST /api/major_confirm name=nas/web&action=confirm',
                'http': '303 | Location: /',
                'out': ['<telegram> ✅ '
                        '[web](https://github.com/example/web/releases/latest) @nas: '
                        'updated (stand-in)',
                        '<webhook ok> {"container": "web", "detail": "updated (stand-in)", '
                        '"event": "update_result", "image": "nginx:1.25", "source_url": '
                        '"https://github.com/example/web/releases/latest", "success": '
                        'true}'],
                'hosts': {'nas': 'inspect --format t=10, inspect --format t=5'},
                'events': ['update_container@nas:web']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'files': {'major_confirmations.json': '{}'},
               'backups': 1}},
    ('one', 'major: confirm nas/web'): {'steps': [{'do': 'major nas web'},
               {'do': 'POST /api/major_confirm name=nas/web&action=confirm',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('headless', 'major: confirm nas/web'): {'steps': [{'do': 'major nas web'},
               {'do': 'POST /api/major_confirm name=nas/web&action=confirm',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('multi', 'major: reject nas/web'): {'steps': [{'do': 'major nas web'},
               {'do': 'POST /api/major_confirm name=nas/web&action=reject',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "reject", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'files': {'major_confirmations.json': '{}'},
               'backups': 1}},
    ('one', 'major: reject nas/web'): {'steps': [{'do': 'major nas web'},
               {'do': 'POST /api/major_confirm name=nas/web&action=reject',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "reject", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('headless', 'major: reject nas/web'): {'steps': [{'do': 'major nas web'},
               {'do': 'POST /api/major_confirm name=nas/web&action=reject',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "reject", "name": "nas/web"}, "source": "web", '
                         '"target": "nas/web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('multi', 'major: confirm web, lock held'): {'steps': [{'do': 'major local web'},
               {'do': 'lock'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /',
                'out': ['<telegram> ⚠️ Update already in progress...']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('one', 'major: confirm web, lock held'): {'steps': [{'do': 'major local web'},
               {'do': 'lock'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /',
                'out': ['<telegram> ⚠️ Update already in progress...']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('headless', 'major: confirm web, lock held'): {'steps': [{'do': 'major local web'},
               {'do': 'lock'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /'},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('multi', 'major: confirm web, nothing deferred'): {'steps': [{'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /',
                'out': ['<telegram> ⚠️ No pending major update for `web`.']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'major: confirm web, nothing deferred'): {'steps': [{'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /',
                'out': ['<telegram> ⚠️ No pending major update for `web`.']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'major: confirm web, nothing deferred'): {'steps': [{'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'major: confirm web, pin list unreadable'): {'steps': [{'do': 'major local web'},
               {'do': 'corrupt_pins'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}',
                         'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('one', 'major: confirm web, pin list unreadable'): {'steps': [{'do': 'major local web'},
               {'do': 'corrupt_pins'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}',
                         'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('headless', 'major: confirm web, pin list unreadable'): {'steps': [{'do': 'major local web'},
               {'do': 'corrupt_pins'},
               {'do': 'POST /api/major_confirm name=web&action=confirm',
                'http': '303 | Location: /?update=pinlist',
                'out': ["<log> Failed to read {DATA}/pinned_containers.json: Expecting ',' "
                        'delimiter: line 1 column 6 (char 5)',
                        '<log> ERROR: {DATA}/pinned_containers.json could not be read '
                        "(Expecting ',' delimiter: line 1 column 6 (char 5)).",
                        '<log> ERROR: treating every container as pinned, so no update '
                        'will run on this host until the file is repaired or removed.']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}',
                         'pinned_containers.json': '<unreadable>'},
               'backups': 1}},
    ('multi', 'major: bogus action'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=bogus',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "bogus", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('one', 'major: bogus action'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=bogus',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "bogus", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('headless', 'major: bogus action'): {'steps': [{'do': 'major local web'},
               {'do': 'POST /api/major_confirm name=web&action=bogus',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "bogus", "name": "web"}, "source": "web", "target": '
                         '"web"}'],
               'files': {'major_confirmations.json': '{"web": {"compose": {}, "image": '
                                                     '"nginx:1.25", "new_version": '
                                                     '"2.0.0", "old_version": "1.25.0"}}'},
               'backups': 1}},
    ('multi', 'major: confirm zzz/web'): {'steps': [{'do': 'POST /api/major_confirm name=zzz/web&action=confirm',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "zzz/web"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'major: confirm zzz/web'): {'steps': [{'do': 'POST /api/major_confirm name=zzz/web&action=confirm',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "zzz/web"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'major: confirm zzz/web'): {'steps': [{'do': 'POST /api/major_confirm name=zzz/web&action=confirm',
                'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/major_confirm", "actor": "admin", "detail": '
                         '{"action": "confirm", "name": "zzz/web"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    # ── maintenance ─────────────────────────────────────────────────
    # :3026. off / forever / hours clamped to 0..720 (default 1 when the
    # field is missing).
    ('multi', 'maintenance: action=forever'): {'steps': [{'do': "POST /api/maintenance action=forever referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "forever"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "forever"}'},
               'backups': 1}},
    ('one', 'maintenance: action=forever'): {'steps': [{'do': "POST /api/maintenance action=forever referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "forever"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "forever"}'},
               'backups': 1}},
    ('headless', 'maintenance: action=forever'): {'steps': [{'do': "POST /api/maintenance action=forever referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "forever"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "forever"}'},
               'backups': 1}},
    ('multi', 'maintenance: action=hours&hours=4'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=4 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "4"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "now+4.0h"}'},
               'backups': 1}},
    ('one', 'maintenance: action=hours&hours=4'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=4 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "4"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "now+4.0h"}'},
               'backups': 1}},
    ('headless', 'maintenance: action=hours&hours=4'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=4 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "4"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "now+4.0h"}'},
               'backups': 1}},
    ('multi', 'maintenance: action=hours&hours=abc'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=abc referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "abc"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'maintenance: action=hours&hours=abc'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=abc referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "abc"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'maintenance: action=hours&hours=abc'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=abc referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "abc"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'maintenance: action=hours&hours=0'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=0 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "0"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'maintenance: action=hours&hours=0'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=0 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "0"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'maintenance: action=hours&hours=0'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=0 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "0"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'maintenance: action=hours&hours=9999'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=9999 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "9999"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "now+720.0h"}'},
               'backups': 1}},
    ('one', 'maintenance: action=hours&hours=9999'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=9999 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "9999"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "now+720.0h"}'},
               'backups': 1}},
    ('headless', 'maintenance: action=hours&hours=9999'): {'steps': [{'do': "POST /api/maintenance action=hours&hours=9999 referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "hours", "hours": "9999"}, "source": "web"}'],
               'files': {'maintenance.json': '{"until": "now+720.0h"}'},
               'backups': 1}},
    ('multi', 'maintenance: empty form'): {'steps': [{'do': "POST /api/maintenance referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "source": '
                         '"web"}'],
               'files': {'maintenance.json': '{"until": "now+1.0h"}'},
               'backups': 1}},
    ('one', 'maintenance: empty form'): {'steps': [{'do': "POST /api/maintenance referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "source": '
                         '"web"}'],
               'files': {'maintenance.json': '{"until": "now+1.0h"}'},
               'backups': 1}},
    ('headless', 'maintenance: empty form'): {'steps': [{'do': "POST /api/maintenance referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "source": '
                         '"web"}'],
               'files': {'maintenance.json': '{"until": "now+1.0h"}'},
               'backups': 1}},
    ('multi', 'maintenance: forever, then off'): {'steps': [{'do': 'POST /api/maintenance action=forever', 'http': '303 | Location: /'},
               {'do': 'POST /api/maintenance action=off', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "forever"}, "source": "web"}',
                         '{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "off"}, "source": "web"}'],
               'files': {'maintenance.json': '{}'},
               'backups': 1}},
    ('one', 'maintenance: forever, then off'): {'steps': [{'do': 'POST /api/maintenance action=forever', 'http': '303 | Location: /'},
               {'do': 'POST /api/maintenance action=off', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "forever"}, "source": "web"}',
                         '{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "off"}, "source": "web"}'],
               'files': {'maintenance.json': '{}'},
               'backups': 1}},
    ('headless', 'maintenance: forever, then off'): {'steps': [{'do': 'POST /api/maintenance action=forever', 'http': '303 | Location: /'},
               {'do': 'POST /api/maintenance action=off', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "forever"}, "source": "web"}',
                         '{"action": "/api/maintenance", "actor": "admin", "detail": '
                         '{"action": "off"}, "source": "web"}'],
               'files': {'maintenance.json': '{}'},
               'backups': 1}},
    # ── window ──────────────────────────────────────────────────────
    # :3441. save needs HH:MM twice; delete clears; no target means nothing
    # happens.
    ('multi', 'window: save web'): {'steps': [{'do': 'POST /api/window '
                      'name=web&action=save&start=02:00&end=04:00&weekdays=1&weekdays=3&weekdays=x',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"action": '
                         '"save", "end": "04:00", "name": "web", "start": "02:00", '
                         '"weekdays": "1"}, "source": "web", "target": "web"}'],
               'files': {'update_windows.json': '{"web": {"end": "04:00", "start": '
                                                '"02:00", "weekdays": [1, 3]}}'},
               'backups': 1}},
    ('one', 'window: save web'): {'steps': [{'do': 'POST /api/window '
                      'name=web&action=save&start=02:00&end=04:00&weekdays=1&weekdays=3&weekdays=x',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"action": '
                         '"save", "end": "04:00", "name": "web", "start": "02:00", '
                         '"weekdays": "1"}, "source": "web", "target": "web"}'],
               'files': {'update_windows.json': '{"web": {"end": "04:00", "start": '
                                                '"02:00", "weekdays": [1, 3]}}'},
               'backups': 1}},
    ('headless', 'window: save web'): {'steps': [{'do': 'POST /api/window '
                      'name=web&action=save&start=02:00&end=04:00&weekdays=1&weekdays=3&weekdays=x',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"action": '
                         '"save", "end": "04:00", "name": "web", "start": "02:00", '
                         '"weekdays": "1"}, "source": "web", "target": "web"}'],
               'files': {'update_windows.json': '{"web": {"end": "04:00", "start": '
                                                '"02:00", "weekdays": [1, 3]}}'},
               'backups': 1}},
    ('multi', 'window: save web, bad time'): {'steps': [{'do': 'POST /api/window name=web&start=2:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "web", "start": "2:00"}, "source": "web", '
                         '"target": "web"}'],
               'backups': 1}},
    ('one', 'window: save web, bad time'): {'steps': [{'do': 'POST /api/window name=web&start=2:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "web", "start": "2:00"}, "source": "web", '
                         '"target": "web"}'],
               'backups': 1}},
    ('headless', 'window: save web, bad time'): {'steps': [{'do': 'POST /api/window name=web&start=2:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "web", "start": "2:00"}, "source": "web", '
                         '"target": "web"}'],
               'backups': 1}},
    ('multi', 'window: save, then delete'): {'steps': [{'do': 'POST /api/window name=web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'},
               {'do': 'POST /api/window name=web&action=delete',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "web", "start": "02:00"}, "source": "web", '
                         '"target": "web"}',
                         '{"action": "/api/window", "actor": "admin", "detail": {"action": '
                         '"delete", "name": "web"}, "source": "web", "target": "web"}'],
               'files': {'update_windows.json': '{}'},
               'backups': 1}},
    ('one', 'window: save, then delete'): {'steps': [{'do': 'POST /api/window name=web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'},
               {'do': 'POST /api/window name=web&action=delete',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "web", "start": "02:00"}, "source": "web", '
                         '"target": "web"}',
                         '{"action": "/api/window", "actor": "admin", "detail": {"action": '
                         '"delete", "name": "web"}, "source": "web", "target": "web"}'],
               'files': {'update_windows.json': '{}'},
               'backups': 1}},
    ('headless', 'window: save, then delete'): {'steps': [{'do': 'POST /api/window name=web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'},
               {'do': 'POST /api/window name=web&action=delete',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "web", "start": "02:00"}, "source": "web", '
                         '"target": "web"}',
                         '{"action": "/api/window", "actor": "admin", "detail": {"action": '
                         '"delete", "name": "web"}, "source": "web", "target": "web"}'],
               'files': {'update_windows.json': '{}'},
               'backups': 1}},
    ('multi', 'window: save nas/web'): {'steps': [{'do': 'POST /api/window name=nas/web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "nas/web", "start": "02:00"}, "source": "web", '
                         '"target": "nas/web"}'],
               'files': {'update_windows.json': '{"nas/web": {"end": "04:00", "start": '
                                                '"02:00", "weekdays": []}}'},
               'backups': 1}},
    ('one', 'window: save nas/web'): {'steps': [{'do': 'POST /api/window name=nas/web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "nas/web", "start": "02:00"}, "source": "web", '
                         '"target": "nas/web"}'],
               'backups': 1}},
    ('headless', 'window: save nas/web'): {'steps': [{'do': 'POST /api/window name=nas/web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "nas/web", "start": "02:00"}, "source": "web", '
                         '"target": "nas/web"}'],
               'backups': 1}},
    ('multi', 'window: save zzz/web'): {'steps': [{'do': 'POST /api/window name=zzz/web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "zzz/web", "start": "02:00"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('one', 'window: save zzz/web'): {'steps': [{'do': 'POST /api/window name=zzz/web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "zzz/web", "start": "02:00"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('headless', 'window: save zzz/web'): {'steps': [{'do': 'POST /api/window name=zzz/web&start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "name": "zzz/web", "start": "02:00"}, "source": "web", '
                         '"target": "zzz/web"}'],
               'backups': 1}},
    ('multi', 'window: no name'): {'steps': [{'do': 'POST /api/window start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "start": "02:00"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'window: no name'): {'steps': [{'do': 'POST /api/window start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "start": "02:00"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'window: no name'): {'steps': [{'do': 'POST /api/window start=02:00&end=04:00',
                'http': '303 | Location: /settings#windows'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/window", "actor": "admin", "detail": {"end": '
                         '"04:00", "start": "02:00"}, "source": "web"}'],
               'backups': 1}},
    # ── group_save ──────────────────────────────────────────────────
    # :3291, the global store; a new name gets a slug, a taken one -2.
    ('multi', 'group_save: new'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('one', 'group_save: new'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('headless', 'group_save: new'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('multi', 'group_save: new, from /settings'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on '
                      "referer='/settings'",
                'http': '303 | Location: /settings#groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('one', 'group_save: new, from /settings'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on '
                      "referer='/settings'",
                'http': '303 | Location: /settings#groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('headless', 'group_save: new, from /settings'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on '
                      "referer='/settings'",
                'http': '303 | Location: /settings#groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('multi', 'group_save: same name twice'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack-2": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('one', 'group_save: same name twice'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack-2": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('headless', 'group_save: same name twice'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}'],
               'files': {'groups.json': '{"my-stack-2": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('multi', 'group_save: edit'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_save group_id=my-stack&name=Renamed&containers=db',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "db", "group_id": "my-stack", "name": "Renamed"}, '
                         '"source": "web", "target": "Renamed"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db"], "name": '
                                        '"Renamed", "restart_dependents": false, '
                                        '"wait_seconds": 30}}'},
               'backups': 1}},
    ('one', 'group_save: edit'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_save group_id=my-stack&name=Renamed&containers=db',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "db", "group_id": "my-stack", "name": "Renamed"}, '
                         '"source": "web", "target": "Renamed"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db"], "name": '
                                        '"Renamed", "restart_dependents": false, '
                                        '"wait_seconds": 30}}'},
               'backups': 1}},
    ('headless', 'group_save: edit'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_save group_id=my-stack&name=Renamed&containers=db',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "db", "group_id": "my-stack", "name": "Renamed"}, '
                         '"source": "web", "target": "Renamed"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db"], "name": '
                                        '"Renamed", "restart_dependents": false, '
                                        '"wait_seconds": 30}}'},
               'backups': 1}},
    ('multi', 'group_save: no containers'): {'steps': [{'do': 'POST /api/group_save name=Empty',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"name": "Empty"}, "source": "web", "target": "Empty"}'],
               'backups': 1}},
    ('one', 'group_save: no containers'): {'steps': [{'do': 'POST /api/group_save name=Empty',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"name": "Empty"}, "source": "web", "target": "Empty"}'],
               'backups': 1}},
    ('headless', 'group_save: no containers'): {'steps': [{'do': 'POST /api/group_save name=Empty',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"name": "Empty"}, "source": "web", "target": "Empty"}'],
               'backups': 1}},
    # ── group_delete ────────────────────────────────────────────────
    # :3329.
    ('multi', 'group_delete'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_delete group_id=my-stack',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_delete", "actor": "admin", "detail": '
                         '{"group_id": "my-stack"}, "source": "web"}'],
               'files': {'groups.json': '{}'},
               'backups': 1}},
    ('one', 'group_delete'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_delete group_id=my-stack',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_delete", "actor": "admin", "detail": '
                         '{"group_id": "my-stack"}, "source": "web"}'],
               'files': {'groups.json': '{}'},
               'backups': 1}},
    ('headless', 'group_delete'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_delete group_id=my-stack',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_delete", "actor": "admin", "detail": '
                         '{"group_id": "my-stack"}, "source": "web"}'],
               'files': {'groups.json': '{}'},
               'backups': 1}},
    ('multi', 'group_delete: unknown'): {'steps': [{'do': "POST /api/group_delete group_id=x referer='/settings'",
                'http': '303 | Location: /settings#groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_delete", "actor": "admin", "detail": '
                         '{"group_id": "x"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'group_delete: unknown'): {'steps': [{'do': "POST /api/group_delete group_id=x referer='/settings'",
                'http': '303 | Location: /settings#groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_delete", "actor": "admin", "detail": '
                         '{"group_id": "x"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'group_delete: unknown'): {'steps': [{'do': "POST /api/group_delete group_id=x referer='/settings'",
                'http': '303 | Location: /settings#groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_delete", "actor": "admin", "detail": '
                         '{"group_id": "x"}, "source": "web"}'],
               'backups': 1}},
    # ── group_reorder ───────────────────────────────────────────────
    # :3339, up/down only.
    ('multi', 'group_reorder: db up'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder group_id=my-stack&container=db&direction=up',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "up", "group_id": "my-stack"}, '
                         '"source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db", "web"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('one', 'group_reorder: db up'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder group_id=my-stack&container=db&direction=up',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "up", "group_id": "my-stack"}, '
                         '"source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db", "web"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('headless', 'group_reorder: db up'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder group_id=my-stack&container=db&direction=up',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "up", "group_id": "my-stack"}, '
                         '"source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db", "web"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('multi', 'group_reorder: db down'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder '
                      'group_id=my-stack&container=db&direction=down',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "down", "group_id": '
                         '"my-stack"}, "source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('one', 'group_reorder: db down'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder '
                      'group_id=my-stack&container=db&direction=down',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "down", "group_id": '
                         '"my-stack"}, "source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('headless', 'group_reorder: db down'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder '
                      'group_id=my-stack&container=db&direction=down',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "down", "group_id": '
                         '"my-stack"}, "source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('multi', 'group_reorder: db sideways'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder '
                      'group_id=my-stack&container=db&direction=sideways',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "sideways", "group_id": '
                         '"my-stack"}, "source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('one', 'group_reorder: db sideways'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder '
                      'group_id=my-stack&container=db&direction=sideways',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "sideways", "group_id": '
                         '"my-stack"}, "source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('headless', 'group_reorder: db sideways'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder '
                      'group_id=my-stack&container=db&direction=sideways',
                'http': '303 | Location: /groups'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder", "actor": "admin", "detail": '
                         '{"container": "db", "direction": "sideways", "group_id": '
                         '"my-stack"}, "source": "web", "target": "db"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["web", "db"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    # ── groups_import_batch ─────────────────────────────────────────
    # :3351, same name (case-blind) merges, bad JSON and empty entries
    # skipped; JSON answer.
    ('multi', 'groups_import_batch'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/groups_import_batch stacks={"name": "my stack!", '
                      '"containers": ["web"], "restart_dependents": false}&stacks={"name": '
                      '"media", "containers": ["plex", ""], "wait_seconds": 5}&stacks={not '
                      'json&stacks={"name": "", "containers": ["x"]}',
                'http': '200 | Content-Type: application/json | body: {"created": 2, "ok": '
                        'true}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/groups_import_batch", "actor": "admin", '
                         '"detail": {"stacks": "{\\"name\\": \\"my stack!\\", '
                         '\\"containers\\": [\\"web\\"], \\"restart_dependents\\": '
                         'false}"}, "source": "web"}'],
               'files': {'groups.json': '{"media": {"containers": ["plex"], "name": '
                                        '"media", "restart_dependents": false, '
                                        '"wait_seconds": 5}, "my-stack": {"containers": '
                                        '["web"], "name": "my stack!", '
                                        '"restart_dependents": false, "wait_seconds": '
                                        '30}}'},
               'backups': 1}},
    ('one', 'groups_import_batch'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/groups_import_batch stacks={"name": "my stack!", '
                      '"containers": ["web"], "restart_dependents": false}&stacks={"name": '
                      '"media", "containers": ["plex", ""], "wait_seconds": 5}&stacks={not '
                      'json&stacks={"name": "", "containers": ["x"]}',
                'http': '200 | Content-Type: application/json | body: {"created": 2, "ok": '
                        'true}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/groups_import_batch", "actor": "admin", '
                         '"detail": {"stacks": "{\\"name\\": \\"my stack!\\", '
                         '\\"containers\\": [\\"web\\"], \\"restart_dependents\\": '
                         'false}"}, "source": "web"}'],
               'files': {'groups.json': '{"media": {"containers": ["plex"], "name": '
                                        '"media", "restart_dependents": false, '
                                        '"wait_seconds": 5}, "my-stack": {"containers": '
                                        '["web"], "name": "my stack!", '
                                        '"restart_dependents": false, "wait_seconds": '
                                        '30}}'},
               'backups': 1}},
    ('headless', 'groups_import_batch'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/groups_import_batch stacks={"name": "my stack!", '
                      '"containers": ["web"], "restart_dependents": false}&stacks={"name": '
                      '"media", "containers": ["plex", ""], "wait_seconds": 5}&stacks={not '
                      'json&stacks={"name": "", "containers": ["x"]}',
                'http': '200 | Content-Type: application/json | body: {"created": 2, "ok": '
                        'true}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/groups_import_batch", "actor": "admin", '
                         '"detail": {"stacks": "{\\"name\\": \\"my stack!\\", '
                         '\\"containers\\": [\\"web\\"], \\"restart_dependents\\": '
                         'false}"}, "source": "web"}'],
               'files': {'groups.json': '{"media": {"containers": ["plex"], "name": '
                                        '"media", "restart_dependents": false, '
                                        '"wait_seconds": 5}, "my-stack": {"containers": '
                                        '["web"], "name": "my stack!", '
                                        '"restart_dependents": false, "wait_seconds": '
                                        '30}}'},
               'backups': 1}},
    # ── group_reorder_batch ─────────────────────────────────────────
    # :3402, unknown members dropped, missing ones appended; JSON answer.
    ('multi', 'group_reorder_batch'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder_batch '
                      'group_id=my-stack&containers=db&containers=ghost',
                'http': '200 | Content-Type: application/json | body: {"ok": true}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder_batch", "actor": "admin", '
                         '"detail": {"containers": "db", "group_id": "my-stack"}, '
                         '"source": "web"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db", "web"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('one', 'group_reorder_batch'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder_batch '
                      'group_id=my-stack&containers=db&containers=ghost',
                'http': '200 | Content-Type: application/json | body: {"ok": true}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder_batch", "actor": "admin", '
                         '"detail": {"containers": "db", "group_id": "my-stack"}, '
                         '"source": "web"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db", "web"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('headless', 'group_reorder_batch'): {'steps': [{'do': 'POST /api/group_save name=My '
                      'Stack!&containers=web&containers=db&wait_seconds=10&restart_dependents=on',
                'http': '303 | Location: /groups'},
               {'do': 'POST /api/group_reorder_batch '
                      'group_id=my-stack&containers=db&containers=ghost',
                'http': '200 | Content-Type: application/json | body: {"ok": true}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_save", "actor": "admin", "detail": '
                         '{"containers": "web", "name": "My Stack!", "restart_dependents": '
                         '"on", "wait_seconds": "10"}, "source": "web", "target": "My '
                         'Stack!"}',
                         '{"action": "/api/group_reorder_batch", "actor": "admin", '
                         '"detail": {"containers": "db", "group_id": "my-stack"}, '
                         '"source": "web"}'],
               'files': {'groups.json': '{"my-stack": {"containers": ["db", "web"], '
                                        '"name": "My Stack!", "restart_dependents": true, '
                                        '"wait_seconds": 10}}'},
               'backups': 1}},
    ('multi', 'group_reorder_batch: unknown'): {'steps': [{'do': 'POST /api/group_reorder_batch group_id=x&containers=db',
                'http': '200 | Content-Type: application/json | body: {"ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/group_reorder_batch", "actor": "admin", '
                         '"detail": {"containers": "db", "group_id": "x"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'group_reorder_batch: unknown'): {'steps': [{'do': 'POST /api/group_reorder_batch group_id=x&containers=db',
                'http': '200 | Content-Type: application/json | body: {"ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_reorder_batch", "actor": "admin", '
                         '"detail": {"containers": "db", "group_id": "x"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'group_reorder_batch: unknown'): {'steps': [{'do': 'POST /api/group_reorder_batch group_id=x&containers=db',
                'http': '200 | Content-Type: application/json | body: {"ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/group_reorder_batch", "actor": "admin", '
                         '"detail": {"containers": "db", "group_id": "x"}, "source": '
                         '"web"}'],
               'backups': 1}},
    # ── test_channel ────────────────────────────────────────────────
    # :2842, the saved settings of one channel, isolated; the verdict is
    # read off what the channel printed.
    ('multi', 'test_channel: nosuch'): {'steps': [{'do': 'POST /api/test_channel name=nosuch',
                'http': '200 | Content-Type: application/json | body: {"error": "unknown '
                        'channel", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "nosuch"}, "source": "web", "target": "nosuch"}'],
               'backups': 1}},
    ('one', 'test_channel: nosuch'): {'steps': [{'do': 'POST /api/test_channel name=nosuch',
                'http': '200 | Content-Type: application/json | body: {"error": "unknown '
                        'channel", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "nosuch"}, "source": "web", "target": "nosuch"}'],
               'backups': 1}},
    ('headless', 'test_channel: nosuch'): {'steps': [{'do': 'POST /api/test_channel name=nosuch',
                'http': '200 | Content-Type: application/json | body: {"error": "unknown '
                        'channel", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "nosuch"}, "source": "web", "target": "nosuch"}'],
               'backups': 1}},
    ('multi', 'test_channel: telegram'): {'steps': [{'do': 'POST /api/test_channel name=telegram',
                'http': '200 | Content-Type: application/json | body: {"error": "unknown '
                        'channel", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "telegram"}, "source": "web", "target": "telegram"}'],
               'backups': 1}},
    ('one', 'test_channel: telegram'): {'steps': [{'do': 'POST /api/test_channel name=telegram',
                'http': '200 | Content-Type: application/json | body: {"error": "unknown '
                        'channel", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "telegram"}, "source": "web", "target": "telegram"}'],
               'backups': 1}},
    ('headless', 'test_channel: telegram'): {'steps': [{'do': 'POST /api/test_channel name=telegram',
                'http': '200 | Content-Type: application/json | body: {"error": "unknown '
                        'channel", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "telegram"}, "source": "web", "target": "telegram"}'],
               'backups': 1}},
    ('multi', 'test_channel: ntfy'): {'steps': [{'do': 'POST /api/test_channel name=ntfy',
                'http': '200 | Content-Type: application/json | body: {"error": "not '
                        'configured", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "ntfy"}, "source": "web", "target": "ntfy"}'],
               'backups': 1}},
    ('one', 'test_channel: ntfy'): {'steps': [{'do': 'POST /api/test_channel name=ntfy',
                'http': '200 | Content-Type: application/json | body: {"error": "not '
                        'configured", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "ntfy"}, "source": "web", "target": "ntfy"}'],
               'backups': 1}},
    ('headless', 'test_channel: ntfy'): {'steps': [{'do': 'POST /api/test_channel name=ntfy',
                'http': '200 | Content-Type: application/json | body: {"error": "not '
                        'configured", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "ntfy"}, "source": "web", "target": "ntfy"}'],
               'backups': 1}},
    ('multi', 'test_channel: webhook'): {'steps': [{'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    ('one', 'test_channel: webhook'): {'steps': [{'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    ('headless', 'test_channel: webhook'): {'steps': [{'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    ('multi', 'test_channel: webhook answers 404'): {'steps': [{'do': 'config webhook_url https://hook.invalid/404'},
               {'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"error": "Webhook '
                        'error: HTTP 404", "ok": false}',
                'out': ['<webhook 404> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    ('one', 'test_channel: webhook answers 404'): {'steps': [{'do': 'config webhook_url https://hook.invalid/404'},
               {'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"error": "Webhook '
                        'error: HTTP 404", "ok": false}',
                'out': ['<webhook 404> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    ('headless', 'test_channel: webhook answers 404'): {'steps': [{'do': 'config webhook_url https://hook.invalid/404'},
               {'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"error": "Webhook '
                        'error: HTTP 404", "ok": false}',
                'out': ['<webhook 404> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    ('multi', 'test_channel: webhook answers 503'): {'steps': [{'do': 'config webhook_url https://hook.invalid/503'},
               {'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"error": "webhook '
                        'send failed — holding the message for redelivery — a real alert '
                        'would be held and retried for up to 15 minutes", "ok": false}',
                'out': ['<webhook 503> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    ('one', 'test_channel: webhook answers 503'): {'steps': [{'do': 'config webhook_url https://hook.invalid/503'},
               {'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"error": "webhook '
                        'send failed — holding the message for redelivery — a real alert '
                        'would be held and retried for up to 15 minutes", "ok": false}',
                'out': ['<webhook 503> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    ('headless', 'test_channel: webhook answers 503'): {'steps': [{'do': 'config webhook_url https://hook.invalid/503'},
               {'do': 'POST /api/test_channel name=webhook',
                'http': '200 | Content-Type: application/json | body: {"error": "webhook '
                        'send failed — holding the message for redelivery — a real alert '
                        'would be held and retried for up to 15 minutes", "ok": false}',
                'out': ['<webhook 503> {"event": "message", "text": "🧪 Docksentry test '
                        'message (webhook)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_channel", "actor": "admin", "detail": '
                         '{"name": "webhook"}, "source": "web", "target": "webhook"}'],
               'backups': 1}},
    # ── test_webhook ────────────────────────────────────────────────
    # :2941, the typed URL; ok unless something raises.
    ('multi', 'test_webhook: kind=bogus url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=bogus&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"error": "invalid '
                        'kind", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "bogus", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'test_webhook: kind=bogus url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=bogus&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"error": "invalid '
                        'kind", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "bogus", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'test_webhook: kind=bogus url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=bogus&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"error": "invalid '
                        'kind", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "bogus", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'test_webhook: kind=webhook url='): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=',
                'http': '200 | Content-Type: application/json | body: {"error": "empty '
                        'URL", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'test_webhook: kind=webhook url='): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=',
                'http': '200 | Content-Type: application/json | body: {"error": "empty '
                        'URL", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'test_webhook: kind=webhook url='): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=',
                'http': '200 | Content-Type: application/json | body: {"error": "empty '
                        'URL", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'test_webhook: kind=webhook url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"event": "message", "text": "🧪 Docksentry test '
                        'message from Web UI (webhook)"}']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'test_webhook: kind=webhook url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"event": "message", "text": "🧪 Docksentry test '
                        'message from Web UI (webhook)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'test_webhook: kind=webhook url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"event": "message", "text": "🧪 Docksentry test '
                        'message from Web UI (webhook)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'test_webhook: kind=discord url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=discord&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"content": "🧪 Docksentry test message from Web UI '
                        '(discord)"}']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "discord", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'test_webhook: kind=discord url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=discord&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"content": "🧪 Docksentry test message from Web UI '
                        '(discord)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "discord", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'test_webhook: kind=discord url=https://hook.invalid/ok'): {'steps': [{'do': 'POST /api/test_webhook kind=discord&url=https://hook.invalid/ok',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook ok> {"content": "🧪 Docksentry test message from Web UI '
                        '(discord)"}']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "discord", "url": "https://hook.invalid/ok"}, "source": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'test_webhook: kind=webhook url=https://hook.invalid/404'): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=https://hook.invalid/404',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook 404> {"event": "message", "text": "🧪 Docksentry test '
                        'message from Web UI (webhook)"}',
                        '<log> Webhook error: HTTP 404']}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook", "url": "https://hook.invalid/404"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('one', 'test_webhook: kind=webhook url=https://hook.invalid/404'): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=https://hook.invalid/404',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook 404> {"event": "message", "text": "🧪 Docksentry test '
                        'message from Web UI (webhook)"}',
                        '<log> Webhook error: HTTP 404']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook", "url": "https://hook.invalid/404"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('headless', 'test_webhook: kind=webhook url=https://hook.invalid/404'): {'steps': [{'do': 'POST /api/test_webhook kind=webhook&url=https://hook.invalid/404',
                'http': '200 | Content-Type: application/json | body: {"ok": true}',
                'out': ['<webhook 404> {"event": "message", "text": "🧪 Docksentry test '
                        'message from Web UI (webhook)"}',
                        '<log> Webhook error: HTTP 404']}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/test_webhook", "actor": "admin", "detail": '
                         '{"kind": "webhook", "url": "https://hook.invalid/404"}, '
                         '"source": "web"}'],
               'backups': 1}},
    # ── backup_import ───────────────────────────────────────────────
    # :3148. Declared length over cap + 8 KB → 413, body over cap → 413, not
    # JSON / no schema_version → 400, else backup.restore.
    ('multi', 'backup_import: a backup'): {'steps': [{'do': "POST /api/backup_import <308 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '200 | Content-Type: application/json | body: {"errors": [], '
                        '"from_version": "x", "links_dropped": 0, "ok": true, "restored": '
                        '["pinned", "notes"], "schema_version": 1}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "308", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'files': {'container_notes.json': '{"web": "hello"}',
                         'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('one', 'backup_import: a backup'): {'steps': [{'do': "POST /api/backup_import <308 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '200 | Content-Type: application/json | body: {"errors": [], '
                        '"from_version": "x", "links_dropped": 0, "ok": true, "restored": '
                        '["pinned", "notes"], "schema_version": 1}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "308", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'files': {'container_notes.json': '{"web": "hello"}',
                         'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('headless', 'backup_import: a backup'): {'steps': [{'do': "POST /api/backup_import <308 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '200 | Content-Type: application/json | body: {"errors": [], '
                        '"from_version": "x", "links_dropped": 0, "ok": true, "restored": '
                        '["pinned", "notes"], "schema_version": 1}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "308", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'files': {'container_notes.json': '{"web": "hello"}',
                         'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('multi', 'backup_import: plain JSON body'): {'steps': [{'do': "POST /api/backup_import <154 bytes> ctype='application/json'",
                'http': '200 | Content-Type: application/json | body: {"errors": [], '
                        '"from_version": "x", "links_dropped": 0, "ok": true, "restored": '
                        '["pinned", "notes"], "schema_version": 1}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "154", "content_type": "application/json"}, "source": '
                         '"web"}'],
               'files': {'container_notes.json': '{"web": "hello"}',
                         'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('one', 'backup_import: plain JSON body'): {'steps': [{'do': "POST /api/backup_import <154 bytes> ctype='application/json'",
                'http': '200 | Content-Type: application/json | body: {"errors": [], '
                        '"from_version": "x", "links_dropped": 0, "ok": true, "restored": '
                        '["pinned", "notes"], "schema_version": 1}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "154", "content_type": "application/json"}, "source": '
                         '"web"}'],
               'files': {'container_notes.json': '{"web": "hello"}',
                         'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('headless', 'backup_import: plain JSON body'): {'steps': [{'do': "POST /api/backup_import <154 bytes> ctype='application/json'",
                'http': '200 | Content-Type: application/json | body: {"errors": [], '
                        '"from_version": "x", "links_dropped": 0, "ok": true, "restored": '
                        '["pinned", "notes"], "schema_version": 1}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "154", "content_type": "application/json"}, "source": '
                         '"web"}'],
               'files': {'container_notes.json': '{"web": "hello"}',
                         'pinned_containers.json': '["db"]'},
               'backups': 1}},
    ('multi', 'backup_import: not JSON'): {'steps': [{'do': "POST /api/backup_import <170 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'file is not JSON, so it is not a Docksentry backup. Expecting '
                        'value: line 1 column 1 (char 0)", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "170", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('one', 'backup_import: not JSON'): {'steps': [{'do': "POST /api/backup_import <170 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'file is not JSON, so it is not a Docksentry backup. Expecting '
                        'value: line 1 column 1 (char 0)", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "170", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('headless', 'backup_import: not JSON'): {'steps': [{'do': "POST /api/backup_import <170 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'file is not JSON, so it is not a Docksentry backup. Expecting '
                        'value: line 1 column 1 (char 0)", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "170", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('multi', 'backup_import: a JSON list'): {'steps': [{'do': "POST /api/backup_import <160 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'is JSON, but not a Docksentry backup — no schema_version in it.", '
                        '"ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "160", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('one', 'backup_import: a JSON list'): {'steps': [{'do': "POST /api/backup_import <160 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'is JSON, but not a Docksentry backup — no schema_version in it.", '
                        '"ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "160", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('headless', 'backup_import: a JSON list'): {'steps': [{'do': "POST /api/backup_import <160 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'is JSON, but not a Docksentry backup — no schema_version in it.", '
                        '"ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "160", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('multi', 'backup_import: no schema_version'): {'steps': [{'do': "POST /api/backup_import <183 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'is JSON, but not a Docksentry backup — no schema_version in it.", '
                        '"ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "183", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('one', 'backup_import: no schema_version'): {'steps': [{'do': "POST /api/backup_import <183 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'is JSON, but not a Docksentry backup — no schema_version in it.", '
                        '"ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "183", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('headless', 'backup_import: no schema_version'): {'steps': [{'do': "POST /api/backup_import <183 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary'",
                'http': '400 | Content-Type: application/json | body: {"error": "⚠️ That '
                        'is JSON, but not a Docksentry backup — no schema_version in it.", '
                        '"ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "detail": '
                         '{"bytes": "183", "content_type": "multipart/form-data"}, '
                         '"source": "web"}'],
               'backups': 1}},
    ('multi', 'backup_import: declared too large'): {'steps': [{'do': "POST /api/backup_import <308 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary' length=3145728",
                'http': '413 | Content-Type: application/json | body: {"error": "⚠️ 3.0 MB '
                        'is too large for a backup file. Nothing was downloaded. The limit '
                        'is 2 MB.", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'backup_import: declared too large'): {'steps': [{'do': "POST /api/backup_import <308 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary' length=3145728",
                'http': '413 | Content-Type: application/json | body: {"error": "⚠️ 3.0 MB '
                        'is too large for a backup file. Nothing was downloaded. The limit '
                        'is 2 MB.", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'backup_import: declared too large'): {'steps': [{'do': "POST /api/backup_import <308 bytes> ctype='multipart/form-data; "
                      "boundary=matrixboundary' length=3145728",
                'http': '413 | Content-Type: application/json | body: {"error": "⚠️ 3.0 MB '
                        'is too large for a backup file. Nothing was downloaded. The limit '
                        'is 2 MB.", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('multi', 'backup_import: body over the cap'): {'steps': [{'do': "POST /api/backup_import <2097162 bytes> ctype='application/json'",
                'http': '413 | Content-Type: application/json | body: {"error": "⚠️ 2.0 MB '
                        'is too large for a backup file. Nothing was downloaded. The limit '
                        'is 2 MB.", "ok": false}'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('one', 'backup_import: body over the cap'): {'steps': [{'do': "POST /api/backup_import <2097162 bytes> ctype='application/json'",
                'http': '413 | Content-Type: application/json | body: {"error": "⚠️ 2.0 MB '
                        'is too large for a backup file. Nothing was downloaded. The limit '
                        'is 2 MB.", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    ('headless', 'backup_import: body over the cap'): {'steps': [{'do': "POST /api/backup_import <2097162 bytes> ctype='application/json'",
                'http': '413 | Content-Type: application/json | body: {"error": "⚠️ 2.0 MB '
                        'is too large for a backup file. Nothing was downloaded. The limit '
                        'is 2 MB.", "ok": false}'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/backup_import", "actor": "admin", "source": '
                         '"web"}'],
               'backups': 1}},
    # ── wizard ──────────────────────────────────────────────────────
    # :3082. Password or a deliberate no_password, then language, cron,
    # valid webhooks only, auto_mode=all saves every local container,
    # ui_mode=simple, setup done.
    ('multi', 'wizard: passwords differ'): {'steps': [{'do': 'POST /api/wizard web_password=a&web_password_confirm=b',
                'http': '303 | Location: /setup?pw=1'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"web_password": "<redacted:1>", "web_password_confirm": '
                         '"<redacted:1>"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'wizard: passwords differ'): {'steps': [{'do': 'POST /api/wizard web_password=a&web_password_confirm=b',
                'http': '303 | Location: /setup?pw=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"web_password": "<redacted:1>", "web_password_confirm": '
                         '"<redacted:1>"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'wizard: passwords differ'): {'steps': [{'do': 'POST /api/wizard web_password=a&web_password_confirm=b',
                'http': '303 | Location: /setup?pw=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"web_password": "<redacted:1>", "web_password_confirm": '
                         '"<redacted:1>"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'wizard: no password, all auto'): {'steps': [{'do': 'POST /api/wizard no_password=1&language=de&cron_schedule=0 5 * * '
                      '*&auto_mode=all&webhook_url=ftp://x&discord_webhook=https://example.org/x',
                'http': '303 | Location: /?saved=1',
                'hosts': {'local': 'ps -q, inspect, image inspect'}}],
     'state': {'pending': ['web@local', 'web@nas'],
               'telegram_says': '*Update-Ergebnis:*',
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"auto_mode": "all", "cron_schedule": "0 5 * * *", '
                         '"discord_webhook": "<redacted:21>", "language": "de", '
                         '"no_password": "<redacted:1>", "webhook_url": "<redacted:7>"}, '
                         '"source": "web"}'],
               'files': {'autoupdate_containers.json': '["docksentry", "web", "db"]',
                         'settings.json': '{"cron_schedule": "0 5 * * *", "language": '
                                          '"de", "ui_mode": "simple", "web_setup_done": '
                                          'true}'},
               'backups': 1}},
    ('one', 'wizard: no password, all auto'): {'steps': [{'do': 'POST /api/wizard no_password=1&language=de&cron_schedule=0 5 * * '
                      '*&auto_mode=all&webhook_url=ftp://x&discord_webhook=https://example.org/x',
                'http': '303 | Location: /?saved=1',
                'hosts': {'local': 'ps -q, inspect, image inspect'}}],
     'state': {'pending': ['web@local'],
               'telegram_says': '*Update-Ergebnis:*',
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"auto_mode": "all", "cron_schedule": "0 5 * * *", '
                         '"discord_webhook": "<redacted:21>", "language": "de", '
                         '"no_password": "<redacted:1>", "webhook_url": "<redacted:7>"}, '
                         '"source": "web"}'],
               'files': {'autoupdate_containers.json': '["docksentry", "web", "db"]',
                         'settings.json': '{"cron_schedule": "0 5 * * *", "language": '
                                          '"de", "ui_mode": "simple", "web_setup_done": '
                                          'true}'},
               'backups': 1}},
    ('headless', 'wizard: no password, all auto'): {'steps': [{'do': 'POST /api/wizard no_password=1&language=de&cron_schedule=0 5 * * '
                      '*&auto_mode=all&webhook_url=ftp://x&discord_webhook=https://example.org/x',
                'http': '303 | Location: /?saved=1',
                'hosts': {'local': 'ps -q, inspect, image inspect'}}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"auto_mode": "all", "cron_schedule": "0 5 * * *", '
                         '"discord_webhook": "<redacted:21>", "language": "de", '
                         '"no_password": "<redacted:1>", "webhook_url": "<redacted:7>"}, '
                         '"source": "web"}'],
               'files': {'autoupdate_containers.json': '["docksentry", "web", "db"]',
                         'settings.json': '{"cron_schedule": "0 5 * * *", "language": '
                                          '"de", "ui_mode": "simple", "web_setup_done": '
                                          'true}'},
               'backups': 1}},
    ('multi', 'wizard: password and channels'): {'steps': [{'do': 'POST /api/wizard '
                      'web_password=pw&web_password_confirm=pw&web_username= bob '
                      '&language=xx&webhook_url=https://hook.invalid/ok&auto_mode=picky',
                'http': '303 | Location: /?saved=1'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"auto_mode": "picky", "language": "xx", "web_password": '
                         '"<redacted:2>", "web_password_confirm": "<redacted:2>", '
                         '"web_username": " bob ", "webhook_url": "<redacted:23>"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"ui_mode": "simple", "web_password": '
                                          '"<scrypt>", "web_setup_done": true, '
                                          '"web_username": "bob"}'},
               'backups': 1}},
    ('one', 'wizard: password and channels'): {'steps': [{'do': 'POST /api/wizard '
                      'web_password=pw&web_password_confirm=pw&web_username= bob '
                      '&language=xx&webhook_url=https://hook.invalid/ok&auto_mode=picky',
                'http': '303 | Location: /?saved=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"auto_mode": "picky", "language": "xx", "web_password": '
                         '"<redacted:2>", "web_password_confirm": "<redacted:2>", '
                         '"web_username": " bob ", "webhook_url": "<redacted:23>"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"ui_mode": "simple", "web_password": '
                                          '"<scrypt>", "web_setup_done": true, '
                                          '"web_username": "bob"}'},
               'backups': 1}},
    ('headless', 'wizard: password and channels'): {'steps': [{'do': 'POST /api/wizard '
                      'web_password=pw&web_password_confirm=pw&web_username= bob '
                      '&language=xx&webhook_url=https://hook.invalid/ok&auto_mode=picky',
                'http': '303 | Location: /?saved=1'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/wizard", "actor": "admin", "detail": '
                         '{"auto_mode": "picky", "language": "xx", "web_password": '
                         '"<redacted:2>", "web_password_confirm": "<redacted:2>", '
                         '"web_username": " bob ", "webhook_url": "<redacted:23>"}, '
                         '"source": "web"}'],
               'files': {'settings.json': '{"ui_mode": "simple", "web_password": '
                                          '"<scrypt>", "web_setup_done": true, '
                                          '"web_username": "bob"}'},
               'backups': 1}},
    # ── ui_mode ─────────────────────────────────────────────────────
    # :3051.
    ('multi', 'ui_mode: simple'): {'steps': [{'do': "POST /api/ui_mode mode=simple referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/ui_mode", "actor": "admin", "detail": {"mode": '
                         '"simple"}, "source": "web"}'],
               'files': {'settings.json': '{"ui_mode": "simple"}'},
               'backups': 1}},
    ('one', 'ui_mode: simple'): {'steps': [{'do': "POST /api/ui_mode mode=simple referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ui_mode", "actor": "admin", "detail": {"mode": '
                         '"simple"}, "source": "web"}'],
               'files': {'settings.json': '{"ui_mode": "simple"}'},
               'backups': 1}},
    ('headless', 'ui_mode: simple'): {'steps': [{'do': "POST /api/ui_mode mode=simple referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ui_mode", "actor": "admin", "detail": {"mode": '
                         '"simple"}, "source": "web"}'],
               'files': {'settings.json': '{"ui_mode": "simple"}'},
               'backups': 1}},
    ('multi', 'ui_mode: bogus'): {'steps': [{'do': "POST /api/ui_mode mode=bogus referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/ui_mode", "actor": "admin", "detail": {"mode": '
                         '"bogus"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'ui_mode: bogus'): {'steps': [{'do': "POST /api/ui_mode mode=bogus referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ui_mode", "actor": "admin", "detail": {"mode": '
                         '"bogus"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'ui_mode: bogus'): {'steps': [{'do': "POST /api/ui_mode mode=bogus referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/ui_mode", "actor": "admin", "detail": {"mode": '
                         '"bogus"}, "source": "web"}'],
               'backups': 1}},
    # ── env_adopt ───────────────────────────────────────────────────
    # :3062 → config.adopt_env_value.
    ('multi', 'env_adopt: language after saving de'): {'steps': [{'do': 'POST /settings language=de',
                'http': '303 | Location: /settings?saved=1'},
               {'do': "POST /api/env_adopt key=language referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'telegram_says': '*Update-Ergebnis:*',
               'audit': ['{"action": "/settings", "actor": "admin", "detail": {"language": '
                         '"de"}, "source": "web"}',
                         '{"action": "/api/env_adopt", "actor": "admin", "detail": {"key": '
                         '"language"}, "source": "web"}'],
               'files': {'settings.json': '{"language": "de", "monitor_enabled": false}'},
               'backups': 1}},
    ('one', 'env_adopt: language after saving de'): {'steps': [{'do': 'POST /settings language=de',
                'http': '303 | Location: /settings?saved=1'},
               {'do': "POST /api/env_adopt key=language referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'telegram_says': '*Update-Ergebnis:*',
               'audit': ['{"action": "/settings", "actor": "admin", "detail": {"language": '
                         '"de"}, "source": "web"}',
                         '{"action": "/api/env_adopt", "actor": "admin", "detail": {"key": '
                         '"language"}, "source": "web"}'],
               'files': {'settings.json': '{"language": "de", "monitor_enabled": false}'},
               'backups': 1}},
    ('headless', 'env_adopt: language after saving de'): {'steps': [{'do': 'POST /settings language=de',
                'http': '303 | Location: /settings?saved=1'},
               {'do': "POST /api/env_adopt key=language referer='/settings'",
                'http': '303 | Location: /settings'}],
     'state': {'pending': ['web@local'],
               'telegram_says': '*Update-Ergebnis:*',
               'audit': ['{"action": "/settings", "actor": "admin", "detail": {"language": '
                         '"de"}, "source": "web"}',
                         '{"action": "/api/env_adopt", "actor": "admin", "detail": {"key": '
                         '"language"}, "source": "web"}'],
               'files': {'settings.json': '{"language": "de", "monitor_enabled": false}'},
               'backups': 1}},
    ('multi', 'env_adopt: nosuch'): {'steps': [{'do': 'POST /api/env_adopt key=nosuch', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/env_adopt", "actor": "admin", "detail": {"key": '
                         '"nosuch"}, "source": "web"}'],
               'backups': 1}},
    ('one', 'env_adopt: nosuch'): {'steps': [{'do': 'POST /api/env_adopt key=nosuch', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/env_adopt", "actor": "admin", "detail": {"key": '
                         '"nosuch"}, "source": "web"}'],
               'backups': 1}},
    ('headless', 'env_adopt: nosuch'): {'steps': [{'do': 'POST /api/env_adopt key=nosuch', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/env_adopt", "actor": "admin", "detail": {"key": '
                         '"nosuch"}, "source": "web"}'],
               'backups': 1}},
    ('multi', 'env_adopt: no key, no Referer'): {'steps': [{'do': 'POST /api/env_adopt referer=None', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'audit': ['{"action": "/api/env_adopt", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('one', 'env_adopt: no key, no Referer'): {'steps': [{'do': 'POST /api/env_adopt referer=None', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/env_adopt", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    ('headless', 'env_adopt: no key, no Referer'): {'steps': [{'do': 'POST /api/env_adopt referer=None', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'audit': ['{"action": "/api/env_adopt", "actor": "admin", "source": "web"}'],
               'backups': 1}},
    # ── GET /api/check ──────────────────────────────────────────────
    # :2172 → `_api_check` (:7270) on a thread: skipped while updating or
    # checking, else every host's check_all, a raise printed per host, then
    # notify_updates. No CSRF check on a GET.
    ('multi', 'GET /api/check'): {'steps': [{'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ["<log> Web UI check error on dead: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '--format', '{{.Names}}|{{.Image}}']' timed "
                        'out after 30 seconds',
                        '<log> Web UI check error on refused: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Connection '
                        'refused',
                        '<log> Web UI check error on flaky: registry did not answer in 15s',
                        '<telegram> 🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01  [4 buttons]',
                        '<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}, {"compose": '
                        'false, "created": "2026-01-01", "image": "nginx:1.25", '
                        '"na…#56a7491d0c'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=5, inspect '
                                   '--format t=10, inspect --format t=5',
                          'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                 'inspect --format ×2, inspect --format t=10, inspect '
                                 '--format t=5',
                          'dead': 'ps --format',
                          'refused': 'ps --format'},
                'events': ['check_all@local',
                           'check_all@nas',
                           'check_all@dead',
                           'check_all@refused',
                           'check_all@flaky']}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'GET /api/check'): {'steps': [{'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<telegram> 🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
                        '<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}], "count": 1, '
                        '"event": "updates_available"}'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=10, inspect '
                                   '--format t=5'},
                'events': ['check_all@local']}],
     'state': {'pending': ['web@local']}},
    ('headless', 'GET /api/check'): {'steps': [{'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}], "count": 1, '
                        '"event": "updates_available"}'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=10, inspect '
                                   '--format t=5'},
                'events': ['check_all@local']}],
     'state': {'pending': ['web@local']}},
    ('multi', 'GET /api/check: update lock held'): {'steps': [{'do': 'lock'},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<log> Web UI check skipped: an update is currently running']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'GET /api/check: update lock held'): {'steps': [{'do': 'lock'},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<log> Web UI check skipped: an update is currently running']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'GET /api/check: update lock held'): {'steps': [{'do': 'lock'},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<log> Web UI check skipped: an update is currently running']},
               {'do': 'unlock'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'GET /api/check: check lock held'): {'steps': [{'do': 'check_lock'},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<log> Web UI check skipped: a check is already running']},
               {'do': 'check_unlock'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'GET /api/check: check lock held'): {'steps': [{'do': 'check_lock'},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<log> Web UI check skipped: a check is already running']},
               {'do': 'check_unlock'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'GET /api/check: check lock held'): {'steps': [{'do': 'check_lock'},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<log> Web UI check skipped: a check is already running']},
               {'do': 'check_unlock'}],
     'state': {'pending': ['web@local']}},
    ('multi', 'GET /api/check twice'): {'steps': [{'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ["<log> Web UI check error on dead: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '--format', '{{.Names}}|{{.Image}}']' timed "
                        'out after 30 seconds',
                        '<log> Web UI check error on refused: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Connection '
                        'refused',
                        '<log> Web UI check error on flaky: registry did not answer in 15s',
                        '<telegram> 🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01  [4 buttons]',
                        '<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}, {"compose": '
                        'false, "created": "2026-01-01", "image": "nginx:1.25", '
                        '"na…#56a7491d0c'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=5, inspect '
                                   '--format t=10, inspect --format t=5',
                          'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                 'inspect --format ×2, inspect --format t=10, inspect '
                                 '--format t=5',
                          'dead': 'ps --format',
                          'refused': 'ps --format'},
                'events': ['check_all@local',
                           'check_all@nas',
                           'check_all@dead',
                           'check_all@refused',
                           'check_all@flaky']},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<log> Web UI check error on dead: no answer within 30 seconds '
                        '(from the last try, not asked again for 60s)',
                        '<log> Web UI check error on refused: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Connection '
                        'refused (from the last try, not asked again for 60s)',
                        '<log> Web UI check error on flaky: registry did not answer in 15s',
                        '<telegram> 🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01  [4 buttons]',
                        '<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}, {"compose": '
                        'false, "created": "2026-01-01", "image": "nginx:1.25", '
                        '"na…#56a7491d0c'],
                'hosts': {'local': 'ps --format, inspect t=60, inspect --format ×2, '
                                   'inspect --format t=10, inspect --format t=5',
                          'nas': 'ps --format, inspect t=60, inspect --format ×2, inspect '
                                 '--format t=10, inspect --format t=5'},
                'events': ['check_all@local', 'check_all@nas', 'check_all@flaky']}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'GET /api/check twice'): {'steps': [{'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<telegram> 🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
                        '<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}], "count": 1, '
                        '"event": "updates_available"}'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=10, inspect '
                                   '--format t=5'},
                'events': ['check_all@local']},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<telegram> 🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
                        '<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}], "count": 1, '
                        '"event": "updates_available"}'],
                'hosts': {'local': 'ps --format, inspect t=60, inspect --format ×2, '
                                   'inspect --format t=10, inspect --format t=5'},
                'events': ['check_all@local']}],
     'state': {'pending': ['web@local']}},
    ('headless', 'GET /api/check twice'): {'steps': [{'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}], "count": 1, '
                        '"event": "updates_available"}'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=10, inspect '
                                   '--format t=5'},
                'events': ['check_all@local']},
               {'do': 'GET /api/check',
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}], "count": 1, '
                        '"event": "updates_available"}'],
                'hosts': {'local': 'ps --format, inspect t=60, inspect --format ×2, '
                                   'inspect --format t=10, inspect --format t=5'},
                'events': ['check_all@local']}],
     'state': {'pending': ['web@local']}},
    ('multi', 'GET /api/check: cross-origin'): {'steps': [{'do': "GET /api/check origin='http://evil.example'",
                'http': '303 | Location: /',
                'out': ["<log> Web UI check error on dead: Command '['docker', '-H', "
                        "'ssh://dead', 'ps', '--format', '{{.Names}}|{{.Image}}']' timed "
                        'out after 30 seconds',
                        '<log> Web UI check error on refused: could not list containers '
                        '(rc=255): ssh: connect to host 192.0.2.30 port 22: Connection '
                        'refused',
                        '<log> Web UI check error on flaky: registry did not answer in 15s',
                        '<telegram> 🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01\n'
                        '• [web](https://github.com/example/web/releases/latest) @nas '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01  [4 buttons]',
                        '<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}, {"compose": '
                        'false, "created": "2026-01-01", "image": "nginx:1.25", '
                        '"na…#56a7491d0c'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=5, inspect '
                                   '--format t=10, inspect --format t=5',
                          'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                 'inspect --format ×2, inspect --format t=10, inspect '
                                 '--format t=5',
                          'dead': 'ps --format',
                          'refused': 'ps --format'},
                'events': ['check_all@local',
                           'check_all@nas',
                           'check_all@dead',
                           'check_all@refused',
                           'check_all@flaky']}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'GET /api/check: cross-origin'): {'steps': [{'do': "GET /api/check origin='http://evil.example'",
                'http': '303 | Location: /',
                'out': ['<telegram> 🔄 *Docker Updates Available*\n'
                        '\n'
                        '• [web](https://github.com/example/web/releases/latest) '
                        '(nginx:1.25) 🔖 v1.25.0 → v1.26.0\n'
                        '  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
                        '<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}], "count": 1, '
                        '"event": "updates_available"}'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=10, inspect '
                                   '--format t=5'},
                'events': ['check_all@local']}],
     'state': {'pending': ['web@local']}},
    ('headless', 'GET /api/check: cross-origin'): {'steps': [{'do': "GET /api/check origin='http://evil.example'",
                'http': '303 | Location: /',
                'out': ['<webhook ok> {"containers": [{"compose": false, "created": '
                        '"2026-01-01", "image": "nginx:1.25", "name": "web", '
                        '"new_version": "1.26.0", "old_version": "1.25.0", "size": "10 '
                        'MB", "source_url": '
                        '"https://github.com/example/web/releases/latest"}], "count": 1, '
                        '"event": "updates_available"}'],
                'hosts': {'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, '
                                   'inspect --format ×2, inspect --format t=10, inspect '
                                   '--format t=5'},
                'events': ['check_all@local']}],
     'state': {'pending': ['web@local']}},
    # ── GET /api/wizard_skip ────────────────────────────────────────
    # :2175, only with a password set.
    ('multi', 'GET /api/wizard_skip'): {'steps': [{'do': 'GET /api/wizard_skip', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local', 'web@nas'],
               'files': {'settings.json': '{"web_setup_done": true}'}}},
    ('one', 'GET /api/wizard_skip'): {'steps': [{'do': 'GET /api/wizard_skip', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'files': {'settings.json': '{"web_setup_done": true}'}}},
    ('headless', 'GET /api/wizard_skip'): {'steps': [{'do': 'GET /api/wizard_skip', 'http': '303 | Location: /'}],
     'state': {'pending': ['web@local'],
               'files': {'settings.json': '{"web_setup_done": true}'}}},
    ('multi', 'GET /api/wizard_skip: no password'): {'steps': [{'do': 'config web_password '},
               {'do': 'GET /api/wizard_skip auth=None', 'http': '303 | Location: /setup'}],
     'state': {'pending': ['web@local', 'web@nas']}},
    ('one', 'GET /api/wizard_skip: no password'): {'steps': [{'do': 'config web_password '},
               {'do': 'GET /api/wizard_skip auth=None', 'http': '303 | Location: /setup'}],
     'state': {'pending': ['web@local']}},
    ('headless', 'GET /api/wizard_skip: no password'): {'steps': [{'do': 'config web_password '},
               {'do': 'GET /api/wizard_skip auth=None', 'http': '303 | Location: /setup'}],
     'state': {'pending': ['web@local']}},
}
# ── end of the generated table ──


if __name__ == "__main__":
    sys.exit(main())
