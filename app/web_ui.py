#!/usr/bin/env python3
"""Optional lightweight Web UI for configuration and status."""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


def create_handler(config, checker, bot, password=None):
    """Create a request handler with access to app components."""

    # Pre-compute password hash if set
    pw_hash = hashlib.sha256(password.encode()).hexdigest() if password else None

    class WebHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default logging

        def _check_auth(self):
            """Check Basic Auth if password is configured."""
            if not pw_hash:
                return True
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pw = decoded.split(":", 1)
                return hashlib.sha256(pw.encode()).hexdigest() == pw_hash
            except Exception:
                return False

        def _send_auth_required(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Docksentry"')
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>401 - Login required</h1>")

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
<html lang="{config.language}">
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
.btn-sm {{ padding: 3px 10px; border-radius: 4px; font-size: 12px; border: none; cursor: pointer; }}
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
<h1>🐳 <span>Docksentry</span></h1>
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
            path = self._get_path()
            if path == "/settings":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)

                # Update language
                if "language" in params:
                    from i18n import available_languages, get_translator
                    new_lang = params["language"][0]
                    if new_lang in available_languages():
                        config.language = new_lang
                        bot.t = get_translator(new_lang)

                # Update debug
                config.debug = "debug" in params

                # Update auto_selfupdate
                config.auto_selfupdate = "auto_selfupdate" in params

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
            else:
                self._send_html("<h1>404</h1>", 404)

        def _page_status(self):
            containers = self._get_containers()
            pending = self._get_pending()
            pending_names = [u["name"] for u in pending]
            pinned = bot._get_pinned()

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

                update_badge = ""
                if c["name"] in pending_names:
                    update_badge = f' <span class="badge badge-yellow">update</span>'

                pinned_badge = ""
                if c["name"] in pinned:
                    pinned_badge = f' <span class="badge badge-red">{t("web_pinned_badge")}</span>'

                # Action buttons
                actions = ""
                if c["name"] in pending_names:
                    actions += f'<form method="POST" action="/api/update" style="display:inline"><input type="hidden" name="name" value="{c["name"]}"><button type="submit" class="btn-sm btn-green">{t("web_update")}</button></form> '
                if c["name"] in pinned:
                    actions += f'<form method="POST" action="/api/unpin" style="display:inline"><input type="hidden" name="name" value="{c["name"]}"><button type="submit" class="btn-sm btn-outline">{t("web_unpin")}</button></form>'
                else:
                    actions += f'<form method="POST" action="/api/pin" style="display:inline"><input type="hidden" name="name" value="{c["name"]}"><button type="submit" class="btn-sm btn-outline">{t("web_pin")}</button></form>'

                rows += f"""<tr>
<td>{c['name']}{update_badge}{pinned_badge}</td>
<td><code>{c['image']}</code></td>
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
<td>{h['timestamp']}</td>
<td>{h['container']}</td>
<td>{icon}</td>
<td style="font-size:12px">{h.get('detail', '')}</td>
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
            t = get_translator(config.language)

            saved = "?saved=1" in self.path
            saved_html = f'<div style="background:#1a3a2a;color:#3fb950;padding:10px;border-radius:6px;margin-bottom:16px">{t("web_saved")}</div>' if saved else ""

            langs = available_languages()
            lang_names = {"en": "English", "de": "Deutsch", "fr": "Français", "es": "Español", "it": "Italiano", "nl": "Nederlands", "pt": "Português", "pl": "Polski", "tr": "Türkçe", "ru": "Русский", "uk": "Українська", "ar": "العربية", "hi": "हिन्दी", "ja": "日本語", "ko": "한국어", "zh": "中文"}
            lang_options = ""
            for l in langs:
                sel = 'selected' if l == config.language else ''
                name = lang_names.get(l, l.upper())
                lang_options += f'<option value="{l}" {sel}>{name}</option>\n'

            debug_checked = 'checked' if config.debug else ''
            auto_su_checked = 'checked' if config.auto_selfupdate else ''

            content = f"""
{saved_html}
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
<input type="text" value="{config.cron_schedule}" disabled title="Change via CRON_SCHEDULE env var">
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
<input type="text" value="{', '.join(config.exclude_containers)}" disabled title="Change via EXCLUDE_CONTAINERS env var">
</div>

<div style="margin-top:16px">
<button type="submit" class="btn">{t("web_save")}</button>
</div>

</form>
</div>

<div class="card">
<h2>Info</h2>
<table>
<tr><td>Bot Token</td><td><code>{config.bot_token[:8]}...{config.bot_token[-4:]}</code></td></tr>
<tr><td>Chat ID</td><td><code>{config.chat_id}</code></td></tr>
<tr><td>Data Dir</td><td><code>{config.data_dir}</code></td></tr>
</table>
</div>"""

            self._send_html(self._render_page(content, "settings"))

        def _page_logs(self):
            from i18n import get_translator
            t = get_translator(config.language)

            query = parse_qs(urlparse(self.path).query)
            container = query.get("container", [""])[0]
            lines = int(query.get("lines", ["50"])[0])

            containers = self._get_containers()

            # Container dropdown
            options = ""
            for c in containers:
                sel = 'selected' if c["name"] == container else ''
                options += f'<option value="{c["name"]}" {sel}>{c["name"]}</option>\n'

            log_html = ""
            if container:
                result = subprocess.run(
                    ["docker", "logs", "--tail", str(lines), container],
                    capture_output=True, text=True, timeout=10
                )
                output = result.stdout or result.stderr
                if output.strip():
                    # Escape HTML
                    import html
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
