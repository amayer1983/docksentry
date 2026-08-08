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
import os
import socket
import time
import urllib.error
import urllib.request



def _retry_after(err):
    """Seconds to wait from a 429, or None if the server did not say.

    Both spellings: the `Retry-After` header (seconds, or an HTTP date we
    do not attempt to parse) and Discord's JSON body, which carries a float
    and is the more precise of the two.
    """
    try:
        body = err.read()
        if body:
            import json as _json
            data = _json.loads(body)
            if isinstance(data, dict) and "retry_after" in data:
                return float(data["retry_after"])
    except Exception:
        pass
    try:
        raw = err.headers.get("Retry-After")
        return float(raw) if raw is not None else None
    except (AttributeError, TypeError, ValueError):
        return None

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
            # 429 is the one status that IS transient, and it says so: the
            # server sends `Retry-After` precisely so a client can wait.
            # Treating it as terminal — which every channel did — dropped
            # the notification outright at the moment the service was
            # busiest, which is when a "3 containers failed to update"
            # message matters most (dc#255, dc#185).
            if e.code == 429 and attempt < 2:
                wait = _retry_after(e)
                if wait is not None and wait <= 60:
                    print(f"{channel}: rate limited, waiting {wait:.1f}s")
                    time.sleep(wait)
                    continue
            # Everything else: the server answered and said no. Retrying an
            # actual 400 or 403 just repeats the same rejection.
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


def channel_setting(config, attr, env_var, default=""):
    """A channel setting: the saved value, else the environment.

    The four plugin channels (ntfy, Matrix, Apprise, Gotify) read
    `os.environ` directly until v2.4.0 — deliberately, so that a whole
    channel stayed one file, with the note that the reader could move
    "when the Web UI gains an ntfy field later". This is later: they have
    fields on the Connections page now.

    `config` wins because that is where both values meet once the
    attribute exists — `Config.from_env` seeds it from the variable and
    `_load_persistent` lays the saved value over it, the same precedence
    every other setting has had for years. The environment is consulted
    only when the attribute is absent altogether: a bare namespace in a
    test, or a caller older than this change. So nothing that worked
    before stops working, and `None` is the signal for "nobody has heard
    of this setting" rather than "it is empty".
    """
    value = getattr(config, attr, None)
    if value is None:
        value = os.environ.get(env_var, default)
    return value


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

    def setting(self, attr, env_var, default=""):
        """This channel's `channel_setting`, for readability in methods."""
        return channel_setting(self.config, attr, env_var, default)

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
