#!/usr/bin/env python3
"""Telegram command matrix: what every typed command answers today, through the real `_handle_message`.

The core split moves the whole `_handle_message` elif chain into a route
table (D7), then into exact command-word matching (D9), and changes how a
dead or refused host is reported on the way (W3, W4). A move that sends
one spelling down a different branch shows up nowhere else: every older
procedure drives one command at a time with its own fakes. This one
drives every command word in every argument form against a real
`TelegramBot` and compares what came back with a table taken from main
before anything moved.

Three set-ups, same rows in each:

  * `multi`  — a five-host registry: `local` and `nas` answer, `dead`
               raises TimeoutExpired on every call, `refused` returns
               rc=255 with ssh's "Connection refused", `flaky` lists its
               containers but its checker's `check_all` raises.
  * `none`   — no registry at all (`hosts=None`).
  * `one`    — a registry holding only the local host.

Per row the table holds the transport log (EN reply texts in send order,
long ones as head plus a sha1), which host backends were called and with
what, the environment events, and the exception `_handle_message` raised,
if any. Rows that raise today are listed in KNOWN_DEFECTS with the slice
that is meant to flip them; they are pinned as they are, so the flip is
seen when it happens rather than assumed.

What is real: Config (from a scrubbed environment, data dir in a tmp
dir), ContainerStore and the per-host HostScopedStore, the UpdateEngine,
`_check_auth` (the chat id matches), the i18n tables, `run_updates` and
the update batch, lifecycle, container_flags, selfcheck, the
UpdateChecker apart from the methods listed below, and every thread a
command starts (started for real and joined after the row).

Environment stand-ins, and only these:

  * the container CLI: `MatrixBackend.run` answers ps / inspect / logs /
    lifecycle from a fixed container list per host and records
    (argv, timeout); `container_backend._default_backend` is the local
    one, so the static self-detection helpers ask it too;
  * the Telegram transport: `send_message`, `api_call`, `answer_callback`,
    `remove_buttons` and `send_document` (which calls urlopen itself)
    are recorders;
  * `selfrestart.go_down` (it SIGTERMs this process) and `selfupdate.run`
    (it pulls an image and starts a helper container) are recorders;
  * `container_backend.RemoteBackend` and `hostcheck.preflight` as
    `_hostcheck_text` uses them (`/hosts check …` would dial out);
  * `changelog.fetch` (GitHub) answers "offline";
  * `UpdateChecker._own_id_candidates` returns HOSTNAME only (the real
    one also reads /etc/hostname, which is this machine's name);
  * on the checker: `check_all` lists through the real
    `get_running_containers` and reports a fixed set as outdated instead
    of asking a registry, `update_container` records instead of
    recreating, `has_selfupdate_available` answers False.

`subprocess.run`, `subprocess.Popen`, `urllib.request.urlopen`,
`socket.create_connection` and `socket.socket.connect` / `connect_ex`
are patched to raise and to record the call, so any of these that gets
past the stand-ins turns its row red — whatever the code around it does
with the exception. Those are the only exits trapped: `os.kill`,
`os.exec*`, `os.system` and DNS lookups are not. The first checks drive
the trap on purpose, one per patched call, with the exception
swallowed, so a trap gone blind is red too.

`none` and `one` share one `single` entry per row while they agree,
which they do on main; each still gets its own PASS line.

`python3 scripts/test_telegram_dispatch_matrix.py --snapshot [--app DIR]`
prints a fresh expectation table (from DIR's app code, default this
tree's). The committed table was generated from main (8a74ba7) and then
read row by row against the branch each one lands in. The ten rows W3
flipped (a plain name with a dead or refused host among the targets)
come from core-split after W3, as do the four `/check web @dead` and
`/update web @dead` rows added with it; W3_SPEC states by hand what
they say. The seven glob rows W4 flipped, and `/update web* @dead` and
`/update web* @all` added with it, come from core-split after W4 and
are checked against W4_SPEC. W7 gave a read's lookup (/status and
/check with a name or glob) a ten-second probe on remote hosts: the 18
rows where that shows (`ps -a --format t=10`, and "no answer within 10
seconds" where dead is named) come from core-split after W7, and
nothing else in the table moved. Each row starts with an empty
dead-host memory; what a read leaves in it for the next command is
the business of `w7_checks` — two commands on one bot, written by hand.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _app_dir():
    if "--app" in sys.argv:
        return os.path.abspath(sys.argv[sys.argv.index("--app") + 1])
    return os.path.join(ROOT, "app")


APP = _app_dir()
sys.path.insert(0, APP)

from container_backend import ContainerBackend         # noqa: E402

# ── escapes: anything reaching the real world is recorded and refused ──
ESCAPES = []


class Escape(Exception):
    """Raised by the patched subprocess / urlopen / socket connect."""


def _escape(kind):
    def _f(*a, **kw):
        what = a[0] if a else kw.get("args", kw.get("url", "?"))
        if hasattr(what, "full_url"):
            what = what.full_url
        ESCAPES.append(f"{kind}({what!r})"[:200])
        raise Escape(f"{kind} escaped the matrix: {what!r}"[:200])
    return _f


def _escape_method(kind):
    """The same trap for a method (`socket.socket.connect`): the
    address is the first argument after the socket itself."""
    trap = _escape(kind)

    def _m(self, *a, **kw):
        return trap(*a, **kw)
    return _m


# ── threads: started for real, joined after the row ────────────────────
STARTED = []
THREAD_ERRORS = []
_RealThread = threading.Thread


class _TrackedThread(_RealThread):
    def start(self):
        STARTED.append(self)
        return super().start()


def _excepthook(args):
    THREAD_ERRORS.append(f"{args.exc_type.__name__} in thread")


# ── a scrubbed environment for Config.from_env ─────────────────────────
_CONFIG_SRC = open(os.path.join(APP, "config.py"), encoding="utf-8").read()
_ENV_KEYS = sorted(set(re.findall(r'_env\(\s*"([A-Z0-9_]+)"', _CONFIG_SRC)))
CHAT = "4242"
USER = "7"
SELF_ID = hashlib.sha256(b"docksentry").hexdigest()


def _scrub_env(data_dir):
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update({
        "BOT_TOKEN": "123:matrix", "CHAT_ID": CHAT, "LANGUAGE": "en",
        "CONTAINER_CLI": "docker", "DATA_DIR": data_dir,
        "HOSTNAME": SELF_ID[:12],
    })


# ── the container CLI ──────────────────────────────────────────────────
class _CP(subprocess.CompletedProcess):
    pass


def _container(name, image, state="running", health="", labels=None,
               restart="unless-stopped"):
    cid = hashlib.sha256(name.encode()).hexdigest()
    if name == "docksentry":
        cid = SELF_ID
    st = {"Status": state, "Running": state == "running",
          "StartedAt": "", "ExitCode": 0 if state == "running" else 137,
          "RestartCount": 0}
    if health:
        st["Health"] = {"Status": health, "Log": [
            {"ExitCode": 0 if health == "healthy" else 1,
             "Output": f"probe says {health}"}]}
    return {
        "Id": cid, "Name": "/" + name, "Created": "2026-01-01T00:00:00Z",
        "Image": "sha256:" + hashlib.sha256(image.encode()).hexdigest(),
        "Config": {"Image": image, "Hostname": cid[:12],
                   "Labels": dict(labels or {}), "Env": []},
        "State": st,
        "HostConfig": {"RestartPolicy": {"Name": restart},
                       "PortBindings": {}, "Binds": []},
        "Mounts": [], "NetworkSettings": {"Networks": {}},
    }


def _lookup(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return "<no value>"
        cur = cur[part]
    return cur


_FIELD = re.compile(r"\{\{\s*(json\s+)?\.([\w.]+)\s*\}\}")
_INDEX = re.compile(r'\{\{\s*index\s+\.([\w.]+)\s+"([^"]*)"\s*\}\}')


def _render(fmt, obj):
    def one(m):
        v = _lookup(obj, m.group(2))
        if m.group(1):
            return json.dumps(v)
        return v if isinstance(v, str) else json.dumps(v)

    def index(m):
        v = _lookup(obj, m.group(1))
        # Labels is a map[string]string: a missing key is "", as in Go.
        v = v.get(m.group(2), "") if isinstance(v, dict) else v
        return v if isinstance(v, str) else json.dumps(v)
    out = _INDEX.sub(index, _FIELD.sub(one, fmt))
    return re.sub(r"\{\{.*?\}\}", "", out)          # range/if: empty


class MatrixBackend(ContainerBackend):
    """One host's CLI. Only `run` is replaced; the typed helpers (ps,
    inspect, logs, …) are the real ones, so their argv is the real argv."""

    cli_binary = "docker"
    #: What a refused host prints. W3's extra checks swap it for one row.
    refused_stderr = ("ssh: connect to host 192.0.2.30 port 22: "
                      "Connection refused")

    def __init__(self, name, containers, mode="ok"):
        self.name = name
        self.containers = {c["Name"][1:]: c for c in containers}
        self.mode = mode
        self.calls = []

    def _find(self, ref):
        if ref in self.containers:
            return self.containers[ref]
        for c in self.containers.values():
            if len(ref) >= 12 and c["Id"].startswith(ref):
                return c
        return None

    def run(self, args, *, timeout=None, text=True, input=None,
            capture_output=True, merge_stderr=False):
        a = [str(x) for x in args]
        self.calls.append((a, timeout))
        if self.mode == "dead":
            raise subprocess.TimeoutExpired(self.build(a), timeout or 30)
        if self.mode == "refused":
            return _CP(self.build(a), 255, "", self.refused_stderr)
        return self._answer(a)

    def _answer(self, a):
        ok = lambda out="": _CP(a, 0, out, "")                     # noqa: E731
        fmt = a[a.index("--format") + 1] if "--format" in a else None
        verb = a[0] if a else ""
        if verb == "ps":
            rows = [c for c in self.containers.values()
                    if "-a" in a or c["State"]["Running"]]
            if "-q" in a:
                full = "--no-trunc" in a
                return ok("\n".join(c["Id"] if full else c["Id"][:12]
                                    for c in rows))
            view = [{"Names": c["Name"][1:], "Image": c["Config"]["Image"],
                     "ID": c["Id"][:12], "State": c["State"]["Status"],
                     "Status": "Up 2 hours" if c["State"]["Running"]
                     else "Exited (137) 1 hour ago",
                     "Labels": ",".join(f"{k}={v}" for k, v in
                                        c["Config"]["Labels"].items())}
                    for c in rows]
            return ok("\n".join(_render(fmt or "{{.Names}}", v)
                                for v in view))
        if verb == "inspect":
            refs = [x for i, x in enumerate(a[1:], 1)
                    if not x.startswith("-") and a[i - 1] != "--format"]
            found = [self._find(r) for r in refs]
            if not refs or any(f is None for f in found):
                missing = [r for r, f in zip(refs, found) if f is None]
                return _CP(a, 1, "", "Error: No such object: "
                           + (missing[0] if missing else ""))
            if fmt is not None:
                return ok("\n".join(_render(fmt, f) for f in found))
            return ok(json.dumps(found))
        if verb == "logs":
            return ok("line one\nline two\n")
        if verb in ("start", "stop", "restart", "kill"):
            name = a[-1]
            if self._find(name) is None:
                return _CP(a, 1, "", f"Error: No such container: {name}")
            return ok(name + "\n")
        if verb == "image" and a[1:2] == ["prune"]:
            return ok("Total reclaimed space: 0B\n")
        if verb == "image" and a[1:2] == ["inspect"]:
            return ok("[]" if fmt is None else "")
        if verb == "system" and a[1:2] == ["df"]:
            return ok("")
        return ok("")


def _hosts_containers():
    src = {"org.opencontainers.image.source": "https://github.com/example/web"}
    return {
        "local": [
            _container("docksentry", "ghcr.io/example/docksentry:latest"),
            _container("web", "nginx:1.25", health="healthy", labels=src),
            _container("webdav", "bytemark/webdav:2.4", state="exited"),
            _container("db", "postgres:16"),
        ],
        "nas": [
            _container("web", "nginx:1.25", health="healthy", labels=src),
            _container("media", "jellyfin/jellyfin:10.9"),
        ],
        "flaky": [
            _container("web", "nginx:1.25", labels=src),
            _container("cache", "redis:7"),
        ],
        "dead": [],
        "refused": [],
    }


OUTDATED = {"local": {"web"}, "nas": {"web"}, "flaky": {"web"},
            "dead": set(), "refused": set()}


def _make_checker_class():
    from update_checker import UpdateChecker, ScanResult

    class MatrixChecker(UpdateChecker):
        """The real checker with its registry half replaced."""

        flaky = False
        outdated = frozenset()
        events = None

        def check_all(self, bot=None, only=None):
            self.events.append(f"check_all@{self.backend.name}"
                               + (f" only={sorted(only)}" if only else ""))
            if self.flaky:
                raise RuntimeError("registry did not answer in 15s")
            listed = self.get_running_containers()
            out = ScanResult()
            out.pin_list_unreadable = getattr(listed,
                                              "pin_list_unreadable", False)
            for c in listed:
                if c["name"] not in self.outdated:
                    continue
                if only is not None and c["name"] not in only:
                    continue
                out.append(dict(c, size="10 MB", created="2026-01-01",
                                old_version="1.25.0", new_version="1.26.0",
                                host=self.backend.name))
            return out

        def update_container(self, name, image, *a, **kw):
            self.events.append(f"update_container@{self.backend.name}:{name}")
            return True, "updated (stand-in)"

        def has_selfupdate_available(self):
            return False

        def get_highest_semver_tag(self, registry, repository, current_tag,
                                   *a, **kw):
            self.events.append(f"tags@{self.backend.name}:{repository}")
            return None, None

    return MatrixChecker


# ── one row ────────────────────────────────────────────────────────────
SETUPS = ("multi", "none", "one")
_PATCHED = False


def _patch_environment():
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    subprocess.run = _escape("subprocess.run")
    subprocess.Popen = _escape("subprocess.Popen")
    urllib.request.urlopen = _escape("urlopen")
    # hostcheck.speaks_tls, websocket_client and smtplib dial through
    # these; http.client and ssl end up in socket.connect as well.
    socket.create_connection = _escape("socket.create_connection")
    socket.socket.connect = _escape_method("socket.connect")
    socket.socket.connect_ex = _escape_method("socket.connect_ex")
    threading.Thread = _TrackedThread
    threading.excepthook = _excepthook

    import changelog
    import container_backend
    import hostcheck
    import selfrestart
    import selfupdate
    from update_checker import UpdateChecker

    changelog.fetch = lambda *a, **k: (
        EVENTS.append("changelog.fetch"),
        (False, "offline (stand-in)"))[1]
    selfrestart.go_down = lambda *a, **k: EVENTS.append("selfrestart.go_down")
    selfupdate.run = lambda ctx, target=None, reply_to=None: EVENTS.append(
        f"selfupdate.run target={target!r}")

    class _PreflightBackend:
        def __init__(self, endpoint, *, name=None, cli_binary="docker"):
            EVENTS.append(f"RemoteBackend({endpoint!r})")
            self.endpoint, self.name = endpoint, name

    container_backend.RemoteBackend = _PreflightBackend
    # Anything that builds its own local backend (the link resolver's
    # label reader on a single host, the bot's own __init__) gets the
    # row's local fake instead of a real DockerBackend.
    container_backend.get_backend = lambda config=None: LOCAL[0]
    hostcheck.preflight = lambda endpoint, backend, hint=None, **k: (
        EVENTS.append(f"hostcheck.preflight({endpoint!r})"),
        [("unreachable", {"endpoint": endpoint,
                          "error": "stand-in: not dialled"})])[1]
    UpdateChecker._own_id_candidates = staticmethod(
        lambda: [os.environ.get("HOSTNAME", "")])


EVENTS = []
LOCAL = [None]
#: Host name → backend mode, laid over the set-up's own for one row (W3's
#: single-host checks make `local` dead or refused). Empty otherwise.
MODE_OVERRIDE = {}
_TMP_ROOT = None


def _seed_pending(cfg, setup):
    rows = [{"name": "web", "image": "nginx:1.25", "host": "local",
             "old_version": "1.25.0", "new_version": "1.26.0"}]
    if setup == "multi":
        rows.append({"name": "web", "image": "nginx:1.25", "host": "nas",
                     "old_version": "1.25.0", "new_version": "1.26.0"})
    with open(cfg.pending_file, "w", encoding="utf-8") as f:
        json.dump(rows, f)


def build_world(setup, env=None):
    """Config, store and host registry for `setup`, with no bot yet.

    Returns (cfg, store, managed, registry, backends, data). `env` is laid
    over the scrubbed environment before Config reads it — the Discord
    matrix needs its credentials in there. Shared with the Discord
    matrix, so both drive the same hosts."""
    _patch_environment()
    from config import Config
    from container_store import ContainerStore, HostScopedStore
    from hosts import HostRegistry, ManagedHost
    import container_backend

    data = tempfile.mkdtemp(dir=_TMP_ROOT)
    _scrub_env(data)
    os.environ.update(env or {})
    cfg = Config.from_env()
    store = ContainerStore(cfg)
    checker_cls = _make_checker_class()
    conts = _hosts_containers()
    names = ["local", "nas", "dead", "refused", "flaky"] \
        if setup == "multi" else ["local"]
    backends, managed = {}, []
    for n in names:
        mode = MODE_OVERRIDE.get(n, n if n in ("dead", "refused") else "ok")
        b = MatrixBackend(n, conts[n], mode=mode)
        if n != "local":
            b.global_args = ("-H", f"ssh://{n}")
        ch = checker_cls(cfg, backend=b)
        ch.events = EVENTS
        ch.outdated = frozenset(OUTDATED[n])
        ch.flaky = n == "flaky"
        backends[n] = b
        if n == "local":
            LOCAL[0] = b
        managed.append(ManagedHost(
            n, b, ch, HostScopedStore(store, n),
            endpoint="" if n == "local" else f"ssh://{n}",
            is_local=n == "local"))
    registry = None if setup == "none" else HostRegistry(managed)
    container_backend._default_backend = backends["local"]
    return cfg, store, managed, registry, backends, data


def build(setup):
    """A real bot for `setup`. Returns (bot, local_checker, backends, log)."""
    from telegram_bot import TelegramBot
    cfg, store, managed, registry, backends, data = build_world(setup)
    bot = TelegramBot(cfg, store, hosts=registry)
    bot.backend = backends["local"]
    bot.start_time = time.time()
    bot.bot_username = "dsbot"
    log = []

    def send_message(text, reply_markup=None, auto=False, _held=False):
        kb = sum(len(r) for r in (reply_markup or {}).get(
            "inline_keyboard", [])) if reply_markup else 0
        log.append(("send", text, kb))
        return True

    def api_call(method, data=None, *a, **k):
        log.append(("api", method, 0))
        return {"ok": True, "result": {}}

    bot.send_message = send_message
    bot.api_call = api_call
    bot.answer_callback = lambda cid, text: log.append(("toast", text, 0))
    bot.remove_buttons = lambda chat_id, message_id: log.append(
        ("unbutton", "", 0))
    bot.send_document = lambda filename, data, caption="": (
        log.append(("document", caption, 0)), True)[1]
    _seed_pending(cfg, setup)
    return bot, managed[0].checker, backends, log, data


def _short(text):
    text = str(text)
    if len(text) <= 400:
        return text
    return text[:300] + "…#" + hashlib.sha1(text.encode()).hexdigest()[:10]


def _call_str(argv, timeout, keep=()):
    """argv as verb plus flags; a flag's value is dropped unless the flag
    is in `keep` (the Discord matrix keeps `--tail`)."""
    parts, skip = [], False
    for i, x in enumerate(argv):
        if skip:
            skip = False
            continue
        if i == 0:
            parts.append(x)
        elif x.startswith("-"):
            parts.append(x)
            skip = x in ("--format", "--tail", "--filter", "-f", "-t",
                         "--time") and x not in keep
        elif argv[i - 1] in keep:
            parts.append(x)
        elif i == 1 and argv[0] in ("image", "system"):
            parts.append(x)
    if timeout is not None:
        parts.append(f"t={timeout}")
    return " ".join(parts)


def _calls_summary(calls, keep=()):
    out, prev, n = [], None, 0
    for argv, timeout in calls:
        s = _call_str(argv, timeout, keep)
        if s == prev:
            n += 1
            continue
        if prev is not None:
            out.append(prev + (f" ×{n}" if n > 1 else ""))
        prev, n = s, 1
    if prev is not None:
        out.append(prev + (f" ×{n}" if n > 1 else ""))
    return ", ".join(out)


def run_row(setup, text):
    """Drive one typed command; return its observed record."""
    import hosts as hostslib
    hostslib.forget_unreachable()
    EVENTS.clear()
    del ESCAPES[:]
    del STARTED[:]
    del THREAD_ERRORS[:]
    bot, checker, backends, log, data = build(setup)
    record = _drive(bot, checker, backends, log, data, text)
    escapes = list(ESCAPES)
    shutil.rmtree(data, ignore_errors=True)
    return record, escapes


def run_seq(setup, texts, short=None):
    """Drive several typed commands one after the other on ONE bot, with
    the dead-host memory left as each one leaves it (W7). Returns one
    record per command — its own messages, backend calls and events
    only — and the escapes of the whole sequence. `short` replaces the
    clip of long messages (`str` keeps them whole)."""
    import hosts as hostslib
    hostslib.forget_unreachable()
    EVENTS.clear()
    del ESCAPES[:]
    del STARTED[:]
    del THREAD_ERRORS[:]
    bot, checker, backends, log, data = build(setup)
    records = [_drive(bot, checker, backends, log, data, text, short=short)
               for text in texts]
    escapes = list(ESCAPES)
    shutil.rmtree(data, ignore_errors=True)
    hostslib.forget_unreachable()
    return records, escapes


def _drive(bot, checker, backends, log, data, text, short=None):
    """One command on a built bot: what it sent, which backends it asked
    and the events, counted from where the previous command left off."""
    from version import VERSION
    short = short or _short
    marks = (len(log), {n: len(b.calls) for n, b in backends.items()},
             len(EVENTS), len(THREAD_ERRORS), len(STARTED))
    raised = None
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            bot._handle_message({"message_id": 1, "text": text,
                                 "from": {"id": int(USER)},
                                 "chat": {"id": int(CHAT)}}, checker, None)
        except Exception as e:                          # noqa: BLE001
            raised = type(e).__name__
        hung = []
        i = marks[4]
        while i < len(STARTED):                         # threads may start threads
            STARTED[i].join(20)
            if STARTED[i].is_alive():
                hung.append(STARTED[i].name)
            i += 1

    def norm(s):
        return short(str(s).replace(data, "{DATA}")
                     .replace(VERSION, "{VERSION}"))

    out = []
    for kind, body, kb in log[marks[0]:]:
        line = norm(body) if kind == "send" else f"<{kind}> {norm(body)}".rstrip()
        if kb:
            line += f"  [{kb} buttons]"
        out.append(line)
    touched = {n: _calls_summary(b.calls[marks[1][n]:])
               for n, b in backends.items() if b.calls[marks[1][n]:]}
    events = [norm(e) for e in EVENTS[marks[2]:]] \
        + list(THREAD_ERRORS[marks[3]:]) \
        + [f"thread did not finish: {h}" for h in hung]
    return {"out": out, "hosts": touched, "events": events,
            "raised": raised}


# ── the rows ───────────────────────────────────────────────────────────
FORMS = ("", " web", " web*", " web @nas", " web @all", " web @typo")

EXTRA = [
    "/dryrun", "/check --dry-run", "/check dry-run", "/check --dry-run @nas",
    "/hosts check ssh://x", "/start foo", "/restart web", "/stopx", "/ping",
    "/pinned", "/notes", "/language", "/maintenancex", "/hostsx", "/statusx",
    "/checkx", "/updatex", "/notacommand", "/updates foo", "/events @nas",
    "/history @nas", "/history web", "/debug x", "/groupsx", "-?",
    "/status@dsbot", "/status@otherbot", "/status @nas", "/status nosuch",
    "/update web @nas", "/note web a  b @x", "/pin web @nas",
    "/stop web @nas", "/logs web @nas", "/check web", "/update web @all",
    # One unreachable host on its own, so what W3/W4 change is pinned
    # without the dead host's raise in front of it.
    "/status web @refused", "/status web* @refused", "/status web @dead",
    "/status web* @dead", "/check web @refused", "/check web* @refused",
    "/check web* @dead", "/update web @refused", "/update web* @refused",
    "/check web @dead", "/update web @dead",
    "/update web* @dead", "/update web* @all",
]


def all_rows():
    from telegram_bot import _BOT_COMMANDS
    texts = []
    for cmd in _BOT_COMMANDS:
        for form in FORMS:
            texts.append(f"/{cmd[0]}{form}")
    for t in EXTRA:
        if t not in texts:
            texts.append(t)
    return [(s, t) for s in SETUPS for t in texts]


#: Rows that are wrong today and pinned as they are, with the slice that
#: is meant to change them. Read off the generated table by hand.
_NOT_FOUND = ("no slice yet: the refused host answers 'not found' and the "
              "dead one 'could not list containers'")
_QUIET = "no slice yet: dead and refused hosts are skipped without a word"
KNOWN_DEFECTS = {
    ("multi", "/start web @all"): _QUIET,
    ("multi", "/restart web @all"): _QUIET,
    ("multi", "/stop web @all"): _QUIET,
}
for _flag in ("pin", "autoupdate", "cooldown", "protect", "setlink", "note",
              "trustrunning", "askmajor"):
    KNOWN_DEFECTS[("multi", f"/{_flag} web @all")] = _NOT_FOUND

#: W3, written by hand before the change and not from --snapshot: what a
#: plain-name /status, /check and /update answer with a dead or refused
#: host among the targets. The table below carries the same rows as
#: generated; this is the independent statement of what they must say.
#: The walk goes on past an unreachable host (flaky is still asked), the
#: hosts that answered answer as before, and the unreachable ones are
#: named in one closing line each — together with "not found" when
#: nothing was found, so a miss never reads as "not there" on a host
#: nobody could ask. /check and /update say it before they start
#: checking, which is when the lookup ends.
_W3_DEAD = "⚠ `dead` unreachable: no answer within 30 seconds"
_W3_REFUSED = ("⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 "
               "port 22: Connection refused")
_W3_BOTH = _W3_DEAD + "\n" + _W3_REFUSED
#: A read's lookup (/status, /check) gives a remote host ten seconds
#: since W7; /update keeps the backend's thirty.
_W3_DEAD_READ = "⚠ `dead` unreachable: no answer within 10 seconds"
_W3_BOTH_READ = _W3_DEAD_READ + "\n" + _W3_REFUSED
_W3_DETAIL = ("🔎 *web*{tag} — ✅ running{health} · ⏱ ?\n\n📦 `nginx:1.25`\n"
              "     `251ad31786ba`\n\n🔁 unless-stopped\n\n"
              "🔄 *Update available*")
_W3_FOUND = (_W3_DETAIL.format(tag="", health=" (healthy)") + "  [2 buttons]",
             _W3_DETAIL.format(tag=" @nas", health=" (healthy)"),
             _W3_DETAIL.format(tag=" @flaky", health=""))
_W3_AVAIL = ("🔄 *Docker Updates Available*\n\n• [web](https://github.com/"
             "example/web/releases/latest){tag} (nginx:1.25) 🔖 v1.25.0 → "
             "v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]")
_W3_RESULT = ("*Update Result:*\n\n✅ [web](https://github.com/example/web/"
              "releases/latest){tag}: updated (stand-in)")
_W3_FLAKY = "⚠️ Host `flaky` could not be checked: registry did not answer in 15s"
_W3_CHECKED = ["check_all@local only=['web']", "check_all@nas only=['web']",
               "check_all@flaky only=['web']"]
_W3_CHECK_OUT = [_W3_BOTH_READ, "🔍 Checking for updates...",
                 _W3_AVAIL.format(tag=""), _W3_AVAIL.format(tag=" @nas"),
                 _W3_FLAKY]
W3_SPEC = {
    "/status web": (list(_W3_FOUND) + [_W3_BOTH_READ], []),
    "/status web @all": (list(_W3_FOUND) + [_W3_BOTH_READ], []),
    "/status nosuch": (["Container `nosuch` not found.\n" + _W3_BOTH_READ], []),
    "/status web @dead": ([_W3_DEAD_READ], []),
    "/status web @refused": ([_W3_REFUSED], []),
    "/check web": (_W3_CHECK_OUT, _W3_CHECKED),
    "/check web @all": (_W3_CHECK_OUT, _W3_CHECKED),
    "/check web @refused": ([_W3_REFUSED], []),
    "/check web @dead": ([_W3_DEAD_READ], []),
    "/update web @refused": ([_W3_REFUSED], []),
    "/update web @dead": ([_W3_DEAD], []),
    "/update web @all": (
        [_W3_BOTH, "🔍 Checking for updates...", _W3_FLAKY,
         "⏳ Updating 2 container(s) matching `web`…",
         "⏳ Starting update for 1 containers...", _W3_RESULT.format(tag=""),
         "⏳ Starting update for 1 containers...",
         _W3_RESULT.format(tag=" @nas")],
        _W3_CHECKED + ["update_container@local:web",
                       "update_container@nas:web"]),
}


def _w7_probe(text):
    """How an unreachable host's lookup was asked: a read with the ten
    second probe since W7, /update with the backend's own default."""
    return "ps -a --format" + ("" if text.startswith("/update") else " t=10")


def w3_checks():
    """The W3 rows against W3_SPEC: what is said, what ran, nothing
    raised, and each unreachable host asked exactly once — a walk that
    stops at the first dead host leaves `refused` and `flaky` unasked."""
    results = []
    for text, (want_out, want_events) in W3_SPEC.items():
        rec, esc = run_row("multi", text)
        results.append((f"W3 {text!r}: says what the spec says",
                        rec["out"] == want_out and not esc,
                        f"got {rec['out']!r}, escapes {esc!r}"))
        results.append((f"W3 {text!r}: raises nothing, runs what it should",
                        rec["raised"] is None
                        and rec["events"] == want_events,
                        f"raised {rec['raised']!r}, events {rec['events']!r}"))
        named = [h for h in ("dead", "refused")
                 if "@" not in text or text.endswith(("@all", "@" + h))]
        if text.endswith("@refused"):
            named = ["refused"]
        asked = all(rec["hosts"].get(h) == _w7_probe(text) for h in named)
        if "@" not in text or text.endswith("@all"):
            asked = asked and "flaky" in rec["hosts"]
        results.append((f"W3 {text!r}: every unreachable host asked once, "
                        f"and the walk went on past it", asked,
                        f"hosts {rec['hosts']!r}"))
    return results + w3_extra_checks()


def _w3_row(setup, text, *, modes=None, refused=None):
    """One row with `modes` laid over the set-up and `refused` as what a
    refused host prints; both put back afterwards."""
    real = MatrixBackend.refused_stderr
    MODE_OVERRIDE.update(modes or {})
    if refused is not None:
        MatrixBackend.refused_stderr = refused
    try:
        return run_row(setup, text)
    finally:
        MODE_OVERRIDE.clear()
        MatrixBackend.refused_stderr = real


_NO_SSH_DIR = "/docksentry-test/no-ssh-here"


def w3_extra_checks():
    """What the table cannot show with its fixed hosts: the SSH hint
    under an unreachable line, and a single-host install whose own
    daemon does not answer."""
    import hostdiag
    from i18n import get_translator
    en = get_translator("en")
    results = []
    # The hint: hostdiag reads the container's ~/.ssh, which is this
    # machine's — pointed at a directory that does not exist, so the
    # sentence is the same everywhere. A fixed short path, not one under
    # the temp dir: a long TMPDIR made the line long enough for Telegram's
    # clip to cut it, and the check failed for the path's sake alone.
    no_dir = _NO_SSH_DIR
    real_home = hostdiag._home_ssh
    hostdiag._home_ssh = lambda: no_dir
    try:
        denied = "user@192.0.2.30: Permission denied (publickey)."
        rec, esc = _w3_row("multi", "/status web @refused", refused=denied)
    finally:
        hostdiag._home_ssh = real_home
    hint = en("hostdiag_ssh_no_dir", dir=no_dir)
    want = [f"⚠ `refused` unreachable: {denied}\n{hint}"]
    results.append(("W3 an unreachable host's SSH hint stands under its line",
                    rec["out"] == want and not esc and bool(hint),
                    f"got {rec['out']!r}, want {want!r}"))
    # One host, and its daemon refuses: not "not found", and no raise.
    gone = ("Cannot connect to the Docker daemon at unix:///var/run/"
            "docker.sock. Is the docker daemon running?")
    rec, esc = _w3_row("none", "/status web", modes={"local": "refused"},
                       refused=gone)
    want = [f"⚠ `local` unreachable: {gone}"]
    results.append(("W3 single host, daemon refuses: /status web says so",
                    rec["out"] == want and rec["raised"] is None and not esc,
                    f"got {rec['out']!r}, raised {rec['raised']!r}"))
    # One host, and its daemon does not answer: no raise out of /check.
    rec, esc = _w3_row("none", "/check web", modes={"local": "dead"})
    want = ["⚠ `local` unreachable: no answer within 30 seconds"]
    results.append(("W3 single host, daemon dead: /check web says so",
                    rec["out"] == want and rec["raised"] is None
                    and rec["events"] == [] and not esc,
                    f"got {rec['out']!r}, raised {rec['raised']!r}"))
    return results


#: W4, written by hand before the change: the same for a glob. Before
#: W4 a dead or refused host read as "no containers match" — on its own
#: that was the whole answer, and among others it was not mentioned at
#: all. Now a glob says it like a plain name does: the hosts that
#: answered as before, one closing line per host that did not.
_W4_OVERVIEW = (
    "*Status — 2 match(es) for* `web*`*:*\n🟢 `web` — running (healthy)\n"
    "⏹ `webdav` — exited",
    "*Status — 1 match(es) for* `web*`*:*\n🟢 `web` @nas — running (healthy)",
    "*Status — 1 match(es) for* `web*`*:*\n🟢 `web` @flaky — running")
_W4_CHECKED = ["check_all@local only=['web', 'webdav']",
               "check_all@nas only=['web']", "check_all@flaky only=['web']"]
W4_SPEC = {
    "/status web*": (list(_W4_OVERVIEW) + [_W3_BOTH_READ], []),
    "/status web* @refused": ([_W3_REFUSED], []),
    "/status web* @dead": ([_W3_DEAD_READ], []),
    "/check web*": (_W3_CHECK_OUT, _W4_CHECKED),
    "/check web* @refused": ([_W3_REFUSED], []),
    "/check web* @dead": ([_W3_DEAD_READ], []),
    "/update web* @refused": ([_W3_REFUSED], []),
    "/update web* @dead": ([_W3_DEAD], []),
    # Nothing matches on the hosts that answered: "no match" leads, the
    # hosts nobody could ask follow in the same message.
    "/check nosuch*": (["⚠️ No containers match `nosuch*`.\n" + _W3_BOTH_READ],
                       []),
    "/status nosuch*": (["⚠️ No containers match `nosuch*`.\n" + _W3_BOTH_READ],
                        []),
    "/update web* @all": (
        [_W3_BOTH, "🔍 Checking for updates...", _W3_FLAKY,
         "⏳ Updating 2 container(s) matching `web*`…",
         "⏳ Starting update for 1 containers...", _W3_RESULT.format(tag=""),
         "⏳ Starting update for 1 containers...",
         _W3_RESULT.format(tag=" @nas")],
        _W4_CHECKED + ["update_container@local:web",
                       "update_container@nas:web"]),
}


def w4_checks():
    """The W4 rows against W4_SPEC, the same three questions as W3:
    what is said, what ran and that nothing raised, and that each
    unreachable host was asked once while the walk went on to flaky."""
    results = []
    for text, (want_out, want_events) in W4_SPEC.items():
        rec, esc = run_row("multi", text)
        results.append((f"W4 {text!r}: says what the spec says",
                        rec["out"] == want_out and not esc,
                        f"got {rec['out']!r}, escapes {esc!r}"))
        results.append((f"W4 {text!r}: raises nothing, runs what it should",
                        rec["raised"] is None
                        and rec["events"] == want_events,
                        f"raised {rec['raised']!r}, events {rec['events']!r}"))
        named = [h for h in ("dead", "refused")
                 if "@" not in text or text.endswith(("@all", "@" + h))]
        asked = all(rec["hosts"].get(h) == _w7_probe(text) for h in named)
        if "@" not in text or text.endswith("@all"):
            asked = asked and "flaky" in rec["hosts"]
        results.append((f"W4 {text!r}: every unreachable host asked once, "
                        f"and the walk went on past it", asked and bool(named),
                        f"hosts {rec['hosts']!r}"))
    return results + w4_extra_checks()


def w4_extra_checks():
    """The glob's SSH hint, and a single host whose own daemon does not
    answer a glob — before W4 both read as "no containers match"."""
    import hostdiag
    from i18n import get_translator
    en = get_translator("en")
    results = []
    no_dir = _NO_SSH_DIR
    real_home = hostdiag._home_ssh
    hostdiag._home_ssh = lambda: no_dir
    try:
        denied = "user@192.0.2.30: Permission denied (publickey)."
        rec, esc = _w3_row("multi", "/check web* @refused", refused=denied)
    finally:
        hostdiag._home_ssh = real_home
    hint = en("hostdiag_ssh_no_dir", dir=no_dir)
    want = [f"⚠ `refused` unreachable: {denied}\n{hint}"]
    results.append(("W4 a glob's unreachable host gets its SSH hint too",
                    rec["out"] == want and not esc and bool(hint),
                    f"got {rec['out']!r}, want {want!r}"))
    gone = ("Cannot connect to the Docker daemon at unix:///var/run/"
            "docker.sock. Is the docker daemon running?")
    rec, esc = _w3_row("none", "/check web*", modes={"local": "refused"},
                       refused=gone)
    want = [f"⚠ `local` unreachable: {gone}"]
    results.append(("W4 single host, daemon refuses: /check web* says so",
                    rec["out"] == want and rec["raised"] is None
                    and rec["events"] == [] and not esc,
                    f"got {rec['out']!r}, raised {rec['raised']!r}"))
    rec, esc = _w3_row("none", "/status web*", modes={"local": "dead"})
    want = ["⚠ `local` unreachable: no answer within 30 seconds"]
    results.append(("W4 single host, daemon dead: /status web* says so",
                    rec["out"] == want and rec["raised"] is None and not esc,
                    f"got {rec['out']!r}, raised {rec['raised']!r}"))
    return results


#: W7, written by hand before the change: two commands on one bot, the
#: second right after the first. A READ that finds a remote host dead
#: leaves it in the dead-host memory, and the next read within its
#: cooldown does not ask it again — not its `ps`, not its check — but
#: names it with the words it gave and how long until it is asked again.
#: The clock is frozen, so a first failure's cooldown is the full 60 s.
#: A WRITE (/update) neither skips a remembered host nor leaves anything
#: in the memory. A check that failed although the daemon answered
#: (flaky: its registry) is not remembered, so /status still asks flaky.
#: The local host is never skipped, not even on its own.
_W7_LATER = " (from a moment ago, not asked again for 60s)"
_W7_DEAD_PS = ("Command '['docker', '-H', 'ssh://dead', 'ps', '-q']' "
               "timed out after 10 seconds")
#: …and what a skipped host says of that, later: errfmt.human's words,
#: never Python's argv, whoever remembered it (review of W7: the raw line
#: came back with the seconds clause after it, and then human could no
#: longer recognise it as a timeout).
_W7_DEAD_LATER = "no answer within 10 seconds" + _W7_LATER
_W7_REFUSED = ("ssh: connect to host 192.0.2.30 port 22: Connection "
               "refused")
_W7_REFUSED_CHECK = "could not list containers (rc=255): " + _W7_REFUSED


def _w7_status_line(host, words):
    return f"⚠️ `{host}` unreachable — not counted above: {words}"


def _w7_seq(texts, setup="multi", modes=None):
    """`run_seq` on a frozen clock, long messages whole."""
    import hosts as hostslib

    class _Frozen:
        @staticmethod
        def monotonic():
            return 4242.0
    real = hostslib.time
    hostslib.time = _Frozen
    MODE_OVERRIDE.update(modes or {})
    try:
        return run_seq(setup, texts, short=str)
    finally:
        MODE_OVERRIDE.clear()
        hostslib.time = real


def w7_checks():
    results = []

    def add(label, ok, detail=""):
        results.append((f"W7 {label}", bool(ok), detail))

    def swap(lines, old, new):
        return [o.replace(old, new) for o in lines]

    # /status, then /status: dead and refused are not asked again.
    (first, second), esc = _w7_seq(["/status", "/status"])
    want = swap(swap(first["out"],
                     _w7_status_line("dead", _W7_DEAD_PS),
                     _w7_status_line("dead", _W7_DEAD_LATER)),
                _w7_status_line("refused", _W7_REFUSED),
                _w7_status_line("refused", _W7_REFUSED + _W7_LATER))
    add("/status then /status: the first is T2's /status row",
        _short(first["out"][0]) == EXPECTED[("multi", "/status")]["out"][0]
        and first["hosts"] == EXPECTED[("multi", "/status")]["hosts"],
        f"got {first!r}")
    add("/status then /status: dead and refused are named with their "
        "words and the seconds left", second["out"] == want
        and want != first["out"] and not esc,
        f"got {second['out']!r}, want {want!r}")
    add("/status then /status: …and neither is asked, the others are",
        second["hosts"] == {"local": "ps -q t=10, inspect t=10",
                            "nas": "ps -q t=10, inspect t=10",
                            "flaky": "ps -q t=10, inspect t=10"},
        f"hosts {second['hosts']!r}")

    # /status, then /check: the check does not wait on them either.
    (first, second), esc = _w7_seq(["/status", "/check"])
    fresh, _e = _w7_seq(["/check"])
    want = [("⚠️ Host `dead` could not be checked: " + _W7_DEAD_LATER)
            if o.startswith("⚠️ Host `dead`")
            else ("⚠️ Host `refused` could not be checked: " + _W7_REFUSED
                  + _W7_LATER) if o.startswith("⚠️ Host `refused`")
            else o for o in fresh[0]["out"]]
    add("/status then /check: dead and refused say the remembered words, "
        "no hint, the rest as a fresh /check",
        second["out"] == want and not esc,
        f"got {second['out']!r}, want {want!r}")
    add("/status then /check: …neither host is asked or checked",
        "dead" not in second["hosts"] and "refused" not in second["hosts"]
        and second["events"] == ["check_all@local", "check_all@nas",
                                 "check_all@flaky"],
        f"hosts {second['hosts']!r}, events {second['events']!r}")

    # /check, then /status: what /check learnt is what /status says, and
    # flaky (registry trouble, daemon fine) is asked as usual.
    (first, second), esc = _w7_seq(["/check", "/status"])
    plain, _e = _w7_seq(["/status"])
    want = swap(swap(plain[0]["out"],
                     _w7_status_line("dead", _W7_DEAD_PS),
                     _w7_status_line("dead", "no answer within 30 seconds"
                                     + _W7_LATER)),
                _w7_status_line("refused", _W7_REFUSED),
                _w7_status_line("refused", _W7_REFUSED_CHECK + _W7_LATER))
    add("/check then /status: the check's words, remembered",
        second["out"] == want and not esc,
        f"got {second['out']!r}, want {want!r}")
    add("/check then /status: …dead and refused not asked, flaky asked",
        "dead" not in second["hosts"] and "refused" not in second["hosts"]
        and second["hosts"].get("flaky") == "ps -q t=10, inspect t=10",
        f"hosts {second['hosts']!r}")

    # /status, then a lookup: /status web, /check web, /check --dry-run.
    (first, second), esc = _w7_seq(["/status", "/status web"])
    tail = ("⚠ `dead` unreachable: " + _W7_DEAD_LATER + "\n"
            "⚠ `refused` unreachable: " + _W7_REFUSED + _W7_LATER)
    want = list(_W3_FOUND) + [tail]
    add("/status then /status web: the lookup names both without asking",
        second["out"] == want and "dead" not in second["hosts"]
        and "refused" not in second["hosts"] and not esc,
        f"got {second['out']!r}, hosts {second['hosts']!r}")
    (first, second), esc = _w7_seq(["/status", "/check web"])
    want = [tail] + _W3_CHECK_OUT[1:]
    add("/status then /check web: the same, then the check on the rest",
        second["out"] == want and second["events"] == _W3_CHECKED
        and "dead" not in second["hosts"]
        and "refused" not in second["hosts"] and not esc,
        f"got {second['out']!r}, events {second['events']!r}")
    (first, second), esc = _w7_seq(["/status", "/check --dry-run"])
    add("/status then /check --dry-run: dead and refused not asked",
        "dead" not in second["hosts"] and "refused" not in second["hosts"]
        and "check_all@dead" not in second["events"]
        and any(o == "⚠️ Host `dead` could not be checked: "
                + _W7_DEAD_LATER for o in second["out"])
        and not esc, f"got {second!r}")

    # A lookup leaves its words too: ten seconds is its probe.
    (first, second), esc = _w7_seq(["/status web", "/status"])
    want = swap(swap(plain[0]["out"],
                     _w7_status_line("dead", _W7_DEAD_PS),
                     _w7_status_line("dead", "no answer within 10 seconds"
                                     + _W7_LATER)),
                _w7_status_line("refused", _W7_REFUSED),
                _w7_status_line("refused", _W7_REFUSED + _W7_LATER))
    add("/status web then /status: the lookup's words, remembered",
        second["out"] == want and "dead" not in second["hosts"] and not esc,
        f"got {second['out']!r}, want {want!r}")

    # A write asks anew, and leaves nothing behind.
    (first, second), esc = _w7_seq(["/status", "/update web @all"])
    fresh, _e = _w7_seq(["/update web @all"])
    add("/status then /update web @all: a write asks dead and refused "
        "again and answers like a fresh one",
        second == fresh[0] and second["hosts"].get("dead")
        == "ps -a --format" and not esc,
        f"got {second!r}, want {fresh[0]!r}")
    (first, second), esc = _w7_seq(["/update web @all", "/status"])
    add("/update web @all then /status: the write left nothing to skip",
        second == plain[0] and second["hosts"].get("dead") == "ps -q t=10"
        and not esc, f"got {second!r}")

    # One host on its own is never skipped.
    (first, second), esc = _w7_seq(["/status web", "/status web"],
                                   setup="none", modes={"local": "dead"})
    add("single host, daemon dead: the second lookup asks it again",
        first == second and second["hosts"].get("local") == "ps -a --format"
        and not esc, f"got {first!r} / {second!r}")
    return results


#: Where each command word lands, read against main at 8a74ba7
#: (app/telegram_bot.py, `_handle_message`). Printed above its rows by
#: --snapshot, so a regenerated table keeps the review notes.
BRANCH_NOTES = {
    "status": ":3661 `startswith('/status ')` for a name or glob, :3776 "
              "`== '/status'` for bare and for `/status @nas` (rewritten to "
              "'/status' at :3674). A read: no @token means every host.",
    "check": ":3976 the three exact dry-run spellings, :4019 "
             "`startswith('/check ')`, :4188 bare `== '/check'`. "
             "'/check --dry-run @nas' is not exact, so it resolves "
             "'--dry-run' as a container name.",
    "update": ":4092 `== '/update'` or `startswith('/update ')`. A write: "
              "no @token means local plus the hint.",
    "updates": ":4309 `== '/updates'`, or `startswith('/updates ')` only "
               "with several hosts — on one host '/updates web' falls "
               "through every branch and nothing answers.",
    "cleanup": ":4380 `== '/cleanup'` or `startswith('/cleanup ')`; the "
               "argument is only searched for an @token.",
    "checkimages": ":4400, same shape as /cleanup.",
    "start": ":4454 `startswith('/start ')` (lifecycle) for any argument, "
             ":4962 bare '/start' is the greeting = /help.",
    "stop": ":4454 lifecycle `startswith('/stop')`, which also takes "
            "'/stopx' (usage line, the argument is empty).",
    "restart": ":4444 bare `== '/restart'` restarts Docksentry and has to "
               "stay above :4454, which takes every other '/restart…'.",
    "maintenance": ":4342 `startswith('/maintenance')`, so '/maintenancex' "
                   "shows the state; any argument goes to parse_duration.",
    "history": ":4525 `== '/history'` only; with an argument nothing answers.",
    "events": ":4430 `== '/events'` only; with an argument nothing answers.",
    "groups": ":4861 `startswith('/groups ')`, :4885 `== '/groups'`; "
              "'/groupsx' matches neither.",
    "pin": ":4545 `startswith('/pin')` — also '/ping' and '/pinned', which "
           "list the pins. Listing is a read, setting a write (_flag_targets).",
    "unpin": ":4558 `startswith('/unpin')`, _state_targets (always a write).",
    "autoupdate": ":4573, _flag_targets like /pin.",
    "cooldown": ":4586, _flag_targets like /pin.",
    "protect": ":4600, _flag_targets like /pin.",
    "setlink": ":4845, _state_targets; usage only when no argument at all.",
    "audit": ":4786; no argument audits Docksentry itself, a name is a "
             "read across hosts (first match wins).",
    "backup": ":4755 `startswith('/backup')`; arguments are ignored.",
    "restore": ":4713 `startswith('/restore')`; always the how-to.",
    "note": ":4613 `startswith('/note')` — also '/notes' (usage).",
    "trustrunning": ":4629, _flag_targets like /pin.",
    "askmajor": ":4642, _flag_targets like /pin.",
    "testchannel": ":4655 `startswith('/testchannel')`; arguments ignored.",
    "hosts": ":4662 `startswith('/hosts')` — also '/hostsx'; 'check <ep>' "
             "probes on a thread.",
    "updateall": ":4721 `startswith('/updateall')`; takes an @token only, "
                 "then goes through the update_all callback.",
    "selfupdate": ":4500 bare, :4503 with the whole rest as the target "
                  "(the @token included).",
    "changelog": ":4205 bare (GitHub fetch), :4253 a container on the local "
                 "host; an @token stays part of the name.",
    "debug": ":4336 `== '/debug'` only; with an argument nothing answers.",
    "logs": ":4905 `startswith('/logs')`; a read across hosts, first match.",
    "lang": ":4512 `startswith('/lang')` — also '/language' (usage).",
    "settings": ":4891 `== '/settings'` only.",
    "help": ":4937 `startswith('/help ')` per-command help, :4962 bare.",
    "other": "no branch: '/statusx', '/checkx', '/updatex', '/notacommand', "
             "'/debug x', '/groupsx' answer nothing. '-?' does not start "
             "with '/' (:3653 only rewrites '/cmd -?'). '/status@otherbot' "
             "is for another bot (:3641) and stays silent.",
}


def _expected_for(setup, text):
    """A row's expectation. The two single-host set-ups share one entry
    while they agree, which they do on main; a slice that makes them
    differ adds a `none` or `one` entry of its own."""
    if (setup, text) in EXPECTED:
        return EXPECTED[(setup, text)]
    if setup in ("none", "one"):
        return EXPECTED.get(("single", text))
    return None


def _word(text):
    from telegram_bot import _BOT_COMMANDS
    w = text.split()[0].lstrip("/").split("@")[0]
    return w if w in {c[0] for c in _BOT_COMMANDS} else "other"


def _fmt_record(rec):
    lines = ["dict("]
    if rec["out"]:
        lines.append("        out=[" + (",\n             ".join(
            repr(o) for o in rec["out"])) + "],")
    else:
        lines.append("        out=[],")
    if rec["hosts"]:
        lines.append("        hosts={" + (",\n               ".join(
            f"{k!r}: {v!r}" for k, v in rec["hosts"].items())) + "},")
    else:
        lines.append("        hosts={},")
    lines.append("        events=" + repr(rec["events"]) + ",")
    lines.append(f"        raised={rec['raised']!r})")
    return "\n".join(lines)


def snapshot():
    """Print the EXPECTED table for the code under --app."""
    recs, bad = {}, []
    for setup, text in all_rows():
        rec, esc = run_row(setup, text)
        if esc:
            bad.append((setup, text, esc))
        recs[(setup, text)] = rec
    for setup, text, esc in bad:
        print(f"# ESCAPED in {setup} {text!r}: {esc}")
    texts = [t for s, t in all_rows() if s == "multi"]
    groups = {}
    for t in texts:
        groups.setdefault(_word(t), []).append(t)
    print("EXPECTED = {")
    for word, ts in groups.items():
        head = f"/{word}" if word != "other" else "no branch"
        print(f"    # ── {head} " + "─" * (60 - len(head)))
        for line in textwrap.wrap(BRANCH_NOTES.get(word, ""), 70):
            print(f"    # {line}")
        for t in ts:
            keys = [("multi", t)]
            if recs[("none", t)] == recs[("one", t)]:
                keys.append(("single", t))
            else:
                keys += [("none", t), ("one", t)]
            for k in keys:
                src = recs[("none", t)] if k[0] == "single" else recs[k]
                print(f"    ({k[0]!r}, {t!r}): {_fmt_record(src)},")
    print("}")
    return 1 if bad else 0


def main():
    global _TMP_ROOT
    _TMP_ROOT = tempfile.mkdtemp(prefix="tg-matrix-")
    try:
        if "--snapshot" in sys.argv:
            return snapshot()
        return check()
    finally:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def _connect_raw(how):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return getattr(s, how)(("127.0.0.1", 1))


def trap_checks():
    """The trap itself, driven: a call past the stand-ins has to land in
    ESCAPES even when the code around it swallows the exception. On an
    untouched tree no row takes that path, so without this nothing would
    notice the trap going blind."""
    import telegram_bot
    results = []
    real = telegram_bot.TelegramBot._build_events_msg
    for kind, call in (
            ("subprocess.run", lambda: subprocess.run(["docker", "info"])),
            ("subprocess.Popen", lambda: subprocess.Popen(["docker"])),
            ("urlopen", lambda: urllib.request.urlopen("https://x.invalid")),
            ("socket.create_connection",
             lambda: socket.create_connection(("127.0.0.1", 1), timeout=0.2)),
            ("socket.connect", lambda: _connect_raw("connect")),
            ("socket.connect_ex", lambda: _connect_raw("connect_ex"))):
        def leaky(self, *a, _call=call, **k):
            try:
                _call()
            except Exception:                           # noqa: BLE001
                pass                                    # swallowed on purpose
            return real(self, *a, **k)
        telegram_bot.TelegramBot._build_events_msg = leaky
        try:
            rec, esc = run_row("none", "/events")
        finally:
            telegram_bot.TelegramBot._build_events_msg = real
        results.append((f"the trap catches a swallowed {kind} past the "
                        f"stand-ins", len(esc) == 1 and esc[0].startswith(kind)))
    return results


def check():
    failed = 0
    for label, ok in trap_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    for label, ok, detail in w3_checks() + w4_checks() + w7_checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"      → {detail}")
        failed += not ok
    for setup, text in all_rows():
        rec, esc = run_row(setup, text)
        want = _expected_for(setup, text)
        label = f"[{setup}] {text!r}"
        if (setup, text) in KNOWN_DEFECTS:
            label += f"  (known defect, {KNOWN_DEFECTS[(setup, text)]})"
        ok = want is not None and rec == want and not esc
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failed += 1
            if want is None:
                print("      → no expectation for this row")
            if esc:
                print(f"      → escaped the stand-ins: {esc}")
            if want is not None and rec != want:
                for k in ("out", "hosts", "events", "raised"):
                    if rec[k] != want[k]:
                        print(f"      → {k}: expected {want[k]!r}")
                        print(f"      →   got      {rec[k]!r}")
    rows = set(all_rows())
    extra = [k for k in EXPECTED
             if not ((k[0] == "single" and ("none", k[1]) in rows)
                     or k in rows)]
    for key in sorted(extra):
        print(f"  FAIL  expectation without a row: {key!r}")
        failed += 1
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


# ── expected, generated by --snapshot from main (8a74ba7), reviewed; ──
# ── the W3 rows from core-split after W3, checked against W3_SPEC ──────
# ── the W4 glob rows from core-split after W4, against W4_SPEC ─────────
# ── the 18 read-lookup rows W7 gave the ten-second probe, after W7 ─────
EXPECTED = {
    # ── /status ─────────────────────────────────────────────────────
    # :3661 `startswith('/status ')` for a name or glob, :3776 `==
    # '/status'` for bare and for `/status @nas` (rewritten to '/status' at
    # :3674). A read: no @token means every host.
    ('multi', '/status'): dict(
        out=["*Container Status:*\n\n📊 *7* Containers\n⚠️ `dead` unreachable — not counted above: Command '['docker', '-H', 'ssh://dead', 'ps', '-q']' timed out after 10 seconds\n⚠️ `refused` unreachable — not counted above: ssh: connect to host 192.0.2.30 port 22: Connection refused · 🟢 2 · ⚪ 5\n🤖 Bot uptime: 0m\n\n───…#f20974b104"],
        hosts={'local': 'ps -q t=10, inspect t=10',
               'nas': 'ps -q t=10, inspect t=10',
               'dead': 'ps -q t=10',
               'refused': 'ps -q t=10',
               'flaky': 'ps -q t=10, inspect t=10'},
        events=[],
        raised=None),
    ('single', '/status'): dict(
        out=['*Container Status:*\n\n📊 *3* Containers · 🟢 1 · ⚪ 2\n🤖 Bot uptime: 0m\n\n────────────────────────────\n\n⚪ `docksentry`\n     ⏱ ? · 📦 `ghcr.io/example/docksentry:latest`\n🟢 `web`\n     ⏱ ? · 📦 `nginx:1.25`\n⚪ `db`\n     ⏱ ? · 📦 `postgres:16`'],
        hosts={'local': 'ps -q t=10, inspect t=10'},
        events=[],
        raised=None),
    ('multi', '/status web'): dict(
        out=['🔎 *web* — ✅ running (healthy) · ⏱ ?\n\n📦 `nginx:1.25`\n     `251ad31786ba`\n\n🔁 unless-stopped\n\n🔄 *Update available*  [2 buttons]',
             '🔎 *web* @nas — ✅ running (healthy) · ⏱ ?\n\n📦 `nginx:1.25`\n     `251ad31786ba`\n\n🔁 unless-stopped\n\n🔄 *Update available*',
             '🔎 *web* @flaky — ✅ running · ⏱ ?\n\n📦 `nginx:1.25`\n     `251ad31786ba`\n\n🔁 unless-stopped\n\n🔄 *Update available*',
             '⚠ `dead` unreachable: no answer within 10 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'local': 'ps -a --format, inspect t=10, image inspect --format t=10, stats --no-stream --format t=20, inspect --size --format t=15, image inspect --format t=15, inspect --format t=10',
               'nas': 'ps -a --format t=10, inspect t=10, image inspect --format t=10, stats --no-stream --format t=20, inspect --size --format t=15, image inspect --format t=15',
               'dead': 'ps -a --format t=10',
               'refused': 'ps -a --format t=10',
               'flaky': 'ps -a --format t=10, inspect t=10, image inspect --format t=10, stats --no-stream --format t=20, inspect --size --format t=15, image inspect --format t=15'},
        events=[],
        raised=None),
    ('single', '/status web'): dict(
        out=['🔎 *web* — ✅ running (healthy) · ⏱ ?\n\n📦 `nginx:1.25`\n     `251ad31786ba`\n\n🔁 unless-stopped\n\n🔄 *Update available*  [2 buttons]'],
        hosts={'local': 'ps -a --format, inspect t=10, image inspect --format t=10, stats --no-stream --format t=20, inspect --size --format t=15, image inspect --format t=15, inspect --format t=10'},
        events=[],
        raised=None),
    ('multi', '/status web*'): dict(
        out=['*Status — 2 match(es) for* `web*`*:*\n🟢 `web` — running (healthy)\n⏹ `webdav` — exited',
             '*Status — 1 match(es) for* `web*`*:*\n🟢 `web` @nas — running (healthy)',
             '*Status — 1 match(es) for* `web*`*:*\n🟢 `web` @flaky — running',
             '⚠ `dead` unreachable: no answer within 10 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'local': 'ps -a --format, inspect t=10, image inspect --format t=10, inspect t=10, image inspect --format t=10',
               'nas': 'ps -a --format t=10, inspect t=10, image inspect --format t=10',
               'dead': 'ps -a --format t=10',
               'refused': 'ps -a --format t=10',
               'flaky': 'ps -a --format t=10, inspect t=10, image inspect --format t=10'},
        events=[],
        raised=None),
    ('single', '/status web*'): dict(
        out=['*Status — 2 match(es) for* `web*`*:*\n🟢 `web` — running (healthy)\n⏹ `webdav` — exited'],
        hosts={'local': 'ps -a --format, inspect t=10, image inspect --format t=10, inspect t=10, image inspect --format t=10'},
        events=[],
        raised=None),
    ('multi', '/status web @nas'): dict(
        out=['🔎 *web* @nas — ✅ running (healthy) · ⏱ ?\n\n📦 `nginx:1.25`\n     `251ad31786ba`\n\n🔁 unless-stopped\n\n🔄 *Update available*'],
        hosts={'nas': 'ps -a --format t=10, inspect t=10, image inspect --format t=10, stats --no-stream --format t=20, inspect --size --format t=15, image inspect --format t=15'},
        events=[],
        raised=None),
    ('single', '/status web @nas'): dict(
        out=['Container `web @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/status web @all'): dict(
        out=['🔎 *web* — ✅ running (healthy) · ⏱ ?\n\n📦 `nginx:1.25`\n     `251ad31786ba`\n\n🔁 unless-stopped\n\n🔄 *Update available*  [2 buttons]',
             '🔎 *web* @nas — ✅ running (healthy) · ⏱ ?\n\n📦 `nginx:1.25`\n     `251ad31786ba`\n\n🔁 unless-stopped\n\n🔄 *Update available*',
             '🔎 *web* @flaky — ✅ running · ⏱ ?\n\n📦 `nginx:1.25`\n     `251ad31786ba`\n\n🔁 unless-stopped\n\n🔄 *Update available*',
             '⚠ `dead` unreachable: no answer within 10 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'local': 'ps -a --format, inspect t=10, image inspect --format t=10, stats --no-stream --format t=20, inspect --size --format t=15, image inspect --format t=15, inspect --format t=10',
               'nas': 'ps -a --format t=10, inspect t=10, image inspect --format t=10, stats --no-stream --format t=20, inspect --size --format t=15, image inspect --format t=15',
               'dead': 'ps -a --format t=10',
               'refused': 'ps -a --format t=10',
               'flaky': 'ps -a --format t=10, inspect t=10, image inspect --format t=10, stats --no-stream --format t=20, inspect --size --format t=15, image inspect --format t=15'},
        events=[],
        raised=None),
    ('single', '/status web @all'): dict(
        out=['Container `web @all` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/status web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/status web @typo'): dict(
        out=['Container `web @typo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/status@dsbot'): dict(
        out=["*Container Status:*\n\n📊 *7* Containers\n⚠️ `dead` unreachable — not counted above: Command '['docker', '-H', 'ssh://dead', 'ps', '-q']' timed out after 10 seconds\n⚠️ `refused` unreachable — not counted above: ssh: connect to host 192.0.2.30 port 22: Connection refused · 🟢 2 · ⚪ 5\n🤖 Bot uptime: 0m\n\n───…#f20974b104"],
        hosts={'local': 'ps -q t=10, inspect t=10',
               'nas': 'ps -q t=10, inspect t=10',
               'dead': 'ps -q t=10',
               'refused': 'ps -q t=10',
               'flaky': 'ps -q t=10, inspect t=10'},
        events=[],
        raised=None),
    ('single', '/status@dsbot'): dict(
        out=['*Container Status:*\n\n📊 *3* Containers · 🟢 1 · ⚪ 2\n🤖 Bot uptime: 0m\n\n────────────────────────────\n\n⚪ `docksentry`\n     ⏱ ? · 📦 `ghcr.io/example/docksentry:latest`\n🟢 `web`\n     ⏱ ? · 📦 `nginx:1.25`\n⚪ `db`\n     ⏱ ? · 📦 `postgres:16`'],
        hosts={'local': 'ps -q t=10, inspect t=10'},
        events=[],
        raised=None),
    ('multi', '/status@otherbot'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/status@otherbot'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/status @nas'): dict(
        out=['*Container Status:*\n\n📊 *2* Containers · 🟢 1 · ⚪ 1\n🤖 Bot uptime: 0m\n\n────────────────────────────\n\n🟢 `web` @nas\n     ⏱ ? · 📦 `nginx:1.25`\n⚪ `media` @nas\n     ⏱ ? · 📦 `jellyfin/jellyfin:10.9`'],
        hosts={'nas': 'ps -q t=10, inspect t=10'},
        events=[],
        raised=None),
    ('single', '/status @nas'): dict(
        out=['Container `@nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/status nosuch'): dict(
        out=['Container `nosuch` not found.\n⚠ `dead` unreachable: no answer within 10 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format t=10',
               'dead': 'ps -a --format t=10',
               'refused': 'ps -a --format t=10',
               'flaky': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/status nosuch'): dict(
        out=['Container `nosuch` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/status web @refused'): dict(
        out=['⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'refused': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/status web @refused'): dict(
        out=['Container `web @refused` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/status web* @refused'): dict(
        out=['⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'refused': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/status web* @refused'): dict(
        out=['⚠️ No containers match `web* @refused`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/status web @dead'): dict(
        out=['⚠ `dead` unreachable: no answer within 10 seconds'],
        hosts={'dead': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/status web @dead'): dict(
        out=['Container `web @dead` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/status web* @dead'): dict(
        out=['⚠ `dead` unreachable: no answer within 10 seconds'],
        hosts={'dead': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/status web* @dead'): dict(
        out=['⚠️ No containers match `web* @dead`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /check ──────────────────────────────────────────────────────
    # :3976 the three exact dry-run spellings, :4019 `startswith('/check
    # ')`, :4188 bare `== '/check'`. '/check --dry-run @nas' is not exact,
    # so it resolves '--dry-run' as a container name.
    ('multi', '/check'): dict(
        out=['🔍 Checking for updates...',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) @nas (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
             '⚠️ Host `dead` could not be checked: no answer within 30 seconds',
             '⚠️ Host `refused` could not be checked: could not list containers (rc=255): ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5 ×2',
               'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5',
               'dead': 'ps --format',
               'refused': 'ps --format'},
        events=['check_all@local', 'check_all@nas', 'check_all@dead', 'check_all@refused', 'check_all@flaky'],
        raised=None),
    ('single', '/check'): dict(
        out=['🔍 Checking for updates...',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5'},
        events=['check_all@local'],
        raised=None),
    ('multi', '/check web'): dict(
        out=['⚠ `dead` unreachable: no answer within 10 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '🔍 Checking for updates...',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) @nas (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5 ×2',
               'nas': 'ps -a --format t=10, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5',
               'dead': 'ps -a --format t=10',
               'refused': 'ps -a --format t=10',
               'flaky': 'ps -a --format t=10'},
        events=["check_all@local only=['web']", "check_all@nas only=['web']", "check_all@flaky only=['web']"],
        raised=None),
    ('single', '/check web'): dict(
        out=['🔍 Checking for updates...',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5'},
        events=["check_all@local only=['web']"],
        raised=None),
    ('multi', '/check web*'): dict(
        out=['⚠ `dead` unreachable: no answer within 10 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '🔍 Checking for updates...',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) @nas (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5 ×2',
               'nas': 'ps -a --format t=10, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5',
               'dead': 'ps -a --format t=10',
               'refused': 'ps -a --format t=10',
               'flaky': 'ps -a --format t=10'},
        events=["check_all@local only=['web', 'webdav']", "check_all@nas only=['web']", "check_all@flaky only=['web']"],
        raised=None),
    ('single', '/check web*'): dict(
        out=['🔍 Checking for updates...',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5'},
        events=["check_all@local only=['web', 'webdav']"],
        raised=None),
    ('multi', '/check web @nas'): dict(
        out=['🔍 Checking for updates...',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) @nas (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]'],
        hosts={'local': 'inspect --format t=5',
               'nas': 'ps -a --format t=10, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5'},
        events=["check_all@nas only=['web']"],
        raised=None),
    ('single', '/check web @nas'): dict(
        out=['Container `web @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/check web @all'): dict(
        out=['⚠ `dead` unreachable: no answer within 10 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '🔍 Checking for updates...',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
             '🔄 *Docker Updates Available*\n\n• [web](https://github.com/example/web/releases/latest) @nas (nginx:1.25) 🔖 v1.25.0 → v1.26.0\n  📦 10 MB | 🗓️ Current: 2026-01-01  [3 buttons]',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5 ×2',
               'nas': 'ps -a --format t=10, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5',
               'dead': 'ps -a --format t=10',
               'refused': 'ps -a --format t=10',
               'flaky': 'ps -a --format t=10'},
        events=["check_all@local only=['web']", "check_all@nas only=['web']", "check_all@flaky only=['web']"],
        raised=None),
    ('single', '/check web @all'): dict(
        out=['Container `web @all` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/check web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/check web @typo'): dict(
        out=['Container `web @typo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/check --dry-run'): dict(
        out=['🔍 Checking for updates...',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) @nas (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._',
             '⚠️ Host `dead` could not be checked: no answer within 30 seconds',
             '⚠️ Host `refused` could not be checked: could not list containers (rc=255): ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5 ×2',
               'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5',
               'dead': 'ps --format',
               'refused': 'ps --format'},
        events=['check_all@local', 'tags@local:library/nginx', 'check_all@nas', 'tags@nas:library/nginx', 'check_all@dead', 'check_all@refused', 'check_all@flaky'],
        raised=None),
    ('single', '/check --dry-run'): dict(
        out=['🔍 Checking for updates...',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5'},
        events=['check_all@local', 'tags@local:library/nginx'],
        raised=None),
    ('multi', '/check dry-run'): dict(
        out=['🔍 Checking for updates...',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) @nas (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._',
             '⚠️ Host `dead` could not be checked: no answer within 30 seconds',
             '⚠️ Host `refused` could not be checked: could not list containers (rc=255): ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5 ×2',
               'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5',
               'dead': 'ps --format',
               'refused': 'ps --format'},
        events=['check_all@local', 'tags@local:library/nginx', 'check_all@nas', 'tags@nas:library/nginx', 'check_all@dead', 'check_all@refused', 'check_all@flaky'],
        raised=None),
    ('single', '/check dry-run'): dict(
        out=['🔍 Checking for updates...',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5'},
        events=['check_all@local', 'tags@local:library/nginx'],
        raised=None),
    ('multi', '/check --dry-run @nas'): dict(
        out=['Container `--dry-run` not found.'],
        hosts={'nas': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/check --dry-run @nas'): dict(
        out=['Container `--dry-run @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/check web @refused'): dict(
        out=['⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'refused': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/check web @refused'): dict(
        out=['Container `web @refused` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/check web* @refused'): dict(
        out=['⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'refused': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/check web* @refused'): dict(
        out=['⚠️ No containers match `web* @refused`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/check web* @dead'): dict(
        out=['⚠ `dead` unreachable: no answer within 10 seconds'],
        hosts={'dead': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/check web* @dead'): dict(
        out=['⚠️ No containers match `web* @dead`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/check web @dead'): dict(
        out=['⚠ `dead` unreachable: no answer within 10 seconds'],
        hosts={'dead': 'ps -a --format t=10'},
        events=[],
        raised=None),
    ('single', '/check web @dead'): dict(
        out=['Container `web @dead` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /update ─────────────────────────────────────────────────────
    # :4092 `== '/update'` or `startswith('/update ')`. A write: no @token
    # means local plus the hint.
    ('multi', '/update'): dict(
        out=['Usage: `/update <name>` or `/update <glob>` (e.g. `/update gluetun-*`). Updates only matching containers that have a pending update.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/update'): dict(
        out=['Usage: `/update <name>` or `/update <glob>` (e.g. `/update gluetun-*`). Updates only matching containers that have a pending update.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/update web'): dict(
        out=['🔍 Checking for updates...',
             '⏳ Updating 1 container(s) matching `web`…\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest): updated (stand-in)'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5, inspect --format'},
        events=["check_all@local only=['web']", 'update_container@local:web'],
        raised=None),
    ('single', '/update web'): dict(
        out=['🔍 Checking for updates...',
             '⏳ Updating 1 container(s) matching `web`…',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest): updated (stand-in)'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5, inspect --format'},
        events=["check_all@local only=['web']", 'update_container@local:web'],
        raised=None),
    ('multi', '/update web*'): dict(
        out=['🔍 Checking for updates...',
             '⏳ Updating 1 container(s) matching `web*`…\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest): updated (stand-in)'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5, inspect --format'},
        events=["check_all@local only=['web', 'webdav']", 'update_container@local:web'],
        raised=None),
    ('single', '/update web*'): dict(
        out=['🔍 Checking for updates...',
             '⏳ Updating 1 container(s) matching `web*`…',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest): updated (stand-in)'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5, inspect --format'},
        events=["check_all@local only=['web', 'webdav']", 'update_container@local:web'],
        raised=None),
    ('multi', '/update web @nas'): dict(
        out=['🔍 Checking for updates...',
             '⏳ Updating 1 container(s) matching `web`…',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest) @nas: updated (stand-in)'],
        hosts={'local': 'inspect --format t=5',
               'nas': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5, inspect --format'},
        events=["check_all@nas only=['web']", 'update_container@nas:web'],
        raised=None),
    ('single', '/update web @nas'): dict(
        out=['Container `web @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/update web @all'): dict(
        out=['⚠ `dead` unreachable: no answer within 30 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '🔍 Checking for updates...',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s',
             '⏳ Updating 2 container(s) matching `web`…',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest): updated (stand-in)',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest) @nas: updated (stand-in)'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=5, inspect --format t=10, inspect --format t=5, inspect --format',
               'nas': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5, inspect --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=["check_all@local only=['web']", "check_all@nas only=['web']", "check_all@flaky only=['web']", 'update_container@local:web', 'update_container@nas:web'],
        raised=None),
    ('single', '/update web @all'): dict(
        out=['Container `web @all` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/update web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/update web @typo'): dict(
        out=['Container `web @typo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/update web @refused'): dict(
        out=['⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'refused': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/update web @refused'): dict(
        out=['Container `web @refused` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/update web* @refused'): dict(
        out=['⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused'],
        hosts={'refused': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/update web* @refused'): dict(
        out=['⚠️ No containers match `web* @refused`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/update web @dead'): dict(
        out=['⚠ `dead` unreachable: no answer within 30 seconds'],
        hosts={'dead': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/update web @dead'): dict(
        out=['Container `web @dead` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/update web* @dead'): dict(
        out=['⚠ `dead` unreachable: no answer within 30 seconds'],
        hosts={'dead': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/update web* @dead'): dict(
        out=['⚠️ No containers match `web* @dead`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/update web* @all'): dict(
        out=['⚠ `dead` unreachable: no answer within 30 seconds\n⚠ `refused` unreachable: ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '🔍 Checking for updates...',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s',
             '⏳ Updating 2 container(s) matching `web*`…',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest): updated (stand-in)',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest) @nas: updated (stand-in)'],
        hosts={'local': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=5, inspect --format t=10, inspect --format t=5, inspect --format',
               'nas': 'ps -a --format, ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5, inspect --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=["check_all@local only=['web', 'webdav']", "check_all@nas only=['web']", "check_all@flaky only=['web']", 'update_container@local:web', 'update_container@nas:web'],
        raised=None),
    ('single', '/update web* @all'): dict(
        out=['⚠️ No containers match `web* @all`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /updates ────────────────────────────────────────────────────
    # :4309 `== '/updates'`, or `startswith('/updates ')` only with several
    # hosts — on one host '/updates web' falls through every branch and
    # nothing answers.
    ('multi', '/updates'): dict(
        out=['*Pending Updates:*\n• `web`\n• `web` @nas'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updates'): dict(
        out=['*Pending Updates:*\n• `web`'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updates web'): dict(
        out=['*Pending Updates:*\n• `web`\n• `web` @nas'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updates web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updates web*'): dict(
        out=['*Pending Updates:*\n• `web`\n• `web` @nas'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updates web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updates web @nas'): dict(
        out=['*Pending Updates:*\n• `web` @nas'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updates web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updates web @all'): dict(
        out=['*Pending Updates:*\n• `web`\n• `web` @nas'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updates web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updates web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updates web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updates foo'): dict(
        out=['*Pending Updates:*\n• `web`\n• `web` @nas'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updates foo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    # ── /cleanup ────────────────────────────────────────────────────
    # :4380 `== '/cleanup'` or `startswith('/cleanup ')`; the argument is
    # only searched for an @token.
    ('multi', '/cleanup'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B',
             '✅ Total reclaimed space: 0 B @nas',
             "❌ Cleanup error: Command '['docker', '-H', 'ssh://dead', 'image', 'prune', '-a', '--force', '--filter', 'until=24h']' timed out after 180 seconds @dead",
             '❌ Cleanup failed: ssh: connect to host 192.0.2.30 port 22: Connection refused @refused',
             '✅ Total reclaimed space: 0 B @flaky'],
        hosts={'local': 'image prune -a --force --filter t=180',
               'nas': 'image prune -a --force --filter t=180',
               'dead': 'image prune -a --force --filter t=180',
               'refused': 'image prune -a --force --filter t=180',
               'flaky': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('single', '/cleanup'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B'],
        hosts={'local': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('multi', '/cleanup web'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B',
             '✅ Total reclaimed space: 0 B @nas',
             "❌ Cleanup error: Command '['docker', '-H', 'ssh://dead', 'image', 'prune', '-a', '--force', '--filter', 'until=24h']' timed out after 180 seconds @dead",
             '❌ Cleanup failed: ssh: connect to host 192.0.2.30 port 22: Connection refused @refused',
             '✅ Total reclaimed space: 0 B @flaky'],
        hosts={'local': 'image prune -a --force --filter t=180',
               'nas': 'image prune -a --force --filter t=180',
               'dead': 'image prune -a --force --filter t=180',
               'refused': 'image prune -a --force --filter t=180',
               'flaky': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('single', '/cleanup web'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B'],
        hosts={'local': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('multi', '/cleanup web*'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B',
             '✅ Total reclaimed space: 0 B @nas',
             "❌ Cleanup error: Command '['docker', '-H', 'ssh://dead', 'image', 'prune', '-a', '--force', '--filter', 'until=24h']' timed out after 180 seconds @dead",
             '❌ Cleanup failed: ssh: connect to host 192.0.2.30 port 22: Connection refused @refused',
             '✅ Total reclaimed space: 0 B @flaky'],
        hosts={'local': 'image prune -a --force --filter t=180',
               'nas': 'image prune -a --force --filter t=180',
               'dead': 'image prune -a --force --filter t=180',
               'refused': 'image prune -a --force --filter t=180',
               'flaky': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('single', '/cleanup web*'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B'],
        hosts={'local': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('multi', '/cleanup web @nas'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B @nas'],
        hosts={'nas': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('single', '/cleanup web @nas'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B'],
        hosts={'local': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('multi', '/cleanup web @all'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B',
             '✅ Total reclaimed space: 0 B @nas',
             "❌ Cleanup error: Command '['docker', '-H', 'ssh://dead', 'image', 'prune', '-a', '--force', '--filter', 'until=24h']' timed out after 180 seconds @dead",
             '❌ Cleanup failed: ssh: connect to host 192.0.2.30 port 22: Connection refused @refused',
             '✅ Total reclaimed space: 0 B @flaky'],
        hosts={'local': 'image prune -a --force --filter t=180',
               'nas': 'image prune -a --force --filter t=180',
               'dead': 'image prune -a --force --filter t=180',
               'refused': 'image prune -a --force --filter t=180',
               'flaky': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('single', '/cleanup web @all'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B'],
        hosts={'local': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    ('multi', '/cleanup web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/cleanup web @typo'): dict(
        out=['🧹 Cleaning up old images...',
             '✅ Total reclaimed space: 0 B'],
        hosts={'local': 'image prune -a --force --filter t=180'},
        events=[],
        raised=None),
    # ── /checkimages ────────────────────────────────────────────────
    # :4400, same shape as /cleanup.
    ('multi', '/checkimages'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache. @local',
             '🧹 Nothing to clean up right now — no unused images or build cache. @nas',
             "⚠️ Host `dead` could not be checked: Command '['docker', '-H', 'ssh://dead', 'system', 'df', '--format', '{{json .}}' @dead",
             '⚠️ Host `refused` could not be checked: ssh: connect to host 192.0.2.30 port 22: Connection refused @refused',
             '🧹 Nothing to clean up right now — no unused images or build cache. @flaky'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20',
               'nas': 'system df --format t=15, images --filter --format t=20',
               'dead': 'system df --format t=15',
               'refused': 'system df --format t=15',
               'flaky': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('single', '/checkimages'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache.'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('multi', '/checkimages web'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache. @local',
             '🧹 Nothing to clean up right now — no unused images or build cache. @nas',
             "⚠️ Host `dead` could not be checked: Command '['docker', '-H', 'ssh://dead', 'system', 'df', '--format', '{{json .}}' @dead",
             '⚠️ Host `refused` could not be checked: ssh: connect to host 192.0.2.30 port 22: Connection refused @refused',
             '🧹 Nothing to clean up right now — no unused images or build cache. @flaky'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20',
               'nas': 'system df --format t=15, images --filter --format t=20',
               'dead': 'system df --format t=15',
               'refused': 'system df --format t=15',
               'flaky': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('single', '/checkimages web'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache.'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('multi', '/checkimages web*'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache. @local',
             '🧹 Nothing to clean up right now — no unused images or build cache. @nas',
             "⚠️ Host `dead` could not be checked: Command '['docker', '-H', 'ssh://dead', 'system', 'df', '--format', '{{json .}}' @dead",
             '⚠️ Host `refused` could not be checked: ssh: connect to host 192.0.2.30 port 22: Connection refused @refused',
             '🧹 Nothing to clean up right now — no unused images or build cache. @flaky'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20',
               'nas': 'system df --format t=15, images --filter --format t=20',
               'dead': 'system df --format t=15',
               'refused': 'system df --format t=15',
               'flaky': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('single', '/checkimages web*'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache.'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('multi', '/checkimages web @nas'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache. @nas'],
        hosts={'nas': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('single', '/checkimages web @nas'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache.'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('multi', '/checkimages web @all'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache. @local',
             '🧹 Nothing to clean up right now — no unused images or build cache. @nas',
             "⚠️ Host `dead` could not be checked: Command '['docker', '-H', 'ssh://dead', 'system', 'df', '--format', '{{json .}}' @dead",
             '⚠️ Host `refused` could not be checked: ssh: connect to host 192.0.2.30 port 22: Connection refused @refused',
             '🧹 Nothing to clean up right now — no unused images or build cache. @flaky'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20',
               'nas': 'system df --format t=15, images --filter --format t=20',
               'dead': 'system df --format t=15',
               'refused': 'system df --format t=15',
               'flaky': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('single', '/checkimages web @all'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache.'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    ('multi', '/checkimages web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/checkimages web @typo'): dict(
        out=['🧹 Nothing to clean up right now — no unused images or build cache.'],
        hosts={'local': 'system df --format t=15, images --filter --format t=20'},
        events=[],
        raised=None),
    # ── /start ──────────────────────────────────────────────────────
    # :4454 `startswith('/start ')` (lifecycle) for any argument, :4962 bare
    # '/start' is the greeting = /help.
    ('multi', '/start'): dict(
        out=['*Docksentry v{VERSION}* 🐳\n\n💡 *Tip:* type `/` in this chat — Telegram shows an autocomplete picker with all commands and their descriptions.\n\n*Commands:*\n/status — Show container status\n/check — Check for updates now\n/update — Update matching containers (name or glob)\n/updates — Show pending updates\n…#fc869fcb47'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/start'): dict(
        out=['*Docksentry v{VERSION}* 🐳\n\n💡 *Tip:* type `/` in this chat — Telegram shows an autocomplete picker with all commands and their descriptions.\n\n*Commands:*\n/status — Show container status\n/check — Check for updates now\n/update — Update matching containers (name or glob)\n/updates — Show pending updates\n…#fdc7ab3473'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/start web'): dict(
        out=['▶️ `web` started.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format, start t=30'},
        events=[],
        raised=None),
    ('single', '/start web'): dict(
        out=['▶️ `web` started.'],
        hosts={'local': 'ps -a --format, start t=30'},
        events=[],
        raised=None),
    ('multi', '/start web*'): dict(
        out=['*start — 2 match(es) for* `web*`*:*\n▶️ `web` started.\n▶️ `webdav` started.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format, start t=30 ×2'},
        events=[],
        raised=None),
    ('single', '/start web*'): dict(
        out=['*start — 2 match(es) for* `web*`*:*\n▶️ `web` started.\n▶️ `webdav` started.'],
        hosts={'local': 'ps -a --format, start t=30 ×2'},
        events=[],
        raised=None),
    ('multi', '/start web @nas'): dict(
        out=['▶️ `web` started. @nas'],
        hosts={'nas': 'ps -a --format, start t=30'},
        events=[],
        raised=None),
    ('single', '/start web @nas'): dict(
        out=['Container `web @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/start web @all'): dict(
        out=['▶️ `web` started.',
             '▶️ `web` started. @nas',
             '▶️ `web` started. @flaky'],
        hosts={'local': 'ps -a --format, start t=30',
               'nas': 'ps -a --format, start t=30',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format, start t=30'},
        events=[],
        raised=None),
    ('single', '/start web @all'): dict(
        out=['Container `web @all` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/start web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/start web @typo'): dict(
        out=['Container `web @typo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/start foo'): dict(
        out=['Container `foo` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/start foo'): dict(
        out=['Container `foo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /stop ───────────────────────────────────────────────────────
    # :4454 lifecycle `startswith('/stop')`, which also takes '/stopx'
    # (usage line, the argument is empty).
    ('multi', '/stop'): dict(
        out=['Usage: `/start <name>`, `/stop <name>`, or `/restart <name>`. Partial name matching works the same as `/pin`.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/stop'): dict(
        out=['Usage: `/start <name>`, `/stop <name>`, or `/restart <name>`. Partial name matching works the same as `/pin`.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/stop web'): dict(
        out=['⚠ Stop `web`? It stays down until something starts it again.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.  [2 buttons]'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, inspect --format t=10'},
        events=[],
        raised=None),
    ('single', '/stop web'): dict(
        out=['⚠ Stop `web`? It stays down until something starts it again.  [2 buttons]'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, inspect --format t=10'},
        events=[],
        raised=None),
    ('multi', '/stop web*'): dict(
        out=['⚠ Stop 2 containers matching `web*`?\nThey stay down until something starts them again.\n`web`, `webdav`\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.  [2 buttons]'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, inspect --format t=10, inspect --format t=5, inspect --format t=10'},
        events=[],
        raised=None),
    ('single', '/stop web*'): dict(
        out=['⚠ Stop 2 containers matching `web*`?\nThey stay down until something starts them again.\n`web`, `webdav`  [2 buttons]'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, inspect --format t=10, inspect --format t=5, inspect --format t=10'},
        events=[],
        raised=None),
    ('multi', '/stop web @nas'): dict(
        out=['⚠ Stop `web`? It stays down until something starts it again.  [2 buttons]'],
        hosts={'local': 'inspect --format t=5',
               'nas': 'ps -a --format, inspect --format t=5, inspect --format t=10'},
        events=[],
        raised=None),
    ('single', '/stop web @nas'): dict(
        out=['Container `web @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/stop web @all'): dict(
        out=['⚠ Stop 3 containers matching `web`?\nThey stay down until something starts them again.\n`web`, `web`, `web`  [2 buttons]'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, inspect --format t=10, inspect --format t=5 ×2',
               'nas': 'ps -a --format, inspect --format t=5, inspect --format t=10',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format, inspect --format t=5, inspect --format t=10'},
        events=[],
        raised=None),
    ('single', '/stop web @all'): dict(
        out=['Container `web @all` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/stop web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/stop web @typo'): dict(
        out=['Container `web @typo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /restart ────────────────────────────────────────────────────
    # :4444 bare `== '/restart'` restarts Docksentry and has to stay above
    # :4454, which takes every other '/restart…'.
    ('multi', '/restart'): dict(
        out=['🔄 Restarting — back in a moment. (restart policy: `unless-stopped`)'],
        hosts={'local': 'inspect --format t=5 ×2, inspect --format t=15'},
        events=['selfrestart.go_down'],
        raised=None),
    ('single', '/restart'): dict(
        out=['🔄 Restarting — back in a moment. (restart policy: `unless-stopped`)'],
        hosts={'local': 'inspect --format t=5 ×2, inspect --format t=15'},
        events=['selfrestart.go_down'],
        raised=None),
    ('multi', '/restart web'): dict(
        out=['🔁 `web` restarted.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, restart --time t=120'},
        events=[],
        raised=None),
    ('single', '/restart web'): dict(
        out=['🔁 `web` restarted.'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, restart --time t=120'},
        events=[],
        raised=None),
    ('multi', '/restart web*'): dict(
        out=['*restart — 2 match(es) for* `web*`*:*\n🔁 `web` restarted.\n🔁 `webdav` restarted.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, restart --time t=120, inspect --format t=5, restart --time t=120'},
        events=[],
        raised=None),
    ('single', '/restart web*'): dict(
        out=['*restart — 2 match(es) for* `web*`*:*\n🔁 `web` restarted.\n🔁 `webdav` restarted.'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, restart --time t=120, inspect --format t=5, restart --time t=120'},
        events=[],
        raised=None),
    ('multi', '/restart web @nas'): dict(
        out=['🔁 `web` restarted. @nas'],
        hosts={'local': 'inspect --format t=5',
               'nas': 'ps -a --format, inspect --format t=5, restart --time t=120'},
        events=[],
        raised=None),
    ('single', '/restart web @nas'): dict(
        out=['Container `web @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/restart web @all'): dict(
        out=['🔁 `web` restarted.',
             '🔁 `web` restarted. @nas',
             '🔁 `web` restarted. @flaky'],
        hosts={'local': 'ps -a --format, inspect --format t=5 ×2, restart --time t=120, inspect --format t=5 ×2',
               'nas': 'ps -a --format, inspect --format t=5, restart --time t=120',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format, inspect --format t=5, restart --time t=120'},
        events=[],
        raised=None),
    ('single', '/restart web @all'): dict(
        out=['Container `web @all` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/restart web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/restart web @typo'): dict(
        out=['Container `web @typo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /maintenance ────────────────────────────────────────────────
    # :4342 `startswith('/maintenance')`, so '/maintenancex' shows the
    # state; any argument goes to parse_duration.
    ('multi', '/maintenance'): dict(
        out=['✅ Maintenance mode is *off*.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/maintenance'): dict(
        out=['✅ Maintenance mode is *off*.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/maintenance web'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/maintenance web'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/maintenance web*'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/maintenance web*'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/maintenance web @nas'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/maintenance web @nas'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/maintenance web @all'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/maintenance web @all'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/maintenance web @typo'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/maintenance web @typo'): dict(
        out=['Usage: `/maintenance` to see status, `/maintenance 2h` (or `30m`, `1d`, `forever`) to enable, `/maintenance off` to disable.'],
        hosts={},
        events=[],
        raised=None),
    # ── /history ────────────────────────────────────────────────────
    # :4525 `== '/history'` only; with an argument nothing answers.
    ('multi', '/history'): dict(
        out=['📋 No updates recorded yet.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/history'): dict(
        out=['📋 No updates recorded yet.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/history web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/history web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/history web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/history web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/history web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/history web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/history web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/history web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/history web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/history web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/history @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/history @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    # ── /events ─────────────────────────────────────────────────────
    # :4430 `== '/events'` only; with an argument nothing answers.
    ('multi', '/events'): dict(
        out=['🩺 No container events recorded yet. Health flips, non-zero exits, OOM kills and crash-restarts will show up here.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/events'): dict(
        out=['🩺 No container events recorded yet. Health flips, non-zero exits, OOM kills and crash-restarts will show up here.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/events web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/events web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/events web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/events web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/events web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/events web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/events web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/events web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/events web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/events web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/events @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/events @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    # ── /groups ─────────────────────────────────────────────────────
    # :4861 `startswith('/groups ')`, :4885 `== '/groups'`; '/groupsx'
    # matches neither.
    ('multi', '/groups'): dict(
        out=['No container groups configured yet. Create one in the Web UI → Groups.',
             'No container groups configured yet. Create one in the Web UI → Groups. @nas',
             'No container groups configured yet. Create one in the Web UI → Groups. @dead',
             'No container groups configured yet. Create one in the Web UI → Groups. @refused',
             'No container groups configured yet. Create one in the Web UI → Groups. @flaky'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/groups'): dict(
        out=['No container groups configured yet. Create one in the Web UI → Groups.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/groups web'): dict(
        out=['⚠️ No group matching `web`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/groups web'): dict(
        out=['⚠️ No group matching `web`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/groups web*'): dict(
        out=['⚠️ No group matching `web*`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/groups web*'): dict(
        out=['⚠️ No group matching `web*`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/groups web @nas'): dict(
        out=['⚠️ No group matching `web`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/groups web @nas'): dict(
        out=['⚠️ No group matching `web @nas`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/groups web @all'): dict(
        out=['⚠️ No group matching `web`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/groups web @all'): dict(
        out=['⚠️ No group matching `web @all`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/groups web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/groups web @typo'): dict(
        out=['⚠️ No group matching `web @typo`. Try /groups to list them.'],
        hosts={},
        events=[],
        raised=None),
    # ── /pin ────────────────────────────────────────────────────────
    # :4545 `startswith('/pin')` — also '/ping' and '/pinned', which list
    # the pins. Listing is a read, setting a write (_flag_targets).
    ('multi', '/pin'): dict(
        out=['📌 No pinned containers.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/pin'): dict(
        out=['📌 No pinned containers.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/pin web'): dict(
        out=['📌 `web` pinned — will be skipped during updates.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/pin web'): dict(
        out=['📌 `web` pinned — will be skipped during updates.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/pin web*'): dict(
        out=['Container `web*` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/pin web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/pin web @nas'): dict(
        out=['📌 `web` pinned — will be skipped during updates. @nas'],
        hosts={'nas': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/pin web @nas'): dict(
        out=['📌 `web` pinned — will be skipped during updates.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/pin web @all'): dict(
        out=['📌 `web` pinned — will be skipped during updates.',
             '📌 `web` pinned — will be skipped during updates. @nas',
             "Could not list containers: Command '['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead",
             'Container `web` not found. @refused',
             '📌 `web` pinned — will be skipped during updates. @flaky'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/pin web @all'): dict(
        out=['📌 `web` pinned — will be skipped during updates.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/pin web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/pin web @typo'): dict(
        out=['📌 `web` pinned — will be skipped during updates.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /unpin ──────────────────────────────────────────────────────
    # :4558 `startswith('/unpin')`, _state_targets (always a write).
    ('multi', '/unpin'): dict(
        out=['Usage: `/unpin container_name`\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/unpin'): dict(
        out=['Usage: `/unpin container_name`'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/unpin web'): dict(
        out=['📌 `web` is not pinned.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/unpin web'): dict(
        out=['📌 `web` is not pinned.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/unpin web*'): dict(
        out=['📌 `web*` is not pinned.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/unpin web*'): dict(
        out=['📌 `web*` is not pinned.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/unpin web @nas'): dict(
        out=['📌 `web` is not pinned. @nas'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/unpin web @nas'): dict(
        out=['📌 `web` is not pinned.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/unpin web @all'): dict(
        out=['📌 `web` is not pinned.',
             '📌 `web` is not pinned. @nas',
             '📌 `web` is not pinned. @dead',
             '📌 `web` is not pinned. @refused',
             '📌 `web` is not pinned. @flaky'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/unpin web @all'): dict(
        out=['📌 `web` is not pinned.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/unpin web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/unpin web @typo'): dict(
        out=['📌 `web` is not pinned.'],
        hosts={},
        events=[],
        raised=None),
    # ── /autoupdate ─────────────────────────────────────────────────
    # :4573, _flag_targets like /pin.
    ('multi', '/autoupdate'): dict(
        out=['⚡ No containers set to auto-update.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/autoupdate'): dict(
        out=['⚡ No containers set to auto-update.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/autoupdate web'): dict(
        out=['⚡ `web` — auto-update enabled.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/autoupdate web'): dict(
        out=['⚡ `web` — auto-update enabled.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/autoupdate web*'): dict(
        out=['Container `web*` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/autoupdate web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/autoupdate web @nas'): dict(
        out=['⚡ `web` — auto-update enabled. @nas'],
        hosts={'nas': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/autoupdate web @nas'): dict(
        out=['⚡ `web` — auto-update enabled.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/autoupdate web @all'): dict(
        out=['⚡ `web` — auto-update enabled.',
             '⚡ `web` — auto-update enabled. @nas',
             "Could not list containers: Command '['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead",
             'Container `web` not found. @refused',
             '⚡ `web` — auto-update enabled. @flaky'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/autoupdate web @all'): dict(
        out=['⚡ `web` — auto-update enabled.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/autoupdate web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/autoupdate web @typo'): dict(
        out=['⚡ `web` — auto-update enabled.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /cooldown ───────────────────────────────────────────────────
    # :4586, _flag_targets like /pin.
    ('multi', '/cooldown'): dict(
        out=['No per-container update cooldowns set. `/cooldown <name> <seconds>` to add one.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/cooldown'): dict(
        out=['No per-container update cooldowns set. `/cooldown <name> <seconds>` to add one.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/cooldown web'): dict(
        out=['`web` update cooldown: *0s*. `/cooldown web <seconds>` to change (0 clears).\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/cooldown web'): dict(
        out=['`web` update cooldown: *0s*. `/cooldown web <seconds>` to change (0 clears).'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/cooldown web*'): dict(
        out=['Container `web*` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/cooldown web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/cooldown web @nas'): dict(
        out=['`web` update cooldown: *0s*. `/cooldown web <seconds>` to change (0 clears). @nas'],
        hosts={'nas': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/cooldown web @nas'): dict(
        out=['⚠️ Seconds must be a number (0–600). E.g. `/cooldown llama-server 20`.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/cooldown web @all'): dict(
        out=['`web` update cooldown: *0s*. `/cooldown web <seconds>` to change (0 clears).',
             '`web` update cooldown: *0s*. `/cooldown web <seconds>` to change (0 clears). @nas',
             "Could not list containers: Command '['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead",
             'Container `web` not found. @refused',
             '`web` update cooldown: *0s*. `/cooldown web <seconds>` to change (0 clears). @flaky'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/cooldown web @all'): dict(
        out=['⚠️ Seconds must be a number (0–600). E.g. `/cooldown llama-server 20`.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/cooldown web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/cooldown web @typo'): dict(
        out=['⚠️ Seconds must be a number (0–600). E.g. `/cooldown llama-server 20`.'],
        hosts={},
        events=[],
        raised=None),
    # ── /protect ────────────────────────────────────────────────────
    # :4600, _flag_targets like /pin.
    ('multi', '/protect'): dict(
        out=['No stop-protected containers. `/protect <name>` to add one.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/protect'): dict(
        out=['No stop-protected containers. `/protect <name>` to add one.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/protect web'): dict(
        out=['🛡 `web` is now protected from Stop. Restart still works.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/protect web'): dict(
        out=['🛡 `web` is now protected from Stop. Restart still works.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/protect web*'): dict(
        out=['Container `web*` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/protect web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/protect web @nas'): dict(
        out=['🛡 `web` is now protected from Stop. Restart still works. @nas'],
        hosts={'nas': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/protect web @nas'): dict(
        out=['🛡 `web` is now protected from Stop. Restart still works.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/protect web @all'): dict(
        out=['🛡 `web` is now protected from Stop. Restart still works.',
             '🛡 `web` is now protected from Stop. Restart still works. @nas',
             "Could not list containers: Command '['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead",
             'Container `web` not found. @refused',
             '🛡 `web` is now protected from Stop. Restart still works. @flaky'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/protect web @all'): dict(
        out=['🛡 `web` is now protected from Stop. Restart still works.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/protect web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/protect web @typo'): dict(
        out=['🛡 `web` is now protected from Stop. Restart still works.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /setlink ────────────────────────────────────────────────────
    # :4845, _state_targets; usage only when no argument at all.
    ('multi', '/setlink'): dict(
        out=['Usage: `/setlink container_name https://github.com/...` — or `/setlink container_name` to clear.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/setlink'): dict(
        out=['Usage: `/setlink container_name https://github.com/...` — or `/setlink container_name` to clear.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/setlink web'): dict(
        out=['🔗 `web` link cleared — OCI labels will be used again.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/setlink web'): dict(
        out=['🔗 `web` link cleared — OCI labels will be used again.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/setlink web*'): dict(
        out=['Container `web*` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/setlink web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/setlink web @nas'): dict(
        out=['🔗 `web` link cleared — OCI labels will be used again. @nas'],
        hosts={'nas': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/setlink web @nas'): dict(
        out=['🔗 URL must start with `http://` or `https://`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/setlink web @all'): dict(
        out=['🔗 `web` link cleared — OCI labels will be used again.',
             '🔗 `web` link cleared — OCI labels will be used again. @nas',
             "Could not list containers: Command '['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead",
             'Container `web` not found. @refused',
             '🔗 `web` link cleared — OCI labels will be used again. @flaky'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/setlink web @all'): dict(
        out=['🔗 URL must start with `http://` or `https://`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/setlink web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/setlink web @typo'): dict(
        out=['🔗 URL must start with `http://` or `https://`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /audit ──────────────────────────────────────────────────────
    # :4786; no argument audits Docksentry itself, a name is a read across
    # hosts (first match wins).
    ('multi', '/audit'): dict(
        out=['🔍 *Docksentry, looking at itself*\n0 Compose container(s): 0 readable, 0 behind a wrong mount, 0 with nothing mounted.\n• I am container `docksentry`\n• 0 mount(s) of my own'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect t=10, ps -a -q t=15, inspect t=30, inspect --format ×2'},
        events=[],
        raised=None),
    ('single', '/audit'): dict(
        out=['🔍 *Docksentry, looking at itself*\n0 Compose container(s): 0 readable, 0 behind a wrong mount, 0 with nothing mounted.\n• I am container `docksentry`\n• 0 mount(s) of my own'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect t=10, ps -a -q t=15, inspect t=30, inspect --format ×2'},
        events=[],
        raised=None),
    ('multi', '/audit web'): dict(
        out=['✅ `web`: clean — every non-default HostConfig and Config field is restored on recreate.'],
        hosts={'local': 'ps -a --format, inspect t=10'},
        events=[],
        raised=None),
    ('single', '/audit web'): dict(
        out=['✅ `web`: clean — every non-default HostConfig and Config field is restored on recreate.'],
        hosts={'local': 'ps -a --format, inspect t=10'},
        events=[],
        raised=None),
    ('multi', '/audit web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/audit web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/audit web @nas'): dict(
        out=['✅ `web`: clean — every non-default HostConfig and Config field is restored on recreate. @nas'],
        hosts={'nas': 'ps -a --format, inspect t=10'},
        events=[],
        raised=None),
    ('single', '/audit web @nas'): dict(
        out=['✅ `web`: clean — every non-default HostConfig and Config field is restored on recreate.'],
        hosts={'local': 'ps -a --format, inspect t=10'},
        events=[],
        raised=None),
    ('multi', '/audit web @all'): dict(
        out=['✅ `web`: clean — every non-default HostConfig and Config field is restored on recreate.'],
        hosts={'local': 'ps -a --format, inspect t=10'},
        events=[],
        raised=None),
    ('single', '/audit web @all'): dict(
        out=['✅ `web`: clean — every non-default HostConfig and Config field is restored on recreate.'],
        hosts={'local': 'ps -a --format, inspect t=10'},
        events=[],
        raised=None),
    ('multi', '/audit web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/audit web @typo'): dict(
        out=['✅ `web`: clean — every non-default HostConfig and Config field is restored on recreate.'],
        hosts={'local': 'ps -a --format, inspect t=10'},
        events=[],
        raised=None),
    # ── /backup ─────────────────────────────────────────────────────
    # :4755 `startswith('/backup')`; arguments are ignored.
    ('multi', '/backup'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/backup'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/backup web'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/backup web'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/backup web*'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/backup web*'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/backup web @nas'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/backup web @nas'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/backup web @all'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/backup web @all'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/backup web @typo'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/backup web @typo'): dict(
        out=['<document> 📦 Docksentry backup — restore via Web UI → Settings → Import.'],
        hosts={},
        events=[],
        raised=None),
    # ── /restore ────────────────────────────────────────────────────
    # :4713 `startswith('/restore')`; always the how-to.
    ('multi', '/restore'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('single', '/restore'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/restore web'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('single', '/restore web'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/restore web*'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('single', '/restore web*'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/restore web @nas'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('single', '/restore web @nas'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/restore web @all'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('single', '/restore web @all'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/restore web @typo'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    ('single', '/restore web @typo'): dict(
        out=["📥 *Restoring*\n\nSend the backup file into this chat — drag it in, or forward one from another Docksentry's chat. I will say what it contains and ask before changing anything.\n\nUse `/backup` to get one."],
        hosts={},
        events=[],
        raised=None),
    # ── /note ───────────────────────────────────────────────────────
    # :4613 `startswith('/note')` — also '/notes' (usage).
    ('multi', '/note'): dict(
        out=['Usage: `/note <container> <text>` — leave the text off to clear it.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/note'): dict(
        out=['Usage: `/note <container> <text>` — leave the text off to clear it.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/note web'): dict(
        out=['📝 Note cleared on `web`.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/note web'): dict(
        out=['📝 Note cleared on `web`.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/note web*'): dict(
        out=['Container `web*` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/note web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/note web @nas'): dict(
        out=['📝 Note cleared on `web`. @nas'],
        hosts={'nas': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/note web @nas'): dict(
        out=['📝 Note on `web`: @nas'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/note web @all'): dict(
        out=['📝 Note cleared on `web`.',
             '📝 Note cleared on `web`. @nas',
             "Could not list containers: Command '['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead",
             'Container `web` not found. @refused',
             '📝 Note cleared on `web`. @flaky'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/note web @all'): dict(
        out=['📝 Note on `web`: @all'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/note web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/note web @typo'): dict(
        out=['📝 Note on `web`: @typo'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/note web a  b @x'): dict(
        out=['❌ Unknown host `x`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/note web a  b @x'): dict(
        out=['📝 Note on `web`: a  b @x'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /trustrunning ───────────────────────────────────────────────
    # :4629, _flag_targets like /pin.
    ('multi', '/trustrunning'): dict(
        out=['🩺 Nothing is set to accept running-but-unhealthy.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/trustrunning'): dict(
        out=['🩺 Nothing is set to accept running-but-unhealthy.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/trustrunning web'): dict(
        out=['✅ `web`: a running-but-unhealthy result will be accepted after an update.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/trustrunning web'): dict(
        out=['✅ `web`: a running-but-unhealthy result will be accepted after an update.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/trustrunning web*'): dict(
        out=['Container `web*` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/trustrunning web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/trustrunning web @nas'): dict(
        out=['✅ `web`: a running-but-unhealthy result will be accepted after an update. @nas'],
        hosts={'nas': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/trustrunning web @nas'): dict(
        out=['✅ `web`: a running-but-unhealthy result will be accepted after an update.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/trustrunning web @all'): dict(
        out=['✅ `web`: a running-but-unhealthy result will be accepted after an update.',
             '✅ `web`: a running-but-unhealthy result will be accepted after an update. @nas',
             "Could not list containers: Command '['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead",
             'Container `web` not found. @refused',
             '✅ `web`: a running-but-unhealthy result will be accepted after an update. @flaky'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/trustrunning web @all'): dict(
        out=['✅ `web`: a running-but-unhealthy result will be accepted after an update.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/trustrunning web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/trustrunning web @typo'): dict(
        out=['✅ `web`: a running-but-unhealthy result will be accepted after an update.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /askmajor ───────────────────────────────────────────────────
    # :4642, _flag_targets like /pin.
    ('multi', '/askmajor'): dict(
        out=['⚠️ No container asks before a major update.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/askmajor'): dict(
        out=['⚠️ No container asks before a major update.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/askmajor web'): dict(
        out=['✅ `web`: a major version jump will ask first.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/askmajor web'): dict(
        out=['✅ `web`: a major version jump will ask first.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/askmajor web*'): dict(
        out=['Container `web*` not found.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/askmajor web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/askmajor web @nas'): dict(
        out=['✅ `web`: a major version jump will ask first. @nas'],
        hosts={'nas': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/askmajor web @nas'): dict(
        out=['✅ `web`: a major version jump will ask first.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/askmajor web @all'): dict(
        out=['✅ `web`: a major version jump will ask first.',
             '✅ `web`: a major version jump will ask first. @nas',
             "Could not list containers: Command '['docker', '-H', 'ssh://dead', 'ps', '-a', '--format', '{{.Names}}']' t @dead",
             'Container `web` not found. @refused',
             '✅ `web`: a major version jump will ask first. @flaky'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/askmajor web @all'): dict(
        out=['✅ `web`: a major version jump will ask first.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/askmajor web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/askmajor web @typo'): dict(
        out=['✅ `web`: a major version jump will ask first.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /testchannel ────────────────────────────────────────────────
    # :4655 `startswith('/testchannel')`; arguments ignored.
    ('multi', '/testchannel'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/testchannel'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/testchannel web'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/testchannel web'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/testchannel web*'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/testchannel web*'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/testchannel web @nas'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/testchannel web @nas'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/testchannel web @all'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/testchannel web @all'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/testchannel web @typo'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/testchannel web @typo'): dict(
        out=['🔔 Sending a test to every channel that is switched on…',
             '🔔 Test notification — if you can read this on a channel, that channel works.'],
        hosts={},
        events=[],
        raised=None),
    # ── /hosts ──────────────────────────────────────────────────────
    # :4662 `startswith('/hosts')` — also '/hostsx'; 'check <ep>' probes on
    # a thread.
    ('multi', '/hosts'): dict(
        out=['🖥 *Managed hosts*\n• `local` — `local`\n• `nas` — `ssh://nas`\n• `dead` — `ssh://dead`\n• `refused` — `ssh://refused`\n• `flaky` — `ssh://flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/hosts'): dict(
        out=['This instance manages a single host.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/hosts web'): dict(
        out=['🖥 *Managed hosts*\n• `local` — `local`\n• `nas` — `ssh://nas`\n• `dead` — `ssh://dead`\n• `refused` — `ssh://refused`\n• `flaky` — `ssh://flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/hosts web'): dict(
        out=['This instance manages a single host.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/hosts web*'): dict(
        out=['🖥 *Managed hosts*\n• `local` — `local`\n• `nas` — `ssh://nas`\n• `dead` — `ssh://dead`\n• `refused` — `ssh://refused`\n• `flaky` — `ssh://flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/hosts web*'): dict(
        out=['This instance manages a single host.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/hosts web @nas'): dict(
        out=['🖥 *Managed hosts*\n• `local` — `local`\n• `nas` — `ssh://nas`\n• `dead` — `ssh://dead`\n• `refused` — `ssh://refused`\n• `flaky` — `ssh://flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/hosts web @nas'): dict(
        out=['This instance manages a single host.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/hosts web @all'): dict(
        out=['🖥 *Managed hosts*\n• `local` — `local`\n• `nas` — `ssh://nas`\n• `dead` — `ssh://dead`\n• `refused` — `ssh://refused`\n• `flaky` — `ssh://flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/hosts web @all'): dict(
        out=['This instance manages a single host.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/hosts web @typo'): dict(
        out=['🖥 *Managed hosts*\n• `local` — `local`\n• `nas` — `ssh://nas`\n• `dead` — `ssh://dead`\n• `refused` — `ssh://refused`\n• `flaky` — `ssh://flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/hosts web @typo'): dict(
        out=['This instance manages a single host.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/hosts check ssh://x'): dict(
        out=['🔎 *Pre-flight for* `ssh://x`\n• Nothing answered at `ssh://x` — stand-in: not dialled'],
        hosts={},
        events=["RemoteBackend('ssh://x')", "hostcheck.preflight('ssh://x')"],
        raised=None),
    ('single', '/hosts check ssh://x'): dict(
        out=['🔎 *Pre-flight for* `ssh://x`\n• Nothing answered at `ssh://x` — stand-in: not dialled'],
        hosts={},
        events=["RemoteBackend('ssh://x')", "hostcheck.preflight('ssh://x')"],
        raised=None),
    # ── /updateall ──────────────────────────────────────────────────
    # :4721 `startswith('/updateall')`; takes an @token only, then goes
    # through the update_all callback.
    ('multi', '/updateall'): dict(
        out=['<toast> Updates are starting...',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest): updated (stand-in)'],
        hosts={'local': 'inspect --format t=10, inspect --format t=5, inspect --format'},
        events=['update_container@local:web'],
        raised=None),
    ('single', '/updateall'): dict(
        out=['<toast> Updates are starting...',
             '⏳ Starting update for 1 containers...',
             '*Update Result:*\n\n✅ [web](https://github.com/example/web/releases/latest): updated (stand-in)'],
        hosts={'local': 'inspect --format t=10, inspect --format t=5, inspect --format'},
        events=['update_container@local:web'],
        raised=None),
    ('multi', '/updateall web'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updateall web'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updateall web*'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updateall web*'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updateall web @nas'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updateall web @nas'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updateall web @all'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updateall web @all'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updateall web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updateall web @typo'): dict(
        out=['`/updateall` updates everything that has an update waiting. It takes a host (`@nas`, `@all`) and nothing else — for one container use `/update <name>`.'],
        hosts={},
        events=[],
        raised=None),
    # ── /selfupdate ─────────────────────────────────────────────────
    # :4500 bare, :4503 with the whole rest as the target (the @token
    # included).
    ('multi', '/selfupdate'): dict(
        out=[],
        hosts={},
        events=['selfupdate.run target=None'],
        raised=None),
    ('single', '/selfupdate'): dict(
        out=[],
        hosts={},
        events=['selfupdate.run target=None'],
        raised=None),
    ('multi', '/selfupdate web'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web'"],
        raised=None),
    ('single', '/selfupdate web'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web'"],
        raised=None),
    ('multi', '/selfupdate web*'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web*'"],
        raised=None),
    ('single', '/selfupdate web*'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web*'"],
        raised=None),
    ('multi', '/selfupdate web @nas'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web @nas'"],
        raised=None),
    ('single', '/selfupdate web @nas'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web @nas'"],
        raised=None),
    ('multi', '/selfupdate web @all'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web @all'"],
        raised=None),
    ('single', '/selfupdate web @all'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web @all'"],
        raised=None),
    ('multi', '/selfupdate web @typo'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web @typo'"],
        raised=None),
    ('single', '/selfupdate web @typo'): dict(
        out=[],
        hosts={},
        events=["selfupdate.run target='web @typo'"],
        raised=None),
    # ── /changelog ──────────────────────────────────────────────────
    # :4205 bare (GitHub fetch), :4253 a container on the local host; an
    # @token stays part of the name.
    ('multi', '/changelog'): dict(
        out=['🔍 Fetching changelog from GitHub...',
             "❌ Couldn't fetch the changelog: offline (stand-in)"],
        hosts={},
        events=['changelog.fetch'],
        raised=None),
    ('single', '/changelog'): dict(
        out=['🔍 Fetching changelog from GitHub...',
             "❌ Couldn't fetch the changelog: offline (stand-in)"],
        hosts={},
        events=['changelog.fetch'],
        raised=None),
    ('multi', '/changelog web'): dict(
        out=["📄 *Changelog for* `web`\n\nDocksentry doesn't fetch and parse arbitrary container changelogs — projects use too many different formats (Keep-a-Changelog, GitHub Releases, HISTORY.md, none at all) for a reliable summary. Instead, here's the upstream source:\n\n🔗 https://github.com/example/web/releases/latest\n\nReleases are typically at `/releases` and changelog files at `/blob/main/CHANGELOG.md`."],
        hosts={'local': 'ps -a --format, inspect --format t=5, inspect --format t=10, inspect --format t=5'},
        events=[],
        raised=None),
    ('single', '/changelog web'): dict(
        out=["📄 *Changelog for* `web`\n\nDocksentry doesn't fetch and parse arbitrary container changelogs — projects use too many different formats (Keep-a-Changelog, GitHub Releases, HISTORY.md, none at all) for a reliable summary. Instead, here's the upstream source:\n\n🔗 https://github.com/example/web/releases/latest\n\nReleases are typically at `/releases` and changelog files at `/blob/main/CHANGELOG.md`."],
        hosts={'local': 'ps -a --format, inspect --format t=5, inspect --format t=10, inspect --format t=5'},
        events=[],
        raised=None),
    ('multi', '/changelog web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/changelog web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/changelog web @nas'): dict(
        out=['Container `web @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/changelog web @nas'): dict(
        out=['Container `web @nas` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/changelog web @all'): dict(
        out=['Container `web @all` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/changelog web @all'): dict(
        out=['Container `web @all` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/changelog web @typo'): dict(
        out=['Container `web @typo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/changelog web @typo'): dict(
        out=['Container `web @typo` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    # ── /debug ──────────────────────────────────────────────────────
    # :4336 `== '/debug'` only; with an argument nothing answers.
    ('multi', '/debug'): dict(
        out=['*Debug Mode:* ON 🔍'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/debug'): dict(
        out=['*Debug Mode:* ON 🔍'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/debug web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/debug web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/debug web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/debug web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/debug web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/debug web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/debug web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/debug web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/debug web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/debug web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/debug x'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/debug x'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    # ── /logs ───────────────────────────────────────────────────────
    # :4905 `startswith('/logs')`; a read across hosts, first match.
    ('multi', '/logs'): dict(
        out=['Usage: `/logs container_name`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/logs'): dict(
        out=['Usage: `/logs container_name`'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/logs web'): dict(
        out=['📋 *Logs for* `web` (last 30 lines):\n```\nline one\nline two\n```'],
        hosts={'local': 'ps -a --format, logs --tail t=10'},
        events=[],
        raised=None),
    ('single', '/logs web'): dict(
        out=['📋 *Logs for* `web` (last 30 lines):\n```\nline one\nline two\n```'],
        hosts={'local': 'ps -a --format, logs --tail t=10'},
        events=[],
        raised=None),
    ('multi', '/logs web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format',
               'nas': 'ps -a --format',
               'dead': 'ps -a --format',
               'refused': 'ps -a --format',
               'flaky': 'ps -a --format'},
        events=[],
        raised=None),
    ('single', '/logs web*'): dict(
        out=['Container `web*` not found.'],
        hosts={'local': 'ps -a --format'},
        events=[],
        raised=None),
    ('multi', '/logs web @nas'): dict(
        out=['📋 *Logs for* `web` (last 30 lines): @nas\n```\nline one\nline two\n```'],
        hosts={'nas': 'ps -a --format, logs --tail t=10'},
        events=[],
        raised=None),
    ('single', '/logs web @nas'): dict(
        out=['📋 *Logs for* `web` (last 30 lines):\n```\nline one\nline two\n```'],
        hosts={'local': 'ps -a --format, logs --tail t=10'},
        events=[],
        raised=None),
    ('multi', '/logs web @all'): dict(
        out=['📋 *Logs for* `web` (last 30 lines):\n```\nline one\nline two\n```'],
        hosts={'local': 'ps -a --format, logs --tail t=10'},
        events=[],
        raised=None),
    ('single', '/logs web @all'): dict(
        out=['📋 *Logs for* `web` (last 30 lines):\n```\nline one\nline two\n```'],
        hosts={'local': 'ps -a --format, logs --tail t=10'},
        events=[],
        raised=None),
    ('multi', '/logs web @typo'): dict(
        out=['❌ Unknown host `typo`. Managed hosts: `local`, `nas`, `dead`, `refused`, `flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/logs web @typo'): dict(
        out=['📋 *Logs for* `web` (last 30 lines):\n```\nline one\nline two\n```'],
        hosts={'local': 'ps -a --format, logs --tail t=10'},
        events=[],
        raised=None),
    # ── /lang ───────────────────────────────────────────────────────
    # :4512 `startswith('/lang')` — also '/language' (usage).
    ('multi', '/lang'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/lang'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/lang web'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/lang web'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/lang web*'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/lang web*'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/lang web @nas'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/lang web @nas'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/lang web @all'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/lang web @all'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/lang web @typo'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/lang web @typo'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    # ── /settings ───────────────────────────────────────────────────
    # :4891 `== '/settings'` only.
    ('multi', '/settings'): dict(
        out=['⚙️ *Current Settings*\n\n🗓️ Schedule: `0 18 * * *`\n🌍 Language: `en`\n🔄 Auto-Selfupdate: OFF\n🔍 Debug: OFF\n🚫 Exclude: `-`\n📌 Pinned: `-`\n⚡ Auto-Update: `-`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/settings'): dict(
        out=['⚙️ *Current Settings*\n\n🗓️ Schedule: `0 18 * * *`\n🌍 Language: `en`\n🔄 Auto-Selfupdate: OFF\n🔍 Debug: OFF\n🚫 Exclude: `-`\n📌 Pinned: `-`\n⚡ Auto-Update: `-`'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/settings web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/settings web'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/settings web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/settings web*'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/settings web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/settings web @nas'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/settings web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/settings web @all'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/settings web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/settings web @typo'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    # ── /help ───────────────────────────────────────────────────────
    # :4937 `startswith('/help ')` per-command help, :4962 bare.
    ('multi', '/help'): dict(
        out=['*Docksentry v{VERSION}* 🐳\n\n💡 *Tip:* type `/` in this chat — Telegram shows an autocomplete picker with all commands and their descriptions.\n\n*Commands:*\n/status — Show container status\n/check — Check for updates now\n/update — Update matching containers (name or glob)\n/updates — Show pending updates\n…#fc869fcb47'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/help'): dict(
        out=['*Docksentry v{VERSION}* 🐳\n\n💡 *Tip:* type `/` in this chat — Telegram shows an autocomplete picker with all commands and their descriptions.\n\n*Commands:*\n/status — Show container status\n/check — Check for updates now\n/update — Update matching containers (name or glob)\n/updates — Show pending updates\n…#fdc7ab3473'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/help web'): dict(
        out=['Unknown command `/web`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/note`,…#2350748a81'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/help web'): dict(
        out=['Unknown command `/web`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/note`,…#2350748a81'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/help web*'): dict(
        out=['Unknown command `/web*`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/note`…#72667f06c8'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/help web*'): dict(
        out=['Unknown command `/web*`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/note`…#72667f06c8'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/help web @nas'): dict(
        out=['Unknown command `/web @nas`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/n…#a5a1ce488f'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/help web @nas'): dict(
        out=['Unknown command `/web @nas`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/n…#a5a1ce488f'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/help web @all'): dict(
        out=['Unknown command `/web @all`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/n…#4ec5e489b0'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/help web @all'): dict(
        out=['Unknown command `/web @all`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/n…#4ec5e489b0'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/help web @typo'): dict(
        out=['Unknown command `/web @typo`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/…#32a792c076'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/help web @typo'): dict(
        out=['Unknown command `/web @typo`. Try one of: `/status`, `/check`, `/update`, `/updates`, `/cleanup`, `/checkimages`, `/start`, `/stop`, `/restart`, `/maintenance`, `/history`, `/events`, `/groups`, `/pin`, `/unpin`, `/autoupdate`, `/cooldown`, `/protect`, `/setlink`, `/audit`, `/backup`, `/restore`, `/…#32a792c076'],
        hosts={},
        events=[],
        raised=None),
    # ── no branch ───────────────────────────────────────────────────
    # no branch: '/statusx', '/checkx', '/updatex', '/notacommand', '/debug
    # x', '/groupsx' answer nothing. '-?' does not start with '/' (:3653
    # only rewrites '/cmd -?'). '/status@otherbot' is for another bot
    # (:3641) and stays silent.
    ('multi', '/dryrun'): dict(
        out=['🔍 Checking for updates...',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) @nas (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._',
             '⚠️ Host `dead` could not be checked: no answer within 30 seconds',
             '⚠️ Host `refused` could not be checked: could not list containers (rc=255): ssh: connect to host 192.0.2.30 port 22: Connection refused',
             '⚠️ Host `flaky` could not be checked: registry did not answer in 15s'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5 ×2',
               'nas': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5',
               'dead': 'ps --format',
               'refused': 'ps --format'},
        events=['check_all@local', 'tags@local:library/nginx', 'check_all@nas', 'tags@nas:library/nginx', 'check_all@dead', 'check_all@refused', 'check_all@flaky'],
        raised=None),
    ('single', '/dryrun'): dict(
        out=['🔍 Checking for updates...',
             '🧪 *Dry run — what an update would do*\n_Nothing is changed below._\n\n• [web](https://github.com/example/web/releases/latest) (nginx:1.25)\n  📦 10 MB | 🗓️ Current: 2026-01-01\n  ↪ via standalone recreate (rebuilt from `docker inspect`)\n\n\n_Nothing was changed. Run /check and tap Update to apply._'],
        hosts={'local': 'ps --format, inspect --format t=5 ×2, inspect t=60, inspect --format ×2, inspect --format t=10, inspect --format t=5'},
        events=['check_all@local', 'tags@local:library/nginx'],
        raised=None),
    ('multi', '/stopx'): dict(
        out=['Usage: `/start <name>`, `/stop <name>`, or `/restart <name>`. Partial name matching works the same as `/pin`.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/stopx'): dict(
        out=['Usage: `/start <name>`, `/stop <name>`, or `/restart <name>`. Partial name matching works the same as `/pin`.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/ping'): dict(
        out=['📌 No pinned containers.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/ping'): dict(
        out=['📌 No pinned containers.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/pinned'): dict(
        out=['📌 No pinned containers.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/pinned'): dict(
        out=['📌 No pinned containers.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/notes'): dict(
        out=['Usage: `/note <container> <text>` — leave the text off to clear it.\n\nℹ️ Local host only — add `@<host>` or `@all` for the others.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/notes'): dict(
        out=['Usage: `/note <container> <text>` — leave the text off to clear it.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/language'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/language'): dict(
        out=['Usage: `/lang en` or `/lang de`\n\n📂 ar, de, en, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, uk, zh'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/maintenancex'): dict(
        out=['✅ Maintenance mode is *off*.'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/maintenancex'): dict(
        out=['✅ Maintenance mode is *off*.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/hostsx'): dict(
        out=['🖥 *Managed hosts*\n• `local` — `local`\n• `nas` — `ssh://nas`\n• `dead` — `ssh://dead`\n• `refused` — `ssh://refused`\n• `flaky` — `ssh://flaky`'],
        hosts={},
        events=[],
        raised=None),
    ('single', '/hostsx'): dict(
        out=['This instance manages a single host.'],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/statusx'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/statusx'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/checkx'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/checkx'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/updatex'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/updatex'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/notacommand'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/notacommand'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '/groupsx'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '/groupsx'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('multi', '-?'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
    ('single', '-?'): dict(
        out=[],
        hosts={},
        events=[],
        raised=None),
}
# ── end of the generated table ──

if __name__ == "__main__":
    sys.exit(main())
