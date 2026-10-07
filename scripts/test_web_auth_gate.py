#!/usr/bin/env python3
"""The gate, not the lock — and the gate was the part nobody drove.

`webauth.verify` is well covered: `test_web_login.py` goes red the
moment you make every password valid. What was not covered is the thing
that *calls* it. On 06.10.2026 a mutation sweep found `_check_auth`
could be made to return `True` unconditionally and not one of 181
procedures noticed — I reproduced it myself, with a full sweep per
mutation. Eight procedures switch it off with
`h._check_auth = lambda: True` so that the page under test can render,
which is reasonable for them and left the gate with nothing.

What it has to get right:

* **no password set means open.** That is documented behaviour, and the
  failure direction matters: a fresh install with no `WEB_PASSWORD` must
  not lock the owner out of the page where the password is set.
* **a password set means closed** until a session cookie or correct
  Basic Auth says otherwise. Basic Auth stays because `curl -u` and
  every existing script uses it; the login form exists because a
  password manager can fill a form and never could fill the browser's
  Basic Auth dialog (#60, @NotRetarded).
* **the password is read fresh on every request**, so changing it in
  Settings takes effect without a restart — and, the half that matters
  here, so does *setting* one on an install that had none.

Driven against the real method, with real headers.
"""
import base64
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402
import webauth  # noqa: E402

checks = {}


class _Cfg:
    def __init__(self, password="", username=""):
        self.web_password = password
        self.web_username = username
        self.language = "en"

    def __getattr__(self, name):
        return ""


def _handler(cfg, headers=None, session=None):
    handler_cls = web_ui.create_handler(
        cfg, checker=None, bot=types.SimpleNamespace(t=None),
        store=None, restart_discord=lambda *_a, **_k: None)
    h = handler_cls.__new__(handler_cls)
    h.path = "/"
    h.headers = headers or {}
    h.client_address = ("127.0.0.1", 0)
    h.rfile = types.SimpleNamespace(read=lambda n: b"")
    # Sessions are their own mechanism with their own tests; what this
    # file needs is to say "there is a session" or "there is not".
    h._session_user = lambda: session
    return h


def _basic(user, password):
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


HASH = webauth.hash_password("correct horse")

# ── no password set: open, on purpose ────────────────────────────────
checks["with no password set, the page is open"] = (
    _handler(_Cfg(password=""))._check_auth() is True)
checks["…which is what keeps a fresh install usable"] = (
    _handler(_Cfg(password=""), headers={})._check_auth() is True)

# ── a password set: closed until proven otherwise ────────────────────
locked = _Cfg(password=HASH)
checks["with a password set, a bare request is refused"] = (
    _handler(locked)._check_auth() is False)
checks["…and so is the wrong password"] = (
    _handler(locked, headers=_basic("", "wrong"))._check_auth() is False)
checks["…and an empty password"] = (
    _handler(locked, headers=_basic("", ""))._check_auth() is False)
checks["…and a header that is not Basic at all"] = (
    _handler(locked,
             headers={"Authorization": "Bearer something"})._check_auth()
    is False)
checks["…and Basic with rubbish behind it"] = (
    _handler(locked,
             headers={"Authorization": "Basic not-base64!!"})._check_auth()
    is False)
checks["…and Basic with no colon in it"] = (
    _handler(locked,
             headers={"Authorization": "Basic "
                      + base64.b64encode(b"nocolon").decode()})._check_auth()
    is False)

# ── …and open to the right one ───────────────────────────────────────
checks["the right password gets in"] = (
    _handler(locked, headers=_basic("", "correct horse"))._check_auth()
    is True)
checks["…and so does a valid session, with no header at all"] = (
    _handler(locked, session="admin")._check_auth() is True)

# ── the username, when one is configured ─────────────────────────────
named = _Cfg(password=HASH, username="andreas")
checks["with a username set, the right pair gets in"] = (
    _handler(named, headers=_basic("andreas", "correct horse"))._check_auth()
    is True)
checks["…and the wrong user with the right password does not"] = (
    _handler(named, headers=_basic("someone", "correct horse"))._check_auth()
    is False)

# ── a plaintext WEB_PASSWORD still works ─────────────────────────────
# It is the only thing the environment can carry, so `verify` takes both
# a hash and a plain value — and the gate must not care which it got.
plain = _Cfg(password="hunter2")
checks["a plaintext WEB_PASSWORD is honoured"] = (
    _handler(plain, headers=_basic("", "hunter2"))._check_auth() is True
    and _handler(plain, headers=_basic("", "nope"))._check_auth() is False)

# ── read fresh, every request ────────────────────────────────────────
# Not a nicety: it is what makes "set a password in Settings" take
# effect, and an install that had none is exactly the one where somebody
# is setting one for the first time.
_live = _Cfg(password="")
_h = _handler(_live)
checks["an open install really is open"] = _h._check_auth() is True
_live.web_password = HASH
checks["…and setting a password closes it without a restart"] = (
    _h._check_auth() is False)
_live.web_password = ""
checks["…and clearing it opens it again"] = _h._check_auth() is True

# ── the gate sits in front of POST, not only GET ─────────────────────
# A read behind a password is worth something; a write behind none is
# the whole problem.
_posted = []
_h = _handler(_Cfg(password=HASH),
              headers={"Host": "x", "Origin": "http://x",
                       "Content-Length": "0"})
_h.path = "/settings"
_h._check_csrf = lambda: True
_h._audit = lambda *a, **k: None
_h._page_settings = lambda: _posted.append("rendered")
_h._send_redirect = lambda loc: _posted.append("redirected")
_h._send_auth_required = lambda path="": _posted.append("asked for a password")
_h.do_POST()
checks["an unauthenticated POST is asked for a password"] = (
    _posted == ["asked for a password"])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
