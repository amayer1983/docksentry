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

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import BaseNotifier


def _homeserver():
    return (os.environ.get("MATRIX_HOMESERVER") or "").strip().rstrip("/")


def _token():
    return (os.environ.get("MATRIX_TOKEN") or "").strip()


def _room():
    return (os.environ.get("MATRIX_ROOM") or "").strip()


class MatrixNotifier(BaseNotifier):
    name = "matrix"
    order = 55

    def __init__(self, config):
        super().__init__(config)
        #: Resolved alias → room ID, so `#alerts:example.com` costs one
        #: extra request per process rather than one per message.
        self._room_id_cache = {}

    def configured(self):
        return bool(_homeserver() and _token() and _room())

    # ── transport ────────────────────────────────────────────────────
    def _request(self, method, path, body=None):
        url = f"{_homeserver()}{path}"
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_token()}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")

    def _resolve_room(self):
        """Room ID for the configured room, resolving an alias if needed."""
        room = _room()
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
        head = self._prefix(f"{len(updates)} update(s) available")
        lines, html_lines = [], []
        for u in updates:
            host = u.get("host") if isinstance(u, dict) else None
            where = f" @{host}" if host and host != "local" else ""
            lines.append(f"• {u['name']}{where} ({u['image']})"
                         f"{self.version_str(u)}")
            html_lines.append(f"<li><b>{u['name']}</b>{where} "
                              f"<code>{u['image']}</code></li>")
        self._send(head + "\n" + "\n".join(lines),
                   f"<b>{head}</b><ul>{''.join(html_lines)}</ul>")

    def send_update_result(self, name, image, success, detail="", source_url=""):
        mark = "✅" if success else "❌"
        head = self._prefix(f"{mark} {name} "
                            f"{'updated' if success else 'FAILED'}")
        plain = f"{head}\n{image}"
        html = f"<b>{head}</b><br/><code>{image}</code>"
        if detail:
            plain += f"\n{detail}"
            html += f"<br/>{detail}"
        if source_url:
            plain += f"\n{source_url}"
            html += f'<br/><a href="{source_url}">changelog</a>'
        self._send(plain, html)

    def send_message(self, text):
        self._send(self._prefix(text))
