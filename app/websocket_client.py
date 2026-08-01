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
        #: The status code from the peer's CLOSE frame, once one has been
        #: received. `recv()` returns None on close and the code is the
        #: only thing that says *why* — Discord answers a bad token with
        #: 4004 and re-IDENTIFYing on that gets an application flagged, so
        #: the caller has to be able to tell it from a routine 1000.
        self.close_code = None
        #: Fragment reassembly state, on the INSTANCE and not in `recv()`.
        #: The gateway drives its heartbeat off a socket timeout, so a
        #: timeout can land between two fragments of one message; locals
        #: would drop everything read so far and the next call would see a
        #: continuation with no start frame. Here `recv()` simply resumes.
        self._frag_chunks = []
        self._frag_op = None
        self._frag_bytes = 0

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
        status = lines[0] if lines else ""
        # Parse the status LINE, don't scan it for "101". A substring test
        # accepts `HTTP/1.1 500 Error 101` — and worse, anything whose
        # reason phrase happens to contain those digits — which is exactly
        # the sort of thing an intercepting proxy returns.
        parts = status.split(None, 2)
        if (len(parts) < 2 or not parts[0].upper().startswith("HTTP/")
                or parts[1] != "101"):
            raise WebSocketError(f"expected HTTP 101, got {status or '<empty>'!r}")
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
    def _fill(self, n):
        """Ensure the buffer holds at least `n` bytes. Does NOT consume.

        Consuming here was a real bug: a read that timed out after the
        header had already been taken out of the buffer left the payload
        behind, and the next call parsed those payload bytes as a header
        (`unknown opcode 0xb`). Since the gateway drives its heartbeat off
        a socket timeout, that happened on any message unlucky enough to
        land on a heartbeat tick. Whatever arrives before the timeout
        stays in the buffer, so the next attempt continues where this one
        stopped.
        """
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk

    def _read_frame(self):
        """One raw frame → (fin, opcode, payload).

        Atomic with respect to the buffer: the header is parsed by
        peeking, and the buffer only advances once the WHOLE frame is
        present. A timeout part-way through therefore costs nothing but
        time — call again and it resumes.
        """
        self._fill(2)
        b1, b2 = self._buf[0], self._buf[1]
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        offset = 2
        if length == 126:
            self._fill(4)
            length = struct.unpack("!H", self._buf[2:4])[0]
            offset = 4
        elif length == 127:
            self._fill(10)
            length = struct.unpack("!Q", self._buf[2:10])[0]
            offset = 10
        if length > MAX_FRAME_BYTES:
            raise WebSocketError(f"frame of {length} bytes exceeds cap")
        # RFC 6455 §5.5: control frames carry at most 125 bytes and may
        # not be fragmented. Enforcing it stops a peer turning a PING into
        # a 16 MB payload we would dutifully echo back.
        if opcode >= 0x8 and (length > 125 or not fin):
            raise WebSocketError(f"malformed control frame (op {opcode:#x})")
        key_len = 4 if masked else 0
        total = offset + key_len + length
        self._fill(total)
        key = self._buf[offset:offset + key_len] if masked else None
        payload = self._buf[offset + key_len:total]
        # Only now, with a complete frame in hand, does the buffer move.
        self._buf = self._buf[total:]
        if key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self):
        """Next complete application message as bytes, or None when the
        peer closed. Ping is answered and control frames are handled here
        so callers only ever see data.

        Partial messages survive a timeout: the fragments read so far live
        on the instance, so a `socket.timeout` raised between two
        fragments costs nothing but time — call again and the message
        finishes assembling.
        """
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_CLOSE:
                if len(payload) >= 2:
                    self.close_code = struct.unpack("!H", payload[:2])[0]
                try:
                    self.send(payload[:2], opcode=OP_CLOSE)
                except Exception:
                    pass
                self._reset_fragments()
                return None
            if opcode == OP_PING:
                # Must echo the payload back (§5.5.3) — the gateway uses
                # this as a liveness check and drops silent clients.
                self.send(payload, opcode=OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY):
                self._frag_op = opcode
                self._frag_chunks = [payload]
                self._frag_bytes = len(payload)
            elif opcode == OP_CONTINUATION:
                if self._frag_op is None:
                    self._reset_fragments()
                    raise WebSocketError("continuation without a start frame")
                self._frag_chunks.append(payload)
                self._frag_bytes += len(payload)
            else:
                raise WebSocketError(f"unknown opcode {opcode:#x}")
            # Each FRAME is capped in `_read_frame`, but a message is not:
            # a peer that never sets FIN can hand us unlimited 1 MB
            # fragments and watch the process grow. Cap the total too.
            if self._frag_bytes > MAX_FRAME_BYTES:
                self._reset_fragments()
                raise WebSocketError(
                    f"reassembled message exceeds {MAX_FRAME_BYTES} bytes")
            if fin:
                message = b"".join(self._frag_chunks)
                self._reset_fragments()
                return message

    def _reset_fragments(self):
        self._frag_chunks = []
        self._frag_op = None
        self._frag_bytes = 0

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
