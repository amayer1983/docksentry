#!/usr/bin/env python3
"""The Settings page takes every value the environment takes, and no more.

On 16.11.2026 `from_env()` widened three ranges: `DOCKER_STOP_TIMEOUT`
and `HEALTHCHECK_MAX_STARTING` to 86400, `DISK_WARN_PERCENT` down to 1.
The Settings form kept copies of the old bounds, in its `min`/`max`
attributes and in the clamps of the save handler. Measured on 18.11.2026
with the real `_page_settings()` and a value of 5400 in the environment:
`value="5400" min="1" max="3600"`, so the field rejects its own value.
The audit measured in headless Firefox that the browser then refuses to
submit the whole form, every tab of it. A POST that got through anyway
stored 3600 for 5400, 3600 for 7200 and 50 for 30.

Since then both sides read `config.INT_RANGES`. This file does not read
that table to decide anything. It measures three things and demands
they agree for every numeric field the two settings pages render:

* what `from_env()` clamps an absurdly low and high value to,
* the `min`/`max` the rendered page carries,
* what the real save handler stores for the same two values.

Then the widened values themselves are driven through both the page and
the save, and the counter-direction: what lies outside the range is
still outside the rendered bounds, and the save still clamps it.
"""

import io
import contextlib
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, HERE)

import config as config_mod  # noqa: E402
import web_ui  # noqa: E402
import test_settings_fields_render as render_mod  # noqa: E402

checks = {}
LOW, HIGH = "-999999999", "999999999"

#: The numeric fields the two settings pages render, enumerated by hand
#: from app/web_ui.py on 18.11.2026 (every `type="number"` input with
#: `form="settings-form"` or `form="conn-form"`, plus the weekday
#: select). The check below fails if the rendered set differs, so a
#: field added later without a range shows up here.
EXPECTED = {
    "web_session_hours", "web_session_max_days",
    "healthcheck_max_starting", "docker_stop_timeout", "pull_timeout",
    "cleanup_grace_hours", "cleanup_backup_days", "disk_warn_percent",
    "weekly_report_hour", "monitor_interval_seconds", "smtp_port",
    "weekly_report_weekday",
}


# ── the three measurements ───────────────────────────────────────────
def render(page, **values):
    """The real page's HTML, with `values` set on the config."""
    cfg = render_mod._config()
    for k, v in values.items():
        setattr(cfg, k, v)
    cls = web_ui.create_handler(cfg, checker=None, bot=None, store=None)
    h = cls.__new__(cls)
    out = {}
    h._send_html = lambda html, status=200: out.update(html=html)
    h._render_page = lambda content, active=None: content
    h._windows_html = lambda t: ""
    h._maint_mode_html = lambda t: ""
    getattr(h, f"_page_{page}")()
    return out.get("html", "")


def fields(html):
    """{name: (min, max, value)} for each numeric settings field."""
    got = {}
    for tag in re.findall(r'<input type="number"[^>]*>', html):
        a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if a.get("form") not in ("settings-form", "conn-form"):
            continue
        got[a["name"]] = (int(a["min"]), int(a["max"]), a.get("value"))
    m = re.search(r'<select name="weekly_report_weekday"[^>]*>(.*?)</select>',
                  html, re.S)
    if m:
        opts = [int(v) for v in re.findall(r'<option value="(\d+)"', m.group(1))]
        sel = re.search(r'<option value="(\d+)" selected', m.group(1))
        got["weekly_report_weekday"] = (min(opts), max(opts),
                                        sel.group(1) if sel else None)
        # A select only offers what it lists: a gap would be a value the
        # environment allows and the page cannot show.
        checks["the weekday select lists every day in its range, once"] = (
            opts == list(range(min(opts), max(opts) + 1)))
    return got


def from_env(key, raw):
    """What a real `Config.from_env()` makes of `ENV=raw`."""
    env = config_mod.PERSISTENT_ENV_VARS[key]
    os.environ[env] = raw
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return int(getattr(config_mod.Config.from_env(), key))
    except Exception as e:                                  # noqa: BLE001
        return f"raised {e!r}"
    finally:
        os.environ.pop(env, None)


class _Cfg:
    """Enough config for the save handler; records nothing but values."""
    _CALLS = ("env_override", "save", "load", "apply", "write", "set_")
    language = "en"

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name.startswith(self._CALLS):
            return lambda *a, **kw: None
        return ""


def save(key, raw, start=12345):
    """POST `key=raw` through the real do_POST and read back the value.

    `start` is a sentinel that is neither end of any range, so a save
    that did not touch the field cannot pass a check on the bounds.
    """
    cfg = _Cfg(**{key: start})
    path = "/connections" if key == "smtp_port" else "/settings"
    cls = web_ui.create_handler(cfg, checker=None,
                                bot=types.SimpleNamespace(t=None), store=None,
                                restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    body = f"settings_page=1&{key}={raw}"
    h.path = path
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x"}
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode())
    h.client_address = ("127.0.0.1", 0)
    h._check_auth = lambda: True
    h._audit = lambda *a, **k: None
    h._send_redirect = lambda loc: None
    h._page_settings = lambda: None
    h._page_connections = lambda: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            h.do_POST()
    except Exception as e:                                  # noqa: BLE001
        return f"raised {e!r}"
    return getattr(cfg, key)


def main():
    tmp = tempfile.mkdtemp(prefix="ds-settings-ranges-")
    os.environ["DATA_DIR"] = tmp
    for env in config_mod.PERSISTENT_ENV_VARS.values():
        os.environ.pop(env, None)

    rendered = {**fields(render("settings")), **fields(render("connections"))}
    checks["the pages render exactly the numeric fields listed here"] = (
        set(rendered) == EXPECTED)
    if set(rendered) != EXPECTED:
        print(f"  rendered but not listed: {sorted(set(rendered) - EXPECTED)}")
        print(f"  listed but not rendered: {sorted(EXPECTED - set(rendered))}")

    # ── one range, three doors ──────────────────────────────────────
    print("  field                       env         form        save")
    for key in sorted(EXPECTED):
        env = (from_env(key, LOW), from_env(key, HIGH))
        form = rendered.get(key, (None, None, None))[:2]
        saved = (save(key, LOW), save(key, HIGH))
        print(f"  {key:27} {str(env):11} {str(form):11} {saved}")
        checks[f"{key}: the form's bounds are the ones from_env() enforces"] = (
            form == env)
        checks[f"{key}: …and so are the save handler's"] = saved == env

    # ── the widened values, on the page and through the save ────────
    wide = [("docker_stop_timeout", 5400), ("docker_stop_timeout", 86400),
            ("healthcheck_max_starting", 7200),
            ("healthcheck_max_starting", 86400),
            ("disk_warn_percent", 30), ("disk_warn_percent", 1)]
    for key, v in wide:
        lo, hi, shown = fields(render("settings", **{key: v}))[key]
        checks[f"{key}={v}: the page shows it in a field that admits it"] = (
            shown == str(v) and lo <= v <= hi)
        checks[f"{key}={v}: the save stores it unchanged"] = (
            save(key, str(v)) == v)

    # ── counter-direction: outside the range is still outside ──────
    outside = [("docker_stop_timeout", 86401, 86400),
               ("docker_stop_timeout", 0, 1),
               ("healthcheck_max_starting", 86401, 86400),
               ("healthcheck_max_starting", 29, 30),
               ("disk_warn_percent", 0, 1),
               ("disk_warn_percent", 101, 100)]
    for key, v, clamped in outside:
        lo, hi, _ = fields(render("settings", **{key: v}))[key]
        checks[f"{key}={v}: the page's bounds still refuse it"] = (
            not lo <= v <= hi)
        checks[f"{key}={v}: …and a save that gets through stores {clamped}"] = (
            save(key, str(v)) == clamped)
    checks["something that is not a number leaves the value alone"] = (
        save("docker_stop_timeout", "abc", start=77) == 77)

    bad = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print("FAIL" if bad else "PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
