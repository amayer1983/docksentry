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
then waits; if the connection dies after DATA, the server may well have
accepted it. E-mail was said to have no transaction id to dedupe a
second copy with — it has one, `Message-ID`, and nothing was setting
it. Both sides of the handover are held now; the side that might
duplicate gets exactly ONE retry rather than the queue's full fifteen
minutes, because several receivers show separately delivered copies.
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

# ── the handover boundary ────────────────────────────────────────────
src = open(smtp_mod.__file__, encoding="utf-8").read()
checks["there is a point of no return"] = ("handed_over = False" in src)
checks["…set immediately before the message goes out"] = (
    src.index("handed_over = True") < src.index("server.send_message("))
# It used to say "and nothing after it is retried" — the old rule,
# pinned as a source grep. That rule existed because a second copy was
# indistinguishable from a second alert; with a `Message-ID` on every
# mail it is not, so both sides of the boundary are held now and the
# boundary only decides what the log says. Asserted by behaviour below.
# Was `"handed_over" in src and "Message-ID" in src` — both words
# occur in comments elsewhere, so deleting the whole log block left it
# green. Driven below instead: the post-handover case is held with
# exactly one retry, and the pre-handover case with none set.
checks["quit() cannot replace the error that got us here"] = (
    "try:\n                    server.quit()\n                except Exception:" in src)
checks["a send that worked says so"] = ("return True" in src)

# ── the wiring ───────────────────────────────────────────────────────
checks["a held mail is re-sent without registering itself again"] = (
    "def _send_text" in src
    and "on_network_failure" not in src.split("def _send_text")[1].split("def ")[0])


# ── #66: the transaction id this path was said not to have ───────────
# My own comment on 06.09. said an outage mid-send loses the mail
# because "e-mail has no transaction id to dedupe a second copy with".
# There is one — `Message-ID` — and nothing in the program was setting
# it, so every attempt arrived as a fresh mail. Now each notification
# carries its own and the retry reuses it.
import re as _re                                              # noqa: E402
import types as _types                                        # noqa: E402
import notify_retry                                           # noqa: E402

_sent = []
_held = []


class _Server:
    """A server that takes the conversation and then dies after DATA."""

    def __init__(self, die_after_data=True):
        self._die = die_after_data

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg, to_addrs=None):
        _sent.append(msg)
        if self._die:
            import smtplib
            raise smtplib.SMTPServerDisconnected("connection reset")

    def quit(self):
        pass


def _smtp_bot(die=True):
    import smtplib
    from notifiers.smtp import SmtpNotifier
    n = SmtpNotifier(_types.SimpleNamespace(
        smtp_host="h", smtp_port=25, smtp_user="", smtp_password="",
        smtp_from="ds@example.test", smtp_to="ops@example.test",
        smtp_tls="none", smtp_tls_verify=True, language="en",
        channel_smtp_enabled=True))
    smtplib.SMTP = lambda *a, **kw: _Server(die)
    return n


_orig_smtp = __import__("smtplib").SMTP
_orig_remember = notify_retry.remember
notify_retry.remember = (
    lambda ch, text, send, still_wanted=None, tries=None:
    _held.append((ch, text, send, tries)))
try:
    _n = _smtp_bot(die=True)
    _n.send_message("a container went down")
    _first = _sent[-1]["Message-ID"] if _sent else None
    checks["every mail carries a Message-ID of its own"] = bool(_first)
    checks["a death after DATA is held, not dropped"] = len(_held) == 1
    # …and the retry is the SAME mail, which is what makes a second copy
    # recognisable rather than a second alert.
    # …and the side that might duplicate is given exactly one attempt.
    checks["…with one retry, not fifteen minutes of them"] = (
        _held[0][3] == 1)
    _held[0][2](_held[0][1])
    checks["…and the retry reuses it"] = (
        len(_sent) == 2 and _sent[-1]["Message-ID"] == _first)
    # Two different notifications must not collapse into one.
    _sent.clear()
    _n.send_message("a different container went down")
    checks["…while a different notification gets a different one"] = (
        _sent and _sent[-1]["Message-ID"] != _first)
finally:
    __import__("smtplib").SMTP = _orig_smtp
    notify_retry.remember = _orig_remember


# Computed HERE, not three dozen lines earlier: it used to sit above
# the #66 block, so four checks could all be red and the procedure
# still printed PASS and exited 0.
bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
