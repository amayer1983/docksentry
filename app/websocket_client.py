#!/usr/bin/env python3
"""A minimal WebSocket client (RFC 6455), standard library only.

Docksentry's Discord bot needs a persistent connection to Discord's
gateway, and the gateway speaks WebSocket. Python's standard library has
no WebSocket client, and adding one would break the project's single
hard promise: no external dependencies.

So this implements the slice of RFC 6455 a gateway client actually uses:
the opening handshake, masked client frames, fragmentation reassembly,
ping/pong, and close. It is NOT a general-purpose library — no
extensions, no per-message compression, no server role. That narrowness
is deliberate; a smaller surface is a smaller thing to get wrong.

The alternative was Discord's HTTP-interactions endpoint, which requires
verifying an Ed25519 signature on every request. There is no asymmetric
crypto in the standard library and no `openssl` binary in the image, and
it would also need a publicly reachable HTTPS endpoint — a hard ask for
something that mostly runs on a home network behind NAT. The gateway
needs neither: it is an outbound connection authenticated by a token.
"""

import base64
import hashlib
import os
import secrets
import socket
import ssl
import struct
from urllib.parse import urlparse

#: RFC 6455 §1.3 — the fixed string a server mixes into the key it echoes.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes (RFC 6455 §5.2)
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: Refuse frames larger than this. The gateway sends nothing remotely
#: this big; the cap exists so a hostile or broken peer can't announce a
#: 2^63-byte payload and have us try to allocate it.
MAX_FRAME_BYTES = 16 * 1024 * 1024


class WebSocketError(Exception):
    """Any protocol-level failure. Callers reconnect rather than inspect."""


def accept_key(client_key):
    """The `Sec-WebSocket-Accept` value a server must return for
    `client_key` (RFC 6455 §4.2.2). Verifying it is what proves we're
    talking to a WebSocket server and not, say, a proxy that happened to
    return 101."""
    digest = hashlib.sha1((client_key + _WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def encode_frame(payload, opcode=OP_TEXT, mask=True):
    """Serialise one frame. Client→server frames MUST be masked (§5.3);
    servers close the connection on an unmasked one, so `mask` defaults
    to True and only the tests turn it off."""
    if isinstance(payload, str):
        payload = payload.encode()
    length = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)          # FIN set: we never fragment outbound
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + key + masked


class WebSocketClient:
    """One connection. Not thread-safe: send from a single thread, or
    serialise around it — the gateway's heartbeat and command sends are
    the two writers and they must not interleave mid-frame."""

    def __init__(self, url, *, timeout=30):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self._buf = b""

    # ── connection ────────────────────────────────────────────────
    def connect(self):
        parts = urlparse(self.url)
        secure = parts.scheme in ("wss", "https")
        host = parts.hostname
        if not host:
            raise WebSocketError(f"no host in {self.url!r}")
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            # Default context: verifies the certificate and hostname. A
            # gateway connection carries the bot token, so an
            # unauthenticated TLS session would hand it to anyone able to
            # intercept.
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw

        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())
        self._verify_handshake(key)
        return self

    def _verify_handshake(self, key):
        """Read the response head and check it really is an upgrade."""
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            head += chunk
            if len(head) > 64 * 1024:
                raise WebSocketError("handshake response too large")
        head, _, rest = head.partition(b"\r\n\r\n")
        # Anything the server pipelined after the handshake is already
        # frame data — keep it, or the first frame goes missing.
        self._buf = rest

        lines = head.decode("latin-1").split("\r\n")
        if not lines or "101" not in lines[0]:
            raise WebSocketError(f"expected HTTP 101, got {lines[0] if lines else '<empty>'!r}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if headers.get("upgrade", "").lower() != "websocket":
            raise WebSocketError("server did not upgrade to websocket")
        expected = accept_key(key)
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketError("Sec-WebSocket-Accept mismatch")

    # ── framing ───────────────────────────────────────────────────
    def _read_exactly(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        """One raw frame → (fin, opcode, payload)."""
        b1, b2 = self._read_exactly(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exactly(8))[0]
        if length > MAX_FRAME_BYTES:
            raise WebSocketError(f"frame of {length} bytes exceeds cap")
        key = self._read_exactly(4) if masked else None
        payload = self._read_exactly(length) if length else b""
        if key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self):
        """Next complete application message as bytes, or None when the
        peer closed. Ping is answered and control frames are handled here
        so callers only ever see data."""
        chunks = []
        msg_op = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_CLOSE:
                try:
                    self.send(payload[:2], opcode=OP_CLOSE)
                except Exception:
                    pass
                return None
            if opcode == OP_PING:
                # Must echo the payload back (§5.5.3) — the gateway uses
                # this as a liveness check and drops silent clients.
                self.send(payload, opcode=OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY):
                msg_op = opcode
                chunks = [payload]
            elif opcode == OP_CONTINUATION:
                chunks.append(payload)
            else:
                raise WebSocketError(f"unknown opcode {opcode:#x}")
            if fin:
                if msg_op is None:
                    raise WebSocketError("continuation without a start frame")
                return b"".join(chunks)

    def send(self, payload, opcode=OP_TEXT):
        if self.sock is None:
            raise WebSocketError("not connected")
        self.sock.sendall(encode_frame(payload, opcode=opcode))

    def close(self):
        if self.sock is None:
            return
        try:
            # 1000 = normal closure.
            self.send(struct.pack("!H", 1000), opcode=OP_CLOSE)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None
