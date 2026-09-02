#!/usr/bin/env python3
"""A notification that hit a dead network is held, and sent again when it
comes back (#66, @NotRetarded).

A short power cut took two of his machines down at once. Between 15:35:03
and 15:35:38 the log reads five `Try again` resolver errors, then `Network
unreachable`; Discord's gateway noticed, reconnected, and delivered the
crash alert. Telegram's three attempts — 2s and 4s apart, about six seconds
of patience against an outage of at least thirty-five — were long spent by
then, and the alert simply stopped existing. His own reading of it:

> I've got to get the notifications to go out when the reconnection occurs
> rather than just dying at the disconnect.

So: hold it, and try again on the next scheduler pass. Three deliberate
limits, because a retry queue is very easy to turn into a second problem.

**Age.** A crash alert that turns up two hours late is a lie — the reader
takes it for something that is happening now. The monitor's own cooldown is
30 minutes per (container, kind), which is the interval at which the same
alert may legitimately fire again, so anything held longer than half of that
could arrive *after* its own successor and read as the newer event. 15
minutes: comfortably longer than a router reboot or an ISP blip, short
enough that what arrives is still worth acting on, and it cannot cross the
next alert of its kind.

**Count.** 20 held messages. One incident produces at most one alert per
container and kind inside the cooldown, so 20 covers twenty distinct
problems; past that the operator has a bigger question than message
delivery. When it overflows the OLDEST goes, because it is the one closest
to being a lie anyway.

**Nothing on disk.** In memory only, gone on restart, on purpose. An
`updating…` state that survived a restart is a mistake this project has
already made once, and the interface never got rid of it. A queue file
would be the same shape of bug: an alert from before a crash, delivered
after it, describing a machine that has since been rebooted.

Order is kept per channel: a channel whose resend fails is skipped for the
rest of the pass rather than hammered, and its remaining messages stay
behind the one that failed.
"""

import time

#: How long a held notification is still worth delivering. See the module
#: docstring — half the monitor's 30-minute per-(container, kind) cooldown.
MAX_AGE_SECONDS = 900

#: How many held notifications are kept at once. Oldest out first.
MAX_QUEUED = 20


def _ago(seconds):
    """`42s` / `12m` / `2h` — the roundness the reader actually needs."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


class RetryQueue:
    """Messages that failed on the network, waiting for it to come back.

    `clock` and `wall` are injectable so a test can age an entry without
    sitting through it; nothing else has a reason to pass them.
    """

    def __init__(self, max_age=MAX_AGE_SECONDS, max_items=MAX_QUEUED,
                 clock=time.monotonic, wall=time.time):
        self.max_age = max_age
        self.max_items = max_items
        self._clock = clock
        self._wall = wall
        self._items = []

    # ── state ────────────────────────────────────────────────────────
    def pending(self):
        """How many messages are waiting."""
        return len(self._items)

    def clear(self):
        """Forget everything. For tests, and for a channel being torn down."""
        self._items = []

    # ── the two things it does ───────────────────────────────────────
    def remember(self, channel, text, send):
        """Hold `text` for `channel`; `send(text)` will be tried again later.

        `send` must return True only when the message actually landed, and
        must NOT call back into `remember` — `flush` decides what happens to
        an entry it is retrying, and a resend that re-queues itself would
        reset the age and never expire.
        """
        if not text:
            return False
        self._items.append({
            "channel": channel,
            "text": text,
            "send": send,
            "at": self._clock(),
            "wall": self._wall(),
        })
        while len(self._items) > self.max_items:
            gone = self._items.pop(0)
            print(f"Notify retry: queue full at {self.max_items}, dropped the "
                  f"oldest held {gone['channel']} message")
        return True

    def flush(self):
        """Try the held messages again. Returns `(sent, dropped)`.

        Deliberately not gated on quiet hours or maintenance: both were
        already asked at the moment the message was created, and asking a
        second time would only mean holding it until it expires unsent.
        """
        if not self._items:
            return (0, 0)
        now = self._clock()
        sent = dropped = 0
        blocked = set()          # channels whose network is still down
        keep = []
        for held in self._items:
            age = now - held["at"]
            if age > self.max_age:
                dropped += 1
                print(f"Notify retry: dropped a {_ago(age)} old "
                      f"{held['channel']} message — too late to still be true")
                continue
            if held["channel"] in blocked:
                keep.append(held)
                continue
            try:
                ok = bool(held["send"](self._prefix(held, now) + held["text"]))
            except Exception as e:                        # pragma: no cover
                print(f"Notify retry: {held['channel']} resend error: {e}")
                ok = False
            if ok:
                sent += 1
                print(f"Notify retry: delivered a held {held['channel']} "
                      f"message, {_ago(age)} late")
            else:
                keep.append(held)
                blocked.add(held["channel"])
        self._items = keep
        return (sent, dropped)

    # ── how a late message says it is late ───────────────────────────
    def _prefix(self, held, now):
        """The one line that keeps a held alert from reading as a fresh one.

        English and hard-coded, like the supergroup notice and the
        `Last logs:` header it will sit next to — it is delivery
        machinery talking about itself, not a translated message.
        """
        when = time.strftime("%H:%M:%S", time.localtime(held["wall"]))
        return (f"⏳ Delayed {_ago(now - held['at'])} — no network when this "
                f"fired. It happened at {when}.\n\n")


#: One queue for the whole process, so order is kept across channels and
#: the cap means what it says.
queue = RetryQueue()


def remember(channel, text, send):
    """Hold a message on the shared queue."""
    return queue.remember(channel, text, send)


def flush():
    """Retry the shared queue. Called from the scheduler loop."""
    return queue.flush()


def pending():
    return queue.pending()
