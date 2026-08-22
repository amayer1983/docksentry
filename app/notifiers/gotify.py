#!/usr/bin/env python3
"""Gotify channel — self-hosted push, one container on your own box.

`Gotify <https://gotify.net>`_ is a small self-hosted push server with
its own Android app: you run it, create an *application* in its UI, and
it hands you a token. Anything that POSTs to ``/message`` with that token
shows up as a push on every device logged into that Gotify.

It earns its place next to ntfy because it fills the same niche from the
other end: ntfy is trivially usable as a hosted service, Gotify is
account-based and firmly self-hosted. Homelab users tend to have strong
opinions about which they want, and supporting both is one file each —
which was the point of the plugin split.

Config (env-only; a token is a credential and has no business on the
data volume):

``GOTIFY_URL``
    Base URL of the server, e.g. ``https://gotify.example.com``. A full
    ``/message`` URL is accepted too and normalised.

``GOTIFY_TOKEN``
    The *application* token from Gotify's UI — not a client token. They
    look alike and only the application one may post; getting it wrong
    gives a 401 that says nothing useful, so the error is spelled out.
"""

import json
import os
import urllib.error
import urllib.request

from .base import BaseNotifier, channel_setting

#: Gotify priorities. Its Android client treats ≥8 as loud (bypasses
#: quiet settings), which is right for a failed update and wrong for
#: "here are some updates".
PRIO_NORMAL = 5
PRIO_HIGH = 8


def _message_url(cfg):
    """Normalise whatever the user put in ``GOTIFY_URL`` to the message
    endpoint. Accepting both the base URL and the full endpoint is not
    politeness — both appear in Gotify's own docs and in every tutorial,
    so both will be pasted in."""
    url = (channel_setting(cfg, "gotify_url", "GOTIFY_URL") or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/message"):
        return url
    return f"{url}/message"


def _token(cfg):
    return (channel_setting(cfg, "gotify_token", "GOTIFY_TOKEN") or "").strip()


class GotifyNotifier(BaseNotifier):
    name = "gotify"
    order = 45

    OWNS = ("gotify_url", "gotify_token")
    REQUIRES = (("gotify_url", "web_gotify_url"),
                ("gotify_token", "web_gotify_token"))

    def configured(self):
        return bool(_message_url(self.config) and _token(self.config))

    # ── transport ────────────────────────────────────────────────────
    def _post(self, title, message, priority=PRIO_NORMAL):
        """POST one message. Best-effort: logs and returns on failure,
        never raises into the facade."""
        url = _message_url(self.config)
        token = _token(self.config)
        if not (url and token):
            return None
        payload = {"title": title, "message": message, "priority": priority}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     # Header rather than the `?token=` query parameter:
                     # the query form ends up in the server's access log.
                     "X-Gotify-Key": token})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print("Gotify rejected the token — GOTIFY_TOKEN must be an "
                      "APPLICATION token (Apps tab), not a client token")
            else:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                print(f"Gotify notification failed: HTTP {e.code} {body}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"Gotify notification failed: {e}")
        return None

    def _title(self, text):
        label = self._bot_label()
        return f"{label} · {text}" if label else text

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
        if not updates:
            return
        import notify_text
        title, body = notify_text.updates_available(
            updates, lang=notify_text.lang_of(self),
            version_of=self.version_str)
        self._post(self._title(title), body, PRIO_NORMAL)

    def send_update_result(self, name, image, success, detail="", source_url=""):
        import notify_text
        title, body = notify_text.update_result(
            name, image, success, detail, source_url,
            lang=notify_text.lang_of(self))
        # A failure is the one thing worth waking someone for.
        self._post(self._title(title), body,
                   PRIO_NORMAL if success else PRIO_HIGH)

    def send_message(self, text):
        self._post(self._title("Docksentry"), text, PRIO_NORMAL)
