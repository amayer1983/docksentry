#!/usr/bin/env python3
"""The self-hostable notification channels: Apprise, Gotify, Matrix.

Each of these was verified end-to-end against a REAL server while it was
written — a live Apprise API, a live Gotify, a live Conduit homeserver —
and the payload shapes below are what those servers actually accepted.
This file pins that down without needing the servers present: the
transport is stubbed and the assertions are about the request we build.

What it deliberately does check is the stuff that silently breaks:
severity mapping (a failed update must be loud), host labels surviving
into the message, and the config shapes users actually paste in.
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import notifiers.apprise as apprise_mod        # noqa: E402
import notifiers.gotify as gotify_mod          # noqa: E402
import notifiers.matrix as matrix_mod          # noqa: E402
from notifiers.apprise import AppriseNotifier, TYPE_FAILURE, TYPE_SUCCESS   # noqa: E402
from notifiers.gotify import GotifyNotifier, PRIO_HIGH, PRIO_NORMAL         # noqa: E402
from notifiers.matrix import MatrixNotifier    # noqa: E402

checks = {}
CFG = types.SimpleNamespace(bot_label="pve1")
UPDATES = [{"name": "paperless", "image": "ghcr.io/x:latest", "host": "nas"},
           {"name": "nginx", "image": "nginx:1.27", "host": "local"}]


class _Resp:
    """A urlopen() result. A real class, not a SimpleNamespace: Python
    looks up __enter__/__exit__ on the type, so `with urlopen(...)` fails
    on an instance that merely has those attributes."""

    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Sent:
    """Captures the request instead of making it."""

    def __init__(self):
        self.reqs = []

    def urlopen(self, req, timeout=None):
        body = req.data.decode() if req.data else ""
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"_raw": body}
        self.reqs.append({"url": req.full_url, "method": req.get_method(),
                          "headers": dict(req.headers), "body": parsed})
        return _Resp()

    @property
    def last(self):
        return self.reqs[-1]


def _patch(mod, sent):
    mod.urllib.request.urlopen = sent.urlopen


_real = apprise_mod.urllib.request.urlopen

# ── Apprise ──────────────────────────────────────────────────────────
os.environ["APPRISE_URL"] = "http://apprise:8000/notify"
sent = Sent()
_patch(apprise_mod, sent)
a = AppriseNotifier(CFG)
checks["apprise: URL alone is enough"] = a.configured() is True
a.send_updates_available(UPDATES)
b = sent.last["body"]
checks["apprise: title carries BOT_LABEL"] = b["title"].startswith("pve1 · ")
checks["apprise: remote host is labelled"] = "@nas" in b["body"]
checks["apprise: local host is not labelled"] = "@local" not in b["body"]
a.send_update_result("nginx", "nginx:1.27", False, "rolled back")
checks["apprise: a failure is type=failure"] = sent.last["body"]["type"] == TYPE_FAILURE
a.send_update_result("nginx", "nginx:1.27", True, "ok")
checks["apprise: a success is type=success"] = sent.last["body"]["type"] == TYPE_SUCCESS
os.environ["APPRISE_URLS"] = "json://box:8901"
a.send_message("hi")
checks["apprise: stateless targets are passed through"] = (
    sent.last["body"].get("urls") == "json://box:8901")
del os.environ["APPRISE_URLS"]
a.send_message("hi")
checks["apprise: stateful sends no urls"] = "urls" not in sent.last["body"]
del os.environ["APPRISE_URL"]
checks["apprise: unset → not configured"] = a.configured() is False
apprise_mod.urllib.request.urlopen = _real

# ── Gotify ───────────────────────────────────────────────────────────
sent = Sent()
_patch(gotify_mod, sent)
g = GotifyNotifier(CFG)
os.environ["GOTIFY_URL"] = "https://gotify.example.com"
checks["gotify: URL without a token is not configured"] = g.configured() is False
os.environ["GOTIFY_TOKEN"] = "AtokenXYZ"
checks["gotify: URL + token → configured"] = g.configured() is True
g.send_message("hi")
checks["gotify: /message is appended to the base URL"] = (
    sent.last["url"] == "https://gotify.example.com/message")
# The token goes in a header, not the query string — a `?token=` lands in
# every reverse proxy's access log.
checks["gotify: token travels in a header"] = (
    "AtokenXYZ" in json.dumps(sent.last["headers"]) and "token=" not in sent.last["url"])
os.environ["GOTIFY_URL"] = "https://gotify.example.com/message"
g.send_message("hi")
checks["gotify: a full endpoint URL is not doubled"] = (
    sent.last["url"] == "https://gotify.example.com/message")
g.send_update_result("nginx", "nginx:1.27", False, "rolled back")
checks["gotify: a failure is high priority (bypasses quiet hours)"] = (
    sent.last["body"]["priority"] == PRIO_HIGH)
g.send_update_result("nginx", "nginx:1.27", True, "ok")
checks["gotify: a success is normal priority"] = (
    sent.last["body"]["priority"] == PRIO_NORMAL)
g.send_updates_available(UPDATES)
checks["gotify: remote host is labelled"] = "@nas" in sent.last["body"]["message"]
del os.environ["GOTIFY_TOKEN"]
checks["gotify: token removed → not configured"] = g.configured() is False
del os.environ["GOTIFY_URL"]
gotify_mod.urllib.request.urlopen = _real

# ── Matrix ───────────────────────────────────────────────────────────
sent = Sent()
_patch(matrix_mod, sent)
m = MatrixNotifier(CFG)
os.environ["MATRIX_HOMESERVER"] = "https://matrix.example.com"
os.environ["MATRIX_TOKEN"] = "syt_abc"
checks["matrix: without a room it is not configured"] = m.configured() is False
os.environ["MATRIX_ROOM"] = "!abc:example.com"
checks["matrix: homeserver + token + room → configured"] = m.configured() is True
m.send_message("hello")
checks["matrix: sends with PUT"] = sent.last["method"] == "PUT"
checks["matrix: uses the client v3 send endpoint"] = (
    "/_matrix/client/v3/rooms/" in sent.last["url"]
    and "/send/m.room.message/" in sent.last["url"])
checks["matrix: bearer token in the header"] = (
    "Bearer syt_abc" in json.dumps(sent.last["headers"]))
# The transaction id makes a retried send idempotent — without it a
# timeout that actually delivered posts the message twice.
first = sent.last["url"]
m.send_message("hello")
checks["matrix: each send gets a fresh transaction id"] = sent.last["url"] != first
checks["matrix: room id is URL-escaped"] = "%21abc" in sent.last["url"].lower()
m.send_updates_available(UPDATES)
body = sent.last["body"]
checks["matrix: plain body is always present"] = bool(body.get("body"))
checks["matrix: HTML variant is offered"] = (
    body.get("format") == "org.matrix.custom.html" and bool(body.get("formatted_body")))
checks["matrix: remote host is labelled"] = "@nas" in body["body"]
m.send_update_result("nginx", "nginx:1.27", False, "rolled back")
checks["matrix: a failure is marked in the text"] = "❌" in sent.last["body"]["body"]
for k in ("MATRIX_HOMESERVER", "MATRIX_TOKEN", "MATRIX_ROOM"):
    del os.environ[k]
checks["matrix: unset → not configured"] = m.configured() is False
matrix_mod.urllib.request.urlopen = _real


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
