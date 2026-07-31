#!/usr/bin/env python3
"""BaseNotifier interface + shared HTTP/formatting helpers.

Every notification channel (Discord, generic webhook, e-mail/SMTP, ntfy, …)
is a small self-contained subclass of :class:`BaseNotifier` living in its own
file under ``app/notifiers/``. The ``Notifier`` facade in ``app/notifier.py``
just iterates over the configured plugins — so a *new* channel is one new file
here, no edits to the dispatch code. That's v2 goal 1 ("neuer Kanal = eine
Datei").

The two helpers that used to sit on ``Notifier`` and are shared across
channels — the bounded POST-with-retry and the cross-channel version badge —
live here so the plugins stay tiny and behave identically.
"""

import json
import socket
import time
import urllib.error
import urllib.request


def post_json_with_retry(url, payload, headers, channel):
    """POST a JSON body with bounded retry for transient network failures
    (timeout / connection error) — 3 attempts, 2s and 4s backoff. Same
    rationale as the Telegram retry in v1.38.1: right after a self-update
    restart the network can still be settling, and a single dropped
    notification is worse than a rare duplicate. HTTP status codes (2xx /
    4xx) return on the first attempt — retry only covers real network
    errors, not user config problems.

    `channel` is a label used only in the error log ("Discord webhook",
    "Webhook") so failures stay distinguishable.
    """
    data = json.dumps(payload).encode()
    merged = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=merged, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            # Server responded (4xx / 5xx) — not a transient network blip,
            # don't retry. The retry loop is meant for the case where the
            # request never reached the server.
            print(f"{channel} error: HTTP {e.code}")
            return None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            print(f"{channel} error: {e}")
            return None
        except Exception as e:
            print(f"{channel} error: {e}")
            return None
    return None


def version_str(u):
    """`v_old → v_new` when both are known and differ, else the single
    known version, else "". Mirrors the Telegram badge (#44) so Discord /
    webhook / e-mail show the same version info."""
    old = (u.get("old_version") or "").strip()
    new = (u.get("new_version") or "").strip()
    if old and new and old != new:
        return f"v{old} → v{new}"
    v = old or new
    return f"v{v}" if v else ""


class BaseNotifier:
    """Interface every channel implements.

    A channel is constructed once with the app ``config``; the facade then
    asks :meth:`configured` whether it's active and, if so, forwards the same
    payloads the public API has always carried. Failures must stay contained
    — a channel never raises into the facade (the facade also guards each call
    best-effort, but channels should log-and-return on their own errors, as
    the network/SMTP helpers already do).
    """

    #: Short, stable channel name — used in log labels and the registry.
    name = "base"

    #: Dispatch order within the facade. Lower runs first. Keeps the existing
    #: Discord → webhook → SMTP ordering stable; new channels append after.
    order = 100

    def __init__(self, config):
        self.config = config

    # ── channel activation ───────────────────────────────────────────
    def configured(self):
        """True when this channel has everything it needs to send."""
        raise NotImplementedError

    # ── payloads (same shapes the facade has always dispatched) ──────
    def send_updates_available(self, updates):
        """Notify about a list of available updates."""

    def send_update_result(self, name, image, success, detail="", source_url=""):
        """Notify about one completed update (success or failure)."""

    def send_message(self, text):
        """Send a plain-text notification."""

    # ── shared helpers (so subclasses read as one line) ──────────────
    @staticmethod
    def version_str(u):
        return version_str(u)

    def _bot_label(self):
        return (getattr(self.config, "bot_label", "") or "").strip()
