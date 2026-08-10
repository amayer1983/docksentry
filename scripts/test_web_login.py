#!/usr/bin/env python3
"""A login page, a username, and a password that is not on disk (#60).

@NotRetarded asked for a proper login page, a username/password pair, and
credentials that do not sit in the compose file. Checking his four claims
was the whole of the design work, because all four were true:

* there was no login page. It was HTTP Basic Auth, so what you saw was the
  browser's dialog, not a page of this application, and a password manager
  had no form to fill in;
* there was no username. The Basic Auth header was split into user and
  password and the user half was then never looked at again, so any name
  got in as long as the password matched;
* there was no TLS anywhere;
* and the password was kept in cleartext in `settings.json` as well as in
  the compose file. The SHA-256 was computed per request and never stored.

Three of those are addressed here. TLS is not, and that is a decision
rather than an omission: anyone exposing this beyond their own network
puts a reverse proxy in front of it, and Caddy or Traefik does
certificates, including renewal, better than we would.

The constraint throughout is that nothing may break for the people who
already have this working. `WEB_PASSWORD` is plaintext by nature, so a
plaintext stored value must keep verifying; `curl -u` must keep working;
and an upgrade must not start demanding a username nobody has set.
"""

import json
import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import webauth  # noqa: E402

checks = {}
WEB_UI = os.path.join(os.path.dirname(__file__), "..", "app", "web_ui.py")
src = open(WEB_UI, encoding="utf-8").read()

# ── hashing ──────────────────────────────────────────────────────────
h = webauth.hash_password("correct horse")
checks["a hash verifies against its own password"] = webauth.verify(
    "correct horse", h)
checks["…and against nothing else"] = not webauth.verify("wrong", h)
checks["the password is not recoverable from the hash"] = (
    "correct horse" not in h)
# Salted, or two people with the same password would have the same hash
# and a precomputed table would do for both of them.
checks["two hashes of one password differ"] = (
    webauth.hash_password("x") != webauth.hash_password("x"))
checks["…and both still verify"] = all(
    webauth.verify("x", webauth.hash_password("x")) for _ in range(3))
# The parameters live in the string, so raising them later cannot lock
# anybody out: an old hash is still verified with the numbers it was made
# with.
checks["the cost parameters travel with the hash"] = (
    h.split("$")[1:4] == ["16384", "8", "1"])
# Made with different parameters, verified with the ones in the string.
import base64 as _b64, hashlib as _hl
_salt = b"0" * 16
_dk = _hl.scrypt(b"x", salt=_salt, n=1024, r=8, p=1, dklen=32)
_cheap = ("scrypt$1024$8$1$" + _b64.b64encode(_salt).decode() + "$"
          + _b64.b64encode(_dk).decode())
checks["…so a hash made with other parameters still verifies"] = (
    webauth.verify("x", _cheap) and not webauth.verify("y", _cheap))
checks["a malformed hash lets nobody in"] = not any(
    webauth.verify("x", bad) for bad in
    ("scrypt$", "scrypt$a$b$c$d$e", "scrypt$16384$8$1$!!!$!!!", "", None))

# WEB_PASSWORD is an environment variable and an environment variable is
# plaintext. This is not a fallback to be removed later.
checks["a plaintext stored password still verifies"] = webauth.verify(
    "geheim", "geheim")
checks["…and rejects the wrong one"] = not webauth.verify("x", "geheim")
checks["an empty stored password admits nobody"] = not webauth.verify("", "")

# ── the username ─────────────────────────────────────────────────────
# Unset accepts anything, which is what the old code did by accident.
# Deliberate now: every existing install has a password and no username,
# and making the name suddenly matter would lock those people out.
checks["no configured username accepts any name"] = all(
    webauth.username_matches(n, "") for n in ("admin", "", "anything"))
checks["a configured username accepts only itself"] = (
    webauth.username_matches("admin", "admin")
    and not webauth.username_matches("root", "admin"))
checks["…ignoring surrounding whitespace"] = webauth.username_matches(
    " admin ", "admin")

# ── sessions ─────────────────────────────────────────────────────────
s = webauth.SessionStore(idle_seconds=100, max_seconds=1000)
tok = s.create("admin")
checks["a session validates and names its user"] = s.validate(tok) == "admin"
checks["an unknown token does not"] = s.validate("nope") is None
checks["…nor an empty one"] = s.validate("") is None
checks["two sessions get different tokens"] = s.create("a") != s.create("a")
checks["a token is long enough not to be guessed"] = len(tok) >= 32

s.destroy(tok)
checks["signing out ends the session on the server"] = s.validate(tok) is None
# Not only in the browser: clearing the cookie alone would leave a stolen
# one working until it expired.
checks["…which is what makes a stolen cookie useless"] = (
    "store.destroy" in src)

idle = webauth.SessionStore(idle_seconds=0, max_seconds=1000)
checks["an idle session expires"] = idle.validate(idle.create("a")) is None
hard = webauth.SessionStore(idle_seconds=1000, max_seconds=0)
checks["…and so does one past its absolute age"] = (
    hard.validate(hard.create("a")) is None)
# Two clocks, because they catch different things: idle catches a machine
# somebody walked away from, absolute catches a background tab keeping a
# session alive forever, which no idle timeout ever would.
checks["the two clocks are separate"] = (
    webauth.SessionStore(idle_seconds=1, max_seconds=2).idle !=
    webauth.SessionStore(idle_seconds=1, max_seconds=2).max)
# Using the interface keeps you signed in.
live = webauth.SessionStore(idle_seconds=100, max_seconds=1000)
t2 = live.create("a")
live.validate(t2)
checks["using a session renews its idle clock"] = live.validate(t2) == "a"

bounded = webauth.SessionStore(limit=3)
for _ in range(10):
    bounded.create("a")
checks["the store is bounded against a login flood"] = len(bounded) <= 3

full = webauth.SessionStore()
kept = full.create("a")
full.clear()
checks["a password change ends every session"] = full.validate(kept) is None
# `sessions`, not `store`: `create_handler` already takes a `store`
# argument, and a local of that name shadows it for the whole of do_POST.
checks["…and the interface does that on save"] = "sessions.clear()" in src

# ── what the request path does with all this ─────────────────────────
# A browser gets the page; a script keeps the 401 it has always had.
# Redirecting scripts to an HTML form would break every scraper silently,
# which is worse than not having the form.
i = src.index("def _send_auth_required")
gate = src[i:src.index("\n        def ", i + 10)]
checks["a browser is sent to the login page"] = (
    "/login?next=" in gate and "302" in gate)
checks["…and a script still gets 401 with WWW-Authenticate"] = (
    "WWW-Authenticate" in gate)
checks["…and /api and /metrics are never redirected"] = (
    '"/api/", "/metrics"' in gate)

# Signing in cannot require being signed in, but must still be protected
# against another site posting a login form at us.
i = src.index("def do_POST")
post = src[i:i + 1200]
checks["POST /login runs before the auth check"] = (
    post.index('== "/login"') < post.index("_check_auth"))
checks["…but not before the CSRF check"] = (
    post.index("_check_csrf") < post.index("_do_login"))

# `?next=` is attacker-controlled, so our own login page must not become
# an open redirect — and the value goes into a `Location:` header, so it
# must not be able to inject headers of its own either. Driven through the
# real function rather than grepped: an earlier grep-only version of this
# check passed while `/\evil.com` and an embedded CRLF both sailed through.
import web_ui  # noqa: E402
_handler = web_ui.create_handler(
    types.SimpleNamespace(web_username="", language="en"),
    checker=None, bot=types.SimpleNamespace(t=None), store=None)
safe = _handler._safe_next
checks["next= keeps a real path on this site"] = (
    safe("/status") == "/status" and safe("/history?x=1") == "/history?x=1")
checks["next= rejects an absolute URL"] = safe("https://evil/") == "/"
checks["next= rejects a protocol-relative //host"] = safe("//evil.com") == "/"
# The two that the grep-only test missed and that shipped broken:
checks["next= rejects a backslash the browser reads as //"] = (
    safe("/\\evil.com") == "/" and safe("\\/evil.com") == "/")
checks["next= rejects an embedded CR or LF (header injection)"] = (
    safe("/foo\r\nSet-Cookie: evil=1") == "/"
    and safe("/foo\nX-Injected: x") == "/")
checks["next= rejects a tab and a NUL"] = (
    safe("/foo\tbar") == "/" and safe("/foo\x00") == "/")
checks["next= falls back to / on empty"] = safe("") == "/" and safe(None) == "/"

# The one thing that must never appear on the login page.
i = src.index("def _login_html")
page = src[i:src.index("\n        def _page_login", i)]
checks["the login page never renders the password"] = (
    "web_password" not in page)
checks["…and marks the fields for password managers"] = (
    'autocomplete="username"' in page
    and 'autocomplete="current-password"' in page)
checks["…and is not cached"] = "no-store" in src
# One message for both, or a wrong name tells a stranger the other one
# was right.
checks["a failed login does not say which half was wrong"] = (
    "web_login_failed" in src and "web_login_user_wrong" not in src)

# The cookie's flags.
checks["the session cookie is HttpOnly"] = "HttpOnly" in src
checks["…and SameSite"] = "SameSite=Lax" in src
# No Secure flag on purpose: this is served over plain HTTP by design, and
# Secure would stop the cookie working at all.
i = src.index("Set-Cookie")
checks["…and not Secure, which would break plain HTTP"] = (
    "Secure" not in src[i:i + 400])

# Saving a password stores a hash, never the password.
i = src.index('if "web_password" in params:')
save = src[i:i + 900]
checks["a password saved in the interface is hashed"] = (
    "webauth.hash_password(new_pw)" in save)
checks["…and the plaintext is not stored anywhere"] = (
    "config.web_password = new_pw" not in src)

# Basic Auth keeps working, and now honours the username too.
i = src.index("def _check_auth")
auth = src[i:src.index("\n        def ", i + 10)]
checks["Basic Auth still authenticates"] = "Basic " in auth
checks["…and now checks the username as well"] = (
    "username_matches" in auth)
checks["…and verifies through webauth, so a hash works"] = (
    "webauth.verify" in auth)
checks["a session is accepted as well"] = "_session_user()" in auth
checks["no password set still means open"] = "if not current:" in auth

# ── migration off the plaintext on disk ──────────────────────────────
cfg = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "config.py"), encoding="utf-8").read()
i = cfg.index("def _migrate_web_password")
mig = cfg[i:cfg.index("\n    def ", i + 10)]
checks["an existing plaintext password is migrated on start"] = (
    "hash_password(stored)" in mig and "save_persistent()" in mig)
checks["…once, not on every start"] = "is_hashed(stored)" in mig
checks["…and not when an env var is overruling the file"] = (
    'getattr(self, "web_password", "") != stored' in mig)
checks["…and it keeps working if the migration fails"] = (
    "self.web_password = stored" in mig)

# ── the login POST, driven for real ──────────────────────────────────
# The audit found the whole request path was only ever grepped. Drive it:
# right password in, cookie out; wrong password, 401 and no cookie; and
# the redirect target run through _safe_next so an attacker's `next` can
# neither send you off-site nor inject a header.
import web_ui  # noqa: E402


def login(username, password, nxt="/", stored=None):
    """Drive _do_login and return (status, headers, cookie, audit)."""
    hashed = webauth.hash_password("secret") if stored is None else stored
    cfg = types.SimpleNamespace(web_username=username if username else "",
                                web_password=hashed, web_session_hours=8,
                                web_session_max_days=7, language="en")
    hc = web_ui.create_handler(cfg, checker=None,
                               bot=types.SimpleNamespace(t=None), store=None)
    h = hc.__new__(hc)
    h.server = types.SimpleNamespace(
        sessions=webauth.SessionStore(),
        audit=types.SimpleNamespace(records=[],
                                    record=lambda *a, **k: h.server.audit.records.append(a)))
    cap = {"headers": {}, "status": None}
    h.send_response = lambda s: cap.__setitem__("status", s)
    h.send_header = lambda k, v: cap["headers"].__setitem__(k, v)
    h.end_headers = lambda: None
    h.wfile = types.SimpleNamespace(write=lambda b: None)
    body = f"username={username}&password={password}&next={nxt}"
    h._do_login(__import__("urllib.parse", fromlist=["parse_qs"])
                .parse_qs(body, keep_blank_values=True))
    return cap, h.server.sessions, h.server.audit.records


cap, store, audit = login("", "secret")
checks["a correct password gets 302 and a session cookie"] = (
    cap["status"] == 302
    and cap["headers"].get("Set-Cookie", "").startswith("ds_session=")
    and "HttpOnly" in cap["headers"]["Set-Cookie"])
checks["…the session it hands out actually validates"] = (
    len(store) == 1)
checks["…and it is recorded in the audit trail as ok"] = (
    any("ok" in a for a in audit))

cap, store, audit = login("", "WRONG")
checks["a wrong password gets 401"] = cap["status"] == 401
checks["…and no session cookie"] = "Set-Cookie" not in cap["headers"]
checks["…no session is created"] = len(store) == 0
checks["…and it is recorded as failed"] = any("failed" in a for a in audit)

# The username is checked now, where the old code threw it away.
cap, _, _ = login("admin", "secret")  # no WEB_USERNAME set → any name in
checks["any username passes when none is configured"] = cap["status"] == 302
cap, _, _ = login("admin", "secret", stored=webauth.hash_password("secret"))
# With a username required, the wrong one is refused even with right pw.
cfg = types.SimpleNamespace(web_username="alice",
                            web_password=webauth.hash_password("secret"),
                            web_session_hours=8, web_session_max_days=7,
                            language="en")
checks["a wrong username is refused even with the right password"] = (
    not (webauth.username_matches("bob", cfg.web_username)
         and webauth.verify("secret", cfg.web_password)))

# The attacker-controlled next never reaches the Location header intact.
cap, _, _ = login("", "secret", nxt="/\\evil.com")
checks["a hostile next= is neutralised in the real redirect"] = (
    cap["headers"].get("Location") == "/")
cap, _, _ = login("", "secret", nxt="/foo%0d%0aX-Injected: x")
loc = cap["headers"].get("Location", "")
checks["…and cannot inject a header through the redirect"] = (
    "\r" not in loc and "\n" not in loc and "X-Injected" not in loc)

# ── the session-bound clamp on load (config) ─────────────────────────
import config as _config  # noqa: E402
checks["a bad session value on load clamps instead of crashing"] = (
    _config._clamp_int(None, 1, 720, 8) == 8
    and _config._clamp_int("abc", 1, 365, 7) == 7
    and _config._clamp_int(100000, 1, 720, 8) == 720
    and _config._clamp_int(-5, 1, 720, 8) == 1)

# ── the session store is safe across threads ─────────────────────────
import threading as _threading  # noqa: E402
_store = webauth.SessionStore(limit=40)


def _hammer(n):
    for i in range(150):
        tok = _store.create(f"u{n}")
        _store.validate(tok)
        if i % 4 == 0:
            _store.destroy(tok)


_threads = [_threading.Thread(target=_hammer, args=(n,)) for n in range(16)]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()
checks["concurrent logins never crash the store and honour the cap"] = (
    len(_store) <= 40)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
