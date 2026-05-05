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


# ═══════════════════════════════════════════════════════════════════
# CSS — themed via custom properties so a future light-mode toggle is
# a one-class swap on <html>. All component classes live here; new
# pages should compose existing classes rather than inline styles.
# ═══════════════════════════════════════════════════════════════════
_BASE_CSS = """
:root {
    /* Backgrounds */
    --bg:           #0d1117;
    --bg-elev:      #161b22;
    --bg-elev-2:    #1c2128;
    --bg-input:     #0d1117;
    /* Borders */
    --border:       #30363d;
    --border-soft:  #21262d;
    /* Text */
    --text:         #c9d1d9;
    --text-muted:   #8b949e;
    --text-faint:   #484f58;
    /* Accents */
    --accent:       #58a6ff;
    --accent-bg:    #1f2937;
    --success:      #3fb950;
    --success-bg:   #1a3a2a;
    --warn:         #d29922;
    --warn-bg:      #3a2f1a;
    --danger:       #f85149;
    --danger-bg:    #3a1a1a;
    --info:         #58a6ff;
    --info-bg:      #1a2a3a;
    --special:      #bc8cff;
    --special-bg:   #2a1a3a;
    /* Buttons */
    --btn-green:    #238636;
    --btn-green-h:  #2ea043;
    --btn-blue:     #1f6feb;
    --btn-blue-h:   #388bfd;
    /* Misc */
    --radius:       8px;
    --radius-sm:    6px;
    --radius-pill: 12px;
    --shadow:       0 1px 0 rgba(0,0,0,0.04);
    --tt-bg:        #1f2937;
    --tt-fg:        #c9d1d9;
}

/* ── Reset & base ───────────────────────────────────────────── */
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
}

/* ── Header & nav ───────────────────────────────────────────── */
.header {
    background: var(--bg-elev);
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
}
.header-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
}
.header h1 { font-size: 18px; display: inline; flex: 1; }
.header h1 span { color: var(--accent); }
.header-host-slot { font-size: 13px; color: var(--text-muted); }
nav { margin-top: 12px; }
nav a {
    color: var(--text-muted);
    text-decoration: none;
    padding: 6px 14px;
    border-radius: var(--radius-sm);
    font-size: 14px;
}
nav a:hover { color: var(--text); background: var(--bg-elev-2); }
nav a.active { color: var(--accent); background: var(--accent-bg); }

.content { max-width: 900px; margin: 24px auto; padding: 0 24px; }

/* ── Cards ──────────────────────────────────────────────────── */
.card {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    margin-bottom: 16px;
}
.card h2 { font-size: 16px; margin-bottom: 12px; color: var(--accent); }
.card-intro {
    font-size: 12px;
    color: var(--text-muted);
    margin-bottom: 12px;
}
.card-warn {
    border-color: var(--warn);
    background: linear-gradient(180deg, rgba(210,153,34,0.08) 0%, var(--bg-elev) 60%);
}
.card-warn h2 { color: var(--warn); }

/* ── Tables ─────────────────────────────────────────────────── */
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th {
    text-align: left;
    padding: 8px 12px;
    color: var(--text-muted);
    border-bottom: 1px solid var(--border);
    font-weight: 500;
}
td { padding: 8px 12px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
tr:hover { background: var(--bg-elev-2); }

/* ── Forms ──────────────────────────────────────────────────── */
form { margin-top: 8px; }
label { display: block; margin-bottom: 4px; font-size: 14px; color: var(--text-muted); }
input, select, textarea {
    background: var(--bg-input);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: var(--radius-sm);
    font-size: 14px;
    width: 100%;
    margin-bottom: 12px;
    font-family: inherit;
}
select { cursor: pointer; }
.form-help {
    font-size: 11px;
    color: var(--text-faint);
    margin: -8px 0 12px 0;
}
.form-row-inline {
    display: flex;
    gap: 8px;
    align-items: flex-start;
}
.form-row-inline > * { margin-bottom: 0; }
.form-checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 8px 0;
}
.form-checkbox-row input[type="checkbox"] {
    width: auto;
    margin: 0;
}
.form-checkbox-row label {
    margin: 0;
    color: var(--text);
    font-weight: 500;
    cursor: pointer;
}
hr.section-divider {
    border: none;
    border-top: 1px solid var(--border);
    margin: 16px 0;
}

/* ── Buttons ────────────────────────────────────────────────── */
.btn {
    background: var(--btn-green);
    color: #fff;
    border: none;
    padding: 8px 20px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: 14px;
    font-family: inherit;
    transition: background 0.15s;
}
.btn:hover { background: var(--btn-green-h); }
.btn-blue { background: var(--btn-blue); }
.btn-blue:hover { background: var(--btn-blue-h); }
.btn-danger { background: var(--danger); }
.btn-danger:hover { background: #ff6b62; }
.btn-outline {
    background: transparent;
    color: var(--text-muted);
    border: 1px solid var(--border);
}
.btn-outline:hover {
    color: var(--text);
    border-color: var(--text-muted);
}
.btn-sm {
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 12px;
    border: none;
    cursor: pointer;
    font-family: inherit;
}
/* Icon-only button — square-ish, just an emoji/glyph inside */
.btn-icon {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-muted);
    width: 30px;
    height: 30px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: 14px;
    line-height: 1;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
}
.btn-icon:hover {
    color: var(--text);
    border-color: var(--text-muted);
    background: var(--bg-elev-2);
}
.btn-icon.is-active {
    color: var(--accent);
    border-color: var(--accent);
}
.btn-icon.is-warn {
    color: var(--warn);
    border-color: var(--warn);
}
.btn-icon.is-danger { color: var(--danger); border-color: var(--danger); }
.btn-row { display: inline-flex; gap: 4px; align-items: center; flex-wrap: wrap; }
.inline-form { display: inline; }
.bulk-cb { width: auto; margin: 0; }
.bulk-bar {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
    margin-bottom: 12px;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border-soft);
    border-radius: var(--radius-sm);
}
.bulk-count {
    font-size: 12px;
    color: var(--text-muted);
    margin-right: 4px;
    min-width: 110px;
}
.bulk-divider {
    width: 1px;
    height: 18px;
    background: var(--border);
    margin: 0 6px;
}
@media (max-width: 600px) { .bulk-divider { display: none; } }

/* ── Layout helpers ─────────────────────────────────────────── */
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }
.stat { text-align: center; }
.stat .num { font-size: 32px; font-weight: bold; color: var(--accent); }
.stat .label { font-size: 12px; color: var(--text-muted); }

/* ── Badges ─────────────────────────────────────────────────── */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: var(--radius-pill);
    font-size: 12px;
}
.badge-green  { background: var(--success-bg); color: var(--success); }
.badge-yellow { background: var(--warn-bg); color: var(--warn); }
.badge-blue   { background: var(--info-bg); color: var(--info); }
.badge-red    { background: var(--danger-bg); color: var(--danger); }
.badge-purple { background: var(--special-bg); color: var(--special); }
.healthy { color: var(--success); }

/* ── Toggle (slider) ────────────────────────────────────────── */
.toggle {
    position: relative;
    display: inline-block;
    width: 36px;
    height: 20px;
    vertical-align: middle;
}
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle .slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background: var(--border);
    border-radius: 20px;
    transition: 0.2s;
}
.toggle .slider:before {
    content: "";
    position: absolute;
    height: 14px; width: 14px;
    left: 3px; bottom: 3px;
    background: var(--text-muted);
    border-radius: 50%;
    transition: 0.2s;
}
.toggle input:checked + .slider { background: var(--btn-green); }
.toggle input:checked + .slider:before { transform: translateX(16px); background: #fff; }

/* ── Pre / code ─────────────────────────────────────────────── */
pre {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 16px;
    overflow-x: auto;
    font-size: 13px;
    line-height: 1.5;
    color: var(--text);
    white-space: pre-wrap;
    word-wrap: break-word;
}

/* ── Footer ─────────────────────────────────────────────────── */
.footer {
    text-align: center;
    padding: 24px;
    font-size: 12px;
    color: var(--text-faint);
}
.footer a { color: #6e7681; text-decoration: none; }
.footer a:hover { color: var(--text); }

/* ── Tabs ───────────────────────────────────────────────────── */
.tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 16px;
    flex-wrap: wrap;
}
.tab-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 8px 14px;
    cursor: pointer;
    font-size: 14px;
    font-family: inherit;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    transition: color 0.15s, border-color 0.15s;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.is-active {
    color: var(--accent);
    border-bottom-color: var(--accent);
}
.tab-btn[disabled] {
    color: var(--text-faint);
    cursor: not-allowed;
}
.tab-pane { display: none; }
.tab-pane.is-active { display: block; }

/* ── Toasts ─────────────────────────────────────────────────── */
.toast-container {
    position: fixed;
    top: 16px;
    right: 16px;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-width: 420px;
}
.toast {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: var(--radius-sm);
    padding: 10px 14px;
    color: var(--text);
    font-size: 14px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    animation: toastIn 0.2s ease-out;
}
.toast.is-success { border-left-color: var(--success); }
.toast.is-warn    { border-left-color: var(--warn); }
.toast.is-danger  { border-left-color: var(--danger); }
.toast.is-leaving { opacity: 0; transition: opacity 0.3s; }
@keyframes toastIn {
    from { opacity: 0; transform: translateX(8px); }
    to   { opacity: 1; transform: translateX(0); }
}

/* ── Empty states ───────────────────────────────────────────── */
.empty {
    text-align: center;
    padding: 32px 16px;
    color: var(--text-muted);
}
.empty-icon { font-size: 32px; opacity: 0.5; margin-bottom: 8px; }
.empty-title { font-size: 14px; color: var(--text); margin-bottom: 4px; }
.empty-hint { font-size: 12px; color: var(--text-faint); }

/* ── Help icon (tooltip) ────────────────────────────────────── */
.help {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px; height: 14px;
    margin-left: 4px;
    border-radius: 50%;
    background: var(--bg-elev-2);
    color: var(--text-muted);
    font-size: 10px;
    cursor: help;
    position: relative;
}
.help:hover {
    background: var(--accent-bg);
    color: var(--accent);
}
.help[data-tt]:hover::after {
    content: attr(data-tt);
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--tt-bg);
    color: var(--tt-fg);
    padding: 6px 10px;
    border-radius: var(--radius-sm);
    font-size: 11px;
    white-space: pre-line;
    width: max-content;
    max-width: 280px;
    text-align: left;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    z-index: 100;
    pointer-events: none;
}

/* ── Confirm dialog (modal) ─────────────────────────────────── */
.modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 900;
}
.modal-backdrop.is-open { display: flex; }
.modal {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    max-width: 480px;
    width: 90%;
}
.modal h3 { color: var(--accent); margin-bottom: 12px; }
.modal-body { margin-bottom: 20px; color: var(--text); font-size: 14px; line-height: 1.5; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; }
"""


# Vanilla JS shipped on every page — provides:
#   - Tabs (data-tabs / data-tab-target)
#   - Toasts (window.dsToast(msg, kind))
#   - Cross-page toast carryover via localStorage
#   - Confirm dialog (window.dsConfirm(message, onYes))
_BASE_JS = """
(function() {
    // ── Tabs ──────────────────────────────────────────────────
    document.querySelectorAll('[data-tabs]').forEach(function(group) {
        var buttons = group.querySelectorAll('.tab-btn');
        var panes = document.querySelectorAll('[data-tab-pane="' + group.dataset.tabs + '"]');
        function activate(name) {
            buttons.forEach(function(b) { b.classList.toggle('is-active', b.dataset.tabTarget === name); });
            panes.forEach(function(p) { p.classList.toggle('is-active', p.dataset.tabName === name); });
            try { localStorage.setItem('ds-tab-' + group.dataset.tabs, name); } catch(e) {}
        }
        buttons.forEach(function(b) {
            b.addEventListener('click', function() { activate(b.dataset.tabTarget); });
        });
        // Restore from localStorage or default to first tab
        var stored = null;
        try { stored = localStorage.getItem('ds-tab-' + group.dataset.tabs); } catch(e) {}
        var initial = stored && Array.from(buttons).some(function(b){return b.dataset.tabTarget===stored;})
            ? stored
            : (buttons[0] && buttons[0].dataset.tabTarget);
        if (initial) activate(initial);
    });

    // ── Toasts ────────────────────────────────────────────────
    var container = document.getElementById('ds-toasts');
    if (!container) {
        container = document.createElement('div');
        container.id = 'ds-toasts';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    window.dsToast = function(msg, kind) {
        var t = document.createElement('div');
        t.className = 'toast' + (kind ? ' is-' + kind : '');
        t.textContent = msg;
        container.appendChild(t);
        setTimeout(function() {
            t.classList.add('is-leaving');
            setTimeout(function() { t.remove(); }, 300);
        }, 4000);
    };
    // Pick up toasts queued from the previous page
    try {
        var queued = JSON.parse(localStorage.getItem('ds-toast-queue') || '[]');
        if (queued.length) {
            queued.forEach(function(item) { window.dsToast(item.msg, item.kind); });
            localStorage.removeItem('ds-toast-queue');
        }
    } catch(e) {}
    // URL-param-driven toast (from server-side redirects)
    var qs = new URLSearchParams(window.location.search);
    if (qs.get('saved') === '1') window.dsToast('Settings saved.', 'success');
    if (qs.get('error'))         window.dsToast(qs.get('error'),    'danger');

    // ── Confirm dialog ────────────────────────────────────────
    var modal = document.getElementById('ds-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'ds-modal';
        modal.className = 'modal-backdrop';
        modal.innerHTML = '<div class="modal" role="dialog" aria-modal="true">' +
            '<h3 id="ds-modal-title">Confirm</h3>' +
            '<div id="ds-modal-body" class="modal-body"></div>' +
            '<div class="modal-actions">' +
            '<button type="button" class="btn-sm btn-outline" id="ds-modal-cancel">Cancel</button>' +
            '<button type="button" class="btn-sm btn" id="ds-modal-ok">Confirm</button>' +
            '</div></div>';
        document.body.appendChild(modal);
    }
    var modalCancel = document.getElementById('ds-modal-cancel');
    var modalOk = document.getElementById('ds-modal-ok');
    var pendingHandler = null;
    function closeModal() {
        modal.classList.remove('is-open');
        pendingHandler = null;
    }
    modalCancel.addEventListener('click', closeModal);
    modal.addEventListener('click', function(e) {
        if (e.target === modal) closeModal();
    });
    modalOk.addEventListener('click', function() {
        var h = pendingHandler;
        closeModal();
        if (h) h();
    });
    window.dsConfirm = function(message, onYes, opts) {
        opts = opts || {};
        document.getElementById('ds-modal-title').textContent = opts.title || 'Confirm';
        document.getElementById('ds-modal-body').textContent = message;
        modalOk.textContent = opts.confirmLabel || 'Confirm';
        modalOk.className = 'btn-sm ' + (opts.danger ? 'btn-danger' : 'btn');
        pendingHandler = onYes;
        modal.classList.add('is-open');
    };
    // Auto-wire forms with data-confirm
    document.querySelectorAll('form[data-confirm]').forEach(function(form) {
        form.addEventListener('submit', function(e) {
            if (form.dataset.dsConfirmed === '1') return;
            e.preventDefault();
            window.dsConfirm(form.dataset.confirm, function() {
                form.dataset.dsConfirmed = '1';
                form.submit();
            }, {
                title: form.dataset.confirmTitle || 'Confirm',
                confirmLabel: form.dataset.confirmLabel || 'Confirm',
                danger: form.dataset.confirmDanger === '1',
            });
        });
    });
})();
"""


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


def create_handler(config, checker, bot, store, password=None):
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
<style>{_BASE_CSS}</style>
</head>
<body>
<div class="header">
<div class="header-row">
<h1 style="flex:1;"><img src="data:image/png;base64,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" alt="Logo" style="height:32px;vertical-align:middle;margin-right:8px"> <span>Docksentry</span></h1>
<div class="header-host-slot"><!-- v2.0: host selector slot --></div>
</div>
<nav>{nav_html}</nav>
</div>
<div class="content">
{content}
</div>
<div class="footer">
Docksentry v{VERSION} · <a href="https://github.com/sponsors/amayer1983" target="_blank" rel="noopener noreferrer">❤ Sponsor</a>
</div>
<script>{_BASE_JS}</script>
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
            elif path.startswith("/container/"):
                self._page_container(path[len("/container/"):])
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

                # Update debug & auto_selfupdate / auto_cleanup (checkboxes)
                config.debug = "debug" in params
                config.auto_selfupdate = "auto_selfupdate" in params
                config.auto_cleanup = "auto_cleanup" in params
                config.cleanup_backup_local_only = "cleanup_backup_local_only" in params

                # Numeric cleanup settings — clamp to sane ranges
                if "cleanup_grace_hours" in params:
                    try:
                        v = int(params["cleanup_grace_hours"][0].strip())
                        config.cleanup_grace_hours = max(0, min(v, 8760))  # ≤ 1 year
                    except (ValueError, IndexError):
                        pass
                if "cleanup_backup_days" in params:
                    try:
                        v = int(params["cleanup_backup_days"][0].strip())
                        config.cleanup_backup_days = max(1, min(v, 365))
                    except (ValueError, IndexError):
                        pass

                # Disk-warning settings
                if "disk_warn_percent" in params:
                    try:
                        v = int(params["disk_warn_percent"][0].strip())
                        config.disk_warn_percent = max(50, min(v, 100))
                    except (ValueError, IndexError):
                        pass
                config.disk_warn_auto_cleanup = "disk_warn_auto_cleanup" in params

                # Quiet hours — accept HH:MM or empty
                def _valid_hhmm(s):
                    if not s:
                        return ""
                    try:
                        h, m = s.split(":")
                        if 0 <= int(h) < 24 and 0 <= int(m) < 60:
                            return f"{int(h):02d}:{int(m):02d}"
                    except (ValueError, AttributeError):
                        pass
                    return ""
                if "quiet_hours_start" in params:
                    config.quiet_hours_start = _valid_hhmm(params["quiet_hours_start"][0].strip())
                if "quiet_hours_end" in params:
                    config.quiet_hours_end = _valid_hhmm(params["quiet_hours_end"][0].strip())

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
                    store.pin(name)
                self._send_redirect("/")
            elif path == "/api/unpin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    store.unpin(name)
                self._send_redirect("/")
            elif path == "/api/autoupdate":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0]
                if name:
                    store.toggle_auto(name)
                self._send_redirect("/")
            elif path == "/api/cleanup":
                threading.Thread(target=self._api_cleanup).start()
                self._send_redirect("/settings?saved=1")
            elif path == "/api/selfupdate":
                threading.Thread(target=self._api_selfupdate).start()
                self._send_redirect("/settings?saved=1")
            elif path == "/api/window":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0].strip()
                action = params.get("action", ["save"])[0]
                if name and action == "delete":
                    store.clear_update_window(name)
                elif name and action == "save":
                    start = params.get("start", [""])[0].strip()
                    end = params.get("end", [""])[0].strip()
                    weekdays = [int(d) for d in params.get("weekdays", [])
                                if d.strip().isdigit()]
                    # Basic validation: HH:MM
                    import re as _re
                    if (_re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", start)
                            and _re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", end)):
                        store.set_update_window(name, start, end, weekdays)
                self._send_redirect("/settings#windows")
            elif path == "/api/ask_major":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0].strip()
                if name:
                    store.toggle_ask_before_major(name)
                self._send_redirect("/")
            elif path == "/api/major_confirm":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                name = params.get("name", [""])[0].strip()
                action = params.get("action", [""])[0]
                if name and action == "confirm":
                    threading.Thread(target=bot._confirm_major_update,
                                     args=(checker, name)).start()
                elif name and action == "reject":
                    store.remove_pending_major(name)
                self._send_redirect("/")
            elif path == "/api/bulk":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = parse_qs(body)
                action = params.get("action", [""])[0]
                names = params.get("names", [])
                # Form sends a single comma-separated value (from JS join);
                # fall back to multi-value POST if browser sends repeated key.
                if len(names) == 1 and "," in names[0]:
                    names = [n.strip() for n in names[0].split(",") if n.strip()]
                names = [n for n in names if n.strip()]
                if action and names:
                    threading.Thread(
                        target=self._api_bulk, args=(action, names)
                    ).start()
                self._send_redirect("/")
            else:
                self._send_html("<h1>404</h1>", 404)

        def _page_status(self):
            containers = self._get_containers()
            pending = self._get_pending()
            pending_names = [u["name"] for u in pending]
            pinned = store.get_pinned()
            auto_list = store.get_autoupdate()
            ask_major = store.get_ask_before_major()
            major_pending = store.get_pending_major() or {}

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

                # Badges (compact, only show what's "different" from default)
                badges = ""
                if c["name"] in pending_names:
                    badges += f' <span class="badge badge-yellow" title="{_e(t("web_badge_update_tt"))}">{t("web_badge_update")}</span>'
                if c["name"] in pinned:
                    badges += f' <span class="badge badge-red" title="{_e(t("web_badge_pinned_tt"))}">{t("web_pinned_badge")}</span>'
                if c["name"] in auto_list:
                    badges += f' <span class="badge badge-purple" title="{_e(t("web_badge_auto_tt"))}">{t("web_autoupdate_badge")}</span>'
                if c["name"] in ask_major:
                    badges += f' <span class="badge badge-blue" title="{_e(t("web_badge_major_tt"))}">⚠</span>'

                # Action buttons — icon-only with tooltips. Container name is
                # escaped for safe use in HTML attributes.
                name_attr = _e(c["name"])
                is_auto = c["name"] in auto_list
                is_askm = c["name"] in ask_major
                is_pinned_c = c["name"] in pinned
                update_btn = (
                    f'<form method="POST" action="/api/update" class="inline-form">'
                    f'<input type="hidden" name="name" value="{name_attr}">'
                    f'<button type="submit" class="btn-icon" title="{_e(t("web_update"))}">🔄</button>'
                    f'</form>'
                ) if c["name"] in pending_names else ''
                pin_form_action = "/api/unpin" if is_pinned_c else "/api/pin"
                pin_btn = (
                    f'<form method="POST" action="{pin_form_action}" class="inline-form">'
                    f'<input type="hidden" name="name" value="{name_attr}">'
                    f'<button type="submit" class="btn-icon{" is-active" if is_pinned_c else ""}" '
                    f'title="{_e(t("web_unpin") if is_pinned_c else t("web_pin"))}">📌</button>'
                    f'</form>'
                )
                auto_btn = (
                    f'<form method="POST" action="/api/autoupdate" class="inline-form">'
                    f'<input type="hidden" name="name" value="{name_attr}">'
                    f'<button type="submit" class="btn-icon{" is-active" if is_auto else ""}" '
                    f'title="{_e(t("web_autoupdate_disable") if is_auto else t("web_autoupdate_enable"))}">⚙</button>'
                    f'</form>'
                )
                ask_btn = (
                    f'<form method="POST" action="/api/ask_major" class="inline-form">'
                    f'<input type="hidden" name="name" value="{name_attr}">'
                    f'<button type="submit" class="btn-icon{" is-warn" if is_askm else ""}" '
                    f'title="{_e(t("web_ask_major_off") if is_askm else t("web_ask_major_on"))}">⚠</button>'
                    f'</form>'
                )
                actions = f'<div class="btn-row">{update_btn}{pin_btn}{auto_btn}{ask_btn}</div>'

                rows += f"""<tr>
<td><input type="checkbox" class="bulk-cb" value="{name_attr}"></td>
<td><a href="/container/{name_attr}" style="color:var(--text);text-decoration:none">{_e(c['name'])}</a>{badges}</td>
<td><code>{_e(c['image'])}</code></td>
<td>{status_badge}</td>
<td>{actions}</td>
</tr>"""

            major_banner = ""
            if major_pending:
                rows_mp = ""
                for n, info in major_pending.items():
                    rows_mp += f"""<tr>
<td>⚠ <code>{_e(n)}</code></td>
<td><code>{_e(info.get('old_version',''))} → {_e(info.get('new_version',''))}</code></td>
<td>
<form method="POST" action="/api/major_confirm" class="inline-form">
<input type="hidden" name="name" value="{_e(n)}">
<input type="hidden" name="action" value="confirm">
<button type="submit" class="btn-sm btn">{t("web_major_confirm")}</button>
</form>
<form method="POST" action="/api/major_confirm" class="inline-form" style="margin-left:6px">
<input type="hidden" name="name" value="{_e(n)}">
<input type="hidden" name="action" value="reject">
<button type="submit" class="btn-sm btn-outline">{t("web_major_reject")}</button>
</form>
</td>
</tr>"""
                major_banner = f"""<div class="card card-warn">
<h2>⚠ {t("web_major_pending_title")}</h2>
<p class="card-intro">{t("web_major_pending_intro")}</p>
<table>{rows_mp}</table>
</div>"""

            content = f"""
{major_banner}
<div class="grid">
<div class="card stat">
    <div class="num">{len(containers)}</div>
    <div class="label">{t("web_containers")}</div>
</div>
<div class="card stat">
    <div class="num">{len(pending)}</div>
    <div class="label">{t("web_updates_available")}</div>
</div>
</div>"""
            content += f"""

<div class="card">
<h2>{t("web_containers")}</h2>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
<span style="font-size:12px;color:#8b949e">{t("web_containers_running", count=len(containers))}</span>
<a href="/api/check" class="btn btn-blue" style="text-decoration:none;font-size:13px">{t("web_check_updates")}</a>
</div>
<form id="bulkForm" method="POST" action="/api/bulk" class="bulk-bar">
<input type="hidden" name="action" id="bulkAction" value="">
<input type="hidden" name="names" id="bulkNames" value="">
<span id="bulkCount" class="bulk-count">{t("web_bulk_none_selected")}</span>
<span class="bulk-divider"></span>
<button type="button" class="btn-sm btn" onclick="bulkSubmit('update')" title="{_e(t('web_bulk_update_tt'))}">🔄 {t("web_bulk_update")}</button>
<button type="button" class="btn-sm btn-outline" onclick="bulkSubmit('pin')" title="{_e(t('web_bulk_pin_tt'))}">📌 {t("web_bulk_pin")}</button>
<button type="button" class="btn-sm btn-outline" onclick="bulkSubmit('unpin')" title="{_e(t('web_bulk_unpin_tt'))}">📌 {t("web_bulk_unpin")}</button>
<button type="button" class="btn-sm btn-outline" onclick="bulkSubmit('autoupdate_on')" title="{_e(t('web_bulk_auto_on_tt'))}">⚙ {t("web_bulk_auto_on")}</button>
<button type="button" class="btn-sm btn-outline" onclick="bulkSubmit('autoupdate_off')" title="{_e(t('web_bulk_auto_off_tt'))}">⚙ {t("web_bulk_auto_off")}</button>
</form>
<table>
<tr><th><input type="checkbox" id="bulkSelectAll" style="width:auto" title="{t("web_bulk_select_all")}"></th><th>{t("web_name")}</th><th>{t("web_image")}</th><th>{t("web_status")}</th><th>{t("web_actions")}</th></tr>
{rows}
</table>
</div>
<script>
(function() {{
    const cbAll = document.getElementById('bulkSelectAll');
    const cbs = document.querySelectorAll('.bulk-cb');
    const countEl = document.getElementById('bulkCount');

    function selected() {{
        return Array.from(cbs).filter(c => c.checked).map(c => c.value);
    }}
    function refresh() {{
        const n = selected().length;
        countEl.textContent = n === 0 ? '{t("web_bulk_none_selected")}'
                                       : n + ' {t("web_bulk_selected_suffix")}';
    }}
    cbAll.addEventListener('change', () => {{
        cbs.forEach(c => c.checked = cbAll.checked);
        refresh();
    }});
    cbs.forEach(c => c.addEventListener('change', refresh));

    window.bulkSubmit = function(action) {{
        const names = selected();
        if (names.length === 0) return;
        document.getElementById('bulkAction').value = action;
        document.getElementById('bulkNames').value = names.join(',');
        document.getElementById('bulkForm').submit();
    }};
    refresh();
}})();
</script>"""

            self._send_html(self._render_page(content, "status"))

        def _page_container(self, name):
            """Stub page for the future per-container detail view (v1.14+).

            For now: shows the container name + which sub-systems will live
            here later (history, logs, per-container settings). Routing is
            wired so links from the Status page already work — we just
            haven't filled in the body yet.
            """
            from i18n import get_translator
            t = get_translator(config.language)
            name = name.strip("/")
            if not name:
                self._send_redirect("/")
                return
            content = f"""
<div class="card">
<h2>{_e(name)}</h2>
<p class="card-intro">{t("web_container_detail_intro")}</p>
<div class="empty">
<div class="empty-icon">🏗️</div>
<div class="empty-title">{t("web_container_detail_soon")}</div>
<div class="empty-hint">{t("web_container_detail_hint")}</div>
</div>
<a href="/" class="btn btn-outline" style="margin-top:8px;display:inline-block">← {t("web_back_to_status")}</a>
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
<div class="empty">
  <div class="empty-icon">📋</div>
  <div class="empty-title">{t("web_history_empty")}</div>
  <div class="empty-hint">{t("web_history_empty_hint")}</div>
</div>
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

            langs = available_languages()
            lang_names = {"en": "English", "de": "Deutsch", "fr": "Français", "es": "Español",
                          "it": "Italiano", "nl": "Nederlands", "pt": "Português", "pl": "Polski",
                          "tr": "Türkçe", "ru": "Русский", "uk": "Українська", "ar": "العربية",
                          "hi": "हिन्दी", "ja": "日本語", "ko": "한국어", "zh": "中文"}
            lang_options = ""
            for l in langs:
                sel = 'selected' if l == config.language else ''
                name = lang_names.get(l, l.upper())
                lang_options += f'<option value="{_e(l)}" {sel}>{_e(name)}</option>\n'

            cb = lambda v: 'checked' if v else ''  # checkbox helper

            # Mask sensitive values
            token_masked = f"{config.bot_token[:4]}...{config.bot_token[-4:]}" if len(config.bot_token) > 8 else "***"
            chat_masked = f"{config.chat_id[:3]}...{config.chat_id[-3:]}" if len(config.chat_id) > 6 else "***"

            telegram_status = 'enabled' if (config.bot_token and config.chat_id) else 'disabled (headless)'

            def help_(text):
                return f'<span class="help" data-tt="{_e(text)}">?</span>'

            content = f"""
<div class="card">
<h2>{t("web_settings")}</h2>
<p class="card-intro">{t("web_settings_intro")}</p>

<form method="POST" action="/settings">
<div class="tabs" data-tabs="settings">
  <button type="button" class="tab-btn" data-tab-target="general">{t("web_tab_general")}</button>
  <button type="button" class="tab-btn" data-tab-target="updates">{t("web_tab_updates")}</button>
  <button type="button" class="tab-btn" data-tab-target="cleanup">{t("web_tab_cleanup")}</button>
  <button type="button" class="tab-btn" data-tab-target="notifs">{t("web_tab_notifications")}</button>
  <button type="button" class="tab-btn" data-tab-target="channels">{t("web_tab_channels")}</button>
</div>

<!-- ── Allgemein ─────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="general">
  <div class="grid">
    <div>
      <label>{t("web_language")}</label>
      <select name="language">{lang_options}</select>
    </div>
    <div>
      <label>{t("web_cron_schedule")} {help_(t("web_cron_help"))}</label>
      <input type="text" name="cron_schedule" value="{_e(config.cron_schedule)}">
    </div>
  </div>
  <label>{t("web_excluded")} {help_(t("web_excluded_help"))}</label>
  <input type="text" name="exclude_containers" value="{_e(', '.join(config.exclude_containers))}" placeholder="container1, container2">
  <div class="form-checkbox-row">
    <input type="checkbox" name="debug" id="cb-debug" {cb(config.debug)}>
    <label for="cb-debug">{t("web_debug_mode")} {help_(t("web_debug_help"))}</label>
  </div>
</div>

<!-- ── Updates ────────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="updates">
  <div class="form-checkbox-row">
    <input type="checkbox" name="auto_selfupdate" id="cb-auto-su" {cb(config.auto_selfupdate)}>
    <label for="cb-auto-su">{t("web_auto_selfupdate")} {help_(t("web_auto_selfupdate_help"))}</label>
  </div>
  <p class="form-help">{t("web_updates_tab_hint")}</p>
</div>

<!-- ── Aufräumen ─────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="cleanup">
  <div class="form-checkbox-row">
    <input type="checkbox" name="auto_cleanup" id="cb-auto-cl" {cb(config.auto_cleanup)}>
    <label for="cb-auto-cl">{t("web_auto_cleanup")}</label>
  </div>
  <p class="form-help">{t("web_auto_cleanup_hint")}</p>

  <div class="grid">
    <div>
      <label>{t("web_cleanup_grace_hours")} {help_(t("web_cleanup_grace_hours_hint"))}</label>
      <input type="number" name="cleanup_grace_hours" value="{_e(config.cleanup_grace_hours)}" min="0" max="8760">
    </div>
    <div>
      <label>{t("web_cleanup_backup_days")} {help_(t("web_cleanup_backup_days_hint"))}</label>
      <input type="number" name="cleanup_backup_days" value="{_e(config.cleanup_backup_days)}" min="1" max="365">
    </div>
  </div>
  <div class="form-checkbox-row">
    <input type="checkbox" name="cleanup_backup_local_only" id="cb-bak-local" {cb(config.cleanup_backup_local_only)}>
    <label for="cb-bak-local">{t("web_cleanup_backup_local_only")}</label>
  </div>
  <p class="form-help">{t("web_cleanup_backup_local_only_hint")}</p>
</div>

<!-- ── Benachrichtigungen ────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="notifs">
  <div class="grid">
    <div>
      <label>{t("web_disk_warn_percent")} {help_(t("web_disk_warn_percent_hint"))}</label>
      <input type="number" name="disk_warn_percent" value="{_e(config.disk_warn_percent)}" min="50" max="100">
    </div>
    <div>
      <div class="form-checkbox-row" style="margin-top:24px">
        <input type="checkbox" name="disk_warn_auto_cleanup" id="cb-disk-acl" {cb(config.disk_warn_auto_cleanup)}>
        <label for="cb-disk-acl">{t("web_disk_warn_auto_cleanup")}</label>
      </div>
      <p class="form-help">{t("web_disk_warn_auto_cleanup_hint")}</p>
    </div>
  </div>

  <hr class="section-divider">

  <div class="grid">
    <div>
      <label>{t("web_quiet_hours_start")}</label>
      <input type="text" name="quiet_hours_start" value="{_e(config.quiet_hours_start)}" placeholder="22:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$|^$">
    </div>
    <div>
      <label>{t("web_quiet_hours_end")}</label>
      <input type="text" name="quiet_hours_end" value="{_e(config.quiet_hours_end)}" placeholder="07:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$|^$">
    </div>
  </div>
  <p class="form-help">{t("web_quiet_hours_hint")}</p>
</div>

<!-- ── Kanäle ────────────────────────────────────── -->
<div class="tab-pane" data-tab-pane="settings" data-tab-name="channels">
  <label>Telegram Topic ID {help_(t("web_topic_id_help"))}</label>
  <input type="text" name="telegram_topic_id" value="{_e(config.telegram_topic_id)}" placeholder="{_e(t('web_topic_id_placeholder'))}">

  <label>Discord Webhook {help_(t("web_discord_help"))}</label>
  <input type="text" name="discord_webhook" value="{_e(config.discord_webhook)}" placeholder="https://discord.com/api/webhooks/...">

  <label>Webhook URL {help_(t("web_webhook_help"))}</label>
  <input type="text" name="webhook_url" value="{_e(config.webhook_url)}" placeholder="https://your-service/webhook">
</div>

<div style="margin-top:16px">
  <button type="submit" class="btn">{t("web_save")}</button>
</div>
</form>
</div>

<div class="card" id="windows">
<h2>{t("web_windows_title")}</h2>
<p class="card-intro">{t("web_windows_intro")}</p>
{self._windows_html(t)}
</div>

<div class="card">
<h2>{t("web_maintenance_title")}</h2>
<p class="card-intro">{t("web_maintenance_intro")}</p>
<form method="POST" action="/api/cleanup" style="display:inline;margin-right:8px"
      data-confirm="{_e(t('web_confirm_cleanup'))}"
      data-confirm-title="{_e(t('web_maintenance_cleanup'))}"
      data-confirm-label="{_e(t('web_confirm_cleanup_btn'))}">
<button type="submit" class="btn btn-blue">🧹 {t("web_maintenance_cleanup")}</button>
</form>
<form method="POST" action="/api/selfupdate" style="display:inline"
      data-confirm="{_e(t('web_confirm_selfupdate'))}"
      data-confirm-title="{_e(t('web_maintenance_selfupdate'))}"
      data-confirm-label="{_e(t('web_confirm_selfupdate_btn'))}"
      data-confirm-danger="1">
<button type="submit" class="btn">⬆️ {t("web_maintenance_selfupdate")}</button>
</form>
<p class="form-help" style="margin-top:12px">
{t("web_maintenance_explain", grace=_e(config.cleanup_grace_hours), days=_e(config.cleanup_backup_days), dir=_e(config.cleanup_backup_dir))}
</p>
</div>

<div class="card">
<h2>Info</h2>
<table>
<tr><td>Version</td><td><code>v{_e(VERSION)}</code></td></tr>
<tr><td>Telegram</td><td><code>{telegram_status}</code></td></tr>
<tr><td>Bot Token</td><td><code>{_e(token_masked)}</code></td></tr>
<tr><td>Chat ID</td><td><code>{_e(chat_masked)}</code></td></tr>
<tr><td>Data Dir</td><td><code>{_e(config.data_dir)}</code></td></tr>
</table>
<p class="form-help" style="margin-top:8px">{t("web_info_credentials_hint")}</p>
</div>"""

            self._send_html(self._render_page(content, "settings"))

        def _windows_html(self, t):
            """Render the Update Windows table + add-form for the Settings page."""
            try:
                containers = self._get_containers()
            except Exception:
                containers = []
            container_names = sorted({c["name"] for c in containers})
            current = store.get_update_windows() or {}

            wd_short = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
            rows_html = ""
            for name in sorted(current.keys()):
                w = current[name]
                days_set = set(w.get("weekdays") or [])
                days = "·".join(wd_short[i] if i in days_set else " " for i in range(7))
                rows_html += f"""<tr>
<td><code>{_e(name)}</code></td>
<td><code>{_e(w.get('start',''))}–{_e(w.get('end',''))}</code></td>
<td><code>{_e(days if days_set else 'all days')}</code></td>
<td>
<form method="POST" action="/api/window" style="display:inline">
<input type="hidden" name="name" value="{_e(name)}">
<input type="hidden" name="action" value="delete">
<button type="submit" class="btn-sm btn-outline">{t("web_delete")}</button>
</form>
</td>
</tr>"""
            if not rows_html:
                rows_html = (f"<tr><td colspan=\"4\" style=\"color:#8b949e;font-size:12px\">"
                             f"{t('web_windows_empty')}</td></tr>")

            options = "".join(f'<option value="{_e(n)}">{_e(n)}</option>'
                              for n in container_names)
            wd_full = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            wd_html = ""
            for i, label in enumerate(wd_full):
                wd_html += (f'<label style="display:inline-block;margin-right:10px;font-size:13px">'
                            f'<input type="checkbox" name="weekdays" value="{i}" '
                            f'style="width:auto;margin-right:4px">{label}</label>')

            return f"""<table style="margin-bottom:14px">
<tr><th>{t("web_name")}</th><th>{t("web_windows_range")}</th><th>{t("web_windows_days")}</th><th>{t("web_actions")}</th></tr>
{rows_html}
</table>
<form method="POST" action="/api/window">
<input type="hidden" name="action" value="save">
<div class="grid">
<div>
<label>{t("web_windows_container")}</label>
<select name="name">{options}</select>
</div>
<div>
<label>{t("web_windows_range")}</label>
<div style="display:flex;gap:8px">
<input type="text" name="start" placeholder="02:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$" required>
<input type="text" name="end" placeholder="04:00" pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$" required>
</div>
</div>
</div>
<div style="margin-top:8px">
<label>{t("web_windows_days")}</label>
{wd_html}
<p style="font-size:11px;color:#484f58;margin:4px 0 0 0">{t("web_windows_days_hint")}</p>
</div>
<button type="submit" class="btn" style="margin-top:8px">{t("web_windows_save")}</button>
</form>"""

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

        def _api_cleanup(self):
            """Run `docker image prune` to free disk space (manual trigger)."""
            try:
                ok, msg = checker.cleanup_images()
                if bot.enabled:
                    bot.send_message(f"{'✅' if ok else '❌'} {msg}")
                if bot.notifier and bot.notifier.has_channels():
                    bot.notifier.send_message(f"🧹 Cleanup: {msg}")
                print(f"Cleanup: {msg}")
            except Exception as e:
                print(f"Web UI cleanup error: {e}")

        def _api_selfupdate(self):
            """Trigger a self-update of the Docksentry container."""
            try:
                # The TelegramBot class owns the selfupdate logic regardless
                # of whether Telegram itself is configured — when disabled,
                # internal send_message() calls are no-ops and the Discord/
                # webhook channels (via notifier) carry the status messages.
                if bot.enabled:
                    bot._handle_selfupdate()
                else:
                    # Headless variant — reuse the auto-selfupdate path which
                    # already runs without sending Telegram messages.
                    bot.check_selfupdate_auto()
            except Exception as e:
                print(f"Web UI selfupdate error: {e}")
                if bot.notifier and bot.notifier.has_channels():
                    bot.notifier.send_message(f"❌ Selfupdate failed: {e}")

        def _api_bulk(self, action, names):
            """Apply a bulk action to a list of containers.

            Supported actions: pin, unpin, autoupdate_on, autoupdate_off,
            update. Update walks through the pending-updates list and runs
            each matching update sequentially.
            """
            try:
                if action == "pin":
                    for n in names:
                        store.pin(n)
                elif action == "unpin":
                    for n in names:
                        store.unpin(n)
                elif action == "autoupdate_on":
                    auto = store.get_autoupdate()
                    for n in names:
                        if n not in auto:
                            auto.append(n)
                    store.save_autoupdate(auto)
                elif action == "autoupdate_off":
                    auto = store.get_autoupdate()
                    auto = [a for a in auto if a not in names]
                    store.save_autoupdate(auto)
                elif action == "update":
                    if not os.path.exists(config.pending_file):
                        return
                    with open(config.pending_file) as f:
                        updates = json.load(f)
                    targets = [u for u in updates if u["name"] in names]
                    for target in targets:
                        compose_kwargs = {k: target[k] for k in target if k.startswith("compose_")}
                        success, msg = checker.update_container(
                            target["name"], target["image"], **compose_kwargs
                        )
                        status = "✅" if success else "❌"
                        if bot.enabled:
                            bot.send_message(f"{status} `{target['name']}`: {msg}")
                        if bot.notifier:
                            bot.notifier.send_update_result(
                                target["name"], target["image"], success, msg
                            )
                    # Drop processed entries from pending
                    remaining = [u for u in updates if u["name"] not in [t["name"] for t in targets]]
                    with open(config.pending_file, "w") as f:
                        json.dump(remaining, f)
                else:
                    print(f"Web UI bulk: unknown action {action!r}")
            except Exception as e:
                print(f"Web UI bulk error: {e}")

    return WebHandler


class WebUI:
    def __init__(self, config, checker, bot, store, port=8080, password=""):
        self.config = config
        self.port = port
        self.handler = create_handler(config, checker, bot, store, password or None)
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
