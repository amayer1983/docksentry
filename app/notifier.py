#!/usr/bin/env python3
"""Multi-channel notification dispatcher (Discord, Webhook, e-mail/SMTP, ntfy).

Thin facade over the channel plugins in ``app/notifiers/``. The dispatch code
no longer knows anything about individual channels: it asks the registry for
the configured plugins and forwards each payload best-effort. Adding a channel
is one new file under ``app/notifiers/`` — no edits here.

``import time`` / ``import urllib.request`` are kept at module scope so the
notifier retry test can patch ``notifier.time.sleep`` /
``notifier.urllib.request.urlopen`` (they mutate the shared module objects the
plugins' transport helper uses).
"""

import time  # noqa: F401  (kept for test monkeypatch surface)
import urllib.request  # noqa: F401  (kept for test monkeypatch surface)

from quiet_hours import is_quiet_now
from notifiers import build_all, version_str


class Notifier:
    """Dispatches notifications to every configured channel plugin."""

    def __init__(self, config):
        self.config = config
        # Instantiate all registered channels once; filter by `configured()`
        # at dispatch time so a live config change (Web UI) is picked up.
        self._plugins = build_all(config)
        self._by_name = {p.name: p for p in self._plugins}

    def _configured_plugins(self):
        return [p for p in self._plugins if p.configured()]

    def has_channels(self):
        """Check if any notification channels are configured."""
        return any(p.configured() for p in self._plugins)

    def _smtp_configured(self):
        """E-mail is active once host + from + to are all set (#2)."""
        p = self._by_name.get("smtp")
        return bool(p and p.configured())

    def _suppressed(self):
        """True if quiet-hours OR maintenance is active right now — skip
        auto-notifications. Manual sends still go through (the caller would
        use a different code path for those)."""
        if is_quiet_now(self.config):
            return True
        try:
            from maintenance import is_active as _maint_active
            if _maint_active(self.config):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _version_str(u):
        """`v_old → v_new` when both are known and differ, else the single
        known version, else "". Mirrors the Telegram badge (#44) so Discord /
        webhook / e-mail show the same version info."""
        return version_str(u)

    def _dispatch(self, method, *args):
        """Call `method` on every configured plugin, best-effort: a channel
        that raises must not stop the others (each existing channel is already
        internally best-effort; this also contains any new/buggy channel)."""
        for p in self._configured_plugins():
            try:
                getattr(p, method)(*args)
            except Exception as e:
                print(f"{p.name} error: {e}")

    def send_updates_available(self, updates):
        """Notify about available updates."""
        if self._suppressed():
            return
        self._dispatch("send_updates_available", updates)

    def send_update_result(self, name, image, success, detail="", source_url=""):
        """Notify about a completed update (success or failure).

        ``source_url`` is the resolved repo / changelog link for the
        container (manual override → OCI label → registry fallback,
        resolved once by the caller via
        ``TelegramBot._enrich_with_source_url``). When present, the
        Discord embed wraps the container name as a clickable link
        and the generic webhook payload carries the URL alongside
        the other fields. Closes a parity gap reported by @NotRetarded
        in #2 — v1.19.2 fixed the Telegram side, this fixes Discord
        and the webhook payload.
        """
        if self._suppressed():
            return
        self._dispatch("send_update_result", name, image, success, detail, source_url)

    def send_message(self, text):
        """Send a plain text notification (subject to quiet hours)."""
        if self._suppressed():
            return
        self._dispatch("send_message", text)

    # ── Backwards-compat delegators ──────────────────────────────────
    # weekly_report.py (not part of this change) and the notifier tests
    # reach into these internal entry points directly. They forward to the
    # matching plugin so the wire behaviour and return values are unchanged.

    def _discord_post(self, payload):
        """POST JSON to Discord webhook (returns HTTP status or None)."""
        return self._by_name["discord"].post(payload)

    def _webhook_send(self, event, data):
        """POST JSON to the generic webhook (returns HTTP status or None)."""
        return self._by_name["webhook"].send_raw(event, data)

    def _smtp_send(self, subject, body):
        """Send a plain-text e-mail via SMTP (best-effort)."""
        return self._by_name["smtp"].send_raw(subject, body)
