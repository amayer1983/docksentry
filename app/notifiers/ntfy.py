#!/usr/bin/env python3
"""ntfy channel — the plugin-split proof: a whole new channel in one file.

ntfy (https://ntfy.sh) is a dead-simple pub/sub: an HTTP POST to a topic URL,
title via the ``Title`` header, priority via the ``Priority`` header, the body
is the message text. Pure stdlib ``urllib``.

Config is read straight from the environment here — deliberately *not* wired
through ``config.py`` or the Web UI in this step:

* Web UI is off-limits in this wave (owned by a parallel change) and the task
  scopes ntfy as env-only for now.
* Keeping the topic config local means the entire channel — activation,
  config, transport, payloads — is this one file, which is exactly the v2
  goal ("neuer Kanal = eine Datei"). ``config.py`` stays untouched.
  When the Web UI gains an ntfy field later, this reader can move to
  ``config`` without touching the dispatch code.

Set either ``NTFY_URL`` (a full topic URL, e.g. ``https://ntfy.sh/my-topic``)
or ``NTFY_SERVER`` + ``NTFY_TOPIC`` (e.g. ``https://ntfy.sh`` + ``my-topic``).
"""

import os
import urllib.error
import urllib.request

from .base import BaseNotifier


def _topic_url():
    """Resolve the topic URL from the environment, or "" when unset.

    ``NTFY_URL`` wins; otherwise ``NTFY_SERVER`` + ``NTFY_TOPIC`` are joined.
    Read live (not cached) so the channel reflects env changes and stays
    trivially testable."""
    url = (os.environ.get("NTFY_URL") or "").strip()
    if url:
        return url
    server = (os.environ.get("NTFY_SERVER") or "").strip().rstrip("/")
    topic = (os.environ.get("NTFY_TOPIC") or "").strip().strip("/")
    if server and topic:
        return f"{server}/{topic}"
    return ""


class NtfyNotifier(BaseNotifier):
    name = "ntfy"
    order = 40

    def configured(self):
        return bool(_topic_url())

    # ── transport ────────────────────────────────────────────────────
    def _post(self, title, body, priority="default"):
        """POST one message to the ntfy topic. Best-effort: logs and returns
        on any failure, never raising into the caller — same contract as the
        other channels."""
        url = _topic_url()
        if not url:
            return None
        label = self._bot_label()
        if label:
            title = f"[{label}] {title}"
        headers = {
            "Title": title,
            "Priority": priority,
            "User-Agent": "Docksentry/1.0",
        }
        req = urllib.request.Request(
            url, data=(body or "").encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            print(f"ntfy error: HTTP {e.code}")
            return None
        except Exception as e:
            print(f"ntfy error: {e}")
            return None

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
        lines = [f"- {u['name']} ({u['image']})"
                 + (f" {self.version_str(u)}" if self.version_str(u) else "")
                 + f" — {u.get('size','?')}, {u.get('created','?')}"
                 for u in updates]
        self._post(f"{len(updates)} Docker update(s) available",
                   "Docksentry found updates for:\n\n" + "\n".join(lines))

    def send_update_result(self, name, image, success, detail="", source_url=""):
        status = "OK" if success else "FAILED"
        body = f"{name} ({image})\n\n{detail}"
        if source_url:
            body += f"\n\n{source_url}"
        self._post(f"Update {status}: {name}", body,
                   priority="default" if success else "high")

    def send_message(self, text):
        # Strip Telegram *bold* markers for a clean plain-text push.
        self._post("Docksentry", text.replace("*", ""))
