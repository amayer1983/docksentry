#!/usr/bin/env python3
"""Clicking Update in the Web UI says what happened.

The button POSTs to `/api/update`, which started a thread and redirected
to `/`. Everything the worker could report arrives long after that
redirect has gone — and what it does report goes to Telegram:

    if not engine.claim_update():
        if bot.enabled:
            bot.send_message(bot.t("update_already_running"))
        return

So on an install without Telegram — which the Web UI exists to serve —
a click during another update was answered with a 302 back to the same
page and nothing else. Same for a container that had dropped out of the
pending list: `if not targets: return`, silently. The user saw a page
that looked exactly as it had a moment earlier and no way to tell
whether anything had happened (audit, 07.11.2026).

The redirect now carries the answer and the status page renders it.

Since Q14 the handler does not look at the lock and guess any more: it
asks the engine's `claim_or_queue_many` once. A free lock is claimed for
the worker, which gives it back when it is done (`/?update=started`); a
held one queues the key, and the page says where it went
(`/?update=queued&pos=N`, `already_queued`, `same_running`,
`queue_full`). Until then a click during another update was answered
"not started, try again" and thrown away. The engine here is a real one
with a real queue, not a stand-in.

Driven against the real handler — what matters is where it redirects to
and whether a worker thread was started at all.
"""

import os
import sys
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402
from update_engine import UpdateEngine  # noqa: E402

checks = {}


def _engine():
    """A real UpdateEngine with the lock and queue fields only, as in the
    Q0 fixtures: what the handler gets is the engine's own answer."""
    eng = UpdateEngine.__new__(UpdateEngine)
    eng._update_lock = threading.Lock()
    eng._queued_selfupdate = None
    eng._swap_in_flight = False
    eng._update_queue = []
    eng._update_queue_lock = threading.Lock()
    eng._queued_at = {}
    eng._updating = {}
    # No groups: a queued selection keeps the order it was asked in.
    eng.store = types.SimpleNamespace(get_groups=lambda: {})
    return eng


def _queued(engine):
    return [k for k, _w in engine.queued_updates()]


def _let_go(engine):
    """Give back whatever is held without starting a drainer: the queue
    is what the checks look at afterwards."""
    if engine._update_lock.locked():
        engine._update_lock.release()


def _post_update(name="db", engine=None, thread_fails=False):
    """POST /api/update on `engine`; returns (redirect target, threads
    started). A started thread is recorded, not run."""
    started = []
    engine = engine if engine is not None else _engine()
    bot = types.SimpleNamespace(
        t=None, enabled=False, engine=engine,
        _run_queued_selfupdate=lambda: None)
    handler_cls = web_ui.create_handler(
        types.SimpleNamespace(web_password="", web_username="", language="en"),
        checker=None, bot=bot, store=None)
    h = handler_cls.__new__(handler_cls)
    h.path = "/api/update"
    body = f"name={name}".encode()
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x"}
    h.client_address = ("127.0.0.1", 0)
    h.rfile = types.SimpleNamespace(read=lambda n: body)
    h._session_user = lambda: "admin"
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    seen = {}
    h._send_redirect = lambda loc: seen.setdefault("to", loc)
    h._send_auth_required = lambda p="": seen.setdefault("to", "auth-required")

    real_thread = web_ui.threading.Thread

    class _Thread:
        def __init__(self, target=None, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            if thread_fails:
                raise RuntimeError("can't start new thread")
            started.append(self._args[0] if self._args else None)

    web_ui.threading.Thread = _Thread
    try:
        h.do_POST()
    except RuntimeError as e:
        seen.setdefault("to", f"raised {e}")
    finally:
        web_ui.threading.Thread = real_thread
    return seen.get("to", "nothing"), started


# ── the happy path still works ───────────────────────────────────────
# First, because everything below is about NOT starting work, and a
# handler that never starts anything would satisfy all of it.
_eng = _engine()
_to, _started = _post_update(engine=_eng)
checks["a free lock starts the update"] = _started == ["db"]
checks["…and the page is told it started"] = _to == "/?update=started"
# The claim is the handler's and goes to the worker with the thread: the
# worker gives it back when it is done (it is recorded here, not run).
checks["…with the claim taken for the worker"] = _eng.update_claimed
checks["…and nothing queued"] = _queued(_eng) == []
_let_go(_eng)

# A thread that cannot start must not strand the claim it was given.
_eng = _engine()
_to, _started = _post_update(engine=_eng, thread_fails=True)
checks["a worker that cannot start gives the claim back"] = (
    not _eng.update_claimed and _started == [])

# ── a busy lock queues the click instead of throwing it away (Q14) ────
_eng = _engine()
assert _eng.claim_update(), "the fresh engine's lock was taken"
_to, _started = _post_update(engine=_eng)
checks["a held lock does not start a second update"] = _started == []
checks["…the click is queued, with its place"] = (
    _to == "/?update=queued&pos=1" and _queued(_eng) == ["db"])
checks["…and the holder's claim is left alone"] = _eng.update_claimed

# The same click again while it waits: it keeps its one place.
_to, _started = _post_update(engine=_eng)
checks["clicking it again says it already waits"] = (
    _to.startswith("/?update=already_queued&since=")
    and _queued(_eng) == ["db"] and _started == [])

# A second container behind it gets the next place.
_to, _started = _post_update(name="web", engine=_eng)
checks["the next container is queued behind it"] = (
    _to == "/?update=queued&pos=2" and _queued(_eng) == ["db", "web"])
_let_go(_eng)

# A lock taken by hand, not through the door, is just as busy.
_eng = _engine()
_eng._update_lock.acquire()
_to, _started = _post_update(engine=_eng)
checks["a lock held without the door queues too"] = (
    _to == "/?update=queued&pos=1" and _started == [])
_let_go(_eng)

# The container that is being updated right now is not a second job.
_eng = _engine()
_eng.claim_update()
_eng._updating["db"] = "1.2"
_to, _started = _post_update(engine=_eng)
checks["clicking the container that is updating queues nothing"] = (
    _to == "/?update=same_running" and _queued(_eng) == [] and _started == [])
_let_go(_eng)

# A full queue is said, and nothing goes on it.
_eng = _engine()
_eng.claim_update()
_full = [f"c{i}" for i in range(UpdateEngine.UPDATE_QUEUE_MAX)]
_eng.enqueue_updates(_full)
_to, _started = _post_update(engine=_eng)
checks["a full queue is reported, with its size"] = (
    _to == f"/?update=queue_full&max={UpdateEngine.UPDATE_QUEUE_MAX}"
    and _queued(_eng) == _full and _started == [])
_let_go(_eng)

_eng = _engine()
_to, _started = _post_update(name="", engine=_eng)
checks["an empty container name starts nothing"] = _started == []
checks["…and says so rather than redirecting blank"] = _to == "/?update=gone"
checks["…and takes no claim"] = not _eng.update_claimed

# ── a queued selection keeps the order its batch would have run in ────
# The drainer runs one key per run, so the batch's group sort never sees
# two of them: the queue itself has to be in that order. A bulk of the
# group [web, db] named db first ran db before web whenever the lock was
# busy, and web before db when it was free (review of Q14).


class _Groups:
    """A store stand-in: only the groups are read here."""

    def __init__(self, groups, fail=False):
        self._groups, self._fail = groups, fail

    def get_groups(self):
        if self._fail:
            raise OSError("groups.json unreadable")
        return self._groups


def _order(keys, store=None, hosts=None):
    eng = _engine()
    eng.store = store if store is not None else _Groups({})
    if hosts is not None:
        eng.hosts = hosts
    eng.claim_update()
    try:
        claimed, outcomes = eng.claim_or_queue_many(keys)
    except Exception as e:                          # noqa: BLE001
        # A request refused by a crash is a failed check here, not a
        # traceback that ends the procedure.
        _let_go(eng)
        return None, f"raised {type(e).__name__}", _queued(eng)
    _let_go(eng)
    return claimed, [k for k, _a, _p in outcomes], _queued(eng)


_stack = {"my-stack": {"containers": ["web", "db"]}}
_c, _asked, _q = _order(["db", "web"], _Groups(_stack))
checks["a queued group goes on the queue head first"] = (
    not _c and _q == ["web", "db"])
checks["…while the answers stay in the order asked"] = _asked == ["db", "web"]
_c, _asked, _q = _order(["solo", "db", "zeta", "web"], _Groups(_stack))
checks["…group members before the rest, the rest as selected"] = (
    _q == ["web", "db", "solo", "zeta"])
_c, _asked, _q = _order(["zeta", "alpha"], _Groups(_stack))
checks["containers in no group keep the order selected"] = (
    _q == ["zeta", "alpha"])
_c, _asked, _q = _order(["db", "web"], _Groups(_stack, fail=True))
checks["unreadable groups keep the selection as it is"] = (
    _c is False and _q == ["db", "web"])
# Two hosts: each host's keys stay together, the hosts in the order they
# first come up — how the Web worker splits a free-lock batch.


class _Hosts:
    is_multi = True

    def __init__(self, stores):
        self._stores = stores

    def get(self, name):
        st = self._stores.get(name)
        return types.SimpleNamespace(store=st) if st is not None else None


_c, _asked, _q = _order(
    ["nas/db", "db", "nas/web", "web"], _Groups({}),
    _Hosts({"local": _Groups(_stack), "nas": _Groups(_stack)}))
checks["each host's group in order, the hosts as they come"] = (
    _q == ["nas/web", "nas/db", "web", "db"])

# ── the page can say it ──────────────────────────────────────────────
# A redirect target nobody renders is the same silence in a new place.
import json as _json  # noqa: E402

_lang = os.path.join(os.path.dirname(__file__), "..", "app", "lang", "en.json")
with open(_lang, encoding="utf-8") as _f:
    _keys = _json.load(_f)
for _what in ("started", "gone", "queued", "already_queued", "queue_full",
              "same_running"):
    checks[f"the '{_what}' answer has words to show"] = (
        bool(_keys.get(f"web_update_{_what}"))
        and bool(_keys.get(f"web_update_{_what}_title")))
checks["a request that queued several has words too"] = bool(
    _keys.get("web_update_queued_many"))


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
    """The status page for `path`, as the browser gets it after the
    redirect."""
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
    import contextlib
    import io
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            h._page_status()
    except Exception as e:                                  # noqa: BLE001
        return f"RAISED {type(e).__name__}: {e}"
    return out.get("html", "")


def _fmt(key, **kw):
    """The English sentence, or a marker no page contains when the key
    is missing (red, not a traceback)."""
    if key not in _keys:
        return "\0missing " + key
    return _keys[key].format(**kw)


_page = _status_page("/?update=queued&pos=3")
checks["the page says where the click went in the queue"] = (
    _fmt("web_update_queued", pos=3) in _page
    and _fmt("web_update_queued_title") in _page)
_page = _status_page("/?update=queued&n=4")
checks["…and how many of a selection are waiting"] = (
    _fmt("web_update_queued_many", count=4) in _page)
_page = _status_page("/?update=already_queued&since=125")
checks["…how long it has waited when it was there already"] = (
    _fmt("web_update_already_queued", since="2m") in _page)
_page = _status_page("/?update=queue_full&max=10")
checks["…that the queue was full, and its size"] = (
    _fmt("web_update_queue_full", max=10) in _page)
_page = _status_page("/?update=same_running")
checks["…that it is the one updating now"] = (
    _fmt("web_update_same_running") in _page)
# The numbers come from the URL, so a hand-made one must not break the
# page or put words on it that nobody sent.
_page = _status_page("/?update=queued&pos=%3Cb%3E")
checks["a position that is not a number is not shown"] = (
    not _page.startswith("RAISED") and "<b>" not in _page
    and _fmt("web_update_queued_title") in _page)
checks["no banner without an answer"] = (
    _fmt("web_update_queued_title") not in _status_page("/")
    and "web_update_queued_title" in _keys)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
