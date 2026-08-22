#!/usr/bin/env python3
"""Matrix channel — notifications into a self-hosted, federated room.

Matrix is the natural fit for the audience that self-hosts Docksentry in
the first place: it's open, you can run the homeserver yourself
(Synapse, Conduit, Dendrite), and it federates, so an alert room can
include people who aren't on your server.

Sending is one authenticated PUT per message. Everything here is
``urllib``; no Matrix SDK, no dependency.

Config (env-only — an access token is a credential):

``MATRIX_HOMESERVER``
    Base URL, e.g. ``https://matrix.example.com``. Not the server *name*
    (``example.com``) — those differ whenever delegation is in play, and
    mixing them up is the single most common Matrix setup mistake.

``MATRIX_TOKEN``
    An access token for the sending account. Create a dedicated user for
    this; a token is equivalent to that account's full access.

``MATRIX_ROOM``
    The internal room ID (``!abc123:example.com``), not a human alias
    (``#alerts:example.com``). An alias is resolved once at send time if
    given, but the ID is stable and cheaper.
"""

import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from container_store import is_safe_link

from .base import BaseNotifier, channel_setting


def _esc(value):
    """Anything user- or registry-controlled that ends up inside
    `formatted_body`.

    Matrix's HTML variant is real HTML, and everything interesting here
    comes from outside: container names, image references, and a `detail`
    that is often a raw docker error. One `<` in any of them mangles the
    markup for every client in the room, and the failure mode of the
    others is worse than cosmetic.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _homeserver(cfg):
    return (channel_setting(cfg, "matrix_homeserver", "MATRIX_HOMESERVER") or "").strip().rstrip("/")


def _token(cfg):
    return (channel_setting(cfg, "matrix_token", "MATRIX_TOKEN") or "").strip()


def _room(cfg):
    return (channel_setting(cfg, "matrix_room", "MATRIX_ROOM") or "").strip()


class MatrixNotifier(BaseNotifier):
    name = "matrix"
    order = 55

    OWNS = ("matrix_homeserver", "matrix_room", "matrix_token")
    REQUIRES = (("matrix_homeserver", "web_matrix_homeserver"),
                ("matrix_room", "web_matrix_room"),
                ("matrix_token", "web_matrix_token"))

    def __init__(self, config):
        super().__init__(config)
        #: Resolved alias → room ID, so `#alerts:example.com` costs one
        #: extra request per process rather than one per message.
        self._room_id_cache = {}

    def configured(self):
        return bool(_homeserver(self.config) and _token(self.config) and _room(self.config))

    # ── transport ────────────────────────────────────────────────────
    def _request(self, method, path, body=None):
        url = f"{_homeserver(self.config)}{path}"
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_token(self.config)}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")

    def _resolve_room(self):
        """Room ID for the configured room, resolving an alias if needed."""
        room = _room(self.config)
        if not room.startswith("#"):
            return room
        if room in self._room_id_cache:
            return self._room_id_cache[room]
        quoted = urllib.parse.quote(room, safe="")
        data = self._request("GET", f"/_matrix/client/v3/directory/room/{quoted}")
        room_id = data.get("room_id") or ""
        if room_id:
            self._room_id_cache[room] = room_id
        return room_id

    def _send(self, plain, html=None):
        """Send one room message. Best-effort: logs and returns on any
        failure rather than raising into the facade."""
        if not self.configured():
            return None
        try:
            room_id = self._resolve_room()
            if not room_id:
                print("Matrix notification failed: room could not be resolved")
                return None
            body = {"msgtype": "m.text", "body": plain}
            if html:
                # Matrix's formatted-body convention: clients that render
                # HTML use it, the rest fall back to `body`. Both must be
                # present or plain-text clients see nothing.
                body["format"] = "org.matrix.custom.html"
                body["formatted_body"] = html
            # The transaction ID makes a retried send idempotent — Matrix
            # deduplicates on it, so a timeout that actually delivered
            # can't post the message twice.
            txn = f"docksentry-{time.time_ns()}"
            quoted = urllib.parse.quote(room_id, safe="")
            return self._request(
                "PUT",
                f"/_matrix/client/v3/rooms/{quoted}/send/m.room.message/{txn}",
                body)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if e.code in (401, 403):
                print("Matrix rejected the token — check MATRIX_TOKEN and that "
                      "the account has joined MATRIX_ROOM")
            else:
                print(f"Matrix notification failed: HTTP {e.code} {detail}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            print(f"Matrix notification failed: {e}")
        return None

    def _prefix(self, text):
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
        head = self._prefix(title)
        # The rich form is Matrix's own presentation of the SAME text —
        # a list instead of bullet characters. Built from the shared
        # per-container line so the two forms cannot say different
        # things (#63).
        html_lines = [f"<li>{_esc(notify_text.container_line(u, self.version_str(u), bullet='').strip())}</li>"
                      for u in updates]
        intro = body.split("\n\n", 1)[0]
        # Both forms carry the intro. The first cut of this dropped it
        # from the plain text and kept it in the HTML — two renderings of
        # one event saying different things, which is the whole failure
        # this change exists to remove.
        self._send(head + "\n" + body,
                   f"<b>{_esc(head)}</b><p>{_esc(intro)}</p>"
                   f"<ul>{''.join(html_lines)}</ul>")

    def send_update_result(self, name, image, success, detail="", source_url=""):
        import notify_text
        mark = "✅" if success else "❌"
        title, _body = notify_text.update_result(
            name, image, success, lang=notify_text.lang_of(self))
        head = self._prefix(f"{mark} {title}")
        plain = f"{head}\n{image}"
        rich = f"<b>{_esc(head)}</b><br/><code>{_esc(image)}</code>"
        if detail:
            plain += f"\n{detail}"
            rich += f"<br/>{_esc(detail)}"
        if source_url:
            plain += f"\n{source_url}"
            # The href is the one place escaping alone is not enough:
            # `html.escape` leaves the SCHEME untouched, so a
            # `javascript:` target survives it intact. `is_safe_link` is
            # the project's rule for what may be rendered as a link (the
            # Web UI and the `docksentry.link` label use the same one);
            # anything it rejects is shown as text instead of linked.
            if is_safe_link(source_url):
                rich += f'<br/><a href="{_esc(source_url)}">changelog</a>'
            else:
                rich += f"<br/>{_esc(source_url)}"
        self._send(plain, rich)

    def send_message(self, text):
        self._send(self._prefix(text))
