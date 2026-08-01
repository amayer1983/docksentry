#!/usr/bin/env python3
"""The hand-rolled WebSocket client (RFC 6455), needed for the Discord bot.

There is no WebSocket in the standard library and adding a dependency
would break the project's one hard promise, so this protocol code is
ours. That makes it exactly the kind of code that needs testing against
something other than its own assumptions — so the interop half of this
file stands up a real server socket, performs the real handshake, and
exchanges real frames.

No network, no Discord: everything is loopback.
"""
import base64
import hashlib
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from websocket_client import (WebSocketClient, WebSocketError,   # noqa: E402
                              accept_key, encode_frame,
                              OP_TEXT, OP_PING, OP_CLOSE, MAX_FRAME_BYTES)

checks = {}
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ── the handshake maths, against the RFC's own worked example ────────
# RFC 6455 §1.3 gives this exact pair, so it checks us against the spec
# rather than against ourselves.
checks["accept key matches the RFC example"] = (
    accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

# ── outbound frames are masked, as servers require ───────────────────
f = encode_frame("hi")
checks["client frames set the mask bit"] = bool(f[1] & 0x80)
checks["masking key makes the payload differ"] = f[6:] != b"hi"
checks["masking is reversible"] = bytes(
    b ^ f[2:6][i % 4] for i, b in enumerate(f[6:])) == b"hi"
# Length encodings at their boundaries (§5.2).
checks["125 bytes: 7-bit length"] = encode_frame("x" * 125)[1] & 0x7F == 125
checks["126 bytes: 16-bit length"] = encode_frame("x" * 126)[1] & 0x7F == 126
checks["70000 bytes: 64-bit length"] = encode_frame("x" * 70000)[1] & 0x7F == 127
checks["FIN is always set outbound"] = bool(encode_frame("x")[0] & 0x80)


# ── interop against a real server ────────────────────────────────────
def _server_frame(payload, opcode=OP_TEXT, fin=True):
    """Server→client: same framing, but unmasked (§5.3)."""
    head = bytearray([(0x80 if fin else 0x00) | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < (1 << 16):
        head.append(126)
        head += struct.pack("!H", n)
    else:
        head.append(127)
        head += struct.pack("!Q", n)
    return bytes(head) + payload


def _read_client_frame(conn):
    """Minimal server-side reader, so we can assert what the client sent."""
    b1 = conn.recv(1)[0]
    b2 = conn.recv(1)[0]
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", conn.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", conn.recv(8))[0]
    key = conn.recv(4) if b2 & 0x80 else None
    data = b""
    while len(data) < length:
        data += conn.recv(length - len(data))
    if key:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return b1 & 0x0F, data


srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
server_saw = {}


def _serve():
    conn, _ = srv.accept()
    req = b""
    while b"\r\n\r\n" not in req:
        req += conn.recv(4096)
    head = req.decode("latin-1")
    key = ""
    for line in head.split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    server_saw["key_present"] = bool(key)
    server_saw["version_13"] = "Sec-WebSocket-Version: 13" in head
    server_saw["upgrade_hdr"] = "Upgrade: websocket" in head
    acc = base64.b64encode(
        hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall(
        ("HTTP/1.1 101 Switching Protocols\r\n"
         "Upgrade: websocket\r\nConnection: Upgrade\r\n"
         f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())

    # 1) a plain message
    conn.sendall(_server_frame(b'{"op":10}'))
    # 2) split across two frames — the gateway does fragment
    conn.sendall(_server_frame(b'{"a":', fin=False))
    conn.sendall(_server_frame(b'1}', opcode=0x0, fin=True))
    # 3) a ping the client must answer before the next message
    conn.sendall(_server_frame(b"beat", opcode=OP_PING))
    conn.sendall(_server_frame(b"after-ping"))
    op, data = _read_client_frame(conn)          # the pong
    server_saw["pong_op"] = op
    server_saw["pong_payload"] = data
    op, data = _read_client_frame(conn)          # a client message
    server_saw["client_msg"] = data
    server_saw["client_msg_masked"] = True       # _read_client_frame unmasked it
    # 4) close
    conn.sendall(_server_frame(struct.pack("!H", 1000), opcode=OP_CLOSE))
    try:
        conn.close()
    except OSError:
        pass


t = threading.Thread(target=_serve, daemon=True)
t.start()

ws = WebSocketClient(f"ws://127.0.0.1:{port}/gw?v=10", timeout=10).connect()
checks["handshake completes against a real server"] = True
checks["client sends a Sec-WebSocket-Key"] = server_saw.get("key_present") is True
checks["client announces version 13"] = server_saw.get("version_13") is True
checks["client sends the Upgrade header"] = server_saw.get("upgrade_hdr") is True

checks["receives a whole message"] = ws.recv() == b'{"op":10}'
checks["reassembles a fragmented message"] = ws.recv() == b'{"a":1}'
# The ping must be answered transparently: recv() returns the DATA frame,
# never the control frame.
checks["ping is answered without surfacing"] = ws.recv() == b"after-ping"

ws.send('{"op":1,"d":null}')
t.join(timeout=10)
checks["client answered the ping with a pong"] = server_saw.get("pong_op") == 0xA
checks["pong echoes the ping payload"] = server_saw.get("pong_payload") == b"beat"
checks["client message arrives intact"] = (
    server_saw.get("client_msg") == b'{"op":1,"d":null}')
checks["server close ends the stream"] = ws.recv() is None
ws.close()
srv.close()

# ── refuses an absurd announced length instead of allocating it ──────
checks["frame size cap is sane"] = 0 < MAX_FRAME_BYTES <= 64 * 1024 * 1024

# ── a server that doesn't upgrade is rejected ────────────────────────
bad = socket.socket()
bad.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
bad.bind(("127.0.0.1", 0))
bad.listen(1)
bport = bad.getsockname()[1]


def _serve_bad():
    conn, _ = bad.accept()
    while b"\r\n\r\n" not in conn.recv(4096):
        pass
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    conn.close()


threading.Thread(target=_serve_bad, daemon=True).start()
try:
    WebSocketClient(f"ws://127.0.0.1:{bport}/", timeout=10).connect()
    checks["a non-upgrading server is refused"] = False
except WebSocketError:
    checks["a non-upgrading server is refused"] = True
bad.close()


# ── the status LINE is parsed, not searched for "101" ────────────────
# `"101" not in lines[0]` accepts `HTTP/1.1 500 Error 101` — a reason
# phrase is free text and an intercepting proxy writes whatever it likes
# there. Anything but a real 101 has to be refused.
def _serve_status(line):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)

    def _go():
        conn, _ = s.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            part = conn.recv(4096)
            if not part:
                return
            req += part
        key = ""
        for ln in req.decode("latin-1").split("\r\n"):
            if ln.lower().startswith("sec-websocket-key:"):
                key = ln.split(":", 1)[1].strip()
        acc = base64.b64encode(
            hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall((f"{line}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
        try:
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_go, daemon=True).start()
    return s


for _desc, _line, _want_ok in (
        ("a 500 whose reason phrase contains 101 is refused",
         "HTTP/1.1 500 Error 101", False),
        ("a genuine 101 is still accepted",
         "HTTP/1.1 101 Switching Protocols", True)):
    _s = _serve_status(_line)
    _port = _s.getsockname()[1]
    try:
        WebSocketClient(f"ws://127.0.0.1:{_port}/", timeout=10).connect()
        checks[_desc] = _want_ok is True
    except WebSocketError:
        checks[_desc] = _want_ok is False
    _s.close()


# ── fragment reassembly survives a timeout between fragments ─────────
# The gateway drives its heartbeat off a socket timeout, so a timeout can
# land BETWEEN two fragments of one message. Keeping `chunks` in a local
# threw the first half away and the next call died with "continuation
# without a start frame" — losing a whole interaction on any message
# unlucky enough to straddle a heartbeat tick.
frag = socket.socket()
frag.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
frag.bind(("127.0.0.1", 0))
frag.listen(1)
fport = frag.getsockname()[1]
_go_on = threading.Event()


def _serve_split():
    conn, _ = frag.accept()
    req = b""
    while b"\r\n\r\n" not in req:
        req += conn.recv(4096)
    key = ""
    for ln in req.decode("latin-1").split("\r\n"):
        if ln.lower().startswith("sec-websocket-key:"):
            key = ln.split(":", 1)[1].strip()
    acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n"
                  ).encode())
    # First half only, then nothing at all until the client has timed out.
    conn.sendall(_server_frame(b'{"op":0,"t":"INTERACT', fin=False))
    _go_on.wait(10)
    conn.sendall(_server_frame(b'ION_CREATE"}', opcode=0x0, fin=True))
    conn.sendall(_server_frame(struct.pack("!H", 4004), opcode=OP_CLOSE))
    try:
        conn.close()
    except OSError:
        pass


threading.Thread(target=_serve_split, daemon=True).start()
fws = WebSocketClient(f"ws://127.0.0.1:{fport}/", timeout=10).connect()
fws.sock.settimeout(0.3)
try:
    fws.recv()
    checks["a timeout mid-message does not return a partial message"] = False
except socket.timeout:
    checks["a timeout mid-message does not return a partial message"] = True
except WebSocketError:
    # The old code raised here instead of timing out — also a failure.
    checks["a timeout mid-message does not return a partial message"] = False
_go_on.set()
fws.sock.settimeout(10)
try:
    _resumed = fws.recv()
except WebSocketError as e:
    _resumed = f"<{e}>"
checks["recv() resumes the message it was in the middle of"] = (
    _resumed == b'{"op":0,"t":"INTERACTION_CREATE"}')

# ── the close code is surfaced, not thrown away ──────────────────────
# 4004 means "bad token, stop"; 1000 means "see you in a second". recv()
# returns None for both, so the code is the only thing that tells them
# apart — and re-IDENTIFYing on 4004 gets an application flagged.
checks["a close still ends the stream"] = fws.recv() is None
checks["the close status code is kept"] = fws.close_code == 4004
fws.close()
frag.close()

# ── a peer that never sets FIN cannot grow us without limit ──────────
# Each FRAME was capped already; the reassembled MESSAGE was not, so an
# endless stream of fin=0 fragments grew the process until it died.
cap = WebSocketClient("ws://127.0.0.1:1/", timeout=1)


CHUNK = 65536
#: Enough headroom past the cap to prove we stopped because of the cap and
#: not because the peer ran out — and a hard stop, so an uncapped client
#: fails this test instead of running until the machine notices.
_PATIENCE = (MAX_FRAME_BYTES // CHUNK) + 64


class _Overrun(Exception):
    pass


class _CannedSocket:
    """Hands out 64 KiB continuation frames, and never a FIN."""

    def __init__(self):
        self.sent = 0

    def recv(self, _n):
        self.sent += 1
        if self.sent > _PATIENCE:
            raise _Overrun(f"client swallowed {self.sent * CHUNK} bytes "
                           "of one message without complaining")
        opcode = OP_TEXT if self.sent == 1 else 0x0
        return _server_frame(b"x" * CHUNK, opcode=opcode, fin=False)

    def sendall(self, _data):
        return None


cap.sock = _CannedSocket()
try:
    cap.recv()
    checks["an endless fragment stream is refused"] = False
except WebSocketError as e:
    checks["an endless fragment stream is refused"] = "exceeds" in str(e)
except _Overrun:
    checks["an endless fragment stream is refused"] = False
checks["…and it gives up near the frame cap, not gigabytes later"] = (
    cap.sock.sent <= (MAX_FRAME_BYTES // CHUNK) + 2)


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
