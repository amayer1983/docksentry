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
# an open redirect.
i = src.index("def _safe_next")
nxt = src[i:src.index("\n        def ", i + 10)]
checks["next= only accepts a path on this site"] = (
    'startswith("/")' in nxt and 'startswith("//")' in nxt)

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

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
