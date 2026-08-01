#!/usr/bin/env python3
"""The Discord gateway client, against a fake gateway (v2.0 Discord bot).

Drives the real client against a local server that speaks Discord's
gateway protocol: HELLO, IDENTIFY, heartbeats, DISPATCH, and the awkward
paths that only show up in production — the server asking us to
reconnect, an invalidated session, and a "zombie" connection where TCP is
fine but heartbeats stop being acknowledged.

Those three are the whole reason this file exists. A bot that handles the
happy path and nothing else looks perfect in testing and then quietly
stops responding a week later, because Discord drops connections
routinely and never says why.

Loopback only — no network, no Discord, no token.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from discord_gateway import (DiscordGateway, classify_close,   # noqa: E402
                             OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY,
                             OP_RESUME, OP_RECONNECT, OP_INVALID_SESSION,
                             OP_HELLO, OP_HEARTBEAT_ACK)
from discord_gateway import _FatalGatewayError                 # noqa: E402

checks = {}
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _frame(payload):
    if isinstance(payload, str):
        payload = payload.encode()
    head = bytearray([0x81])
    n = len(payload)
    if n < 126:
        head.append(n)
    else:
        head.append(126)
        head += struct.pack("!H", n)
    return bytes(head) + payload


def _read_msg(conn):
    b1 = conn.recv(1)
    if not b1:
        return None
    b2 = conn.recv(1)[0]
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", conn.recv(2))[0]
    key = conn.recv(4) if b2 & 0x80 else None
    data = b""
    while len(data) < length:
        chunk = conn.recv(length - len(data))
        if not chunk:
            return None
        data += chunk
    if key:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    if (b1[0] & 0x0F) == 0x8:
        return None
    return json.loads(data)


def _handshake(conn):
    req = b""
    while b"\r\n\r\n" not in req:
        part = conn.recv(4096)
        if not part:
            return False
        req += part
    key = ""
    for line in req.decode("latin-1").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n").encode())
    return True


class FakeGateway:
    """Serves a scripted sequence of connections."""

    def __init__(self, script):
        self.script = script            # list of per-connection handlers
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.seen = []
        self.done = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/?v=10&encoding=json"

    def _run(self):
        for handler in self.script:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if not _handshake(conn):
                continue
            try:
                handler(self, conn)
            except Exception:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self.done.set()


# ── 1. happy path: hello → identify → ready → dispatch ───────────────
def _happy(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 300}})))
    msg = _read_msg(conn)
    gw.seen.append(("identify", msg))
    conn.sendall(_frame(json.dumps({
        "op": OP_DISPATCH, "s": 1, "t": "READY",
        "d": {"session_id": "sess-1", "resume_gateway_url": gw.url,
              "user": {"username": "docksentry-test"}}})))
    conn.sendall(_frame(json.dumps({
        "op": OP_DISPATCH, "s": 2, "t": "INTERACTION_CREATE",
        "d": {"id": "42", "data": {"name": "status"}}})))
    # Answer one heartbeat so the client doesn't think we're a zombie.
    hb = _read_msg(conn)
    gw.seen.append(("heartbeat", hb))
    conn.sendall(_frame(json.dumps({"op": OP_HEARTBEAT_ACK})))
    time.sleep(0.6)


events = []
gw = FakeGateway([_happy])
g = DiscordGateway("tok", on_event=lambda n, d: events.append((n, d)),
                   url=gw.url, log=lambda *_: None)
th = threading.Thread(target=g.run_forever, daemon=True)
th.start()
time.sleep(2.2)
g.stop()
th.join(timeout=5)

ident = dict(gw.seen).get("identify") or {}
checks["sends IDENTIFY after HELLO"] = ident.get("op") == OP_IDENTIFY
checks["IDENTIFY carries the token"] = (ident.get("d") or {}).get("token") == "tok"
checks["IDENTIFY declares intents"] = "intents" in (ident.get("d") or {})
hb = dict(gw.seen).get("heartbeat") or {}
checks["sends heartbeats"] = hb.get("op") == OP_HEARTBEAT
checks["heartbeat carries the sequence"] = hb.get("d") in (1, 2)
checks["READY is surfaced"] = ("READY", ) in [(e[0],) for e in events]
checks["session id is remembered"] = g.session_id == "sess-1"
checks["dispatch reaches the handler"] = any(
    n == "INTERACTION_CREATE" for n, _ in events)
gw.sock.close()

# ── 2. server says "reconnect" → client resumes, not re-identifies ───
def _drop_then_expect_resume(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("first", _read_msg(conn)))
    conn.sendall(_frame(json.dumps({
        "op": OP_DISPATCH, "s": 7, "t": "READY",
        "d": {"session_id": "sess-2", "resume_gateway_url": gw.url,
              "user": {"username": "x"}}})))
    conn.sendall(_frame(json.dumps({"op": OP_RECONNECT})))
    time.sleep(0.3)


def _expect_resume(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("second", _read_msg(conn)))
    time.sleep(0.5)


gw2 = FakeGateway([_drop_then_expect_resume, _expect_resume])
g2 = DiscordGateway("tok", url=gw2.url, log=lambda *_: None)
th2 = threading.Thread(target=g2.run_forever, daemon=True)
th2.start()
time.sleep(4.0)
g2.stop()
th2.join(timeout=5)
seen2 = dict(gw2.seen)
checks["first connection identifies"] = (seen2.get("first") or {}).get("op") == OP_IDENTIFY
second = seen2.get("second") or {}
checks["after RECONNECT it RESUMEs"] = second.get("op") == OP_RESUME
checks["RESUME carries the session"] = (second.get("d") or {}).get("session_id") == "sess-2"
checks["RESUME carries the sequence"] = (second.get("d") or {}).get("seq") == 7
gw2.sock.close()

# ── 3. INVALID_SESSION(false) forces a clean IDENTIFY ────────────────
def _invalidate(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("a", _read_msg(conn)))
    conn.sendall(_frame(json.dumps({
        "op": OP_DISPATCH, "s": 3, "t": "READY",
        "d": {"session_id": "sess-3", "resume_gateway_url": gw.url,
              "user": {"username": "x"}}})))
    conn.sendall(_frame(json.dumps({"op": OP_INVALID_SESSION, "d": False})))
    time.sleep(0.3)


def _after_invalidate(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("b", _read_msg(conn)))
    time.sleep(0.5)


gw3 = FakeGateway([_invalidate, _after_invalidate])
g3 = DiscordGateway("tok", url=gw3.url, log=lambda *_: None)
th3 = threading.Thread(target=g3.run_forever, daemon=True)
th3.start()
time.sleep(9.0)
g3.stop()
th3.join(timeout=5)
seen3 = dict(gw3.seen)
checks["an invalidated session re-IDENTIFYs, never RESUMEs"] = (
    (seen3.get("b") or {}).get("op") == OP_IDENTIFY)
gw3.sock.close()

# ── 4. zombie: we beat, they never ack → we must not sit there ───────
def _zombie(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 200}})))
    _read_msg(conn)                       # identify
    conn.sendall(_frame(json.dumps({
        "op": OP_DISPATCH, "s": 1, "t": "READY",
        "d": {"session_id": "z", "resume_gateway_url": gw.url,
              "user": {"username": "x"}}})))
    # Swallow every heartbeat without ever acking.
    start = time.time()
    while time.time() - start < 1.5:
        if _read_msg(conn) is None:
            break
    gw.seen.append(("zombie_gave_up", True))


def _zombie_second(gw, conn):
    # HELLO first: the client is waiting for it before it says anything.
    # Reading before sending deadlocks both sides.
    conn.sendall(_frame(json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("reconnected", _read_msg(conn)))
    time.sleep(0.3)


gw4 = FakeGateway([_zombie, _zombie_second])
g4 = DiscordGateway("tok", url=gw4.url, log=lambda *_: None)
th4 = threading.Thread(target=g4.run_forever, daemon=True)
th4.start()
time.sleep(8.0)
reconnected = dict(gw4.seen).get("reconnected") is not None
g4.stop()
th4.join(timeout=5)
checks["an unacknowledged heartbeat forces a reconnect"] = reconnected
gw4.sock.close()

# ── 5. close codes that must not be retried ──────────────────────────
checks["bad token (4004) is fatal"] = classify_close(4004)
checks["disallowed intents (4014) is fatal"] = classify_close(4014)
checks["a normal close (1000) is retryable"] = not classify_close(1000)
checks["rate limited (4008) is retryable"] = not classify_close(4008)


# ── 6. a fatal close code actually STOPS the loop ────────────────────
# `classify_close` existed but nothing called it: the close code was
# discarded by the WebSocket layer, so 4004 looked like any other drop and
# the bot reconnected and re-IDENTIFYed forever. Discord flags
# applications that do that.
def _close_frame(code):
    return _frame_op(struct.pack("!H", code), 0x8)


def _frame_op(payload, opcode):
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    else:
        head.append(126)
        head += struct.pack("!H", n)
    return bytes(head) + payload


def _refuse_4004(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO,
                                    "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("identified", _read_msg(conn)))
    conn.sendall(_close_frame(4004))
    time.sleep(0.2)


def _must_not_happen(gw, conn):
    gw.seen.append(("retried_after_4004", True))
    time.sleep(0.2)


gw5 = FakeGateway([_refuse_4004, _must_not_happen])
logged5 = []
g5 = DiscordGateway("bad-token", url=gw5.url, log=logged5.append,
                    sleep=lambda *_: None)
th5 = threading.Thread(target=g5.run_forever, daemon=True)
th5.start()
th5.join(timeout=10)
seen5 = dict(gw5.seen)
checks["a 4004 close ends run_forever instead of reconnecting"] = (
    not th5.is_alive() and g5.running is False)
checks["…and it never re-IDENTIFYs on a rejected token"] = (
    seen5.get("retried_after_4004") is None)
checks["…and it says why, naming the token"] = any(
    "4004" in m and "token" in m.lower() for m in logged5)
gw5.sock.close()

# ── 7. a rejected RESUME must not be retried unchanged ───────────────
# 4007 (bad seq) and 4009 (session timed out) both mean "that session is
# no good". RESUMing again with the same session id and sequence number
# gets the same close, forever. The next attempt has to IDENTIFY clean.
def _ready_then_reconnect(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO,
                                    "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("s7_first", _read_msg(conn)))
    conn.sendall(_frame(json.dumps({
        "op": OP_DISPATCH, "s": 11, "t": "READY",
        "d": {"session_id": "sess-7", "resume_gateway_url": gw.url,
              "user": {"username": "x"}}})))
    conn.sendall(_frame(json.dumps({"op": OP_RECONNECT})))
    time.sleep(0.2)


def _reject_the_resume(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO,
                                    "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("s7_resume", _read_msg(conn)))
    conn.sendall(_close_frame(4009))          # session timed out
    time.sleep(0.2)


def _after_the_rejection(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO,
                                    "d": {"heartbeat_interval": 30000}})))
    gw.seen.append(("s7_third", _read_msg(conn)))
    time.sleep(0.3)


gw6 = FakeGateway([_ready_then_reconnect, _reject_the_resume,
                   _after_the_rejection])
g6 = DiscordGateway("tok", url=gw6.url, log=lambda *_: None,
                    sleep=lambda *_: None)
th6 = threading.Thread(target=g6.run_forever, daemon=True)
th6.start()
gw6.done.wait(timeout=15)
time.sleep(0.3)
g6.stop()
th6.join(timeout=5)
seen6 = dict(gw6.seen)
checks["a dropped session is RESUMEd once"] = (
    (seen6.get("s7_resume") or {}).get("op") == OP_RESUME)
checks["a RESUME rejected with 4009 is not RESUMEd again"] = (
    (seen6.get("s7_third") or {}).get("op") == OP_IDENTIFY)
checks["…and the dead session id is forgotten"] = g6.session_id != "sess-7"
gw6.sock.close()

# The same, as a unit, for 4007 — the codes come from one set and a
# regression in either is the same "reconnect loop that never recovers".
import types as _types    # noqa: E402

for _code in (4007, 4009):
    _u = DiscordGateway("tok", url="wss://gateway.example/", log=lambda *_: None)
    _u.session_id, _u.seq, _u.resume_url = "s", 42, "wss://old/"
    _u.ws = _types.SimpleNamespace(close_code=_code)
    try:
        _u._closed(True)
        _raised = None
    except Exception as _e:               # noqa: BLE001 — shape is the point
        _raised = _e
    checks[f"close {_code} clears the session so the next attempt identifies"] = (
        _u.session_id is None and _u.seq is None and _u.resume_url is None
        and _raised is not None
        and not isinstance(_raised, _FatalGatewayError))

_f = DiscordGateway("tok", url="wss://gateway.example/", log=lambda *_: None)
_f.ws = _types.SimpleNamespace(close_code=4014)
try:
    _f._closed(False)
    _fatal = None
except Exception as _e:                   # noqa: BLE001
    _fatal = _e
checks["a disallowed-intents close raises the fatal error"] = isinstance(
    _fatal, _FatalGatewayError)
_o = DiscordGateway("tok", url="wss://gateway.example/", log=lambda *_: None)
_o.ws = _types.SimpleNamespace(close_code=1000)
try:
    _o._closed(False)
    _ordinary = None
except Exception as _e:                   # noqa: BLE001
    _ordinary = _e
checks["…while an ordinary close is only a reconnect"] = (
    _ordinary is not None and not isinstance(_ordinary, _FatalGatewayError))

# ── 8. the reconnect penalty is forgiven by a healthy connection ─────
# `backoff = 1.0` sat after `_connect_once`, which only returns when
# `stop()` was called — every real disconnect leaves by raising. So the
# penalty only ever climbed, and after a handful of unrelated drops a
# routine Discord deploy left the bot offline for minutes.
def _healthy_then_drop(gw, conn):
    conn.sendall(_frame(json.dumps({"op": OP_HELLO,
                                    "d": {"heartbeat_interval": 30000}})))
    _read_msg(conn)
    conn.sendall(_frame(json.dumps({
        "op": OP_DISPATCH, "s": 1, "t": "READY",
        "d": {"session_id": "sess-8", "resume_gateway_url": gw.url,
              "user": {"username": "x"}}})))
    time.sleep(0.2)          # up and working, then the server goes away


gw7 = FakeGateway([_healthy_then_drop] * 3)
g7 = DiscordGateway("tok", url=gw7.url, log=lambda *_: None,
                    sleep=lambda *_: None, healthy_after=0.05)
th7 = threading.Thread(target=g7.run_forever, daemon=True)
th7.start()
gw7.done.wait(timeout=20)
time.sleep(0.4)
backoff_after_three_drops = g7.backoff
g7.stop()
th7.join(timeout=5)
checks["three healthy connections do not compound the backoff"] = (
    backoff_after_three_drops <= 2.0)
gw7.sock.close()

# …and a connection that never got anywhere still counts against us.
_b = DiscordGateway("tok", url="wss://gateway.example/", log=lambda *_: None,
                    healthy_after=0.05)
checks["a connection that never reached READY is not 'healthy'"] = (
    _b._was_healthy() is False)
_b._ready_at = time.monotonic() - 1.0
checks["…one that stayed up past the threshold is"] = _b._was_healthy() is True

# ── 9. the resume URL comes from the server, so it is pinned to TLS ──
# `resume_gateway_url` decides where the next connection goes and the
# WebSocket layer picks TLS purely from the scheme, so a `ws://` value
# would put the bot token on the wire in cleartext.
_r = DiscordGateway("tok", url="wss://gateway.discord.gg/?v=10",
                    log=lambda *_: None)
_r._handle({"op": OP_DISPATCH, "t": "READY", "s": 1,
            "d": {"session_id": "s", "resume_gateway_url": "ws://evil.example/"}})
checks["a ws:// resume URL is refused"] = _r.resume_url == _r.url
_r._handle({"op": OP_DISPATCH, "t": "READY", "s": 2,
            "d": {"session_id": "s",
                  "resume_gateway_url": "http://evil.example/"}})
checks["…and so is any other scheme"] = _r.resume_url == _r.url
_r._handle({"op": OP_DISPATCH, "t": "READY", "s": 3,
            "d": {"session_id": "s",
                  "resume_gateway_url": "wss://eu.gateway.discord.gg/"}})
checks["a wss:// resume URL is honoured"] = (
    _r.resume_url == "wss://eu.gateway.discord.gg/")
_r._handle({"op": OP_DISPATCH, "t": "READY", "s": 4,
            "d": {"session_id": "s"}})
checks["no resume URL at all falls back to the configured gateway"] = (
    _r.resume_url == _r.url)


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
