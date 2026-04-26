#!/usr/bin/env python3
"""Optional lightweight Web UI for configuration and status."""

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


def _e(value):
    """HTML-escape a value (including quotes) for safe insertion into HTML
    content or attribute values. Always coerces to str first."""
    return html.escape(str(value if value is not None else ""), quote=True)


# Cloud metadata endpoints — credential theft targets, hard-blocked
_CLOUD_METADATA_HOSTS = {
    "169.254.169.254",          # AWS, Azure, OpenStack, DigitalOcean (IPv4 link-local)
    "fd00:ec2::254",            # AWS IPv6 metadata
    "metadata.google.internal", # GCP
    "metadata.goog",            # GCP
    "metadata",                 # Some cloud providers' short hostname
}

# Discord webhook hosts (used for stricter discord_webhook validation)
_DISCORD_HOSTS = {
    "discord.com",
    "discordapp.com",
    "canary.discord.com",
    "ptb.discord.com",
}


def _validate_cron(expr):
    """Validate a 5-field cron expression. Returns (ok, error_message).

    Mirrors the parsing logic in Scheduler._matches_cron — if any field would
    raise ValueError there at runtime, validation fails here at save time.
    Empty / blank expressions are rejected (cron is required).
    """
    if not expr or not expr.strip():
        return False, "empty"
    parts = expr.strip().split()
    if len(parts) != 5:
        return False, f"need 5 space-separated fields, got {len(parts)}"

    field_names = ("minute", "hour", "day-of-month", "month", "day-of-week")
    for name, pattern in zip(field_names, parts):
        if pattern == "*":
            continue
        try:
            if "/" in pattern and "-" in pattern.split("/")[0]:
                range_part, step_part = pattern.split("/", 1)
                start_s, end_s = range_part.split("-")
                int(start_s); int(end_s); int(step_part)
            elif pattern.startswith("*/"):
                int(pattern[2:])
            elif "," in pattern:
                for v in pattern.split(","):
                    int(v)
            elif "-" in pattern:
                start_s, end_s = pattern.split("-")
                int(start_s); int(end_s)
            else:
                int(pattern)
        except (ValueError, IndexError):
            return False, f"invalid {name} field: {pattern!r}"

    return True, None


def _validate_webhook_url(url, kind="generic"):
    """
    Validate a user-supplied webhook URL.

    kind="generic": http(s) only, blocks cloud metadata endpoints. Allows
        private/LAN addresses (selfhosted users frequently target Ntfy/Gotify/
        Home Assistant on internal networks — that's legitimate).
    kind="discord": additionally requires the host to be an official Discord
        webhook host.

    Returns (ok: bool, error_message: str|None). Empty/blank URLs are treated
    as "disabled" and pass validation.
    """
    if not url or not url.strip():
        return True, None

    url = url.strip()

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Invalid URL ({exc})"

    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"Only http:// and https:// URLs are allowed (got {parsed.scheme!r})"

    if not parsed.hostname:
        return False, "URL has no hostname"

    host_lower = parsed.hostname.lower()

    # Cloud metadata: block hostname form
    if host_lower in _CLOUD_METADATA_HOSTS:
        return False, f"Cloud metadata endpoint ({host_lower}) is blocked"

    # Cloud metadata: block IP-literal form (e.g. http://169.254.169.254)
    try:
        ip = ipaddress.ip_address(host_lower)
        if str(ip) in _CLOUD_METADATA_HOSTS:
            return False, "Cloud metadata endpoint IP is blocked"
        # Also block link-local addresses — they're rarely used legitimately
        # and can be abused (AWS metadata is a link-local IP).
        if ip.is_link_local:
            return False, f"Link-local address ({ip}) is blocked"
    except ValueError:
        pass  # Not a literal IP, that's fine

    if kind == "discord" and host_lower not in _DISCORD_HOSTS:
        return False, f"Discord webhook host must be discord.com (got {host_lower})"

    return True, None


def create_handler(config, checker, bot, password=None):
    """Create a request handler with access to app components."""

    # Pre-compute password hash if set
    pw_hash = hashlib.sha256(password.encode()).hexdigest() if password else None

    class WebHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default logging

        def _check_auth(self):
            """Check Basic Auth if password is configured.

            Uses hmac.compare_digest for the hash comparison to avoid the
            theoretical timing-side-channel that comes with `==` on bytes.
            """
            if not pw_hash:
                return True
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pw = decoded.split(":", 1)
                submitted = hashlib.sha256(pw.encode()).hexdigest()
                return hmac.compare_digest(submitted, pw_hash)
            except Exception:
                return False

        def _check_csrf(self):
            """Origin/Referer-based CSRF check for state-changing requests.

            Modern browsers always send `Origin` on cross-origin POSTs (and
            usually on same-origin POSTs too). For older browsers we fall
            back to `Referer`. Either header's host:port must match the
            request's `Host` header.

            A request that arrives without Origin AND without Referer is
            rejected — every legitimate browser sends at least one.
            """
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return False

            origin = (self.headers.get("Origin") or "").strip()
            referer = (self.headers.get("Referer") or "").strip()
            source = origin or referer
            if not source:
                return False

            try:
                source_netloc = urlparse(source).netloc.lower()
            except Exception:
                return False

            if not source_netloc:
                return False

            # Compare host:port. Browsers always include the port in netloc
            # when it's non-default, and the Host header includes it too.
            return source_netloc == host

        def _send_auth_required(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Docksentry"')
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>401 - Login required</h1>")

        def _send_forbidden(self, reason="Forbidden"):
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h1>403 - {_e(reason)}</h1>".encode())

        def _send_html(self, html, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def _send_redirect(self, path="/"):
            self.send_response(303)
            self.send_header("Location", path)
            self.end_headers()

        def _get_path(self):
            """Return path without query string."""
            return urlparse(self.path).path

        def _get_containers(self):
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Status}}"],
                capture_output=True, text=True
            )
            containers = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) == 3:
                    containers.append({
                        "name": parts[0],
                        "image": parts[1],
                        "status": parts[2],
                    })
            return containers

        def _get_pending(self):
            if os.path.exists(config.pending_file):
                with open(config.pending_file) as f:
                    return json.load(f)
            return []

        def _render_page(self, content, active="status"):
            from i18n import get_translator
            from version import VERSION
            t = get_translator(config.language)

            nav_items = [
                ("status", f'📊 {t("web_nav_status")}', "/"),
                ("history", f'📋 {t("web_nav_history")}', "/history"),
                ("logs", f'📜 {t("web_nav_logs")}', "/logs"),
                ("settings", f'⚙️ {t("web_nav_settings")}', "/settings"),
            ]
            nav_html = ""
            for key, label, href in nav_items:
                cls = ' class="active"' if key == active else ""
                nav_html += f'<a href="{href}"{cls}>{label}</a> '

            return f"""<!DOCTYPE html>
<html lang="{_e(config.language)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Docksentry</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #0d1117; color: #c9d1d9; line-height: 1.6; }}
.header {{ background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; }}
.header h1 {{ font-size: 18px; display: inline; }}
.header h1 span {{ color: #58a6ff; }}
nav {{ margin-top: 12px; }}
nav a {{ color: #8b949e; text-decoration: none; padding: 6px 14px; border-radius: 6px; font-size: 14px; }}
nav a:hover {{ color: #c9d1d9; background: #21262d; }}
nav a.active {{ color: #58a6ff; background: #1f2937; }}
.content {{ max-width: 900px; margin: 24px auto; padding: 0 24px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 16px; }}
.card h2 {{ font-size: 16px; margin-bottom: 12px; color: #58a6ff; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th {{ text-align: left; padding: 8px 12px; color: #8b949e; border-bottom: 1px solid #30363d; font-weight: 500; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
tr:hover {{ background: #1c2128; }}
.healthy {{ color: #3fb950; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; }}
.badge-green {{ background: #1a3a2a; color: #3fb950; }}
.badge-yellow {{ background: #3a2f1a; color: #d29922; }}
.badge-blue {{ background: #1a2a3a; color: #58a6ff; }}
form {{ margin-top: 8px; }}
label {{ display: block; margin-bottom: 4px; font-size: 14px; color: #8b949e; }}
input, select {{ background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 8px 12px;
    border-radius: 6px; font-size: 14px; width: 100%; margin-bottom: 12px; }}
select {{ cursor: pointer; }}
.btn {{ background: #238636; color: #fff; border: none; padding: 8px 20px; border-radius: 6px;
    cursor: pointer; font-size: 14px; }}
.btn:hover {{ background: #2ea043; }}
.btn-blue {{ background: #1f6feb; }}
.btn-blue:hover {{ background: #388bfd; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 600px) {{ .grid {{ grid-template-columns: 1fr; }} }}
.stat {{ text-align: center; }}
.stat .num {{ font-size: 32px; font-weight: bold; color: #58a6ff; }}
.stat .label {{ font-size: 12px; color: #8b949e; }}
.badge-red {{ background: #3a1a1a; color: #f85149; }}
.badge-purple {{ background: #2a1a3a; color: #bc8cff; }}
.btn-sm {{ padding: 3px 10px; border-radius: 4px; font-size: 12px; border: none; cursor: pointer; }}
.toggle {{ position: relative; display: inline-block; width: 36px; height: 20px; vertical-align: middle; }}
.toggle input {{ opacity: 0; width: 0; height: 0; }}
.toggle .slider {{ position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
    background: #30363d; border-radius: 20px; transition: 0.2s; }}
.toggle .slider:before {{ content: ""; position: absolute; height: 14px; width: 14px; left: 3px; bottom: 3px;
    background: #8b949e; border-radius: 50%; transition: 0.2s; }}
.toggle input:checked + .slider {{ background: #238636; }}
.toggle input:checked + .slider:before {{ transform: translateX(16px); background: #fff; }}
.btn-green {{ background: #238636; color: #fff; }}
.btn-green:hover {{ background: #2ea043; }}
.btn-outline {{ background: transparent; color: #8b949e; border: 1px solid #30363d; }}
.btn-outline:hover {{ color: #c9d1d9; border-color: #8b949e; }}
pre {{ background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 16px;
    overflow-x: auto; font-size: 13px; line-height: 1.5; color: #c9d1d9; white-space: pre-wrap; word-wrap: break-word; }}
.footer {{ text-align: center; padding: 24px; font-size: 12px; color: #484f58; }}
</style>
</head>
<body>
<div class="header">
<h1><img src="data:image/png;base64,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" alt="Logo" style="height:32px;vertical-align:middle;margin-right:8px"> <span>Docksentry</span></h1>
<nav>{nav_html}</nav>
</div>
<div class="content">
{content}
</div>
<div class="footer">Docksentry v{VERSION}</div>
</body>
</html>"""

        def do_GET(self):
            if not self._check_auth():
                return self._send_auth_required()
            path = self._get_path()
            if path == "/" or path == "/status":
                self._page_status()
            elif path == "/history":
                self._page_history()
            elif path == "/logs":
                self._page_logs()
            elif path == "/settings":
                self._page_settings()
            elif path == "/api/check":
                threading.Thread(target=self._api_check).start()
                self._send_redirect("/")
            else:
                self._send_html("<h1>404</h1>", 404)

        def do_POST(self):
            if not self._check_auth():
                return self._send_auth_required()
            # CSRF mitigation: every POST must originate from the same host.
            # Forged cross-origin POSTs (from a malicious site abusing the
            # admin's cached Basic Auth credentials) are rejected here.
            if not self._check_csrf():
                return self._send_forbidden("CSRF check failed")
            path = self._get_path()
            if path == "/settings":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)

                # --- Validate before mutating any state ---
                errors = []
                if "cron_schedule" in params and params["cron_schedule"][0].strip():
                    ok, err = _validate_cron(params["cron_schedule"][0].strip())
                    if not ok:
                        errors.append(f"Cron schedule: {err}")
                if "discord_webhook" in params:
                    ok, err = _validate_webhook_url(
                        params["discord_webhook"][0].strip(), kind="discord"
                    )
                    if not ok:
                        errors.append(f"Discord webhook: {err}")
                if "webhook_url" in params:
                    ok, err = _validate_webhook_url(
                        params["webhook_url"][0].strip(), kind="generic"
                    )
                    if not ok:
                        errors.append(f"Webhook URL: {err}")
                if errors:
                    from urllib.parse import quote
                    self._send_redirect("/settings?error=" + quote(" | ".join(errors)))
                    return

                # --- All inputs validated; apply changes ---
                # Update language
                if "language" in params:
                    from i18n import available_languages, get_translator
                    new_lang = params["language"][0]
                    if new_lang in available_languages():
                        config.language = new_lang
                        bot.t = get_translator(new_lang)

                # Update debug & auto_selfupdate (checkboxes)
                config.debug = "debug" in params
                config.auto_selfupdate = "auto_selfupdate" in params

                # Update cron schedule
                if "cron_schedule" in params and params["cron_schedule"][0].strip():
                    config.cron_schedule = params["cron_schedule"][0].strip()

                # Update exclude containers
                if "exclude_containers" in params:
                    raw = params["exclude_containers"][0].strip()
                    config.exclude_containers = [c.strip() for c in raw.split(",") if c.strip()] if raw else []

                # Update Discord webhook
                if "discord_webhook" in params:
                    config.discord_webhook = params["discord_webhook"][0].strip()

                # Update generic webhook
                if "webhook_url" in params:
                    config.webhook_url = params["webhook_url"][0].strip()

                # Update Telegram Topic ID
                if "telegram_topic_id" in params:
                    config.telegram_topic_id = params["telegram_topic_id"][0].strip()

                # Persist all changes
                config.save_persistent()

                self._send_redirect("/settings?saved=1")
            elif path == "/api/update":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    threading.Thread(target=self._api_update, args=(name,)).start()
                self._send_redirect("/")
            elif path == "/api/pin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    pinned = bot._get_pinned()
                    if name not in pinned:
                        pinned.append(name)
                        bot._save_pinned(pinned)
                self._send_redirect("/")
            elif path == "/api/unpin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    pinned = bot._get_pinned()
                    if name in pinned:
                        pinned.remove(name)
                        bot._save_pinned(pinned)
                self._send_redirect("/")
            elif path == "/api/autoupdate":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    auto_list = bot._get_autoupdate()
                    if name in auto_list:
                        auto_list.remove(name)
                    else:
                        auto_list.append(name)
                    bot._save_autoupdate(auto_list)
                self._send_redirect("/")
            else:
                self._send_html("<h1>404</h1>", 404)

        def _page_status(self):
            containers = self._get_containers()
            pending = self._get_pending()
            pending_names = [u["name"] for u in pending]
            pinned = bot._get_pinned()
            auto_list = bot._get_autoupdate()

            from i18n import get_translator
            t = get_translator(config.language)

            rows = ""
            for c in containers:
                status_text = c["status"]
                if "healthy" in status_text.lower():
                    status_badge = '<span class="badge badge-green">healthy</span>'
                elif "starting" in status_text.lower():
                    status_badge = '<span class="badge badge-yellow">starting</span>'
                else:
                    status_badge = f'<span class="badge badge-blue">running</span>'

                # Badges
                badges = ""
                if c["name"] in pending_names:
                    badges += f' <span class="badge badge-yellow">update</span>'
                if c["name"] in pinned:
                    badges += f' <span class="badge badge-red">{t("web_pinned_badge")}</span>'
                if c["name"] in auto_list:
                    badges += f' <span class="badge badge-purple">{t("web_autoupdate_badge")}</span>'

                # Action buttons (container name is escaped for safe use in HTML attributes)
                name_attr = _e(c["name"])
                actions = ""
                if c["name"] in pending_names:
                    actions += f'<form method="POST" action="/api/update" style="display:inline"><input type="hidden" name="name" value="{name_attr}"><button type="submit" class="btn-sm btn-green">{t("web_update")}</button></form> '
                if c["name"] in pinned:
                    actions += f'<form method="POST" action="/api/unpin" style="display:inline"><input type="hidden" name="name" value="{name_attr}"><button type="submit" class="btn-sm btn-outline">{t("web_unpin")}</button></form> '
                else:
                    actions += f'<form method="POST" action="/api/pin" style="display:inline"><input type="hidden" name="name" value="{name_attr}"><button type="submit" class="btn-sm btn-outline">{t("web_pin")}</button></form> '
                # Autoupdate toggle
                is_auto = c["name"] in auto_list
                checked = "checked" if is_auto else ""
                auto_title = _e(t("web_autoupdate_disable") if is_auto else t("web_autoupdate_enable"))
                actions += f'<form method="POST" action="/api/autoupdate" style="display:inline" title="{auto_title}"><input type="hidden" name="name" value="{name_attr}"><label class="toggle"><input type="checkbox" {checked} onchange="this.form.submit()"><span class="slider"></span></label></form>'

                rows += f"""<tr>
<td>{_e(c['name'])}{badges}</td>
<td><code>{_e(c['image'])}</code></td>
<td>{status_badge}</td>
<td>{actions}</td>
</tr>"""

            content = f"""
<div class="grid">
<div class="card stat">
    <div class="num">{len(containers)}</div>
    <div class="label">{t("web_containers")}</div>
</div>
<div class="card stat">
    <div class="num">{len(pending)}</div>
    <div class="label">{t("web_updates_available")}</div>
</div>
</div>

<div class="card">
<h2>{t("web_containers")}</h2>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
<span style="font-size:12px;color:#8b949e">{t("web_containers_running", count=len(containers))}</span>
<a href="/api/check" class="btn btn-blue" style="text-decoration:none;font-size:13px">{t("web_check_updates")}</a>
</div>
<table>
<tr><th>{t("web_name")}</th><th>{t("web_image")}</th><th>{t("web_status")}</th><th>{t("web_actions")}</th></tr>
{rows}
</table>
</div>"""

            self._send_html(self._render_page(content, "status"))

        def _page_history(self):
            from i18n import get_translator
            t = get_translator(config.language)

            history = []
            if os.path.exists(config.history_file):
                try:
                    with open(config.history_file) as f:
                        history = json.load(f)
                except (json.JSONDecodeError, IOError):
                    pass

            if not history:
                content = f"""<div class="card">
<h2>{t("web_history")}</h2>
<p style="color:#8b949e">{t("web_history_empty")}</p>
</div>"""
            else:
                rows = ""
                for h in reversed(history):
                    icon = '<span class="badge badge-green">✅</span>' if h["success"] else '<span class="badge badge-yellow">❌</span>'
                    rows += f"""<tr>
<td>{_e(h.get('timestamp', ''))}</td>
<td>{_e(h.get('container', ''))}</td>
<td>{icon}</td>
<td style="font-size:12px">{_e(h.get('detail', ''))}</td>
</tr>"""

                content = f"""<div class="card">
<h2>{t("web_history")}</h2>
<table>
<tr><th>{t("web_date")}</th><th>{t("web_name")}</th><th>{t("web_result")}</th><th>{t("web_detail")}</th></tr>
{rows}
</table>
</div>"""

            self._send_html(self._render_page(content, "history"))

        def _page_settings(self):
            from i18n import available_languages, get_translator
            from version import VERSION
            t = get_translator(config.language)

            query = parse_qs(urlparse(self.path).query)
            saved = "saved" in query
            saved_html = f'<div style="background:#1a3a2a;color:#3fb950;padding:10px;border-radius:6px;margin-bottom:16px">{t("web_saved")}</div>' if saved else ""

            error_msg = query.get("error", [""])[0]
            error_html = (
                f'<div style="background:#3a1a1a;color:#f85149;padding:10px;border-radius:6px;margin-bottom:16px">'
                f'⚠️ {_e(error_msg)}</div>'
                if error_msg else ""
            )

            langs = available_languages()
            lang_names = {"en": "English", "de": "Deutsch", "fr": "Français", "es": "Español", "it": "Italiano", "nl": "Nederlands", "pt": "Português", "pl": "Polski", "tr": "Türkçe", "ru": "Русский", "uk": "Українська", "ar": "العربية", "hi": "हिन्दी", "ja": "日本語", "ko": "한국어", "zh": "中文"}
            lang_options = ""
            for l in langs:
                sel = 'selected' if l == config.language else ''
                name = lang_names.get(l, l.upper())
                lang_options += f'<option value="{_e(l)}" {sel}>{_e(name)}</option>\n'

            debug_checked = 'checked' if config.debug else ''
            auto_su_checked = 'checked' if config.auto_selfupdate else ''

            # Mask sensitive values
            token_masked = f"{config.bot_token[:4]}...{config.bot_token[-4:]}" if len(config.bot_token) > 8 else "***"
            chat_masked = f"{config.chat_id[:3]}...{config.chat_id[-3:]}" if len(config.chat_id) > 6 else "***"

            content = f"""
{saved_html}
{error_html}
<div class="card">
<h2>{t("web_settings")}</h2>
<form method="POST" action="/settings">

<div class="grid">
<div>
<label>{t("web_language")}</label>
<select name="language">
{lang_options}
</select>
</div>
<div>
<label>{t("web_cron_schedule")}</label>
<input type="text" name="cron_schedule" value="{_e(config.cron_schedule)}">
</div>
</div>

<div class="grid">
<div>
<label><input type="checkbox" name="debug" {debug_checked} style="width:auto;margin-right:8px"> {t("web_debug_mode")}</label>
</div>
<div>
<label><input type="checkbox" name="auto_selfupdate" {auto_su_checked} style="width:auto;margin-right:8px"> {t("web_auto_selfupdate")}</label>
</div>
</div>

<div style="margin-top:8px">
<label>{t("web_excluded")}</label>
<input type="text" name="exclude_containers" value="{_e(', '.join(config.exclude_containers))}" placeholder="container1, container2">
</div>

<div style="margin-top:8px">
<label>Telegram Topic ID</label>
<input type="text" name="telegram_topic_id" value="{_e(config.telegram_topic_id)}" placeholder="{_e(t('web_topic_id_placeholder'))}">
</div>

<div style="margin-top:8px">
<label>Discord Webhook</label>
<input type="text" name="discord_webhook" value="{_e(config.discord_webhook)}" placeholder="https://discord.com/api/webhooks/...">
</div>

<div style="margin-top:8px">
<label>Webhook URL</label>
<input type="text" name="webhook_url" value="{_e(config.webhook_url)}" placeholder="https://your-service/webhook">
</div>

<div style="margin-top:16px">
<button type="submit" class="btn">{t("web_save")}</button>
</div>

</form>
</div>

<div class="card">
<h2>Info</h2>
<table>
<tr><td>Version</td><td><code>v{_e(VERSION)}</code></td></tr>
<tr><td>Bot Token</td><td><code>{_e(token_masked)}</code></td></tr>
<tr><td>Chat ID</td><td><code>{_e(chat_masked)}</code></td></tr>
<tr><td>Data Dir</td><td><code>{_e(config.data_dir)}</code></td></tr>
</table>
<p style="font-size:12px;color:#484f58;margin-top:8px">Bot Token and Chat ID can only be changed via environment variables.</p>
</div>"""

            self._send_html(self._render_page(content, "settings"))

        def _page_logs(self):
            from i18n import get_translator
            t = get_translator(config.language)

            query = parse_qs(urlparse(self.path).query)
            container = query.get("container", [""])[0]
            lines = int(query.get("lines", ["50"])[0])

            containers = self._get_containers()

            # Container dropdown (escape names — they appear in HTML attribute and content)
            options = ""
            for c in containers:
                sel = 'selected' if c["name"] == container else ''
                name_e = _e(c["name"])
                options += f'<option value="{name_e}" {sel}>{name_e}</option>\n'

            log_html = ""
            if container:
                result = subprocess.run(
                    ["docker", "logs", "--tail", str(lines), container],
                    capture_output=True, text=True, timeout=10
                )
                output = result.stdout or result.stderr
                if output.strip():
                    log_html = f'<pre>{html.escape(output.strip())}</pre>'
                else:
                    log_html = f'<p style="color:#8b949e">No logs found.</p>'

            content = f"""
<div class="card">
<h2>{t("web_logs")}</h2>
<form method="GET" action="/logs" style="display:flex;gap:12px;align-items:end;margin-bottom:16px">
<div style="flex:1">
<label>Container</label>
<select name="container">{options}</select>
</div>
<div style="width:100px">
<label>{t("web_logs_lines")}</label>
<input type="number" name="lines" value="{lines}" min="10" max="500">
</div>
<button type="submit" class="btn btn-blue" style="height:38px">{t("web_logs_show")}</button>
</form>
{log_html}
</div>"""

            self._send_html(self._render_page(content, "logs"))

        def _api_update(self, name):
            """Trigger update for a single container from Web UI."""
            try:
                if not os.path.exists(config.pending_file):
                    return
                with open(config.pending_file) as f:
                    updates = json.load(f)
                target = next((u for u in updates if u["name"] == name), None)
                if not target:
                    return
                compose_kwargs = {k: target[k] for k in target if k.startswith("compose_")}
                success, msg = checker.update_container(name, target["image"], **compose_kwargs)
                status = "✅" if success else "❌"
                bot.send_message(f"{status} `{name}`: {msg}")
                if bot.notifier:
                    bot.notifier.send_update_result(name, target["image"], success, msg)
                # Remove from pending
                remaining = [u for u in updates if u["name"] != name]
                with open(config.pending_file, "w") as f:
                    json.dump(remaining, f)
            except Exception as e:
                print(f"Web UI update error: {e}")

        def _api_check(self):
            try:
                updates = checker.check_all(bot=bot)
                if updates:
                    bot.notify_updates(updates)
            except Exception as e:
                print(f"Web UI check error: {e}")

    return WebHandler


class WebUI:
    def __init__(self, config, checker, bot, port=8080, password=""):
        self.config = config
        self.port = port
        self.handler = create_handler(config, checker, bot, password or None)
        self.server = None
        self.thread = None

    def start(self):
        self.server = HTTPServer(("0.0.0.0", self.port), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"Web UI started on port {self.port}")

    def stop(self):
        if self.server:
            self.server.shutdown()
