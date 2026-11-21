#!/usr/bin/env python3
"""An unreadable pin list means every container is pinned, on every path.

Since 2.19.0 a damaged `pinned_containers.json` no longer empties the
pending list: `check_all` carries the host's entries over, so badges
and update buttons stay where they were. Only `check_all` ever looked at
pins, though. Measured 18.11.2026 before this procedure existed: the
pin file truncated to `["db"`, `db` still pending from before, and the
Telegram button and "Update all" both recreated `db`. The container the
user had pinned was updated because the file that said so was broken.

So every manual update path now refuses while the pin list cannot be
read, before it takes the lock or touches the pending file, and says
why: Telegram (button, "Update all", the major confirmation, `/update
<name>`), Discord (`/update`, `/updateall` and its confirmation press)
and the Web UI (the row button, the bulk bar, the major confirmation
and the worker behind them).

The second half is what /check said meanwhile. With nothing checked,
`check_all` came back empty and /check answered "All images are up to
date". Measured the same day. Now Telegram, Discord and the Web UI's
per-row check say the pin list could not be read and nothing was checked.

Driven through the real handlers, a real `UpdateChecker` reading a real
pin file and the real pending file on disk. Stood in for: the daemon
(`ps`), the registry, and `update_container` itself, which records the
call instead of recreating anything, because whether it is reached is
the question. Every refusal has its counter-direction on the same path
with a readable pin list, or a path that refuses everything would pass.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import telegram_bot  # noqa: E402
import web_ui  # noqa: E402
from discord_bot import DiscordBot  # noqa: E402
from i18n import get_translator  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402
from update_checker import UpdateChecker  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402

with open(os.path.join(os.path.dirname(__file__), "..", "app", "lang",
                       "en.json"), encoding="utf-8") as _f:
    EN = json.load(_f)

REFUSED = EN["update_refused_pin_list"]
NOT_CHECKED = EN["check_pin_list_unreadable"]

DAMAGED = b'["db"'          # truncated: it named db
HEALTHY = b'[]'             # readable, pins nothing
PINS_DB = b'["db"]'         # readable, pins db

ENTRY = {"name": "db", "image": "reg/db:1", "host": "local"}

checks = {}


def _quiet(fn, *a, **kw):
    """Run `fn` with stdout captured. The damaged reads print their two
    ERROR lines every time, which is right and is not what is tested."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


class _Daemon:
    """Answers `ps` with one container, `db`. Environment, not decision."""

    name = "local"

    def run(self, args, timeout=None, **kw):
        if args[:1] == ["ps"]:
            return types.SimpleNamespace(
                returncode=0, stdout="db|reg/db:1\n", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")


def _world(pin_bytes, pending=(ENTRY,)):
    """A checker over a real pin file and a real pending file."""
    tmp = tempfile.mkdtemp(prefix="ds-pinpaths-")
    cfg = types.SimpleNamespace(
        debug=False, language="en", exclude_containers=[],
        pinned_file=os.path.join(tmp, "pinned_containers.json"),
        pending_file=os.path.join(tmp, "pending.json"),
        advisories_file=os.path.join(tmp, "adv.json"),
        # Discord's constructor reads these.
        discord_bot_token="t", discord_app_id="app", discord_guild_id="g",
        save_persistent=lambda: None)
    with open(cfg.pinned_file, "wb") as f:
        f.write(pin_bytes)
    with open(cfg.pending_file, "w", encoding="utf-8") as f:
        json.dump([dict(u) for u in pending], f)
    ck = UpdateChecker(cfg, backend=_Daemon())
    # The registry, as in test_pin_fail_closed: `db` has a real update.
    ck._own_container_name = lambda: ""
    ck._labels_for = lambda names: {}
    ck._parse_image = lambda img: ("reg", "db", "1")
    ck._container_image_id = lambda name: "sha256:RUN"
    ck._get_local_digests = lambda img: ["sha256:LOCAL"]
    ck._get_local_repo_digests = lambda img: ["db@sha256:LOCAL"]
    ck._get_remote_digest = lambda *a, **k: "sha256:MOVED"
    ck._list_remote_tags = lambda reg, repo: []
    ck._get_image_size = lambda img: "1 GB"
    ck._get_image_created = lambda img: "2026-01-01"
    ck._get_image_version_label = lambda img: ""
    ck.get_remote_image_meta = lambda *a, **k: {}
    ck._registry_environment = lambda: ""
    ck._parse_semver = lambda t: None
    ck.has_selfupdate_available = lambda: False
    ck.netns_target_name = lambda n: None
    # The action itself. Recorded, never run.
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
        return [u.get("name") for u in json.load(f)]


def _engine():
    """A real engine's lock, queue and in-flight map, built by hand the
    way test_update_queue_duplicate builds it."""
    eng = UpdateEngine.__new__(UpdateEngine)
    eng._update_lock = threading.Lock()
    eng._update_queue = []
    eng._queued_at = {}
    eng._update_queue_lock = threading.Lock()
    eng._updating = {}
    eng._queued_selfupdate = None
    eng._swap_in_flight = False
    # Since Q11c a single update runs through the shared batch, which
    # reads the host's groups and cooldowns and the disk setting.
    eng.store = _MajorStore()
    eng.config = types.SimpleNamespace(disk_warn_auto_cleanup=False)
    return eng


# ═════════════════════════════════════════════════════════════════════
# Telegram
# ═════════════════════════════════════════════════════════════════════
class _MajorStore:
    """The pending-major store `_confirm_major_update` reads."""

    def __init__(self):
        self.major = {"db": {"image": "reg/db:2", "compose": {}}}

    def get_pending_major(self):
        return dict(self.major)

    def remove_pending_major(self, name):
        self.major.pop(name, None)

    def get_groups(self):
        return {}

    def get_ask_before_major(self):
        return []

    def get_cooldown(self, name):
        return 0


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
    bot._multi = lambda: False
    bot._maybe_cooldown = lambda *a, **k: None
    bot.engine = _engine()
    bot.notifier = None
    bot.store = _MajorStore()
    bot._store_for = lambda host_name: bot.store
    bot.link_resolver = types.SimpleNamespace(
        resolve_container_link=lambda *a, **k: "")
    bot._process_update_batch = (
        lambda *a, **k: UpdateEngine._process_update_batch(bot, *a, **k))
    bot.mark_updating = bot.engine.mark_updating
    bot.clear_updating = bot.engine.clear_updating
    return bot, said


def _tg_path(label, run, pin_bytes):
    """Run one Telegram path; (updated, said, pending, major left)."""
    ck, cfg = _world(pin_bytes)
    bot, said = _tg(ck, cfg)
    try:
        _quiet(run, bot, ck)
    except Exception as e:                                  # noqa: BLE001
        said.append(f"RAISED {type(e).__name__}: {e}")
    return ck.updated, said, _pending(cfg), dict(bot.store.major)


TG_PATHS = {
    "the Telegram update button":
        lambda bot, ck: bot._run_single_update(ck, "db"),
    "Telegram \"Update all\" from a notification":
        lambda bot, ck: bot.run_updates(ck, updates=[dict(ENTRY)]),
    "Telegram \"Update all\" without a snapshot":
        lambda bot, ck: bot.run_updates(ck),
    "the Telegram major confirmation":
        lambda bot, ck: bot._confirm_major_update(ck, "db"),
}

for label, run in TG_PATHS.items():
    updated, said, pending, major = _tg_path(label, run, DAMAGED)
    checks[f"{label}: an unreadable pin list updates nothing"] = (
        updated == [])
    checks[f"…and says why ({label})"] = REFUSED in said
    checks[f"…and leaves the pending entry where it was ({label})"] = (
        pending == ["db"] and "db" in major)
    if updated or REFUSED not in said:
        print(f"      → {label}: updated {updated}, said {said}")
    # Counter-direction: the same path with a readable list still runs.
    updated, said, pending, major = _tg_path(label, run, HEALTHY)
    checks[f"{label}: a readable pin list still runs the update"] = (
        updated == ["db"] and REFUSED not in said)
    if updated != ["db"]:
        print(f"      → {label} (healthy): updated {updated}, said {said}")


# ── Telegram: typed commands, through the real dispatch ──────────────
class _SyncThread:
    """Runs the target on .start(), so what it does is visible."""

    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


class _OnlyDb:
    """The local CLI for `_typed`: `ps` lists one container, `db`, so the
    glob is matched by the real lookup (`find_on_hosts`)."""

    def run(self, args, **kw):
        import subprocess
        out = "db\n" if args and args[0] == "ps" else ""
        return subprocess.CompletedProcess(args, 0, out, "")


def _typed(text, pin_bytes):
    """`text` typed into Telegram; (said, run_updates payload, notified)."""
    ck, cfg = _world(pin_bytes)
    stub = types.SimpleNamespace(update_running=False, bot_username="",
                                 hosts=None, backend=_OnlyDb(), sent=[],
                                 updated_with="unset", notified=None)
    stub._handle_message = types.MethodType(TelegramBot._handle_message, stub)
    stub._select_containers = types.MethodType(
        TelegramBot._select_containers, stub)
    for _m in ("_multi", "_resolve_targets", "_host_hint", "_backend_for",
               "_checker_for", "_run_full_check", "_lookup_tail"):
        setattr(stub, _m, types.MethodType(getattr(TelegramBot, _m), stub))
    stub._host_tag = TelegramBot._host_tag
    stub._is_glob = TelegramBot._is_glob
    stub._help_alias = TelegramBot._help_alias
    stub._check_auth = lambda chat_id, user_id, kind="message": True
    stub.t = get_translator("en")
    stub.send_message = lambda text, **kw: stub.sent.append(text)
    stub.notify_updates = (lambda updates, auto=False:
                           setattr(stub, "notified", list(updates)))
    stub.notify_no_updates = (lambda: stub.sent.append(EN["all_up_to_date"]))
    stub._build_dry_run = lambda updates, checker: "DRY RUN"
    stub.run_updates = (lambda checker, updates=None:
                        setattr(stub, "updated_with", updates))
    real = telegram_bot.threading
    telegram_bot.threading = types.SimpleNamespace(Thread=_SyncThread,
                                                   Lock=threading.Lock)
    try:
        _quiet(stub._handle_message,
               {"text": text, "from": {"id": 1}, "chat": {"id": 1}}, ck, None)
    except Exception as e:                                  # noqa: BLE001
        stub.sent.append(f"RAISED {type(e).__name__}: {e}")
    finally:
        telegram_bot.threading = real
    return stub.sent, stub.updated_with, stub.notified


said, updated_with, _n = _typed("/update db*", DAMAGED)
checks["Telegram /update <name>: an unreadable pin list runs nothing"] = (
    updated_with == "unset")
checks["…and says why, not \"no update matches\""] = (
    REFUSED in said
    and EN["update_scoped_none"].split("{")[0] not in "\n".join(said))
if REFUSED not in said:
    print(f"      → /update db*: {said}")
said, updated_with, _n = _typed("/update db*", HEALTHY)
checks["…while a readable pin list hands the update on"] = (
    [u["name"] for u in (updated_with if isinstance(updated_with, list)
                         else [])] == ["db"])


# ═════════════════════════════════════════════════════════════════════
# What /check says
# ═════════════════════════════════════════════════════════════════════
UP_TO_DATE = EN["all_up_to_date"]

for label, text in (("/check", "/check"),
                    ("/check --dry-run", "/check --dry-run"),
                    ("/check <name>", "/check db*")):
    if text == "/check":
        # The arg-less /check runs on a thread of its own in the real
        # dispatch; `_run_full_check` is that thread's body.
        def _run(pin_bytes):
            ck, cfg = _world(pin_bytes)
            bot, said = _tg(ck, cfg)
            bot.notify_updates = lambda ups, auto=False: said.append("FOUND")
            bot.notify_no_updates = lambda: said.append(UP_TO_DATE)
            _quiet(bot._run_full_check, ck, None)
            return said
    else:
        def _run(pin_bytes, text=text):
            said, _u, notified = _typed(text, pin_bytes)
            return said + (["FOUND"] if notified else [])
    said = _run(DAMAGED)
    scoped_up = EN["check_scoped_uptodate"].split("{")[0]
    checks[f"{label} with an unreadable pin list says nothing was checked"] = (
        NOT_CHECKED in said)
    checks[f"…and does not call it up to date ({label})"] = (
        UP_TO_DATE not in said
        and not any(m.startswith(scoped_up) for m in said))
    if NOT_CHECKED not in said or UP_TO_DATE in said:
        print(f"      → {label}: {said}")
    # Counter-directions: a readable list that pins db is genuinely
    # "nothing to update", and one that pins nothing finds the update.
    said = _run(PINS_DB)
    checks[f"{label}: a readable list that pins everything is still "
           f"\"up to date\""] = (
        NOT_CHECKED not in said
        and (UP_TO_DATE in said
             or any(m.startswith(scoped_up) for m in said)))
    said = _run(HEALTHY)
    checks[f"{label}: …and one that pins nothing finds the update"] = (
        NOT_CHECKED not in said
        and ("FOUND" in said or "DRY RUN" in said))


# ═════════════════════════════════════════════════════════════════════
# Discord
# ═════════════════════════════════════════════════════════════════════
class _Engine:
    """The real lock and the engine's real door onto it; the batch is
    recorded instead of run."""

    claim_update = UpdateEngine.claim_update
    release_update = UpdateEngine.release_update
    # The release kicks the queue drain since Q13; with no queue here it
    # starts nothing.
    kick_drain = UpdateEngine.kick_drain
    cleanup_guarded = UpdateEngine.cleanup_guarded
    # The Web handlers claim or queue through the engine (Q14).
    claim_or_queue_many = UpdateEngine.claim_or_queue_many
    enqueue_updates = UpdateEngine.enqueue_updates
    _queue_answer = UpdateEngine._queue_answer
    # Queued keys go on in the batch's order (review of Q14); no store
    # here, so they keep the order asked.
    _in_group_order = UpdateEngine._in_group_order
    updating = UpdateEngine.updating

    def __init__(self):
        self._update_lock = threading.Lock()
        self.update_running = False
        self.batches = []
        self._update_queue = []
        self._update_queue_lock = threading.Lock()
        self._queued_at = {}
        self._updating = {}

    def _process_update_batch(self, updates, checker, *, auto):
        self.batches.append([u["name"] for u in updates])
        for u in updates:
            checker.update_container(u["name"], u["image"])
        return [f"✅ `{u['name']}`: updated" for u in updates], len(updates), []


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


def _discord(pin_bytes):
    ck, cfg = _world(pin_bytes)
    host = types.SimpleNamespace(name="local", is_local=True,
                                 endpoint="unix:///x", checker=ck,
                                 backend=ck.backend, store=None)
    eng = _Engine()
    bot = DiscordBot(cfg, None, eng, hosts=_Registry([host]), checker=ck,
                     log=lambda *_: None)
    return bot, ck, cfg, eng


def _call(bot, name, **opts):
    return _quiet(bot._dispatch, {
        "member": {"user": {"id": "u1"}},
        "data": {"name": name,
                 "options": [{"name": k, "value": v}
                             for k, v in opts.items()]}})


def _text(reply):
    return getattr(reply, "content", reply) or ""


for pins, readable in ((DAMAGED, False), (HEALTHY, True)):
    bot, ck, cfg, eng = _discord(pins)
    out = _text(_call(bot, "update", container="db"))
    if not readable:
        checks["Discord /update: an unreadable pin list updates nothing"] = (
            ck.updated == [] and eng.batches == [])
        checks["…and says why (Discord /update)"] = REFUSED in out
        checks["…and leaves the pending entry (Discord /update)"] = (
            _pending(cfg) == ["db"])
        if REFUSED not in out:
            print(f"      → Discord /update: {out}")
    else:
        checks["Discord /update: a readable pin list still runs it"] = (
            ck.updated == ["db"] and REFUSED not in out)

    bot, ck, cfg, eng = _discord(pins)
    reply = _call(bot, "updateall")
    if not readable:
        checks["Discord /updateall: refused before it even asks"] = (
            REFUSED in _text(reply)
            and not getattr(reply, "components", None))
    else:
        checks["Discord /updateall: a readable pin list asks as before"] = (
            bool(getattr(reply, "components", None)))

# The press: the question was asked while the file was fine, and the
# file broke while it sat there.
bot, ck, cfg, eng = _discord(HEALTHY)
reply = _call(bot, "updateall")
_uid = (reply.components[0]["components"][0]["custom_id"]
        if getattr(reply, "components", None) else "")
with open(cfg.pinned_file, "wb") as f:
    f.write(DAMAGED)
out = _text(_quiet(bot._on_component, _uid,
                   {"member": {"user": {"id": "u1"}}}))
checks["Discord /updateall: a pin list that broke while the question "
       "waited stops the press"] = ck.updated == [] and eng.batches == []
checks["…and the press says why"] = REFUSED in out
checks["…and the pending entry stays"] = _pending(cfg) == ["db"]
if REFUSED not in out:
    print(f"      → Discord press: {out}")
# Counter-direction for the press.
bot, ck, cfg, eng = _discord(HEALTHY)
reply = _call(bot, "updateall")
_uid = (reply.components[0]["components"][0]["custom_id"]
        if getattr(reply, "components", None) else "")
_quiet(bot._on_component, _uid, {"member": {"user": {"id": "u1"}}})
checks["…while a press with a readable list runs the update"] = (
    ck.updated == ["db"])

for pins, label in ((DAMAGED, "unreadable"), (PINS_DB, "pins db"),
                    (HEALTHY, "pins nothing")):
    bot, ck, cfg, eng = _discord(pins)
    out = _text(_call(bot, "check"))
    if label == "unreadable":
        checks["Discord /check with an unreadable pin list says nothing "
               "was checked"] = NOT_CHECKED in out
        checks["…and does not call it up to date (Discord)"] = (
            EN["chan_all_up_to_date"] not in out)
        if NOT_CHECKED not in out:
            print(f"      → Discord /check: {out}")
    elif label == "pins db":
        checks["Discord /check: a readable list that pins everything is "
               "still \"up to date\""] = out == EN["chan_all_up_to_date"]
    else:
        checks["Discord /check: …and one that pins nothing finds the "
               "update"] = "`db`" in out and NOT_CHECKED not in out


# ═════════════════════════════════════════════════════════════════════
# Web UI
# ═════════════════════════════════════════════════════════════════════
class _WebBot:
    """What the handlers read off the bot. The engine is the recording
    one above, so the worker's batch is visible."""

    def __init__(self, ck):
        self.enabled = True
        self.update_running = False
        self.engine = _Engine()
        self.sent = []
        self.t = get_translator("en")
        self.store = None
        self._ck = ck
        self.queued_selfupdates = 0

    def send_message(self, m, **kw):
        self.sent.append(m)

    def _remove_from_pending(self, keys):
        TelegramBot._remove_from_pending(self, keys)

    def _pending_key(self, u):
        return (u.get("host") or "local", u["name"])

    def _run_queued_selfupdate(self):
        self.queued_selfupdates += 1

    def _confirm_major_update(self, checker, key):
        self.sent.append(f"MAJOR {key}")


def _handler(ck, cfg, wbot):
    cls = web_ui.create_handler(cfg, checker=ck, bot=wbot, store=None)
    return cls.__new__(cls)


def _post(path, body, pin_bytes):
    """POST `body` to `path`; (redirect, threads started, ck, cfg)."""
    ck, cfg = _world(pin_bytes)
    cfg.web_password = ""
    cfg.web_username = ""
    wbot = _WebBot(ck)
    wbot.config = cfg
    h = _handler(ck, cfg, wbot)
    h.path = path
    raw = body.encode()
    h.headers = {"Content-Length": str(len(raw)), "Host": "x",
                 "Origin": "http://x"}
    h.client_address = ("127.0.0.1", 0)
    h.rfile = types.SimpleNamespace(read=lambda n: raw)
    h._session_user = lambda: "admin"
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    seen = {}
    h._send_redirect = lambda loc: seen.setdefault("to", loc)
    h._send_auth_required = lambda p="": seen.setdefault("to", "auth")
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


WEB_POSTS = {
    "the Web UI update button": ("/api/update", "name=db"),
    "the Web UI bulk update": ("/api/bulk", "action=update&names=db"),
    "the Web UI major confirmation": ("/api/major_confirm",
                                      "name=db&action=confirm"),
}
for label, (path, body) in WEB_POSTS.items():
    to, started, ck, cfg = _post(path, body, DAMAGED)
    checks[f"{label}: an unreadable pin list starts nothing"] = (
        started == [])
    checks[f"…and the page is told why ({label})"] = (
        to == "/?update=pinlist")
    checks[f"…and the pending entry stays ({label})"] = (
        _pending(cfg) == ["db"])
    if to != "/?update=pinlist":
        print(f"      → {label}: redirected to {to}")
    to, started, ck, cfg = _post(path, body, HEALTHY)
    checks[f"{label}: a readable pin list still starts it"] = (
        len(started) == 1 and to != "/?update=pinlist")

# Some bulk actions are not updates, and a broken pin list must not
# stop someone pinning, for instance — that may be how they repair it.
to, started, ck, cfg = _post("/api/bulk", "action=pin&names=db", DAMAGED)
checks["…while a bulk pin still goes through with the list unreadable"] = (
    len(started) == 1 and to == "/")

# The worker behind all three, reached directly.
for pins, readable in ((DAMAGED, False), (HEALTHY, True)):
    ck, cfg = _world(pins)
    wbot = _WebBot(ck)
    wbot.config = cfg
    h = _handler(ck, cfg, wbot)
    # The worker runs under the claim its handler took (Q14).
    wbot.engine.claim_update()
    _quiet(h._run_web_update_batch, ["db"])
    if not readable:
        checks["the Web UI worker refuses with an unreadable pin list"] = (
            ck.updated == [] and wbot.engine.batches == [])
        checks["…says why where it can (Telegram)"] = REFUSED in wbot.sent
        checks["…and leaves the pending entry"] = _pending(cfg) == ["db"]
    else:
        checks["the Web UI worker runs with a readable pin list"] = (
            ck.updated == ["db"])

# The page has to be able to say it: a redirect nobody renders is the
# same silence in a new place.
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


_shown = _status_page("/?update=pinlist")
checks["the status page shows the refusal it was redirected with"] = (
    EN["web_update_pinlist"] in _shown
    and EN["web_update_pinlist_title"] in _shown)
checks["…and only then"] = EN["web_update_pinlist"] not in _status_page("/")

# The per-row check button: the one Web reply with the "up to date" shape.
for pins, label in ((DAMAGED, "unreadable"), (PINS_DB, "pins db"),
                    (HEALTHY, "pins nothing")):
    ck, cfg = _world(pins)
    wbot = _WebBot(ck)
    wbot.config = cfg
    h = _handler(ck, cfg, wbot)
    try:
        res = _quiet(h._api_check_one, "db")
    except Exception as e:                                  # noqa: BLE001
        res = {"raised": f"{type(e).__name__}: {e}"}
    if label == "unreadable":
        # `ok: False` is what makes the page show "Check failed (…)"
        # instead of "db is up to date" (app.js).
        checks["the Web UI row check with an unreadable pin list is not "
               "\"up to date\""] = res.get("ok") is False
        checks["…and says nothing was checked"] = (
            res.get("error") == EN["web_check_one_pinlist"])
        if res.get("ok") is not False:
            print(f"      → Web check_one: {res}")
    elif label == "pins db":
        checks["the Web UI row check: a readable list that pins it is "
               "still \"up to date\""] = (
            res.get("ok") is True and res.get("found") is False)
    else:
        checks["the Web UI row check: …and one that pins nothing finds "
               "the update"] = res.get("ok") is True and res.get("found")

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
