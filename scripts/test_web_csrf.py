#!/usr/bin/env python3
"""The one check that stands between a cached login and another site.

`docs/security.md` says every state-changing request to the Web UI is
checked against its origin. On 06.10.2026 a mutation sweep found the
promise was kept by nothing: `_check_csrf` could be made to return
`True` unconditionally and all 181 procedures stayed green.

The reason was in the tests themselves. Three procedures mention CSRF;
**two of them switch it off** — `h._check_csrf = lambda: True` — so that
the thing they actually wanted to test could run. The third compares the
position of two strings in the source. A check that is disabled by the
suite is not covered by it; it is covered *against*.

What it defends: Basic Auth credentials live in the browser, so a form
on any other page can POST to the Web UI and the browser will attach
them. The Origin header is what distinguishes "the admin clicked a
button on this page" from "the admin had another tab open". A request
with neither Origin nor Referer is rejected rather than trusted —
every real browser sends at least one on a POST, so the only thing that
arrives without them is something that is not a browser.

Driven through `do_POST`, with real headers, against the real method.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402

checks = {}


class _Cfg:
    """Enough config for a handler to be built."""

    def __init__(self):
        self.web_username = ""
        self.web_password_hash = ""
        self.language = "en"

    def __getattr__(self, name):
        return ""


def _post(headers, path="/settings"):
    """One POST. Returns ("reached", …) or ("forbidden", reason)."""
    handler_cls = web_ui.create_handler(
        _Cfg(), checker=None, bot=types.SimpleNamespace(t=None),
        store=None, restart_discord=lambda *_a, **_k: None)
    h = handler_cls.__new__(handler_cls)
    h.path = path
    body = "a=1"
    hdrs = {"Content-Length": str(len(body))}
    hdrs.update(headers)
    h.headers = hdrs
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode())
    h.client_address = ("127.0.0.1", 0)
    out = []
    # Auth is a different gate and is covered elsewhere; what is under
    # test here is the one below it. `_check_csrf` itself is NOT
    # replaced — that is the whole point of this file.
    h._check_auth = lambda: True
    h._audit = lambda *a, **k: None
    h._send_forbidden = lambda reason="Forbidden": out.append(("forbidden",
                                                               reason))
    h._send_redirect = lambda loc: out.append(("reached", loc))
    h._page_settings = lambda: out.append(("reached", "page"))
    h._page_connections = lambda: out.append(("reached", "page"))
    h._do_login = lambda form: out.append(("reached", "login"))
    try:
        h.do_POST()
    except Exception as e:                                  # noqa: BLE001
        # A handler that blew up past the gate still tells us the gate
        # let it through, which is the only question here.
        out.append(("reached", f"raised {type(e).__name__}"))
    return out[0] if out else ("nothing", "")


def _blocked(headers, path="/settings"):
    return _post(headers, path)[0] == "forbidden"


SAME = {"Host": "docksentry.lan:8080"}

# ── the ordinary, legitimate request ─────────────────────────────────
# Stated first: if this were blocked too, every check below would pass
# for the wrong reason and the Web UI would simply be broken.
checks["a POST from the page itself gets through"] = not _blocked(
    dict(SAME, Origin="http://docksentry.lan:8080"))
checks["…and so does one with only a Referer"] = not _blocked(
    dict(SAME, Referer="http://docksentry.lan:8080/settings"))

# ── the attack it exists for ─────────────────────────────────────────
checks["a POST from another site is rejected"] = _blocked(
    dict(SAME, Origin="https://evil.example"))
checks["…including one that only differs in port"] = _blocked(
    dict(SAME, Origin="http://docksentry.lan:9090"))
checks["…and one whose Referer is elsewhere"] = _blocked(
    dict(SAME, Referer="https://evil.example/page"))
checks["…and a hostname that merely starts the same"] = _blocked(
    dict(SAME, Origin="http://docksentry.lan.evil.example:8080"))

# ── the header that is simply absent ─────────────────────────────────
# Rejected rather than trusted: every real browser sends at least one of
# the two on a POST, so what arrives without them is not a browser.
checks["a POST with neither Origin nor Referer is rejected"] = _blocked(SAME)
checks["…and one with no Host header at all"] = _blocked(
    {"Origin": "http://docksentry.lan:8080"})
checks["…and one whose Origin is unparseable"] = _blocked(
    dict(SAME, Origin="not a url"))
checks["…and one whose Origin is empty"] = _blocked(dict(SAME, Origin=""))

# ── signing in is checked too ────────────────────────────────────────
# It is the one POST that cannot require being signed in already, which
# is exactly why it needs this: without it another site could post a
# login form at us.
checks["the login form is checked as well"] = _blocked(
    dict(SAME, Origin="https://evil.example"), path="/login")
checks["…and a genuine login still works"] = not _blocked(
    dict(SAME, Origin="http://docksentry.lan:8080"), path="/login")

# ── and the suite must stop switching it off ─────────────────────────
# Two procedures used to set `_check_csrf = lambda: True` so that what
# they really wanted to test could run. Both already send matching
# headers, so the override bought nothing and cost the only coverage
# this check had.
for _name in ("test_settings_post.py", "test_setup_password.py"):
    _p = os.path.join(os.path.dirname(__file__), _name)
    _src = open(_p, encoding="utf-8").read() if os.path.exists(_p) else ""
    checks[f"{_name} no longer switches the CSRF check off"] = (
        "_check_csrf = lambda" not in _src)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
