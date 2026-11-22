#!/usr/bin/env python3
"""A Web UI locked without a password says why, and how to get in.

Since v2.19.0 an instance that has been configured before stays locked
when it has no Web UI password: a lost or quarantined settings.json must
not hand the setup wizard to the first visitor (`test_web_auth_gate.py`).
The lock is right and stays. What it did not do was explain itself.
Measured on the live 2.20.0-beta.1 instance and on 0779842: no
`WEB_PASSWORD`, no `web_password` in settings.json, settings saved once
— and every request got a bare "401 - Login required", with any user
and an empty password, and the login form said only "Docksentry is
password protected", for a password nobody had set.

Now the 401, the login form and a failed sign-in say that no Web UI
password is set, that the UI is locked because this instance was
configured before, and that `WEB_PASSWORD` in the environment, then a
restart, is the way in. The start prints one line saying the same. A
fresh install and one with a password say none of it.

Driven against the real handler with a real `Config` on a temporary
data directory, the way the live instance got there: settings saved
once, no password anywhere. The responses are read off the bytes the
handler writes.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import config as _config_mod  # noqa: E402
import web_ui  # noqa: E402
import webauth  # noqa: E402

LANG = os.path.join(os.path.dirname(__file__), "..", "app", "lang")


def _lang(code):
    with open(os.path.join(LANG, f"{code}.json"), encoding="utf-8") as f:
        return json.load(f)


EN, DE = _lang("en"), _lang("de")
# `.get`, so a tree without the key is red, not a traceback.
HINT = EN.get("web_locked_no_password", "\0missing hint")
HINT_DE = DE.get("web_locked_no_password", "\0missing hint de")

checks = {}


def _real_config(saved_once, password=None, language=None):
    """A real Config on a fresh data directory.

    `saved_once`: settings were saved once, without a password — what
    the live instance had. `password` goes into the environment."""
    work = tempfile.mkdtemp(prefix="ds-locked-")
    keep = {k: os.environ.get(k) for k in ("DATA_DIR", "WEB_PASSWORD",
                                           "LANGUAGE")}
    os.environ["DATA_DIR"] = work
    os.environ.pop("WEB_PASSWORD", None)
    os.environ.pop("LANGUAGE", None)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            if saved_once:
                c = _config_mod.Config.from_env()
                c.language = language or "en"
                c.save_persistent()
            if password:
                os.environ["WEB_PASSWORD"] = password
            c = _config_mod.Config.from_env()
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return c


def _request(cfg, method, path, headers=None, body=b""):
    """(status, headers, body text) for one request to the real handler."""
    cls = web_ui.create_handler(cfg, checker=None,
                                bot=types.SimpleNamespace(t=None),
                                store=None,
                                restart_discord=lambda *_a, **_k: None)
    h = cls.__new__(cls)
    h.path = path
    h.command = method
    h.request_version = "HTTP/1.1"
    h.requestline = f"{method} {path} HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    h.headers = dict({"Host": "x", "Content-Length": str(len(body))},
                     **(headers or {}))
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.server = types.SimpleNamespace(
        sessions=webauth.SessionStore(idle_seconds=3600, max_seconds=86400),
        audit=None, token_seen={})
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            (h.do_GET if method == "GET" else h.do_POST)()
        except Exception as e:                              # noqa: BLE001
            return 0, {}, f"RAISED {type(e).__name__}: {e}"
    raw = h.wfile.getvalue().decode("utf-8", "replace")
    head, _sep, text = raw.partition("\r\n\r\n")
    lines = head.split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        status = 0
    hdrs = {}
    for ln in lines[1:]:
        k, _s, v = ln.partition(":")
        hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, text


def _says(text, hint=HINT):
    """The hint, as the page writes it (escaped)."""
    return web_ui._e(hint) in text


def _login_post(cfg, user, pw):
    body = urlencode({"username": user, "password": pw,
                      "next": "/"}).encode()
    return _request(cfg, "POST", "/login",
                    headers={"Origin": "http://x",
                             "Content-Type":
                                 "application/x-www-form-urlencoded"},
                    body=body)


def _basic(user, pw):
    import base64
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _startup_log(cfg):
    """What `WebUI.start` prints, with the socket and thread stood in."""
    class _Server:
        def __init__(self, *a, **k):
            pass

        def serve_forever(self):
            pass

    real = web_ui._QuietHTTPServer
    web_ui._QuietHTTPServer = _Server
    out = io.StringIO()
    try:
        ui = web_ui.WebUI(cfg, checker=None, bot=types.SimpleNamespace(t=None),
                          store=None, port=0)
        with contextlib.redirect_stdout(out):
            ui.start()
    except Exception as e:                                  # noqa: BLE001
        return f"RAISED {type(e).__name__}: {e}"
    finally:
        web_ui._QuietHTTPServer = real
    return out.getvalue()


# ── the state from the live instance ─────────────────────────────────
locked = _real_config(saved_once=True)
checks["the reproduction: settings saved once, no password anywhere"] = (
    os.path.exists(locked.settings_file)
    and not (getattr(locked, "web_password", "") or ""))

status, hdrs, text = _request(locked, "GET", "/")
checks["a script asking for / is still refused (the lock stays)"] = (
    status == 401)
checks["…and the 401 says no password is set and how to get in"] = (
    _says(text))
if not _says(text):
    print(f"      → GET / (script): {status} {text[:200]!r}")

status, hdrs, text = _request(locked, "GET", "/api/status",
                              headers={"Sec-Fetch-Mode": "cors"})
checks["a background fetch is refused too, and told the same"] = (
    status == 401 and _says(text))

status, hdrs, text = _request(locked, "GET", "/",
                              headers={"Accept": "text/html"})
checks["a browser asking for / is sent to the login page"] = (
    status == 302 and hdrs.get("location", "").startswith("/login"))

status, hdrs, text = _request(locked, "GET", "/login?next=/",
                              headers={"Accept": "text/html"})
checks["the login page is shown"] = status == 200 and "<form" in text
checks["…and says no password is set and how to get in"] = _says(text)
checks["…instead of only \"password protected\""] = (
    web_ui._e(EN["web_login_intro"]) not in text)
if not _says(text):
    print(f"      → GET /login: {status}")

for user in ("", "admin", "someone"):
    status, _h, text = _login_post(locked, user, "")
    checks[f"signing in as {user or 'nobody'!r} with an empty password is "
           f"refused"] = status == 401
    checks[f"…and the answer says why ({user or 'nobody'!r})"] = _says(text)
    status, _h, text = _request(locked, "GET", "/", headers=_basic(user, ""))
    checks[f"Basic Auth as {user or 'nobody'!r} with an empty password is "
           f"refused"] = status == 401 and _says(text)

log = _startup_log(locked)
_line = [ln for ln in log.splitlines() if "WEB_PASSWORD" in ln]
checks["the start says it in the log, once"] = (
    len(_line) == 1 and "locked" in _line[0].lower()
    and "restart" in _line[0].lower())
if len(_line) != 1:
    print(f"      → start printed: {log!r}")

# In the configured language.
locked_de = _real_config(saved_once=True, language="de")
status, _h, text = _request(locked_de, "GET", "/login?next=/",
                            headers={"Accept": "text/html"})
checks["the login page says it in the configured language"] = (
    _says(text, HINT_DE) and HINT_DE != HINT)

# ── the way in it names actually works ───────────────────────────────
withpw = _real_config(saved_once=True, password="hunter2")
status, _h, text = _request(withpw, "GET", "/", headers=_basic("any", "hunter2"))
checks["WEB_PASSWORD in the environment, after a restart, gets in"] = (
    status not in (401, 302, 0))
status, _h, text = _request(withpw, "GET", "/")
checks["…and with it set, the 401 does not claim no password is set"] = (
    status == 401 and not _says(text))
status, _h, text = _request(withpw, "GET", "/login?next=/",
                            headers={"Accept": "text/html"})
checks["…nor the login page"] = status == 200 and not _says(text)
status, _h, text = _login_post(withpw, "any", "wrong")
checks["…nor a failed sign-in"] = status == 401 and not _says(text)
checks["…nor the start"] = "WEB_PASSWORD" not in _startup_log(withpw)

# ── a genuine first run is open and says nothing of the sort ─────────
fresh = _real_config(saved_once=False)
status, _h, text = _request(fresh, "GET", "/login?next=/",
                            headers={"Accept": "text/html"})
checks["a fresh install is still open (the login page sends you on)"] = (
    status == 302)
checks["…and its start says nothing about a lock"] = (
    "WEB_PASSWORD" not in _startup_log(fresh))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
