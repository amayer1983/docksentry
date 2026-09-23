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
from urllib.parse import urlparse

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

#: What to tell the operator when one of the above ends the bot. A close
#: code alone sends people to Discord's docs; the sentence says what to go
#: and change.
FATAL_CLOSE_REASONS = {
    4004: "the bot token was rejected — check DISCORD_BOT_TOKEN",
    4010: "invalid shard",
    4011: "this bot is in too many servers to run unsharded",
    4012: "invalid API version",
    4013: "invalid intents",
    4014: "disallowed intents — enable them in the Discord developer portal",
}

#: Close codes that mean "this session is gone, start a new one". They are
#: NOT fatal — reconnecting is exactly right — but RESUMing again with the
#: same session id and sequence number is not: Discord already said that
#: pair is no good, so retrying it loops forever. Clear the session and
#: IDENTIFY clean instead.
STALE_SESSION_CLOSE_CODES = {
    4007,  # invalid seq — our sequence number is not one they recognise
    4009,  # session timed out
}

#: We only need to be told about interactions, which arrive regardless of
#: intents. Requesting none is deliberate: message content and member
#: lists are privileged, need portal opt-in, and we don't read either.
DEFAULT_INTENTS = 0

#: How long a connection has to have been up (measured from READY) before
#: we call it healthy and forgive the reconnect penalty. Without this the
#: backoff only ever climbs: every real disconnect leaves `_connect_once`
#: by raising, so a handful of unrelated drops over a week turn a routine
#: Discord deploy into minutes of silence.
HEALTHY_AFTER = 60.0

#: How many resumes may be refused in a row before the session is thrown
#: away and the client identifies fresh. Three, because a gateway that is
#: shedding load answers the first one or two with something transient —
#: and because the fallback identify has always worked, so there is no
#: value in a fourth round trip that only learns the same thing again.
MAX_RESUME_ATTEMPTS = 3


class DiscordGateway:
    """One gateway connection, with resume and backoff.

    `on_event(name, data)` is called for every DISPATCH. It runs on the
    receive loop, so anything slow in there delays heartbeats — hand work
    off to a thread if it isn't instant.
    """

    def __init__(self, token, *, intents=DEFAULT_INTENTS, on_event=None,
                 url=GATEWAY_URL, log=print, sleep=time.sleep,
                 healthy_after=HEALTHY_AFTER):
        self.token = token
        self.intents = intents
        self.on_event = on_event
        self.url = url
        self.log = log
        #: Injectable so tests don't sit out a real backoff.
        self._sleep = sleep
        self._healthy_after = healthy_after

        self.ws = None
        self.session_id = None
        self.resume_url = None
        self.seq = None
        self.running = False
        #: Current reconnect penalty, on the instance so the reset is
        #: observable (and so `stop()` leaves it inspectable).
        self.backoff = 1.0
        #: When READY last landed on this connection, or None if it never
        #: did. `_was_healthy()` reads it to decide whether the connection
        #: that just died had earned a clean slate.
        self._ready_at = None
        #: Set when a heartbeat goes out, cleared by its ACK. If one is
        #: still outstanding when the next is due, the connection is a
        #: "zombie" — TCP is up but Discord isn't listening — and the only
        #: fix is to tear it down and reconnect.
        self._awaiting_ack = False
        self._heartbeat_interval = None
        self._next_heartbeat = 0.0
        #: Consecutive resume attempts that never reached RESUMED. Reset
        #: by anything that does come up, and by a session Discord has
        #: already told us to drop.
        self._resume_failures = 0
        #: Whether this attempt got as far as sending a RESUME frame.
        self._sent_resume = False

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
        self._sent_resume = True
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
        self.backoff = 1.0
        while self.running:
            try:
                resuming = bool(self.session_id and self.seq is not None)
                self._connect_once(resuming)
                self.backoff = 1.0   # a clean exit resets the penalty
            except _FatalGatewayError as e:
                self.log(f"Discord gateway refused the connection: {e}")
                self.running = False
                return
            except Exception as e:
                if not self.running:
                    return
                # A connection that came up, said READY and then stayed up
                # was not a failing connection — whatever ended it (a
                # deploy, a load shed, an op 7) is a fresh event and starts
                # from a fresh penalty. Only back-to-back failures compound.
                if self._was_healthy():
                    self.backoff = 1.0
                elif resuming and self._sent_resume:
                    # Only a RESUME we actually sent counts. DNS that
                    # will not resolve, a refused TCP connect, a 503 on
                    # the upgrade — none of those are the session's
                    # fault, and throwing it away for them spends an
                    # IDENTIFY on a session Discord would still honour.
                    self._give_up_on_session()
                self.log(f"Discord gateway disconnected ({e}); "
                         f"reconnecting in {self.backoff:.0f}s")
                self._sleep(self.backoff * (0.8 + 0.4 * random.random()))
                self.backoff = min(self.backoff * 2, max_backoff)
            finally:
                self._close_socket()

    def _was_healthy(self):
        """True when the connection that just ended had been READY for
        long enough to count as working rather than as another failure."""
        started = self._ready_at
        return (started is not None
                and (time.monotonic() - started) >= self._healthy_after)

    def _give_up_on_session(self, _limit=None):
        """Drop the session once resuming has failed often enough.

        A resume that never reaches RESUMED leaves the session exactly
        as unusable as it was, so trying it again is a round trip spent
        to learn nothing. After `MAX_RESUME_ATTEMPTS` of them the session
        goes and the next attempt identifies — which is the path that
        has been working all along, a few seconds later than it needed
        to (three backed-off attempts, so six to eight).

        Only failures that never came up count: a connection that was
        READY and then dropped is an ordinary event and resumes from a
        clean slate.
        """
        limit = MAX_RESUME_ATTEMPTS if _limit is None else _limit
        self._resume_failures += 1
        if self._resume_failures < limit:
            return False
        self.session_id = None
        self.seq = None
        self.resume_url = None
        self._resume_failures = 0
        self.log(f"Discord gateway: {limit} resumes refused in a row — "
                 "dropping the session and identifying fresh")
        return True

    def _connect_once(self, resuming):
        url = self.resume_url if (resuming and self.resume_url) else self.url
        # Timestamped, because @NotRetarded's bot was silent for seven
        # minutes after a restart with NOTHING in the log to pull on —
        # and a silence the log cannot explain is a defect of the log
        # (#63). Every state transition now says when it happened, so
        # the next occurrence names its slow step itself.
        self._connect_started = time.monotonic()
        self._sent_resume = False
        # Cleared BEFORE the handshake, not after it. The old order left
        # it behind whenever `connect()` itself raised — and then
        # `_was_healthy()` answered for a connection that had ended hours
        # earlier, reset the penalty to one second, and the client went
        # at a gateway answering 503 once a second. Fourteen times in
        # twenty seconds, measured here on 22.09.
        self._ready_at = None
        self.log(f"Discord gateway: connecting "
                 f"({'resume' if resuming else 'fresh identify'})…")
        self.ws = WebSocketClient(url).connect()
        self._awaiting_ack = False

        hello = self._recv_json()
        if hello is None:
            self._closed(resuming)
        if hello.get("op") != OP_HELLO:
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
                self._closed(resuming)
            self._handle(msg)

    def _closed(self, resuming):
        """The gateway closed the socket. Always raises — the only
        question is which kind of raise, and that is decided by the close
        code the peer sent with the CLOSE frame.

        Three outcomes:

        * a code in `FATAL_CLOSE_CODES` — bad token, disallowed intents,
          a shard configuration Discord will never accept. Reconnecting
          cannot fix any of them and re-IDENTIFYing on 4004 is how an
          application gets flagged, so this stops the loop for good.
        * 4007/4009 while RESUMing — the session or sequence we resumed
          with is not one Discord recognises. Retrying the same RESUME
          loops forever, so drop the session and let the next attempt
          IDENTIFY clean.
        * anything else — an ordinary drop. Reconnect and, where we can,
          resume.
        """
        code = getattr(self.ws, "close_code", None)
        if code is not None and classify_close(code):
            raise _FatalGatewayError(
                f"close code {code} — {FATAL_CLOSE_REASONS.get(code, 'refused')}")
        if code in STALE_SESSION_CLOSE_CODES:
            self.session_id = None
            self.seq = None
            self.resume_url = None
            # Discord ended this one itself, so it is not a resume of
            # ours that failed. Leaving the count standing made a later
            # pair of genuine refusals log "3 resumes refused in a row".
            self._resume_failures = 0
            raise WebSocketError(
                f"gateway rejected the session (close code {code}) — "
                "the next attempt will identify fresh")
        where = "while resuming" if resuming else "while connecting"
        raise WebSocketError(
            f"gateway closed the connection {where}"
            + (f" (close code {code})" if code is not None else ""))

    def _since_connect(self):
        """" (N.Ns after connect start)" — or nothing, defensively."""
        t0 = getattr(self, "_connect_started", None)
        if t0 is None:
            return ""
        return f" ({time.monotonic() - t0:.1f}s after connect start)"

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
                self.resume_url = self._safe_resume_url(
                    data.get("resume_gateway_url"))
                self._ready_at = time.monotonic()
                self._resume_failures = 0
                user = (data.get("user") or {}).get("username", "?")
                self.log(f"Discord bot connected as {user}"
                         + self._since_connect())
            elif name == "RESUMED":
                # A successful resume never gets a READY — Discord sends
                # RESUMED instead, and until now that path logged nothing
                # at all. A reconnect whose success is silent is half of
                # how seven quiet minutes stay unexplained.
                self._ready_at = time.monotonic()
                self._resume_failures = 0
                self.log("Discord gateway: session resumed"
                         + self._since_connect())
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
                self.resume_url = None
                self._resume_failures = 0
            self._sleep(1 + 4 * random.random())
            raise WebSocketError("gateway invalidated the session")

    def _safe_resume_url(self, url):
        """Pin the server-supplied resume URL to TLS.

        `resume_gateway_url` arrives over the wire and decides where the
        next connection goes — and `WebSocketClient` picks TLS purely from
        the scheme. A `ws://` value would therefore send the bot token in
        cleartext to whatever host it names, so anything that isn't
        `wss://` is refused and the configured URL is used instead. The
        configured one is ours, so a loopback `ws://` test gateway still
        works; only the *server's* say-so is distrusted.
        """
        if not url:
            return self.url
        scheme = ""
        try:
            scheme = (urlparse(url).scheme or "").lower()
        except ValueError:
            scheme = ""
        if scheme != "wss":
            self.log(f"Discord gateway offered a non-wss resume URL "
                     f"({url!r}); ignoring it and reconnecting to the "
                     "configured gateway instead")
            return self.url
        return self._carry_query(url)

    def _carry_query(self, url):
        """The resume URL with our `?v=&encoding=` on it.

        `resume_gateway_url` arrives bare — `wss://gateway-us-east1-b
        .discord.gg`, no query — while the gateway needs the API version
        and encoding on every connection. Connecting without them got the
        session invalidated on every single reconnect: RESUME went out,
        Discord answered op 9, and the client fell back to a fresh
        IDENTIFY that worked because THAT url carries the query.

        Believed twice on a reading and dropped once on a bad
        observation before it was measured properly. What settled it was
        a forced disconnect — `ss -K` on the gateway socket, see
        CLAUDE.md — run as a control: bare URL → invalidated, query
        carried over → `session resumed`, three times.

        A resume URL that brought its own query is left alone.
        """
        if not url or urlparse(url).query:
            return url
        mine = ""
        try:
            mine = urlparse(self.url).query
        except ValueError:                               # pragma: no cover
            mine = ""
        if not mine:
            return url
        return url.rstrip("/") + "/?" + mine

    def _close_socket(self):
        """Hang up — and say whether we mean to come back.

        `run_forever` closes in a `finally`, so this ran after every
        disconnect including the ones we resume from, and it sent 1000.
        That is Discord's "I am finished", and asking to resume a session
        we have just declared finished is wrong on its own terms.

        It is NOT what was breaking the resume — that was the missing
        query on the resume URL, measured with a forced disconnect. This
        one has never been measured against the live gateway: both
        controls killed the socket outright, so no close frame went out
        either way. It is here because the contract says so, not because
        a log said so.

        `self.running` is the intent. `stop()` clears it before closing,
        and a fatal refusal clears it too, so those still close normally.
        """
        if self.ws is not None:
            try:
                self.ws.close(code=4000 if self.running else 1000)
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
