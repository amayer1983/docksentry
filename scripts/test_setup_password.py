#!/usr/bin/env python3
"""The first-run wizard makes you set a password (#60, @NotRetarded + owner).

Two people arrived at the same request from opposite ends. @NotRetarded
tried to get an initial "create a username and password" screen and found
there wasn't one — deleting the password from the compose file and the
settings file did not produce a setup prompt, because setup was already
marked done. The owner wanted exactly that flow built: fresh boot with no
password, and the first thing you do in the interface is set one.

So the setup wizard now has a password step, and it is first. It is a
gate: you leave it either with a password (typed twice, matching, stored
as a scrypt hash) or by deliberately ticking a "no password" box for the
reverse-proxy / trusted-LAN case. The JS enforces it for feedback; this
file is about the half that actually matters, the server — because the JS
can be skipped and the box can be forged, and neither must open a
passwordless dashboard by accident.

Not retroactive, on purpose: an existing install already has
web_setup_done set and never sees the wizard, so upgrading to this does
not suddenly demand a password from someone running open behind their own
proxy. New installs get the step; everyone else sets it under Settings,
which already worked.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import web_ui  # noqa: E402
import webauth  # noqa: E402

checks = {}


def wizard(body):
    """Drive POST /api/wizard and return (config, redirect target)."""
    cfg = types.SimpleNamespace(
        web_password="", web_username="", web_setup_done=False,
        language="en", cron_schedule="0 18 * * *", discord_webhook="",
        webhook_url="", ui_mode="advanced", bot_token="", chat_id="",
        saved=False)
    cfg.save_persistent = lambda: setattr(cfg, "saved", True)
    hc = web_ui.create_handler(cfg, checker=None,
                               bot=types.SimpleNamespace(enabled=False, t=None),
                               store=None)
    h = hc.__new__(hc)
    h.path = "/api/wizard"
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x"}
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode())
    h.client_address = ("127.0.0.1", 0)
    out = {}
    h._send_redirect = lambda loc: out.__setitem__("loc", loc)
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._audit_post = lambda p: None
    h.do_POST()
    return cfg, out.get("loc", "")


# ── the gate: no password, no opt-out → setup does not complete ──────
cfg, loc = wizard("web_password=&web_password_confirm=&language=en&auto_mode=manual")
checks["no password and no opt-out does not finish setup"] = (
    cfg.web_setup_done is False)
checks["…and sends the user back to the wizard with an error"] = (
    loc == "/setup?pw=1")
checks["…and stores no password"] = cfg.web_password == ""

# Mismatched confirmation is refused the same way.
cfg, loc = wizard("web_password=abc&web_password_confirm=xyz&auto_mode=manual")
checks["a mismatched confirmation does not finish setup"] = (
    cfg.web_setup_done is False and loc == "/setup?pw=1")

# ── a real password: hashed, stored, setup done ─────────────────────
cfg, loc = wizard("web_username=chef&web_password=hunter2&"
                  "web_password_confirm=hunter2&language=en&auto_mode=manual")
checks["a matching password finishes setup"] = cfg.web_setup_done is True
checks["…stored as a scrypt hash, not plaintext"] = (
    webauth.is_hashed(cfg.web_password) and "hunter2" not in cfg.web_password)
checks["…that the password then verifies against"] = (
    webauth.verify("hunter2", cfg.web_password))
checks["…and the username is kept"] = cfg.web_username == "chef"
checks["…and it lands on the dashboard"] = loc.startswith("/?")

# ── the deliberate opt-out: no password, but a conscious choice ─────
cfg, loc = wizard("no_password=1&language=en&auto_mode=manual")
checks["the no-password tick finishes setup"] = cfg.web_setup_done is True
checks["…and leaves no password set (open, as chosen)"] = (
    cfg.web_password == "")

# ── the skip endpoint cannot jump the password by URL ───────────────
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "web_ui.py"), encoding="utf-8").read()
i = src.index('elif path == "/api/wizard_skip":')
skip = src[i:i + 700]
checks["the skip endpoint refuses to open a passwordless dashboard"] = (
    'if not (getattr(config, "web_password"' in skip
    and 'self._send_redirect("/setup")' in skip)
# And it is no longer linked from the wizard — the in-step tick is the
# only escape now.
i = src.index("def _page_setup")
page = src[i:src.index("\n        def ", i + 10)]
checks["the blanket skip link is gone from the wizard"] = (
    "/api/wizard_skip" not in page)

# ── the wizard renders the password step, first, with its parts ─────
checks["the wizard has five steps now"] = page.count('data-step-pane=') == 5
checks["…step one is the password"] = (
    page.index('data-step-pane="1"') < page.index('name="web_password"')
    < page.index('data-step-pane="2"'))
checks["…with a confirm field and the no-password tick"] = (
    'name="web_password_confirm"' in page and 'name="no_password"' in page)
checks["…and the password field is a password input, not text"] = (
    'type="password" name="web_password"' in page)

# ── the logout button is an SVG, not a glyph that may be missing ────
# It shipped as ⏻ (U+23FB) and rendered as a tofu box in @NotRetarded's
# font (#60). The theme toggle beside it was already an SVG.
i = src.index("logout_html = ")
lo = src[i:i + 900]
checks["the logout control is an SVG, not the power glyph"] = (
    "<svg" in lo and "⏻" not in lo)
checks["…and still carries an accessible label"] = "aria-label" in lo

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
