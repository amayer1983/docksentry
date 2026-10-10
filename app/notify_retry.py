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

import threading
import time

#: How long a held notification is still worth delivering. See the module
#: docstring — half the monitor's 30-minute per-(container, kind) cooldown.
MAX_AGE_SECONDS = 900

#: How many held notifications are kept at once. Oldest out first.
MAX_QUEUED = 20


class _Stop:
    """What a resend returns to say "it did not land, and do not try again".

    The queue can only see True or False, and for most channels that is
    the whole truth: a POST either got a response or it did not. SMTP has
    a third state, and it is the one #66 is about — the server took the
    mail and the connection died before it said so. Retrying that does
    not replace the mail, it delivers a second copy, and the queue would
    otherwise keep going for its full fifteen minutes.

    Measured before this existed, with an injected clock: a network that
    came back half-way produced thirty copies the server accepted, all
    under one `Message-ID`. Falsy on purpose, so anything that only asks
    `if result:` still reads it as "not delivered".
    """

    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):                                   # pragma: no cover
        return "notify_retry.STOP"


#: Returned by a `send`, never passed in. See `_Stop`.
STOP = _Stop()


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
        #: `remember` runs on whichever thread was sending — the Telegram
        #: poller, the Discord bot, a web-triggered update — while `flush`
        #: runs on the scheduler's. Without this, an entry appended while
        #: flush was rebuilding the list was thrown away by its final
        #: assignment: not retried, not reported, gone. And the two
        #: overlap exactly during an outage, which is the only time
        #: either of them does anything.
        self._lock = threading.Lock()

    # ── state ────────────────────────────────────────────────────────
    def pending(self):
        """How many messages are waiting."""
        with self._lock:
            return len(self._items)

    def clear(self):
        """Forget everything. For tests, and for a channel being torn down."""
        with self._lock:
            self._items = []

    def snapshot(self):
        """A marker for `drop_since`. Opaque; do not read it."""
        with self._lock:
            return [id(i) for i in self._items]

    def drop_since(self, marker):
        """Remove everything queued after `marker` was taken. Returns how
        many went.

        For the "send a test" button. A test runs the real send path on
        purpose — that is the point of it — and since a failed mail is
        held for redelivery, pressing it while the server was down left
        an entry behind that arrived minutes later as a surprise.
        @NotRetarded hit exactly that: the page said the test had
        failed, and the message turned up when he restarted Mailrise.
        A test is a question about now; it must not leave work behind.
        """
        keep_ids = set(marker)
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if id(i) in keep_ids]
            return before - len(self._items)

    # ── the two things it does ───────────────────────────────────────
    def remember(self, channel, text, send, still_wanted=None):
        """Hold `text` for `channel`; `send(text)` will be tried again later.

        `still_wanted()` is asked again at delivery time. The stored `send`
        is the raw transport — it has to be, or a resend would re-enter the
        code that queued it — and that means it walks straight past the
        channel switch. Measured: queue a message, turn the channel off,
        flush, and it arrives anyway, up to fifteen minutes later. A switch
        that only applies to messages which happened to succeed the first
        time is not a switch.

        `send` must return True only when the message actually landed, and
        must NOT call back into `remember` — `flush` decides what happens to
        an entry it is retrying, and a resend that re-queues itself would
        reset the age and never expire.

        It may also return `STOP`, which is falsy and means "not
        delivered, and trying again would be worse than giving up" — a
        mail the server took before the connection died, or a refusal
        that will be refused again. That is the only way the queue can
        know the difference, because from out here a failure is a
        failure.
        """
        if not text:
            return False
        with self._lock:
            self._items.append({
                "channel": channel,
                "text": text,
                "send": send,
                "wanted": still_wanted,
                "at": self._clock(),
                "wall": self._wall(),
            })
            while len(self._items) > self.max_items:
                gone = self._items.pop(0)
                print(f"Notify retry: queue full at {self.max_items}, dropped "
                      f"the oldest held {gone['channel']} message")
        return True

    def flush(self):
        """Try the held messages again. Returns `(sent, dropped)`.

        Deliberately not gated on quiet hours or maintenance: both were
        already asked at the moment the message was created, and asking a
        second time would only mean holding it until it expires unsent.
        """
        # Taken out under the lock and worked on outside it: a resend can
        # sit on a socket for a minute, and holding the lock that long
        # would block every thread trying to hand us a failed message —
        # during an outage, which is when both happen at once.
        with self._lock:
            if not self._items:
                return (0, 0)
            batch, self._items = self._items, []
        now = self._clock()
        sent = dropped = 0
        blocked = set()          # channels whose network is still down
        keep = []
        for held in batch:
            age = now - held["at"]
            if age > self.max_age:
                dropped += 1
                print(f"Notify retry: dropped a {_ago(age)} old "
                      f"{held['channel']} message — too late to still be true")
                continue
            if held["channel"] in blocked:
                keep.append(held)
                continue
            wanted = held.get("wanted")
            if wanted is not None:
                try:
                    if not wanted():
                        dropped += 1
                        print(f"Notify retry: dropped a held "
                              f"{held['channel']} message — the channel was "
                              f"switched off while it waited")
                        # Asked as `enabled`, not `active`: a channel
                        # whose credential is empty for a moment because
                        # somebody is editing it has not been switched
                        # off, and dropping its held crash alert for that
                        # would be the wrong half of the rule.
                        continue
                except Exception:                         # pragma: no cover
                    pass
            try:
                # The raw result, not `bool()` of it: `STOP` is falsy,
                # so collapsing it here would lose the one thing it
                # says and the entry would be retried anyway.
                result = held["send"](self._prefix(held, now) + held["text"])
            except Exception as e:                        # pragma: no cover
                print(f"Notify retry: {held['channel']} resend error: {e}")
                result = False
            if result:
                sent += 1
                print(f"Notify retry: delivered a held {held['channel']} "
                      f"message, {_ago(age)} late")
            elif result is STOP:
                dropped += 1
                # Deliberately not "another copy": `STOP` has two
                # causes and only one of them has delivered anything.
                # A refusal has delivered nothing, and telling the
                # operator he may have a duplicate when the server said
                # no sends him looking for a mail that does not exist.
                # The channel printed which it was on the way here.
                print(f"Notify retry: giving up on a held "
                      f"{held['channel']} message — the channel says "
                      f"another attempt would not help")
                # Blocked for the rest of the pass like any other
                # failure: a connection that takes a message and then
                # dies is a connection, and dialling the other nineteen
                # held alerts into it costs a timeout each.
                blocked.add(held["channel"])
            else:
                keep.append(held)
                blocked.add(held["channel"])
        # Survivors go back in FRONT of anything that arrived while we
        # were sending: they are older, and the queue is delivered oldest
        # first.
        with self._lock:
            self._items = keep + self._items
            # The cap applies here too, which it did not: `remember`
            # trims, but during a flush it appends to a list that
            # started empty, so nothing trimmed the survivors going back
            # in. Measured with an injected clock: twenty held alerts
            # plus twenty arriving per pass reached 620 entries over the
            # fifteen minutes, bounded only by the age window. The
            # oldest go first, same rule and same wording as `remember`.
            while len(self._items) > self.max_items:
                gone = self._items.pop(0)
                dropped += 1
                print(f"Notify retry: queue full at {self.max_items}, "
                      f"dropped the oldest held {gone['channel']} message")
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


def remember(channel, text, send, still_wanted=None):
    """Hold a message on the shared queue."""
    return queue.remember(channel, text, send, still_wanted)


def flush():
    """Retry the shared queue. Called from the scheduler loop."""
    return queue.flush()


def pending():
    return queue.pending()
