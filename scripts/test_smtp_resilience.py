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

    `where` is "connect" (the network is down), "mail_from" (the relay
    drops the envelope), "after_data" (it takes the body and dies before
    saying so), "refuse" (a permanent no) or None (it works).
    """

    where = "after_data"

    def __init__(self, *a, **kw):
        _DIALLED.append(self.where)
        if self.where == "connect":
            raise smtplib.SMTPServerDisconnected("network down")

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def data(self, arg):
        # What `sendmail` calls once the envelope is accepted. Reaching
        # this means the server is taking the message.
        _ACCEPTED.append(arg)
        if self.where == "after_data":
            raise smtplib.SMTPServerDisconnected("died after DATA")
        return (250, b"ok")

    def send_message(self, msg, to_addrs=None):
        if self.where == "mail_from":
            raise smtplib.SMTPServerDisconnected("dropped at MAIL FROM")
        if self.where == "refuse":
            raise smtplib.SMTPRecipientsRefused({"a@b": (550, b"no such user")})
        self._msg = msg
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
    passes = _drain(q, clock, "connect", "after_data")
    checks["a half-returned network cannot turn one alert into a stack"] = (
        len(_ACCEPTED) <= 2 and not q.pending())

    # 2 — the worst case from the other side: taken and dropped twice.
    # One copy for the first attempt, one for its retry, then the queue
    # gives up rather than delivering a third.
    clock = {"t": 0.0}
    q = notify_retry.RetryQueue(clock=lambda: clock["t"], wall=lambda: 0.0)
    notify_retry.queue = q
    _drain(q, clock, "after_data", "after_data")
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
    _Transport.where = "after_data"
    _n = _notifier()
    _n.send_message("a container went down")
    _ids = [m.split("Message-ID:")[1].split("\n")[0].strip() for m in _ACCEPTED]
    checks["every mail carries a Message-ID of its own"] = bool(_ids and _ids[0])
    checks["…with the domain from SMTP_FROM, display name and all"] = (
        _ids and _ids[0].endswith("@example.test>"))
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
        _Transport.where = "after_data"
        _notifier()._send_text("held and failing again")
    finally:
        notify_retry.remember = _orig_remember
    checks["a held mail is re-sent without registering itself again"] = (
        _spied == [])

    # 8 — `quit()` talks to the server too, so on a broken connection it
    # raises as well, and an exception from a `finally` REPLACES the one
    # that got us here. We would then classify the wrong failure.
    class _BadQuit(_Transport):
        where = "after_data"

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
    checks["quit() cannot replace the error that got us here"] = (
        len(_ACCEPTED) == 1 and q.pending() == 1)
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
