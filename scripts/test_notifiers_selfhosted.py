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
import contextlib
import io
import json
import os
import sys
import types
import urllib.error

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

# The error path used to print up to 200 bytes of Apprise's response body.
# The request that produced it carried APPRISE_URLS, and Apprise echoes
# the URLs it could not parse straight back — those embed tokens. That
# lands in `docker logs` and in every aggregator downstream of it.
os.environ["APPRISE_URLS"] = "pover://user@sup3rs3cr3ttoken"


def _boom(req, timeout=None):
    raise urllib.error.HTTPError(
        req.full_url, 400, "Bad Request", {},
        io.BytesIO(b'{"error": "invalid urls: pover://user@sup3rs3cr3ttoken"}'))


apprise_mod.urllib.request.urlopen = _boom
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    a.send_message("hi")
_printed = _out.getvalue()
checks["apprise: a failing send never prints the token in APPRISE_URLS"] = (
    "sup3rs3cr3ttoken" not in _printed)
checks["apprise: …nor the response body it came in"] = (
    "invalid urls" not in _printed)
checks["apprise: …but it still says the send failed, with the status"] = (
    "failed" in _printed and "400" in _printed)
del os.environ["APPRISE_URLS"]

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

# ── formatted_body is real HTML, and everything in it comes from outside
# A container name, an image reference and a docker error all reach it
# unescaped; one `<` in any of them mangles the markup for every client in
# the room, and `source_url` lands inside an href.
m.send_update_result("web<script>alert(1)</script>",
                     "img:1&2", False,
                     'failed: cannot mount "<vol>" & died',
                     "https://example.com/notes?a=1&b=2")
_fb = sent.last["body"]["formatted_body"]
checks["matrix: a container name is escaped into the HTML body"] = (
    "<script>" not in _fb and "&lt;script&gt;" in _fb)
checks["matrix: a docker error is escaped too"] = (
    "<vol>" not in _fb and "&lt;vol&gt;" in _fb)
checks["matrix: an ampersand in an image ref is escaped"] = "img:1&amp;2" in _fb
checks["matrix: the plain body is left alone (it is not HTML)"] = (
    "<script>" in sent.last["body"]["body"])
checks["matrix: a safe changelog URL is still a link"] = (
    'href="https://example.com/notes?a=1&amp;b=2"' in _fb)

# A link target survives `html.escape` with its scheme intact, so escaping
# alone is not enough — `is_safe_link` is the project's rule for what may
# be rendered as a link, and the Web UI and `docksentry.link` use the same
# one.
m.send_update_result("nginx", "nginx:1", True, "",
                     "javascript:alert(document.cookie)")
_fb = sent.last["body"]["formatted_body"]
checks["matrix: a javascript: changelog URL is never made into an href"] = (
    "href=" not in _fb)
checks["matrix: …and it is still shown, as text"] = "javascript:alert" in _fb
m.send_update_result("nginx", "nginx:1", True, "",
                     'https://ok.example/" onmouseover="alert(1)')
checks["matrix: a URL that would break out of the attribute is not linked"] = (
    "href=" not in sent.last["body"]["formatted_body"])

m.send_updates_available([{"name": "web<b>", "image": "img<1>", "host": "nas"}])
_fb = sent.last["body"]["formatted_body"]
checks["matrix: the update LIST escapes names and images too"] = (
    "<b>web<b>" not in _fb and "&lt;b&gt;" in _fb and "img&lt;1&gt;" in _fb)

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
