#!/usr/bin/env python3
"""A mail lost to the network is held; one the server refused is not.

@NotRetarded watches for outages through SMTP into Mailrise, which is the
one path that was still dropping a failed alert on the floor (#66).

The classification is the whole job here, and it is a trap:
`smtplib.SMTPException` is a subclass of `OSError` — measured — so the
obvious `except OSError` would file a wrong password and a refused
recipient under "network trouble" and retry both every flush, forever,
against a server that will keep saying no.

The second trap is delivery itself. `sendmail` hands the message over and
then waits; if the connection dies once the body is out, the server may
well have accepted it. E-mail was said to have no transaction id to
dedupe a second copy with — it has one, `Message-ID`, and nothing was
setting it. Both sides of the handover are held now, and the copy is
capped by the one thing that can see it: the attempt itself answers
`notify_retry.STOP` once a copy may be out there.

Everything below the classification is DRIVEN. The first version of this
file pinned the rule with source greps, and an audit took them apart one
by one: the cap could be deleted from the queue entirely and all 181
procedures stayed green; "there is a point of no return" was satisfied by
a comment; "a send that worked says so" was satisfied by `return True`
inside a different function; and "re-sent without registering itself
again" looked for the word `on_network_failure`, so making `_send_text`
call `remember` directly — the exact bug the name describes — kept it
green. A grep cannot tell live code from a comment. These count copies.
"""
import os
import smtplib
import socket
import ssl
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "notifiers"))

import notify_retry  # noqa: E402
from notifiers import smtp as smtp_mod  # noqa: E402

checks = {}
_net = smtp_mod._is_network

# ── the classification ───────────────────────────────────────────────
checks["a name that does not resolve is the network"] = _net(socket.gaierror(-2, "x"))
checks["a refused connection is the network"] = _net(ConnectionRefusedError())
checks["a timeout is the network"] = _net(TimeoutError())
checks["a server that hangs up mid-conversation is the network"] = _net(
    smtplib.SMTPServerDisconnected("closed"))

checks["a wrong password is NOT the network"] = not _net(
    smtplib.SMTPAuthenticationError(535, b"bad"))
checks["a refused sender is NOT the network"] = not _net(
    smtplib.SMTPSenderRefused(550, b"no", "a@b"))
checks["a refused recipient is NOT the network"] = not _net(
    smtplib.SMTPRecipientsRefused({"a@b": (550, b"no")}))
checks["an unsupported command is NOT the network"] = not _net(
    smtplib.SMTPNotSupportedError("no starttls"))
checks["a certificate we do not trust is NOT the network"] = not _net(
    ssl.SSLCertVerificationError("bad cert"))
# The trap itself, stated as a fact so it cannot come back.
checks["…and smtplib's own errors really are OSErrors"] = issubclass(
    smtplib.SMTPException, OSError)

# ── the two SSL errors that are the wire, not an answer ──────────────
# STARTTLS is the default transport, and OpenSSL raises these when the
# TCP connection dies during the handshake. They were classified as "the
# server said no", so an outage on the standard transport dropped the
# alert instead of holding it — measured, held=False for both while the
# same outage over `smtp_tls=none` was held=True.
checks["a handshake cut off mid-way is the network"] = (
    _net(ssl.SSLEOFError("eof")) and _net(ssl.SSLZeroReturnError("zero")))

# ── driven: how many copies can the server end up with? ──────────────
# The whole of #66 in one number. A real transport is modelled, including
# `data()`, because that is the boundary the code now watches: `sendmail`
# does MAIL FROM and RCPT TO first, and a relay that drops the connection
# there has nothing.
import types as _types                                        # noqa: E402

_ACCEPTED = []        # every body the server took
_DIALLED = []         # every connection attempt


class _Transport:
    """A `smtplib.SMTP` stand-in that fails exactly where told.

    It mirrors the real call chain rather than approximating it, because
    the code under test watches a specific point in that chain: real
    `send_message` goes to `sendmail`, which does MAIL FROM, RCPT TO,
    then `data()`, and `data()` sends the DATA command, waits for a
    `354`, and only then sends the body. An earlier version of this
    class had `data()` append and raise with no `getreply` at all — so
    the 354 watch never fired here, and a mutation that broke it kept
    this procedure green. The guard ratchet caught that.

    `where` is where it dies: "connect" (nothing is listening),
    "mail_from" (the relay drops the envelope), "at_data_cmd" (it dies
    on the DATA command, before saying go-ahead — nothing was handed
    over), "after_354" (it says go-ahead and dies while the body is
    going out), "greylist" (a 4xx "come back later"), "refused" (a 5xx
    no) or None (it works).
    """

    where = "after_354"

    def __init__(self, *a, **kw):
        _DIALLED.append(self.where)
        if self.where == "connect":
            raise smtplib.SMTPServerDisconnected("network down")

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def getreply(self):
        if self.where == "at_data_cmd":
            raise smtplib.SMTPServerDisconnected("died on the DATA command")
        if self.where == "data_refused":
            # A server may answer the DATA command with a refusal
            # instead of go-ahead. `smtplib` turns that into
            # `SMTPDataError`; nothing was handed over, and a 5xx will
            # be a 5xx again in thirty seconds.
            return (554, b"5.7.1 Message rejected")
        return (354, b"go ahead, end with <CRLF>.<CRLF>")

    #: A reply code the server gives AFTER the body, instead of 250.
    #: Set by the end-of-data cases; None the rest of the time.
    answer_code = None

    def data(self, arg):
        code, msg = self.getreply()     # the 354 — or a death, or a no
        if code != 354:
            raise smtplib.SMTPDataError(code, msg)
        _ACCEPTED.append(arg)           # the server is taking the body
        if self.where == "after_354":
            raise smtplib.SMTPServerDisconnected("died mid-body")
        if self.answer_code:
            # It took the body and then said something. That is an
            # answer, not silence, and it decides.
            raise smtplib.SMTPDataError(self.answer_code, b"end-of-data reply")
        return (250, b"ok")

    def send_message(self, msg, to_addrs=None):
        if self.where == "mail_from":
            raise smtplib.SMTPServerDisconnected("dropped at MAIL FROM")
        if str(self.where).startswith("answer_"):
            return self.data(msg.as_string())
        if self.where == "greylist":
            raise smtplib.SMTPSenderRefused(
                450, b"4.2.0 Greylisted, try again later", "ds@example.test")
        if self.where == "refused":
            raise smtplib.SMTPRecipientsRefused(
                {"ops@example.test": (550, b"5.1.1 No such user")})
        if self.where == "mixed_recipients":
            raise smtplib.SMTPRecipientsRefused(
                {"a@example.test": (450, b"4.2.0 Greylisted"),
                 "b@example.test": (550, b"5.1.1 No such user")})
        return self.data(msg.as_string())

    def quit(self):
        pass


def _notifier():
    from notifiers.smtp import SmtpNotifier
    return SmtpNotifier(_types.SimpleNamespace(
        smtp_host="h", smtp_port=25, smtp_user="", smtp_password="",
        smtp_from="Docksentry <ds@example.test>", smtp_to="ops@example.test",
        smtp_tls="none", smtp_tls_verify=True, language="en",
        channel_smtp_enabled=True))


def _drain(queue, clock, first, then, limit=200):
    """Send once with the transport failing at `first`, then flush the
    queue with it failing at `then` until the queue lets go. Returns the
    number of flush passes."""
    _ACCEPTED.clear()
    _DIALLED.clear()
    _Transport.where = first
    _notifier().send_message("a container went down")
    _Transport.where = then
    passes = 0
    while queue.pending() and passes < limit:
        clock["t"] += 30                    # the scheduler's flush interval
        queue.flush()
        passes += 1
    return passes


_orig_smtp = smtplib.SMTP
_orig_queue = notify_retry.queue
smtplib.SMTP = _Transport
try:
    # 1 — the storm. Network gone, so attempt one never reaches the
    # server; it comes back half-way and the server then takes the mail
    # and dies. Before the cap covered this path: thirty copies the
    # server accepted, all under one `Message-ID`, measured with this
    # same harness.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    passes = _drain(q, clock, "connect", "after_354")
    checks["a half-returned network cannot turn one alert into a stack"] = (
        len(_ACCEPTED) <= 2 and not q.pending())

    # 2 — the worst case from the other side: taken and dropped twice.
    # One copy for the first attempt, one for its retry, then the queue
    # gives up rather than delivering a third.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _drain(q, clock, "after_354", "after_354")
    checks["…and taken-then-dropped twice stops at two copies"] = (
        len(_ACCEPTED) == 2 and not q.pending())
    checks["…both of them the same mail, not two alerts"] = (
        len({m.split("Message-ID:")[1].split("\n")[0].strip()
             for m in _ACCEPTED}) == 1)

    # 3 — the free side stays free. The network is down for a minute and
    # then returns; the alert must survive that, not be spent on the
    # first flush. The first cap did exactly that: the entry was dropped
    # at t=30 while the network came back at t=35.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _ACCEPTED.clear()
    _DIALLED.clear()
    _Transport.where = "connect"
    _notifier().send_message("a container went down")
    for _t in (30.0, 60.0, 90.0):
        clock["t"] = _t
        if _t >= 90.0:
            _Transport.where = None          # the network is back
        q.flush()
    checks["an outage longer than one flush does not cost the alert"] = (
        len(_ACCEPTED) == 1 and not q.pending())

    # 4 — a drop on the envelope is not a handover. Nothing was sent, so
    # this side must keep its free retries; reporting it as "the mail may
    # have arrived" capped an alert the server had never seen.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _ACCEPTED.clear()
    _Transport.where = "mail_from"
    _notifier().send_message("a container went down")
    _held_once = q.pending() == 1
    clock["t"] = 30.0
    q.flush()                                 # still failing: must survive
    checks["a drop on the envelope keeps its free retries"] = (
        _held_once and not _ACCEPTED and q.pending() == 1)

    # 4b — the DATA command itself. `data()` sends it and waits for a
    # `354` before a single byte of the body goes out, so a connection
    # that dies there has handed over nothing and the retry is free.
    # The boundary used to be "we called `data()`", which loses the
    # alert on a relay that drops at that moment.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _ACCEPTED.clear()
    _Transport.where = "at_data_cmd"
    _notifier().send_message("a container went down")
    _held = q.pending() == 1
    # The line stays down for the first retry, and only then comes
    # back. Healing it before the single flush made this pass whether
    # the attempt was free or capped — both worlds deliver on the one
    # retry they are given, so "free" was never observed.
    clock["t"] = 30.0
    q.flush()                                 # still at the DATA command
    _survived = q.pending() == 1
    _Transport.where = None                   # now the line comes back
    clock["t"] = 60.0
    q.flush()
    checks["dying on the DATA command still gets a free retry"] = (
        _held and _survived and len(_ACCEPTED) == 1 and not q.pending())

    # 4b-ii — and a server that answers the DATA command with a refusal
    # has taken nothing. Reading that as a handover would hold an alert
    # the server has already rejected, and then give up on it after one
    # attempt instead of saying the mail was refused.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _ACCEPTED.clear()
    _Transport.where = "data_refused"
    _notifier().send_message("a container went down")
    checks["a DATA command answered with 554 is a refusal, not a handover"] = (
        not q.pending() and not _ACCEPTED)

    # 4b-iii — a server that ANSWERS after the 354 has told us what
    # happened, and the answer beats the guess. The first version asked
    # `if taken:` before the reply code, so an end-of-data greylisting
    # was filed as "it may have arrived" and given one retry, while the
    # same greylisting at RCPT TO was retried for the full fifteen
    # minutes — the same thing at a different moment, two verdicts. And
    # a `552 too big` produced "the connection died while the message
    # was going out" for a line that was perfectly alive.
    for _code, _want_held, _label in (
            (451, True, "a 4xx after the body is still 'come back later'"),
            (552, False, "…and a 5xx after it is a refusal, not a maybe"),
            (550, False, "…including one that calls it spam")):
        clock = {"t": 0.0}
        q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
        notify_retry.queue = q
        _ACCEPTED.clear()
        _Transport.where = f"answer_{_code}"
        _Transport.answer_code = _code
        _notifier().send_message("a container went down")
        checks[_label] = (q.pending() == 1) is _want_held
    _Transport.answer_code = None

    # 4c — greylisting. A relay answering `450 … try again later` is
    # asking us to come back, and that is the most ordinary 4xx there
    # is. It used to be filed as a refusal and the alert thrown away
    # while the server was telling us to retry.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _ACCEPTED.clear()
    _Transport.where = "greylist"
    _notifier().send_message("a container went down")
    _greylisted = q.pending() == 1
    _Transport.where = None                   # the greylist has expired
    clock["t"] = 30.0
    q.flush()
    checks["a greylisted mail is come back to, not thrown away"] = (
        _greylisted and len(_ACCEPTED) == 1 and not q.pending())

    # 4d — …and a permanent no is not held in the first place. The queue
    # would ask the same server the same question every thirty seconds
    # for a quarter of an hour and get the same answer.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _ACCEPTED.clear()
    _Transport.where = "refused"
    _notifier().send_message("a container went down")
    checks["a 5xx refusal is never queued at all"] = (
        not q.pending() and not _ACCEPTED)

    # 4e — every recipient refused, with mixed codes. smtplib only
    # raises this when all of them said no, so `{a: 450 greylisted,
    # b: 550 no such user}` under the old `max()` was a permanent
    # verdict on a problem a retry would have fixed for one of them.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _Transport.where = "mixed_recipients"
    _notifier().send_message("a container went down")
    checks["mixed recipient codes follow the recoverable one"] = (
        q.pending() == 1)
    _Transport.where = "after_354"

    # 5 — a refusal on the RETRY path is not retried for a quarter of an
    # hour. `_is_network` makes that call on the first attempt; the queue
    # only ever saw True or False, so a 550 looped against a server that
    # kept saying no.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _DIALLED.clear()
    _Transport.where = "connect"
    _notifier().send_message("a container went down")
    _Transport.where = "refuse"
    clock["t"] = 30.0
    q.flush()
    checks["a server that says no is not asked again every 30s"] = (
        not q.pending())

    # 6 — and the mail itself carries its identity, reused by the retry.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _ACCEPTED.clear()
    _Transport.where = "after_354"
    _n = _notifier()
    _n.send_message("a container went down")
    _ids = [m.split("Message-ID:")[1].split("\n")[0].strip() for m in _ACCEPTED]
    checks["every mail carries a Message-ID of its own"] = bool(_ids and _ids[0])
    # NOT read off the wire: `EmailMessage.__setitem__` + `as_string()`
    # normalise a malformed id back to a single `>`, so the broken
    # `make_msgid(domain="example.test>")` looked identical there —
    # measured, the check this replaces passed with `parseaddr` taken
    # out again. The id that matters is the in-memory one: it goes into
    # the log line and into the retry's `msgid` argument.
    _, _, _inmem = _notifier()._attempt("Docksentry", "x")
    checks["…with the domain from SMTP_FROM, display name and all"] = (
        _inmem is not None
        and _inmem.endswith("@example.test>")
        and not _inmem.endswith(">>")
        and _inmem.count(">") == 1)
    _ACCEPTED.clear()
    _n.send_message("a different container went down")
    _other = [m.split("Message-ID:")[1].split("\n")[0].strip() for m in _ACCEPTED]
    checks["…while a different notification gets a different one"] = (
        _other and _other[0] != _ids[0])

    # 7 — `_send_text` is the queue's own send. If it registered a retry
    # of its own, the entry would reset its age and never expire. Spied
    # rather than grepped: the old check looked for the word
    # `on_network_failure` in the function, and a direct `remember` call
    # — the actual bug — kept it green.
    _spied = []
    _orig_remember = notify_retry.remember
    notify_retry.remember = lambda *a, **kw: _spied.append(a)
    try:
        _Transport.where = "after_354"
        _notifier()._send_text("held and failing again")
    finally:
        notify_retry.remember = _orig_remember
    checks["a held mail is re-sent without registering itself again"] = (
        _spied == [])

    # 8 — `quit()` talks to the server too, so on a broken connection it
    # raises as well, and an exception from a `finally` REPLACES the one
    # that got us here. We would then classify the wrong failure.
    class _BadQuit(_Transport):
        # `mail_from`, not `after_354`. With the body already out, the
        # verdict is TAKEN whatever the exception says, so the check
        # could not fall: removing the `try/except` around `quit()`
        # left it green while the real failure was misclassified. On
        # the envelope the classification is what decides, which is
        # where a swapped exception actually shows.
        where = "mail_from"

        def quit(self):
            # Deliberately NOT a network error: if this one replaced the
            # real `SMTPServerDisconnected`, the failure would be filed
            # as a refusal and the alert dropped instead of held. That is
            # the difference the check reads.
            raise smtplib.SMTPResponseException(550, b"no")

    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    smtplib.SMTP = _BadQuit
    _ACCEPTED.clear()
    _notifier().send_message("a container went down")
    # Nothing was handed over, so this must be held for a FREE retry.
    # If `quit()`'s own exception replaced the real one, the failure is
    # read as a refusal and the alert is dropped instead.
    checks["quit() cannot replace the error that got us here"] = (
        not _ACCEPTED and q.pending() == 1)
    smtplib.SMTP = _Transport
finally:
    smtplib.SMTP = _orig_smtp
    notify_retry.queue = _orig_queue


# Computed HERE, not three dozen lines earlier: it used to sit above
# the #66 block, so four checks could all be red and the procedure
# still printed PASS and exited 0.
bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
