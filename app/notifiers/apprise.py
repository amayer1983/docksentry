#!/usr/bin/env python3
"""Apprise channel — one integration, a hundred destinations.

`Apprise <https://github.com/caronc/apprise>`_ is a notification fan-out:
you run its small API container, tell *it* where your notifications should
go (Pushover, Signal, Rocket.Chat, Mattermost, Slack, SMS gateways, …),
and everything else just POSTs to it.

That makes it worth more than any single channel we could add. Adding
Pushover here buys Docksentry users Pushover; adding Apprise buys them
everything Apprise supports, and every service it gains later, without
another line here. It also costs nothing in dependencies: Apprise's own
Python library is irrelevant to us — we talk to its HTTP endpoint with
``urllib``, exactly like the other channels.

Config (env-only, like ntfy — a URL is deployment detail, not a setting
worth persisting to the data volume):

``APPRISE_URL``
    The full notify endpoint. Two shapes work, and which you use is an
    Apprise-side decision:

    * *stateful* — ``http://apprise:8000/notify/docksentry``, where the
      destination URLs live in Apprise's own config under the key
      ``docksentry``. Nothing sensitive reaches Docksentry.
    * *stateless* — ``http://apprise:8000/notify`` combined with
      ``APPRISE_URLS``, where the destinations are passed per request.

``APPRISE_URLS``
    Optional, comma-separated Apprise URLs (``pover://…``, ``mailto://…``).
    Only used by the stateless endpoint. Treat as a secret: these embed
    tokens.

``APPRISE_TAG``
    Optional tag filter for the stateful endpoint, so one Apprise config
    can route Docksentry elsewhere than its other senders.
"""

import json
import os
import urllib.error
import urllib.request

from .base import BaseNotifier, channel_setting

#: Apprise's own severities. Mapping onto them (rather than sending
#: everything as "info") is what makes a failed update stand out on
#: destinations that colour or prioritise by type.
TYPE_INFO = "info"
TYPE_SUCCESS = "success"
TYPE_WARNING = "warning"
TYPE_FAILURE = "failure"


def _endpoint(cfg):
    return (channel_setting(cfg, "apprise_url", "APPRISE_URL") or "").strip()


def _targets(cfg):
    return (channel_setting(cfg, "apprise_urls", "APPRISE_URLS") or "").strip()


def _tag(cfg):
    return (channel_setting(cfg, "apprise_tag", "APPRISE_TAG") or "").strip()


class AppriseNotifier(BaseNotifier):
    name = "apprise"
    order = 50

    def configured(self):
        return bool(_endpoint(self.config))

    # ── transport ────────────────────────────────────────────────────
    def _post(self, title, body, kind=TYPE_INFO):
        """POST one notification. Best-effort: logs and returns on any
        failure rather than raising into the facade, same as every other
        channel — a broken notification must never take an update with it.
        """
        url = _endpoint(self.config)
        if not url:
            return None
        payload = {"title": title, "body": body, "type": kind, "format": "text"}
        targets = _targets(self.config)
        if targets:
            # The stateless endpoint requires the destinations inline.
            payload["urls"] = targets
        tag = _tag(self.config)
        if tag:
            payload["tag"] = tag
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status
        except urllib.error.HTTPError as e:
            # Deliberately NOT the response body. The request that
            # produced this error carried APPRISE_URLS, and Apprise echoes
            # the URLs it could not parse straight back in its error
            # message — those embed tokens (`pover://user@token`,
            # `mailto://user:password@host`). Printing them puts a
            # credential in `docker logs` and in every log aggregator
            # downstream of it. The status code says what went wrong;
            # Apprise's own log says the rest, on the box that owns them.
            hint = " — check APPRISE_URL and APPRISE_URLS" if e.code < 500 else ""
            print(f"Apprise notification failed: HTTP {e.code}{hint} "
                  "(response body withheld: it can echo APPRISE_URLS)")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"Apprise notification failed: {e}")
        return None

    def _title(self, text):
        """Prefix with BOT_LABEL when set, so several Docksentry instances
        sharing one Apprise config stay distinguishable."""
        label = self._bot_label()
        return f"{label} · {text}" if label else text

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
        if not updates:
            return
        lines = []
        for u in updates:
            host = u.get("host") if isinstance(u, dict) else None
            where = f" @{host}" if host and host != "local" else ""
            lines.append(f"• {u['name']}{where} ({u['image']})"
                         f"{self.version_str(u)}")
        self._post(self._title(f"{len(updates)} update(s) available"),
                   "\n".join(lines), TYPE_INFO)

    def send_update_result(self, name, image, success, detail="", source_url=""):
        status = "updated" if success else "FAILED"
        body = f"{name} ({image})"
        if detail:
            body += f"\n{detail}"
        if source_url:
            body += f"\n{source_url}"
        self._post(self._title(f"{name} {status}"), body,
                   TYPE_SUCCESS if success else TYPE_FAILURE)

    def send_message(self, text):
        self._post(self._title("Docksentry"), text, TYPE_INFO)
