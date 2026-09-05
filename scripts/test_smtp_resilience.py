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
accepted it, and e-mail has no transaction id to dedupe a second copy
with. So only a failure BEFORE the handover is retried — a lost mail
after it stays lost, which is the lesser of the two.
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
checks["…and nothing after it is retried"] = ("if not handed_over" in src)
checks["quit() cannot replace the error that got us here"] = (
    "try:\n                    server.quit()\n                except Exception:" in src)
checks["a send that worked says so"] = ("return True" in src)

# ── the wiring ───────────────────────────────────────────────────────
checks["a held mail is re-sent without registering itself again"] = (
    "def _send_text" in src
    and "on_network_failure" not in src.split("def _send_text")[1].split("def ")[0])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
