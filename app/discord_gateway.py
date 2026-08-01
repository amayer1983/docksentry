#!/usr/bin/env python3
"""Discord gateway client — the connection an interactive bot needs.

Discord's REST API can send messages, but it cannot *receive* anything.
To react to a slash command or a button press, a bot needs either a
publicly reachable HTTPS endpoint (which also means verifying an Ed25519
signature on every request — no asymmetric crypto in the standard
library, no `openssl` in the image, and a public endpoint is a hard ask
for something running behind a home NAT) or an outbound WebSocket to the
gateway. This is the gateway.

The protocol, in the order it happens:

  1. connect, receive HELLO (op 10) carrying `heartbeat_interval`
  2. send IDENTIFY (op 2) — or RESUME (op 6) if we're recovering
  3. heartbeat (op 1) every interval, carrying the last sequence number
  4. the server ACKs each one (op 11)
  5. everything else arrives as DISPATCH (op 0) with a `t` event name

Deliberately single-threaded. The socket has one writer — heartbeats and
commands both go out from the same loop, driven by a receive timeout —
because `WebSocketClient` is not thread-safe and interleaving two writers
mid-frame corrupts the stream. A second thread here would buy nothing:
the loop is idle almost all the time.

Reconnect handling is the part that matters in practice. Discord drops
connections routinely (deploys, load shedding, op 7) and a bot that
treats every drop as fatal is a bot that silently stops working. So:
resume where possible, re-identify where not, and back off when the
reconnects start looping.
"""

import json
import random
import socket
import time

from websocket_client import WebSocketClient, WebSocketError

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Opcodes (Discord gateway v10)
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

#: Close codes Discord uses for "your fault, don't come back". Retrying
#: these just burns rate limit and, for 4014, can get an app flagged.
FATAL_CLOSE_CODES = {
    4004,  # authentication failed — bad token
    4010,  # invalid shard
    4011,  # sharding required
    4012,  # invalid API version
    4013,  # invalid intents
    4014,  # disallowed intents (privileged intent not enabled in the portal)
}

#: We only need to be told about interactions, which arrive regardless of
#: intents. Requesting none is deliberate: message content and member
#: lists are privileged, need portal opt-in, and we don't read either.
DEFAULT_INTENTS = 0


class DiscordGateway:
    """One gateway connection, with resume and backoff.

    `on_event(name, data)` is called for every DISPATCH. It runs on the
    receive loop, so anything slow in there delays heartbeats — hand work
    off to a thread if it isn't instant.
    """

    def __init__(self, token, *, intents=DEFAULT_INTENTS, on_event=None,
                 url=GATEWAY_URL, log=print):
        self.token = token
        self.intents = intents
        self.on_event = on_event
        self.url = url
        self.log = log

        self.ws = None
        self.session_id = None
        self.resume_url = None
        self.seq = None
        self.running = False
        #: Set when a heartbeat goes out, cleared by its ACK. If one is
        #: still outstanding when the next is due, the connection is a
        #: "zombie" — TCP is up but Discord isn't listening — and the only
        #: fix is to tear it down and reconnect.
        self._awaiting_ack = False
        self._heartbeat_interval = None
        self._next_heartbeat = 0.0

    # ── frames ────────────────────────────────────────────────────
    def _send(self, op, data=None):
        self.ws.send(json.dumps({"op": op, "d": data}))

    def _identify(self):
        self._send(OP_IDENTIFY, {
            "token": self.token,
            "intents": self.intents,
            "properties": {"os": "linux", "browser": "docksentry",
                           "device": "docksentry"},
        })

    def _resume(self):
        self._send(OP_RESUME, {
            "token": self.token,
            "session_id": self.session_id,
            "seq": self.seq,
        })

    def _heartbeat(self):
        self._send(OP_HEARTBEAT, self.seq)
        self._awaiting_ack = True
        self._next_heartbeat = time.time() + (self._heartbeat_interval or 41.25)

    # ── the loop ──────────────────────────────────────────────────
    def run_forever(self, *, max_backoff=300):
        """Connect and keep the connection alive until `stop()`.

        Backoff is exponential with jitter. The jitter is not decoration:
        without it every Docksentry instance that dropped during the same
        Discord deploy reconnects in lockstep, which is how a service
        outage turns into a thundering herd on recovery.
        """
        self.running = True
        backoff = 1.0
        while self.running:
            try:
                resuming = bool(self.session_id and self.seq is not None)
                self._connect_once(resuming)
                backoff = 1.0        # a clean session resets the penalty
            except _FatalGatewayError as e:
                self.log(f"Discord gateway refused the connection: {e}")
                self.running = False
                return
            except Exception as e:
                if not self.running:
                    return
                self.log(f"Discord gateway disconnected ({e}); "
                         f"reconnecting in {backoff:.0f}s")
                time.sleep(backoff * (0.8 + 0.4 * random.random()))
                backoff = min(backoff * 2, max_backoff)
            finally:
                self._close_socket()

    def _connect_once(self, resuming):
        url = self.resume_url if (resuming and self.resume_url) else self.url
        self.ws = WebSocketClient(url).connect()
        self._awaiting_ack = False

        hello = self._recv_json()
        if hello is None or hello.get("op") != OP_HELLO:
            raise WebSocketError(f"expected HELLO, got {hello}")
        self._heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0
        # Discord asks for the first beat after interval * jitter so that
        # clients reconnecting together don't all beat on the same tick.
        self._next_heartbeat = time.time() + self._heartbeat_interval * random.random()

        if resuming:
            self._resume()
        else:
            self._identify()

        while self.running:
            timeout = max(0.0, self._next_heartbeat - time.time())
            self.ws.sock.settimeout(timeout or 0.01)
            try:
                msg = self._recv_json()
            except socket.timeout:
                if self._awaiting_ack:
                    # Zombie: we beat, they never acked. Reconnect and
                    # resume rather than sit on a dead socket.
                    raise WebSocketError("heartbeat not acknowledged")
                self._heartbeat()
                continue
            if msg is None:
                raise WebSocketError("gateway closed the connection")
            self._handle(msg)

    def _recv_json(self):
        raw = self.ws.recv()
        if raw is None:
            return None
        return json.loads(raw)

    def _handle(self, msg):
        op = msg.get("op")
        if msg.get("s") is not None:
            self.seq = msg["s"]

        if op == OP_DISPATCH:
            name = msg.get("t")
            data = msg.get("d") or {}
            if name == "READY":
                self.session_id = data.get("session_id")
                # Resuming has to go to the URL READY handed us, not the
                # generic gateway — the session lives on that specific
                # server and the generic endpoint would reject it.
                self.resume_url = data.get("resume_gateway_url") or self.url
                user = (data.get("user") or {}).get("username", "?")
                self.log(f"Discord bot connected as {user}")
            if self.on_event:
                try:
                    self.on_event(name, data)
                except Exception as e:
                    # A handler blowing up must not take the connection
                    # with it — the bot would go quiet over one bad event.
                    self.log(f"Discord handler error on {name}: {e}")
        elif op == OP_HEARTBEAT:
            # The server can ask for one out of band.
            self._heartbeat()
        elif op == OP_HEARTBEAT_ACK:
            self._awaiting_ack = False
        elif op == OP_RECONNECT:
            raise WebSocketError("gateway asked us to reconnect")
        elif op == OP_INVALID_SESSION:
            # d=True means the session is still resumable; False means
            # start clean. Discord asks for a short wait either way so a
            # broken client can't hammer IDENTIFY.
            if not msg.get("d"):
                self.session_id = None
                self.seq = None
            time.sleep(1 + 4 * random.random())
            raise WebSocketError("gateway invalidated the session")

    def _close_socket(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def stop(self):
        self.running = False
        self._close_socket()


class _FatalGatewayError(Exception):
    """A close code that reconnecting cannot fix (bad token, bad intents)."""


def classify_close(code):
    """True when `code` means 'stop trying'. Kept a function so the
    reconnect loop and the tests agree on the list."""
    return code in FATAL_CLOSE_CODES
