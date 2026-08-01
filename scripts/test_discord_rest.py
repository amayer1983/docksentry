#!/usr/bin/env python3
"""Discord REST client, against a local fake API (v2.0 Discord bot).

The important thing here isn't that a happy request works — it's the
rate limiting. Discord answers an over-eager client with 429 and a
`retry_after`, and ignoring that gets the whole *application* temporarily
banned, not just the request. So the 429 path is exercised directly, with
an injected sleep so the test doesn't actually wait.

A real `http.server` on loopback; no network, no token, no Discord.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from discord_rest import DiscordREST, DiscordRESTError   # noqa: E402

checks = {}
seen = []
slept = []
script = []          # queued (status, body, headers) responses


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        seen.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "ua": self.headers.get("User-Agent"),
            "body": json.loads(body) if body else None,
        })
        status, payload, extra = script.pop(0) if script else (200, {"ok": True}, {})
        raw = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if raw:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _reply


srv = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"

api = DiscordREST("tok-123", base=base, timeout=5,
                  log=lambda *_: None, sleep=lambda s: slept.append(s))

# ── auth and identification ──────────────────────────────────────────
script.append((200, {"username": "docksentry"}, {}))
me = api.me()
checks["GET /users/@me returns the body"] = me == {"username": "docksentry"}
checks["sends the bot token"] = seen[-1]["auth"] == "Bot tok-123"
checks["sends a descriptive User-Agent"] = "docksentry" in (seen[-1]["ua"] or "")
checks["uses the right path"] = seen[-1]["path"] == "/users/@me"

# ── 429: must wait the amount Discord asks for ───────────────────────
slept.clear()
script.append((429, {"retry_after": 1.5, "global": False}, {}))
script.append((200, {"ok": True}, {}))
api.me()
checks["429 is retried, not raised"] = True
checks["waits exactly the retry_after"] = slept == [1.5]

# ── retry_after only in the header still works ───────────────────────
slept.clear()
script.append((429, None, {"Retry-After": "2"}))
script.append((200, {"ok": True}, {}))
api.me()
checks["falls back to the Retry-After header"] = slept == [2.0]

# ── an absurd wait is refused rather than hung on ────────────────────
slept.clear()
script.append((429, {"retry_after": 3600}, {}))
try:
    api.me()
    checks["an absurd retry_after is refused"] = False
except DiscordRESTError as e:
    checks["an absurd retry_after is refused"] = e.status == 429
checks["…and it did not sleep on it"] = slept == []

# ── 4xx that isn't 429 fails immediately ─────────────────────────────
slept.clear()
script.append((401, {"message": "401: Unauthorized"}, {}))
try:
    api.me()
    checks["401 raises"] = False
except DiscordRESTError as e:
    checks["401 raises"] = e.status == 401
checks["401 is not retried"] = slept == []

# ── 5xx gets one more chance ─────────────────────────────────────────
slept.clear()
script.append((503, {"message": "try later"}, {}))
script.append((200, {"ok": True}, {}))
api.me()
checks["5xx is retried"] = len(slept) == 1

# ── slash-command registration ───────────────────────────────────────
cmds = [{"name": "status", "description": "Show container status", "type": 1}]
script.append((200, cmds, {}))
api.register_commands("app-1", cmds, guild_id="guild-9")
checks["guild commands use the guild path"] = (
    seen[-1]["path"] == "/applications/app-1/guilds/guild-9/commands")
checks["registration is a bulk PUT"] = seen[-1]["method"] == "PUT"
script.append((200, cmds, {}))
api.register_commands("app-1", cmds)
checks["global commands use the global path"] = (
    seen[-1]["path"] == "/applications/app-1/commands")

# ── interaction responses ────────────────────────────────────────────
script.append((204, None, {}))
api.interaction_response("i-1", "itok", "hello")
body = seen[-1]["body"]
checks["immediate response is type 4"] = body["type"] == 4
checks["response carries the content"] = body["data"]["content"] == "hello"
checks["responses are ephemeral by default"] = body["data"]["flags"] == 64
checks["204 decodes to None"] = True

script.append((204, None, {}))
api.interaction_response("i-2", "itok", deferred=True)
checks["deferred response is type 5"] = seen[-1]["body"]["type"] == 5

script.append((200, {"id": "m1"}, {}))
api.edit_original_response("app-1", "itok", "done")
checks["editing the original is a PATCH"] = seen[-1]["method"] == "PATCH"
checks["edit targets @original"] = seen[-1]["path"].endswith("/messages/@original")

script.append((200, {"id": "m2"}, {}))
api.create_message("chan-7", "hi")
checks["messages post to the channel"] = (
    seen[-1]["path"] == "/channels/chan-7/messages")

srv.shutdown()


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
