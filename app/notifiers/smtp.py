#!/usr/bin/env python3
"""E-mail / SMTP channel (#2) — unchanged transport + subject/body format.

STARTTLS/SSL/none transport selection, multi-recipient parsing, BOT_LABEL
subject prefix — all exactly as before the plugin split.
"""

import notify_retry

from .base import BaseNotifier


def _is_network(err):
    """Is `err` the network failing, rather than the server saying no?

    The distinction decides whether a failed mail is worth holding for a
    retry, and getting it wrong is expensive in both directions: a
    rejected message retried every flush is a loop against a server that
    will keep refusing it, and a lost one is a crash alert nobody sees.

    `smtplib.SMTPException` is a subclass of `OSError` — measured, not
    assumed — so the obvious `except OSError` would call a wrong password
    and a refused recipient "network trouble" and retry both forever. The
    SMTP classes are therefore asked FIRST, and only what is left over
    falls through to the socket errors.

    `SMTPServerDisconnected` is the one smtplib raises with no response
    code behind it: the far end went away mid-conversation. Everything
    else in that family carries something the server actually said.
    """
    # Imported here, like `send_raw` does: this module keeps `smtplib`
    # and `ssl` out of import time on purpose.
    import smtplib
    import ssl

    # Not every `SSLError` is an answer. `SSLEOFError` and
    # `SSLZeroReturnError` are what OpenSSL raises when the TCP
    # connection dies *during* the handshake — the wire failing, with no
    # refusal behind it — and STARTTLS is the default transport here, so
    # treating them as "the server said no" dropped the alert on exactly
    # the path most installs use. Measured: both were held=False before
    # this, while the same outage over `smtp_tls=none` was held=True.
    if isinstance(err, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return True
    if isinstance(err, ssl.SSLError):
        return False                 # a TLS refusal is an answer, not silence
    if isinstance(err, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(err, smtplib.SMTPException):
        return False
    return isinstance(err, OSError)  # gaierror, ConnectionError, TimeoutError


class SmtpNotifier(BaseNotifier):
    name = "smtp"
    order = 30

    OWNS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password",
            "smtp_from", "smtp_to", "smtp_tls")
    REQUIRES = (("smtp_host", "web_smtp_host"),
                ("smtp_from", "web_smtp_from"),
                ("smtp_to", "web_smtp_to"))

    def configured(self):
        """E-mail is active once host + from + to are all set (#2)."""
        c = self.config
        return bool(c.smtp_host and c.smtp_from and c.smtp_to)

    # ── transport ────────────────────────────────────────────────────
    def send_document(self, filename, data, subject, body=""):
        """One e-mail with a file attached.

        E-mail is the only delivery-only channel that can carry a file,
        and a backup arriving in your inbox is the copy that survives the
        machine it came from — which is the whole point of the ones
        @famewolf asked for (#2). No back channel, so nothing can *ask*
        for it; the Web UI and the scheduled copy are what trigger it.

        Same best-effort contract as everything else here: logged and
        dropped on failure, never raised into the caller.
        """
        return self.send_raw(subject, body or subject,
                             attachment=(filename, data))

    def send_raw(self, subject, body, attachment=None,
                 on_network_failure=None, msgid=None):
        """Send a plain-text e-mail via SMTP. `smtp_tls` selects the
        transport: "starttls" (default, 587), "ssl" (implicit, 465) or
        "none". SMTP_TO may be a comma/semicolon-separated list. Best-effort:
        logs and returns on any failure, never raising into the caller —
        same contract as the Discord/webhook channels."""
        import smtplib
        import ssl
        from email.message import EmailMessage
        from email.utils import make_msgid, parseaddr
        c = self.config
        recipients = [r.strip() for r in c.smtp_to.replace(";", ",").split(",") if r.strip()]
        if not recipients:
            return
        label = self._bot_label()
        msg = EmailMessage()
        msg["Subject"] = (f"[{label}] " if label else "") + subject
        msg["From"] = c.smtp_from
        msg["To"] = ", ".join(recipients)
        # The transaction id this path was said not to have (#66). Nobody
        # was setting one, so every attempt arrived as a fresh mail and a
        # resend was indistinguishable from a new alert. Generated once
        # per NOTIFICATION and carried through the retry, so a second
        # copy carries the same id, so a reader — or a receiver that
        # deduplicates on it — can tell it is the same alert and not a
        # second incident. The body differs (the retry is prefixed with
        # how late it is) and `Date` is restamped, so "identical" is
        # about the identity, not the bytes.
        # `parseaddr`, because SMTP_FROM may carry a display name —
        # "Docksentry <alerts@example.com>" would otherwise give the
        # domain `example.com>` and an id ending `@example.com>>`, which
        # is invalid and exactly the sort of thing a submission server
        # rewrites. A rewritten id defeats the whole point.
        _dom = parseaddr(c.smtp_from or "")[1].rpartition("@")[2].strip()
        msgid = msgid or make_msgid(domain=_dom or None)
        msg["Message-ID"] = msgid
        msg.set_content(body)
        if attachment:
            name, data = attachment
            # `application/json` rather than octet-stream: a mail client
            # that can preview it should, and one that cannot falls back
            # to an attachment anyway.
            msg.add_attachment(
                data if isinstance(data, bytes) else data.encode("utf-8"),
                maintype="application", subtype="json", filename=name)
        # Verify the server's certificate. `smtplib` without an explicit
        # context falls back to `ssl._create_stdlib_context()`, which on
        # this Python IS `_create_unverified_context` — measured:
        # check_hostname False, verify_mode 0. So the SMTP password was
        # handed to whatever answered on that host and port, with any
        # certificate at all, and no compromise of the real mail server
        # needed. (wud#352, found by sweeping comparable tools.)
        #
        # SMTP_TLS_VERIFY=false exists for internal mail servers with a
        # self-signed certificate, which is a real and common setup — but
        # it has to be asked for, and it says so when used, because the
        # thing being risked is a password.
        verify = getattr(c, "smtp_tls_verify", True)
        if verify:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl._create_unverified_context()
            print("SMTP: certificate verification is OFF "
                  "(SMTP_TLS_VERIFY=false) — the password is sent to "
                  "whatever answers on that address.")
        # Which side of the point of no return a failure happened on.
        # Before DATA a retry is free: the server has nothing. From the
        # moment the DATA command goes out it may have the mail and be
        # unable to say so, and then a retry DUPLICATES rather than
        # replaces (#66, @NotRetarded).
        #
        # `send_message` is not that boundary, which is what this used to
        # use: it does MAIL FROM and RCPT TO first, and a relay that
        # drops the connection on MAIL FROM — rate limit, overload —
        # was reported as "the mail may have arrived" when the server had
        # seen nothing at all. So the flag is set by `smtplib`'s own
        # `data()`, wrapped on this one connection object; that is the
        # call that sends the message body, and anything after it has to
        # be assumed delivered.
        handed = {"over": False}
        try:
            if c.smtp_tls == "ssl":
                server = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port,
                                          timeout=15, context=ctx)
            else:
                server = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15)
            try:
                if c.smtp_tls == "starttls":
                    server.starttls(context=ctx)
                if c.smtp_user:
                    server.login(c.smtp_user, c.smtp_password)
                _data = getattr(server, "data", None)
                if callable(_data):
                    def _marked_data(arg, _d=_data):
                        handed["over"] = True
                        return _d(arg)

                    server.data = _marked_data
                else:
                    # Cannot see the boundary, so assume the worse of the
                    # two: "it may have arrived" caps the retry, while
                    # "it cannot have" would keep delivering copies. No
                    # `smtplib` transport lacks `data()` — this is for
                    # whatever a future one or a stand-in looks like.
                    handed["over"] = True
                server.send_message(msg, to_addrs=recipients)
            finally:
                # `quit` talks to the server, so on a broken connection it
                # raises too — and an exception from a `finally` REPLACES
                # the one that got us here. We would then classify the
                # wrong failure. Read from the stdlib, not guessed.
                try:
                    server.quit()
                except Exception:
                    pass
            return True
        except ssl.SSLCertVerificationError as e:
            # Name the escape hatch. Before this change the mail went out
            # regardless, so anyone with a self-signed internal server sees
            # a new failure and deserves to be told why and what to do.
            print(f"SMTP error: the server's certificate could not be "
                  f"verified ({e}). If this is an internal mail server with "
                  f"a self-signed certificate, set SMTP_TLS_VERIFY=false — "
                  f"but be aware that sends the password unverified.")
        except Exception as e:
            print(f"SMTP error: {e}")
            if _is_network(e) and on_network_failure:
                if handed["over"]:
                    # The far end went away with the body already sent,
                    # so the mail may be sitting in a queue there. It is
                    # held anyway — a crash alert nobody sees is worse
                    # than one that arrives twice — but the retry is the
                    # last one: if that one is taken and dropped too,
                    # the queue gives up instead of delivering a third.
                    #
                    # What the same `Message-ID` on both copies buys is
                    # not measured here and is not promised: some
                    # receivers deduplicate on it, and the ones asked
                    # about in #66 do not for separate deliveries. It
                    # makes the copy *recognisable*; whether anything
                    # collapses it is the receiver's business.
                    print(f"SMTP: the connection died with the message "
                          f"already sent, so it may have arrived. Holding "
                          f"it for one more attempt under the same "
                          f"Message-ID ({msgid}) — if that one is taken "
                          f"and dropped as well, it is given up rather "
                          f"than retried for the queue's full lifetime.")
                on_network_failure(msgid, handed["over"])
        return False

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
        import notify_text
        subject, body = notify_text.updates_available(
            updates, lang=notify_text.lang_of(self),
            version_of=self.version_str, bullet="-")
        self.send_raw(subject, body)

    def send_update_result(self, name, image, success, detail="", source_url=""):
        import notify_text
        subject, body = notify_text.update_result(
            name, image, success, detail, source_url,
            lang=notify_text.lang_of(self))
        self.send_raw(subject, body)

    def _send_text(self, text, msgid=None):
        """One plain mail: `True`, `False`, or `notify_retry.STOP`.

        This is what the retry queue calls, and it is the only place that
        can tell the queue apart the two reasons an attempt failed. It
        never registers a retry of its own — `flush` decides what happens
        to an entry it is retrying, and a resend that re-queued itself
        would reset the age and never expire.

        * `True` — it landed.
        * `False` — the network failed before the server had the message.
          Worth another go; nothing was delivered.
        * `STOP` — either the server took it and then died (a further
          attempt is a second copy, not a second chance), or the failure
          was a refusal rather than the network, and a server that says
          no says no again in thirty seconds. Both used to come back as
          plain `False`, so the queue kept going for its full fifteen
          minutes: measured at thirty copies the server accepted, under
          one `Message-ID`.

        `msgid` is the identity of the NOTIFICATION, not of the attempt:
        the queue hands back the one the first try used, so the retry is
        the same mail rather than a second one."""
        reason = {"network": False, "handed_over": False}

        def _note(_msgid=None, handed_over=False):
            # Only called for network failures, and only by `send_raw`;
            # observing, never queueing.
            reason["network"] = True
            reason["handed_over"] = handed_over

        if self.send_raw("Docksentry", text.replace("*", ""), msgid=msgid,
                         on_network_failure=_note):
            return True
        if reason["handed_over"] or not reason["network"]:
            return notify_retry.STOP
        return False

    def send_message(self, text):
        # Strip Telegram *bold* markers for a clean plain-text mail.
        #
        # A failure after `DATA` is held too now. We still cannot know
        # whether the server took the mail — but the retry carries the
        # same `Message-ID`, so a receiver that honours it collapses the
        # copy, and the ones that do not show a crash alert twice rather
        # than not at all (#66, @NotRetarded).
        def _hold(msgid=None, duplicate_risk=False):
            # No try budget here, deliberately. The first version capped
            # this side at one retry, and that was wrong in both
            # directions: it spent the single attempt on the next flush
            # thirty seconds later whether or not the network was back
            # (measured — the alert was gone at t=30 while the network
            # returned at t=35), and it did nothing at all for the far
            # more likely case where attempt one died at the *connect*,
            # so no cap was set and the retry path then kept delivering.
            #
            # The cap belongs where the knowledge is: `_send_text` sees
            # each attempt and answers `STOP` once a copy may be out.
            # `duplicate_risk` is therefore no longer needed to decide
            # anything — it is what the log line above reports.
            notify_retry.remember(
                self.name, text,
                lambda t, _m=msgid: self._send_text(t, msgid=_m),
                still_wanted=lambda: self.enabled)

        self.send_raw("Docksentry", text.replace("*", ""),
                      on_network_failure=_hold)
