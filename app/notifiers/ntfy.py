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
For a protected topic, set ``NTFY_TOKEN`` (an ntfy access token) or
``NTFY_USER`` + ``NTFY_PASSWORD``.
"""

import os
import urllib.error
import urllib.request

import notify_retry

from .base import BaseNotifier, channel_setting


def _header_value(text):
    """An HTTP header value that survives a non-ASCII title.

    Python encodes header values as latin-1, so a single emoji raises
    `UnicodeEncodeError` inside `urlopen` — which the broad handler below
    swallows, dropping the notification with one log line. The README
    itself recommends `BOT_LABEL=🖥 pve1`, so anyone following the
    documentation and using ntfy got no notifications at all and no
    explanation. (Found by sweeping dockcheck's issue history; dc#120.)

    RFC 2047 is the standard encoding for non-ASCII in a header, and ntfy
    decodes it — verified against a real ntfy server, which returned the
    title as `🖥 pve1`. Only pure ASCII is left alone, so the common case
    stays readable in logs and to any proxy in between.

    The test for "needs encoding" is ASCII, deliberately, and NOT "does
    latin-1 accept it". An umlaut passes the latin-1 test — Python encodes
    `ü` to 0xFC quite happily — but ntfy reads the header as UTF-8, where
    0xFC alone is invalid. Measured against a live server: `Grün Größe`
    sent raw arrived as `Gr<?>n Gr<?>e`. So a German title would have been
    quietly mangled rather than dropped, which is the harder bug to notice
    of the two.
    """
    text = text or ""
    if text.isascii():
        return text
    import base64
    return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode() + "?="


def _auth_header(cfg):
    """`Authorization` for a protected ntfy topic, or "" when unset.

    A self-hosted ntfy with `auth-default-access: deny`, or a reserved
    topic on ntfy.sh, needs credentials — and Docksentry had no way to send
    any, so every push 401'd with a single log line and the user had no
    setting to reach for. Reserved topics are the normal way to keep a
    topic name from being guessable, so this is not an exotic setup.

    `NTFY_TOKEN` (an ntfy access token, `tk_...`) is preferred and becomes
    a Bearer header. `NTFY_USER` + `NTFY_PASSWORD` fall back to Basic,
    which ntfy also accepts.
    """
    token = (channel_setting(cfg, "ntfy_token", "NTFY_TOKEN") or "").strip()
    if token:
        return f"Bearer {token}"
    user = (channel_setting(cfg, "ntfy_user", "NTFY_USER") or "").strip()
    pw = channel_setting(cfg, "ntfy_password", "NTFY_PASSWORD") or ""
    if user:
        import base64
        raw = f"{user}:{pw}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")
    return ""


def _topic_url(cfg):
    """Resolve the topic URL from the environment, or "" when unset.

    ``NTFY_URL`` wins; otherwise ``NTFY_SERVER`` + ``NTFY_TOPIC`` are joined.
    Read live (not cached) so the channel reflects env changes and stays
    trivially testable."""
    url = (channel_setting(cfg, "ntfy_url", "NTFY_URL") or "").strip()
    if url:
        return url
    server = (channel_setting(cfg, "ntfy_server", "NTFY_SERVER") or "").strip().rstrip("/")
    topic = (channel_setting(cfg, "ntfy_topic", "NTFY_TOPIC") or "").strip().strip("/")
    if server and topic:
        return f"{server}/{topic}"
    return ""


class NtfyNotifier(BaseNotifier):
    name = "ntfy"
    order = 40

    OWNS = ("ntfy_url", "ntfy_server", "ntfy_topic", "ntfy_token",
            "ntfy_user", "ntfy_password")

    def missing(self):
        """ntfy takes a topic URL *or* a server plus a topic, so its
        requirement is not a flat list and the default cannot express it.
        Naming both halves would be misleading — filling in either one is
        enough — so it names the pair the user has not started."""
        if self.configured():
            return []
        if (self.setting("ntfy_server", "NTFY_SERVER") or "").strip():
            return ["web_ntfy_topic"]
        if (self.setting("ntfy_topic", "NTFY_TOPIC") or "").strip():
            return ["web_ntfy_server"]
        return ["web_ntfy_url"]

    def configured(self):
        return bool(_topic_url(self.config))

    # ── transport ────────────────────────────────────────────────────
    def _post(self, title, body, priority="default", on_network_failure=None):
        """POST one message to the ntfy topic. Best-effort: logs and returns
        on any failure, never raising into the caller — same contract as the
        other channels.

        `on_network_failure` is called, with no arguments, only when the
        send died on the network — and never for an HTTP status. That is
        the one distinction the retry queue needs: a 400 or a 401 comes
        back identical fifteen minutes later, an unreachable server may
        not (#66)."""
        url = _topic_url(self.config)
        if not url:
            return None
        label = self._bot_label()
        if label:
            title = f"[{label}] {title}"
        headers = {
            "Title": _header_value(title),
            "Priority": priority,
            "User-Agent": "Docksentry/1.0",
        }
        auth = _auth_header(self.config)
        if auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(
            url, data=(body or "").encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            # First, and it has to stay first: HTTPError is a subclass of
            # URLError, so the network clause below would otherwise
            # swallow every 4xx and have the queue repeat it for fifteen
            # minutes.
            print(f"ntfy error: HTTP {e.code}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # The same four errors the other self-hosted channels catch,
            # spelled the same way: `socket.timeout` has been an alias of
            # `TimeoutError` since 3.10, and `URLError` is itself an
            # `OSError`, so this tuple is the whole of "the network".
            print(f"ntfy error: {e}")
            if on_network_failure is not None:
                on_network_failure()
            return None
        except Exception as e:
            # Everything else is ours, not the network's — a title that
            # will not encode, say. Repeating it changes nothing.
            print(f"ntfy error: {e}")
            return None

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
        import notify_text
        title, body = notify_text.updates_available(
            updates, lang=notify_text.lang_of(self),
            version_of=self.version_str, bullet="-")
        self._post(title, body)

    def send_update_result(self, name, image, success, detail="", source_url=""):
        import notify_text
        title, body = notify_text.update_result(
            name, image, success, detail, source_url,
            lang=notify_text.lang_of(self))
        self._post(title, body,
                   priority="default" if success else "high")

    def _post_text(self, text):
        """Push `text`, False ONLY when the network was the reason it did
        not arrive.

        The queue's resend entry point — see `DiscordNotifier._post_text`
        for why it is this and not `send_message`.
        """
        # Strip Telegram *bold* markers for a clean plain-text push.
        reached = [True]
        self._post("Docksentry", text.replace("*", ""),
                   on_network_failure=lambda: reached.__setitem__(0, False))
        return reached[0]

    def send_message(self, text):
        if not self._post_text(text):
            print(f"{self.name} send failed: no answer — holding the message "
                  f"for redelivery")
            notify_retry.remember(self.name, text, self._post_text)
