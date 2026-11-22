#!/usr/bin/env python3
"""A pin set after the update was found holds on every manual path.

Pinning a container does not touch the pending list, so the entry from
before the pin stays, and only `check_all` ever skipped a pinned
container. Measured on 0779842 (2.19.1-beta.1) with a readable pin list
naming `db`, `db` still pending from before and the update lock free:
the Telegram button, "Update all" with and without a snapshot
(`/updateall`), the major confirmation (Telegram and Web), Discord
`/update` and `/updateall` with its press, and the Web UI row and bulk
buttons all reached `update_container` for `db`. Only a typed `/update`
held, because it checks first.

Now the update itself asks, right before `update_container`: the pin
list of that container's host (an unreadable one holds everything, as
the refusals before the lock already do), `EXCLUDE_CONTAINERS`, and the
`docksentry.pin` / `docksentry.exclude` / `docksentry.enable=false`
labels. A held container is not updated, its pending entry or deferred
major stays, and the answer says why, per container, on every channel.
The scheduled pass is left as it was: its list comes from a check that
has just applied the same rules.

Driven through the real handlers and the real batch, with a real
`UpdateChecker` reading a real pin file and a real pending file on disk.
Stood in for: the daemon (`ps`, the label `inspect`), the registry, and
`update_container` itself, which records the call instead of recreating
anything. Every hold has its counter-direction on the same path with
nothing holding, or a path that refuses everything would pass.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import types
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402
from discord_bot import DiscordBot  # noqa: E402
from i18n import get_translator  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402
from update_checker import EVERYTHING_PINNED, UpdateChecker  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402

with open(os.path.join(os.path.dirname(__file__), "..", "app", "lang",
                       "en.json"), encoding="utf-8") as _f:
    EN = json.load(_f)

ENTRY = {"name": "db", "image": "reg/db:1", "host": "local"}
OTHER = {"name": "web", "image": "reg/web:1", "host": "local"}

#: name → (pin file, EXCLUDE_CONTAINERS, labels on db, the reason said)
HOLDS = {
    "pinned": (b'["db"]', [], {}, "pinned"),
    "excluded": (b'[]', ["db"], {}, "excluded"),
    "excluded by a glob": (b'[]', ["d*"], {}, "excluded"),
    "a docksentry.pin label": (b'[]', [], {"docksentry.pin": "true"},
                               "pinned_label"),
    "a docksentry.exclude label": (b'[]', [], {"docksentry.exclude": "true"},
                                   "excluded"),
    "docksentry.enable=false": (b'[]', [], {"docksentry.enable": "false"},
                                "excluded"),
}
FREE = (b'[]', [], {}, None)

checks = {}


def _quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def _tail(why):
    """The part of the held line that says why, after the name."""
    # `.get`, so a tree without the key is red, not a traceback.
    text = EN.get(f"update_held_{why}", "\0missing {name} " + why)
    return text.split("{name}", 1)[1]


def _says(said, why, name="db"):
    """Some message names `name` and gives `why`."""
    tail = _tail(why)
    return any(name in m and tail in m for m in said)


class _Daemon:
    """Answers `ps` and the label `inspect`. Environment, not decision."""

    name = "local"

    def __init__(self, labels):
        self.labels = labels
        self.inspected = []

    def run(self, args, timeout=None, **kw):
        if args[:1] == ["ps"]:
            return types.SimpleNamespace(
                returncode=0, stdout="db|reg/db:1\nweb|reg/web:1\n",
                stderr="")
        if args[:3] == ["inspect", "--format", "{{json .Config.Labels}}"]:
            self.inspected.append(args[3])
            lab = self.labels if args[3] == "db" else {}
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(lab), stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")


def _world(hold, pending=(ENTRY,)):
    pin_bytes, exclude, labels, _why = hold
    tmp = tempfile.mkdtemp(prefix="ds-pinheld-")
    cfg = types.SimpleNamespace(
        debug=False, language="en", exclude_containers=list(exclude),
        pinned_file=os.path.join(tmp, "pinned_containers.json"),
        pending_file=os.path.join(tmp, "pending.json"),
        advisories_file=os.path.join(tmp, "adv.json"),
        discord_bot_token="t", discord_app_id="app", discord_guild_id="g",
        save_persistent=lambda: None)
    with open(cfg.pinned_file, "wb") as f:
        f.write(pin_bytes)
    with open(cfg.pending_file, "w", encoding="utf-8") as f:
        json.dump([dict(u) for u in pending], f)
    ck = UpdateChecker(cfg, backend=_Daemon(labels))
    ck._own_container_name = lambda: ""
    ck.netns_target_name = lambda n: None
    ck.has_selfupdate_available = lambda: False
    ck.updated = []

    def _update_container(name, image, **kw):
        ck.updated.append(name)
        return True, "OK"
    ck.update_container = _update_container
    return ck, cfg


def _pending(cfg):
    if not os.path.exists(cfg.pending_file):
        return []
    with open(cfg.pending_file, encoding="utf-8") as f:
        return sorted(u.get("name") for u in json.load(f))


def _engine():
    eng = UpdateEngine.__new__(UpdateEngine)
    eng._update_lock = threading.Lock()
    eng._update_queue = []
    eng._queued_at = {}
    eng._update_queue_lock = threading.Lock()
    eng._updating = {}
    eng._queued_selfupdate = None
    eng._swap_in_flight = False
    return eng


class _MajorStore:
    def __init__(self):
        self.major = {"db": {"image": "reg/db:2", "compose": {}}}

    def get_pending_major(self):
        return dict(self.major)

    def remove_pending_major(self, name):
        self.major.pop(name, None)

    def add_pending_major(self, name, data):
        self.major[name] = data

    def get_groups(self):
        return {}

    def get_ask_before_major(self):
        return []


def _tg(ck, cfg):
    said = []
    bot = TelegramBot.__new__(TelegramBot)
    bot.config = cfg
    bot.t = get_translator("en")
    bot.send_message = lambda m, **kw: said.append(m)
    bot._enrich_with_source_url = lambda ups: None
    bot._display_name = lambda u: u["name"]
    bot._host_by_name = lambda n: None
    bot._checker_for = lambda host, checker: checker
    bot._group_of = lambda k: None
    bot._run_queued_selfupdate = lambda: None
    bot._drain_update_queue = lambda *a, **k: None
    bot._multi = lambda: False
    bot._maybe_cooldown = lambda *a, **k: None
    bot.engine = _engine()
    bot.notifier = None
    bot.hosts = None
    bot.store = _MajorStore()
    bot._store_for = lambda host_name: bot.store
    bot.link_resolver = types.SimpleNamespace(
        resolve_container_link=lambda *a, **k: "")
    bot._process_update_batch = (
        lambda *a, **k: UpdateEngine._process_update_batch(bot, *a, **k))
    bot.mark_updating = bot.engine.mark_updating
    bot.clear_updating = bot.engine.clear_updating
    return bot, said


def _run_tg(run, hold, pending=(ENTRY,)):
    ck, cfg = _world(hold, pending)
    bot, said = _tg(ck, cfg)
    try:
        _quiet(run, bot, ck)
    except Exception as e:                                  # noqa: BLE001
        said.append(f"RAISED {type(e).__name__}: {e}")
    return ck.updated, said, _pending(cfg), dict(bot.store.major)


# ═════════════════════════════════════════════════════════════════════
# Telegram
# ═════════════════════════════════════════════════════════════════════
TG_PATHS = {
    "the Telegram update button":
        lambda bot, ck: bot._run_single_update(ck, "db"),
    "a queued Telegram tap, drained":
        lambda bot, ck: bot._run_single_update(ck, "db", from_queue=True),
    "Telegram \"Update all\" from a notification":
        lambda bot, ck: bot.run_updates(ck, updates=[dict(ENTRY)]),
    "Telegram \"Update all\" without a snapshot (/updateall)":
        lambda bot, ck: bot.run_updates(ck),
    "the major confirmation (Telegram, and the Web UI's button)":
        lambda bot, ck: bot._confirm_major_update(ck, "db"),
}

for label, run in TG_PATHS.items():
    major_path = "major" in label
    for hname, hold in HOLDS.items():
        updated, said, pending, major = _run_tg(run, hold)
        checks[f"{label}, db {hname}: not updated"] = updated == []
        checks[f"…and says why ({label}, {hname})"] = _says(said, hold[3])
        checks[f"…and the {'deferred major' if major_path else 'pending entry'}"
               f" stays ({label}, {hname})"] = (
            "db" in major if major_path else pending == ["db"])
        if updated or not _says(said, hold[3]):
            print(f"      → {label}, {hname}: updated {updated}, said {said}")
    updated, said, pending, major = _run_tg(run, FREE)
    checks[f"{label}: with nothing holding it, db is updated"] = (
        updated == ["db"]
        and not any(_tail(w) in m for m in said
                    for w in ("pinned", "excluded", "pinned_label")))
    if updated != ["db"]:
        print(f"      → {label} (free): updated {updated}, said {said}")

# Only what is held stays: "Update all" without a snapshot used to delete
# the whole pending file afterwards.
for label, run in (
        ("Telegram \"Update all\" without a snapshot",
         lambda bot, ck: bot.run_updates(ck)),
        ("Telegram \"Update all\" from a notification",
         lambda bot, ck: bot.run_updates(
             ck, updates=[dict(ENTRY), dict(OTHER)]))):
    updated, said, pending, _m = _run_tg(run, HOLDS["pinned"],
                                         pending=(ENTRY, OTHER))
    checks[f"{label}, db pinned and web not: web is updated"] = (
        updated == ["web"])
    checks[f"…and only db stays pending ({label})"] = pending == ["db"]

# The same notification tapped twice: held while pinned, then unpinned.
# A hold the batch noted on the entry the first time must not keep the
# updated container in the pending list the second time.
ck, cfg = _world(HOLDS["pinned"])
bot, said = _tg(ck, cfg)
_snapshot = [dict(ENTRY)]
_quiet(bot.run_updates, ck, updates=_snapshot)
with open(cfg.pinned_file, "wb") as f:
    f.write(b"[]")
_quiet(bot.run_updates, ck, updates=_snapshot)
checks["the same notification after an unpin updates db"] = (
    ck.updated == ["db"])
checks["…and takes it off the pending list"] = _pending(cfg) == []

# The list may break after the refusal before the lock asked: the
# update asks again, and an unreadable list holds the container.
def _breaks_later(bot, ck):
    real, calls = ck._get_pinned, []

    def _read():
        calls.append(1)
        return frozenset() if len(calls) == 1 else EVERYTHING_PINNED
    ck._get_pinned = _read
    try:
        bot.run_updates(ck)
    finally:
        ck._get_pinned = real


updated, said, pending, _m = _run_tg(_breaks_later, FREE)
checks["a pin list that breaks after the first refusal still holds the "
       "update"] = updated == []
checks["…and says the pin list could not be read"] = _says(
    said, "pin_list_unreadable")
checks["…and the pending entry stays"] = pending == ["db"]


# The scheduled pass is left as it was: its list comes from a check that
# just applied the same rules, and nothing about it changed here.
ck, cfg = _world(HOLDS["a docksentry.pin label"])
bot, said = _tg(ck, cfg)
_quiet(UpdateEngine._process_update_batch, bot, [dict(ENTRY)], ck, auto=True)
checks["the scheduled batch is unchanged: it does not ask again"] = (
    ck.updated == ["db"] and ck.backend.inspected.count("db") <= 1)


# ═════════════════════════════════════════════════════════════════════
# Discord — the real batch behind the engine
# ═════════════════════════════════════════════════════════════════════
class _Engine:
    def __init__(self, tb):
        self._update_lock = threading.Lock()
        self.update_running = False
        self._tb = tb

    def _process_update_batch(self, updates, checker, *, auto):
        return UpdateEngine._process_update_batch(self._tb, updates, checker,
                                                  auto=auto)


class _Registry:
    def __init__(self, hosts):
        self.hosts = hosts

    def __iter__(self):
        return iter(self.hosts)

    def __len__(self):
        return len(self.hosts)

    is_multi = False

    @property
    def local(self):
        return self.hosts[0]

    @property
    def names(self):
        return [h.name for h in self.hosts]

    def get(self, n):
        return next((h for h in self.hosts if h.name == n), None)


def _discord(hold, pending=(ENTRY,)):
    ck, cfg = _world(hold, pending)
    tb, _said = _tg(ck, cfg)
    host = types.SimpleNamespace(name="local", is_local=True,
                                 endpoint="unix:///x", checker=ck,
                                 backend=ck.backend, store=None)
    bot = DiscordBot(cfg, None, _Engine(tb), hosts=_Registry([host]),
                     checker=ck, log=lambda *_: None)
    return bot, ck, cfg


def _call(bot, name, **opts):
    return _quiet(bot._dispatch, {
        "member": {"user": {"id": "u1"}},
        "data": {"name": name,
                 "options": [{"name": k, "value": v}
                             for k, v in opts.items()]}})


def _text(reply):
    return getattr(reply, "content", reply) or ""


def _updateall(bot):
    reply = _call(bot, "updateall")
    uid = (reply.components[0]["components"][0]["custom_id"]
           if getattr(reply, "components", None) else "")
    return _text(_quiet(bot._on_component, uid,
                        {"member": {"user": {"id": "u1"}}}))


DC_PATHS = {
    "Discord /update": lambda bot: _text(_call(bot, "update",
                                               container="db")),
    "Discord /updateall and its press": _updateall,
}
for label, run in DC_PATHS.items():
    for hname, hold in HOLDS.items():
        bot, ck, cfg = _discord(hold)
        out = run(bot)
        checks[f"{label}, db {hname}: not updated"] = ck.updated == []
        checks[f"…and says why ({label}, {hname})"] = _says([out], hold[3])
        checks[f"…and the pending entry stays ({label}, {hname})"] = (
            _pending(cfg) == ["db"])
        if ck.updated or not _says([out], hold[3]):
            print(f"      → {label}, {hname}: updated {ck.updated}: {out}")
    bot, ck, cfg = _discord(FREE)
    run(bot)
    checks[f"{label}: with nothing holding it, db is updated"] = (
        ck.updated == ["db"] and _pending(cfg) == [])

bot, ck, cfg = _discord(HOLDS["pinned"], pending=(ENTRY, OTHER))
_updateall(bot)
checks["Discord /updateall, db pinned and web not: web is updated and "
       "only db stays pending"] = (
    ck.updated == ["web"] and _pending(cfg) == ["db"])


# ═════════════════════════════════════════════════════════════════════
# Web UI
# ═════════════════════════════════════════════════════════════════════
class _WebBot:
    def __init__(self, ck, cfg):
        self.enabled = True
        self.update_running = False
        self.config = cfg
        tb, _s = _tg(ck, cfg)
        self.engine = _Engine(tb)
        self.sent = []
        self.t = get_translator("en")
        self.store = None
        self.majors = []

    def send_message(self, m, **kw):
        self.sent.append(m)

    def _remove_from_pending(self, keys):
        TelegramBot._remove_from_pending(self, keys)

    def _pending_key(self, u):
        return (u.get("host") or "local", u["name"])

    def _run_queued_selfupdate(self):
        pass

    def _confirm_major_update(self, checker, key):
        self.majors.append(key)


def _handler(ck, cfg, wbot):
    cls = web_ui.create_handler(cfg, checker=ck, bot=wbot, store=None)
    return cls.__new__(cls)


def _post(path, body, hold):
    """POST `body` to `path`; (redirect, threads started, ck, cfg)."""
    ck, cfg = _world(hold)
    cfg.web_password = ""
    cfg.web_username = ""
    wbot = _WebBot(ck, cfg)
    h = _handler(ck, cfg, wbot)
    h.path = path
    raw = body.encode()
    h.headers = {"Content-Length": str(len(raw)), "Host": "x",
                 "Origin": "http://x"}
    h.client_address = ("127.0.0.1", 0)
    h.rfile = types.SimpleNamespace(read=lambda n: raw)
    h._session_user = lambda: "admin"
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    h._action_target = lambda params: ("local", "db", None, ck, None)
    seen = {}
    h._send_redirect = lambda loc: seen.setdefault("to", loc)
    started = []

    class _Thread:
        def __init__(self, target=None, args=(), **kw):
            self._args = args

        def start(self):
            started.append(self._args)

    real = web_ui.threading.Thread
    web_ui.threading.Thread = _Thread
    try:
        _quiet(h.do_POST)
    except Exception as e:                                  # noqa: BLE001
        seen.setdefault("to", f"RAISED {type(e).__name__}: {e}")
    finally:
        web_ui.threading.Thread = real
    return seen.get("to", "nothing"), started, ck, cfg


def _held_query(to):
    q = parse_qs(urlparse(to).query)
    return (q.get("update") or [""])[0], (q.get("held") or [""])[0]


WEB_POSTS = {
    "the Web UI update button": ("/api/update", "name=db"),
    "the Web UI bulk update": ("/api/bulk", "action=update&names=db"),
    "the Web UI major confirmation": ("/api/major_confirm",
                                      "name=db&action=confirm"),
}
# The page answers the click before a worker starts, from the pin list
# and EXCLUDE_CONTAINERS. Labels are left to the worker: they cost an
# `inspect` per container, on a host that may be slow to answer.
for label, (path, body) in WEB_POSTS.items():
    for hname in ("pinned", "excluded", "excluded by a glob"):
        hold = HOLDS[hname]
        to, started, ck, cfg = _post(path, body, hold)
        upd, held = _held_query(to)
        checks[f"{label}, db {hname}: no worker starts"] = started == []
        checks[f"…and the page is told which and why ({label}, {hname})"] = (
            upd == "pinned" and held == f"{hold[3]}:db")
        checks[f"…and the pending entry stays ({label}, {hname})"] = (
            _pending(cfg) == ["db"])
        if upd != "pinned":
            print(f"      → {label}, {hname}: redirected to {to}")
    to, started, ck, cfg = _post(path, body, FREE)
    checks[f"{label}: with nothing holding it, the worker starts"] = (
        len(started) == 1 and "pinned" not in to)

# The worker behind the row and the bulk bar, reached directly.
for hname, hold in HOLDS.items():
    ck, cfg = _world(hold)
    wbot = _WebBot(ck, cfg)
    h = _handler(ck, cfg, wbot)
    _quiet(h._run_web_update_batch, ["db"])
    checks[f"the Web UI worker, db {hname}: not updated"] = ck.updated == []
    checks[f"…says why where it can (Telegram, {hname})"] = _says(
        wbot.sent, hold[3])
    checks[f"…and the pending entry stays (Web worker, {hname})"] = (
        _pending(cfg) == ["db"])
ck, cfg = _world(HOLDS["pinned"], pending=(ENTRY, OTHER))
wbot = _WebBot(ck, cfg)
h = _handler(ck, cfg, wbot)
_quiet(h._run_web_update_batch, ["db", "web"])
checks["the Web UI worker, db pinned and web not: web is updated and only "
       "db stays pending"] = ck.updated == ["web"] and _pending(cfg) == ["db"]
ck, cfg = _world(FREE)
wbot = _WebBot(ck, cfg)
h = _handler(ck, cfg, wbot)
_quiet(h._run_web_update_batch, ["db"])
checks["the Web UI worker: with nothing holding it, db is updated"] = (
    ck.updated == ["db"] and _pending(cfg) == [])

# A bulk of two, one of them pinned: the other still goes.
to, started, ck, cfg = _post("/api/bulk", "action=update&names=db,web",
                             HOLDS["pinned"])
upd, held = _held_query(to)
checks["a Web UI bulk update with db pinned still starts the rest"] = (
    len(started) == 1 and upd == "pinned" and held == "pinned:db")


# The page has to be able to say it.
class _FakeStore:
    def get_pinned(self): return []
    def get_autoupdate(self): return []
    def get_ask_before_major(self): return []
    def get_groups(self): return {}
    def get_notes(self): return {}
    def get_links(self): return {}
    def get_pending_major(self): return {}
    def is_protect_stop(self, name): return False
    def is_trust_running(self, name): return False
    def get_cooldown(self, name): return 0


class _PageChecker:
    def _own_container_name(self): return "docksentry"
    def get_disk_usage(self): return 42.0, 1, 2
    def read_advisories(self): return {}


def _status_page(path):
    missing = "/nonexistent/docksentry-test"
    cfg = types.SimpleNamespace(
        language="en", auto_selfupdate=False, ui_mode="advanced",
        debug=False, disk_warn_percent=85, status_view="table",
        pending_file=missing, history_file=missing, maintenance_file=missing)
    bot = types.SimpleNamespace(engine=types.SimpleNamespace(updating={}))
    cls = web_ui.create_handler(cfg, _PageChecker(), bot=bot,
                                store=_FakeStore())
    h = cls.__new__(cls)
    h.path = path
    out = {}
    h._send_html = lambda html, status=200: out.update(html=html)
    h._render_page = lambda content, active=None, wide=None: content
    h._get_containers = lambda: []
    h._get_pending = lambda host=None: []
    try:
        _quiet(h._page_status)
    except Exception as e:                                  # noqa: BLE001
        return f"RAISED {type(e).__name__}: {e}"
    return out.get("html", "")


_shown = _status_page("/?update=pinned&held=pinned:db,excluded:nas/web,"
                      "bogus:x")
_TITLE = EN.get("web_update_pinned_title", "\0missing title")
_COUNT = EN.get("web_update_pinned", "\0missing {count}").format(count=2)
checks["the status page says how many were not updated"] = (
    _TITLE in _shown and _COUNT in _shown)
checks["…and why, per container"] = (
    _tail("pinned") in _shown and _tail("excluded") in _shown
    and "web @nas" in _shown and "db: " in _shown)
checks["…and nothing for a reason it does not know"] = "bogus" not in _shown
checks["…and only then"] = (
    _TITLE not in _status_page("/"))
_xss = _status_page("/?update=pinned&held=pinned:%3Cscript%3E")
checks["…and a name in the query is escaped"] = (
    "&lt;script&gt;" in _xss and "<li>📌 <script>" not in _xss)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
