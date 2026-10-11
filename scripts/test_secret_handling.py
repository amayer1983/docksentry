#!/usr/bin/env python3
"""Three promises about secrets that nothing was holding.

Found by a suite-wide mutation sweep on 11.10.2026: each of these can be
deleted and none of 201 procedures goes red.

* **A new password ends the old password's sessions.** Otherwise
  changing it is theatre: whoever was signed in with the old one stays
  signed in, which is exactly the case where somebody changes a password
  — because they think someone else has it. `SessionStore.clear()` was
  driven by `test_web_login.py`; what nothing held was the *wiring*,
  and the only check on it was a grep for `"sessions.clear()"` in the
  source. I made the call unreachable and the procedure stayed green,
  because the literal was still there.
* **`settings.json` is written 0600.** It carries webhook URLs with
  tokens in them, the Web UI password hash and the bot token. `0o600`
  appeared in no procedure anywhere.
* **The bot token is masked on the settings page.** A screenshot of
  that page is the most-shared artefact this project has — three of the
  four bug reports with images attached show it. `token_masked` and
  `chat_masked` appeared in no procedure.

Each is driven: a real `do_POST`, a real file on disk read back with
`os.stat`, a real page rendered and searched for the secret itself.
"""

import os
import re
import stat
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402
from webauth import SessionStore  # noqa: E402

checks = {}

PASSWORD = "correct-horse-battery"
TOKEN = "123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
CHAT = "987654321"


class _Cfg:
    """Enough config to render and to save, and nothing more."""

    # `env_overrides` is read as a list, `env_override(...)` is called:
    # the prefix rule cannot serve both, so the list is explicit.
    _CALLABLE = ("env_override(", "save", "load", "apply", "write", "set_",
                 "has_", "reload")
    _CALL_NAMES = ("env_override",)

    def __init__(self, **kw):
        self.language = "en"
        self.bot_token = TOKEN
        self.chat_id = CHAT
        self.web_password = ""
        self.saved = 0
        for k, v in kw.items():
            setattr(self, k, v)

    def save_persistent(self):
        self.saved += 1

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._CALL_NAMES or name.startswith(
                tuple(c for c in self._CALLABLE if not c.endswith("("))):
            return lambda *a, **kw: None
        if name.endswith("s"):
            # Plural reads are iterated (`env_overrides`); an empty list
            # is the shape the page expects.
            return []
        return ""


class _Store:
    """The state store, answering empty — the page needs it to render."""

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **kw: {}


def _handler(cfg, sessions=None):
    cls = web_ui.create_handler(cfg, checker=None,
                               bot=types.SimpleNamespace(t=None),
                               store=_Store(),
                               restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    h.client_address = ("127.0.0.1", 0)
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    h._audit_post = lambda *a, **k: None
    h._send_redirect = lambda loc: None
    h._page_settings = lambda: None
    h._page_connections = lambda: None
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h.wfile = types.SimpleNamespace(write=lambda b: None)
    if sessions is not None:
        h._sessions = lambda: sessions
    return h


def _post(h, body, path="/settings"):
    h.path = path
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x",
                 "Content-Type": "application/x-www-form-urlencoded"}
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode())
    try:
        h.do_POST()
    except Exception as e:                                  # noqa: BLE001
        return f"raised {type(e).__name__}: {e}"
    return ""


# ── a new password ends the old one's sessions ───────────────────────
_store = SessionStore()
_tok = _store.create("andreas")
checks["a session exists to begin with"] = bool(
    _store.validate(_tok))

_cfg = _Cfg()
_raised = _post(_handler(_cfg, sessions=_store),
                f"settings_page=1&web_password={PASSWORD}")
checks["saving a new password signs the old sessions out"] = (
    _store.validate(_tok) is None)
checks["…without the save itself failing"] = (_raised == ""
                                              and _cfg.saved == 1)

# The counter-direction, and it is the one that stops this passing on a
# page that drops every session on every save: a settings save that does
# NOT touch the password leaves people signed in. Being logged out on
# every unrelated change is how an interface teaches people to hate it.
_store2 = SessionStore()
_tok2 = _store2.create("andreas")
_cfg2 = _Cfg(web_password="already-set")
_post(_handler(_cfg2, sessions=_store2), "settings_page=1&language=de")
checks["…while an unrelated save leaves them alone"] = bool(
    _store2.validate(_tok2))

# An empty field is not a change: the form submits `web_password=` on
# every settings save, and treating that as "set the password to
# nothing" would both wipe the password and sign everybody out on an
# unrelated save. (A non-empty field always signs everybody out, even
# when the text matches the current password — the field is only
# non-empty because somebody typed in it, so that is the right way
# round.)
_store3 = SessionStore()
_tok3 = _store3.create("andreas")
_cfg3 = _Cfg(web_password="already-set")
_post(_handler(_cfg3, sessions=_store3),
      "settings_page=1&web_password=&language=de")
checks["…and an empty password field changes nothing"] = (
    bool(_store3.validate(_tok3))
    and _cfg3.web_password == "already-set")

# ── settings.json is not world-readable ─────────────────────────────
# It holds the webhook URLs, the password hash and the bot token. On a
# shared data volume 0644 hands all of that to anyone who can read the
# directory.
from config import Config  # noqa: E402

_d = tempfile.mkdtemp(prefix="secret-")
_path = os.path.join(_d, "settings.json")
_c = Config.__new__(Config)
_c.settings_file = _path
with open(_path, "w", encoding="utf-8") as _f:
    _f.write("{}")
os.chmod(_path, 0o644)
_c._restrict_settings_perms()
_mode = stat.S_IMODE(os.stat(_path).st_mode)
checks["settings.json is readable only by its owner"] = _mode == 0o600
if _mode != 0o600:
    print(f"      → mode is {oct(_mode)}")

# A path that cannot be chmodded must not take the save down with it —
# some filesystems ignore it entirely.
_c2 = Config.__new__(Config)
_c2.settings_file = "/proc/nonexistent/settings.json"
try:
    _c2._restrict_settings_perms()
    checks["…and a filesystem that refuses chmod is survived"] = True
except Exception:                                           # noqa: BLE001
    checks["…and a filesystem that refuses chmod is survived"] = False

# ── the page does not print the token ───────────────────────────────
# The settings page is the single most screenshotted thing in this
# project. A full bot token in one of those is a stranger's bot.
def _settings_html(cfg):
    sent = []
    h = _handler(cfg)
    # The real renderer, not the stub `_handler` installs for the POST
    # half — rendering is the thing being measured here.
    del h._page_settings
    h.path = "/settings"
    h.headers = {}
    h._send_html = lambda html, code=200: sent.append(html)
    h._render_page = lambda content, active: content
    try:
        h._page_settings()
    except Exception as e:                                  # noqa: BLE001
        return f"raised {type(e).__name__}: {e}"
    return "\n".join(sent)


_html = _settings_html(_Cfg())
checks["the settings page renders"] = "raised" not in _html and _html != ""
checks["…and the bot token is not in it"] = TOKEN not in _html
checks["…nor the chat id"] = CHAT not in _html
# Masked, not simply absent: the operator has to be able to tell which
# token is configured, and "nothing here" reads as "not configured".
checks["…but enough of the token to recognise it"] = TOKEN[:4] in _html
checks["…and enough of the chat id"] = CHAT[:3] in _html

# A short value is not sliced into something that reveals most of it.
_html = _settings_html(_Cfg(bot_token="123:AB", chat_id="42"))
checks["a short token is hidden entirely, not partly"] = (
    "123:AB" not in _html and "***" in _html)

# And with nothing configured there is no mask to mistake for a value.
_html = _settings_html(_Cfg(bot_token="", chat_id=""))
checks["an unconfigured bot says so"] = "headless" in _html.lower()

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
