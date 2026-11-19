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

The redirect now carries the answer and the status page renders it. The
lock read is advisory on purpose: the real claim is still the engine's
`claim_update()` in the worker, and this only decides which sentence to
show. The read is the engine's `update_claimed` (Q3), so the engine
here is a real one with its claim held, not a stand-in with `locked()`.

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
    """A real UpdateEngine with only the lock fields, as in the Q0
    fixtures: what the handler asks is the engine's own answer, not a
    stand-in's `locked()`."""
    eng = UpdateEngine.__new__(UpdateEngine)
    eng._update_lock = threading.Lock()
    eng._queued_selfupdate = None
    eng._swap_in_flight = False
    return eng


def _post_update(name="db", lock_held=False, update_running=False,
                 has_engine=True):
    """POST /api/update; returns (redirect target, threads started).

    `lock_held` claims the engine's update lock through its door for the
    length of the request and gives it back afterwards."""
    started = []
    engine = _engine() if has_engine else None
    if lock_held:
        assert engine.claim_update(), "the fresh engine's lock was taken"
    try:
        return _post_update_on(engine, name, update_running, started)
    finally:
        if lock_held:
            engine.release_update()


def _post_update_on(engine, name, update_running, started):
    bot = types.SimpleNamespace(
        t=None, enabled=False, update_running=update_running,
        engine=engine)
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
    h.client_address = ("127.0.0.1", 0)
    h._session_user = lambda: "admin"
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    seen = {}
    h._send_redirect = lambda loc: seen.setdefault("to", loc)
    h._send_auth_required = lambda p="": seen.setdefault("to", "auth-required")
    h._api_update = lambda key: started.append(key)

    real_thread = web_ui.threading.Thread

    class _Thread:
        def __init__(self, target=None, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            started.append(self._args[0] if self._args else None)

    web_ui.threading.Thread = _Thread
    try:
        h.do_POST()
    finally:
        web_ui.threading.Thread = real_thread
    return seen.get("to", "nothing"), started


# ── the happy path still works ───────────────────────────────────────
# First, because everything below is about NOT starting work, and a
# handler that never starts anything would satisfy all of it.
_to, _started = _post_update()
checks["a free lock starts the update"] = _started == ["db"]
checks["…and the page is told it started"] = _to == "/?update=started"

# ── …and the two silent paths now speak ──────────────────────────────
_to, _started = _post_update(lock_held=True)
checks["a held lock does not start a second update"] = _started == []
checks["…and the click is answered, not swallowed"] = _to == "/?update=busy"

_to, _started = _post_update(update_running=True)
checks["an update already running is reported too"] = _to == "/?update=busy"
checks["…and starts nothing"] = _started == []

_to, _started = _post_update(name="")
checks["an empty container name starts nothing"] = _started == []
checks["…and says so rather than redirecting blank"] = _to == "/?update=gone"

# A Telegram-less install is the whole point, so the lock read must not
# depend on a bot that has an engine at all.
_to, _started = _post_update(has_engine=False)
checks["no engine on the bot is not a crash"] = _to == "/?update=started"
checks["…and the update still starts"] = _started == ["db"]

# Nor on an engine that is half built (the `__new__` fixtures elsewhere
# leave fields out): the read is a best guess, never a crash.
_to, _started = _post_update_on(UpdateEngine.__new__(UpdateEngine), "db",
                               False, [])
checks["an engine without its lock is not a crash either"] = (
    _to == "/?update=started" and _started == ["db"])

# The read only looks: the claim it found is still there afterwards,
# and still the holder's to give back.
_eng = _engine()
_eng.claim_update()
_post_update_on(_eng, "db", False, [])
checks["asking about a held lock leaves it held"] = _eng._update_lock.locked()
_eng.release_update()

# ── the page can say it ──────────────────────────────────────────────
# A redirect target nobody renders is the same silence in a new place.
import json as _json  # noqa: E402

_lang = os.path.join(os.path.dirname(__file__), "..", "app", "lang", "en.json")
with open(_lang, encoding="utf-8") as _f:
    _keys = _json.load(_f)
for _what in ("started", "busy", "gone"):
    checks[f"the '{_what}' answer has words to show"] = (
        bool(_keys.get(f"web_update_{_what}"))
        and bool(_keys.get(f"web_update_{_what}_title")))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
