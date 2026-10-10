#!/usr/bin/env python3
"""E-mail / SMTP channel (#2) — unchanged transport + subject/body format.

STARTTLS/SSL/none transport selection, multi-recipient parsing, BOT_LABEL
subject prefix — all exactly as before the plugin split.
"""

import notify_retry

from .base import BaseNotifier


#: Why an attempt failed. The retry queue only understands "delivered"
#: and "not delivered", so the one caller that has to tell these apart
#: is `_send_text`, and getting it wrong is expensive in both directions
#: — see each constant.
#:
#: NOTHING was delivered on the first three; only `TAKEN` may have been.
NETWORK = "network"        # the wire failed. Another go is free.
TEMPORARY = "temporary"    # the server said "later" (4xx). Also free, and
                           # this is what greylisting looks like — the
                           # most ordinary 4xx there is. Treating it as a
                           # refusal threw the alert away while the
                           # server was telling us to come back.
PERMANENT = "permanent"    # the server said no (5xx, bad auth, bad
                           # recipient). It will say no again in thirty
                           # seconds, so retrying is a loop.
TAKEN = "taken"            # the body was going out when the line died.
                           # It may be sitting in a queue over there, so
                           # another go is a second copy.


def _smtp_code(err):
    """The 3-digit reply behind an `smtplib` error, or None.

    Read rather than assumed: `SMTPResponseException` carries `smtp_code`,
    and `SMTPRecipientsRefused` carries none of its own — its codes are
    per address in `recipients`. Without this, a `450 Greylisted` and a
    `550 No such user` are the same object shape.
    """
    import smtplib

    code = getattr(err, "smtp_code", None)
    if isinstance(code, int) and 100 <= code <= 599:
        return code
    if isinstance(err, smtplib.SMTPRecipientsRefused):
        codes = [v[0] for v in (err.recipients or {}).values()
                 if isinstance(v, (tuple, list)) and v]
        # The RECOVERABLE one decides. `max` was the first answer here —
        # "if any address is a hard no, a retry cannot fix that one" —
        # and it throws the mail away for the addresses a retry WOULD
        # fix. smtplib only raises this when every recipient was
        # refused, so `{a: 450 greylisted, b: 550 no such user}` under
        # `max` is a permanent verdict on a temporary problem, and the
        # alert is gone. Under `min` it is retried, and the 550 address
        # simply keeps saying no — which costs nothing it was not going
        # to cost anyway.
        if codes:
            return min(codes)
    return None


def _why(err, taken):
    """Which of the four an exception is. `taken` is the 354 boundary.

    The order matters and the first version had it wrong. `taken` only
    says the server answered `354`; it does not say the line then died.
    A server that takes the body and then ANSWERS — `451 Greylisted` from
    an end-of-data restriction, `552 message size exceeds limit`, `550
    rejected as spam` — has told us exactly what happened, and the answer
    beats the guess.

    Measured against a real socket server: `451` at end-of-data came back
    as `TAKEN`, so the alert got one retry and was dropped, while the
    same greylisting at `RCPT TO` was correctly retried for the full
    fifteen minutes. The two are the same thing happening at different
    moments. And a `552` produced the line "the connection died while
    the message was going out" for a connection that was perfectly
    alive, which sends the operator looking for a mail that was never
    accepted.

    So: a reply code decides, whenever there is one. `TAKEN` is for the
    case it was always about — the line going quiet after the server
    said go-ahead, with nothing to read.
    """
    code = _smtp_code(err)
    if code is not None:
        return TEMPORARY if 400 <= code <= 499 else PERMANENT
    if taken:
        return TAKEN
    if _is_network(err):
        return NETWORK
    return PERMANENT


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

    # Not every `SSLError` is an answer. `SSLEOFError` is what OpenSSL
    # raises when the TCP connection dies *during* the handshake — the
    # wire failing, with no refusal behind it — and STARTTLS is the
    # default transport here, so treating it as "the server said no"
    # dropped the alert on exactly the path most installs use. Measured
    # against a real socket: a clean FIN mid-handshake gives
    # `SSLEOFError`, an RST gives `ConnectionResetError`, and before
    # this change the first was held=False while the second was True —
    # the same outage, two answers.
    #
    # `SSLZeroReturnError` rides along on the reading that it is the
    # same shape of failure. It is NOT measured: trying to produce one
    # on this path gave a `close_notify` back as `b""` with no
    # exception at all, which is a TLS shutdown *after* the handshake.
    # Harmless to include, and said out loud rather than implied.
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

    def send_raw(self, subject, body, attachment=None):
        """Send a plain-text e-mail. `True` when it landed.

        The ordinary way in, and the same best-effort contract as every
        other channel: logged and returned on any failure, never raised
        into the caller. `_attempt` is for the one caller that needs to
        know WHY it failed.
        """
        return bool(self._attempt(subject, body, attachment=attachment)[0])

    def _attempt(self, subject, body, attachment=None, msgid=None):
        """One delivery: `(ok, why, msgid)`.

        `why` is None on success and otherwise one of `NETWORK`,
        `TEMPORARY`, `PERMANENT`, `TAKEN` — the distinction the retry
        queue cannot make for itself, because from out there a failure is
        just a failure. `msgid` comes back so a retry can carry the
        identity of the first attempt rather than being a second mail.

        `smtp_tls` selects the transport: "starttls" (default, 587),
        "ssl" (implicit, 465) or "none". SMTP_TO may be a comma- or
        semicolon-separated list.
        """
        import smtplib
        import ssl
        from email.message import EmailMessage
        from email.utils import make_msgid, parseaddr
        c = self.config
        recipients = [r.strip() for r in c.smtp_to.replace(";", ",").split(",") if r.strip()]
        if not recipients:
            # `SMTP_TO=" ; , "` passes `configured()` and parses to
            # nothing. It used to return silently, so the alert vanished
            # without a word in the log — and a retry could not fix it.
            print("SMTP error: SMTP_TO is set but contains no address, so "
                  "there is nobody to send to. Nothing was sent.")
            return (False, PERMANENT, msgid)
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
        # is invalid.
        #
        # Measured, because the first version of this comment claimed
        # more: the header that went out on the wire was valid either
        # way, because `EmailMessage.__setitem__` and `as_string()`
        # normalise the trailing `>`. No server ever saw the broken id,
        # so none of them rewrote anything. What WAS broken is the id
        # this function hands back — the one that goes in the log line
        # and into the retry — and that is the one that has to match the
        # first attempt for the second copy to be recognisable.
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
        # The transport is decided before the context, because what
        # the warning below should SAY depends on it — and because an
        # unknown value must refuse before anything is built.
        # A value we do not know means plain SMTP, and plain SMTP
        # with `smtp_user` set puts the password on the wire. `tls`
        # is the obvious thing to type and it is not one of the
        # three, so that typo silently downgraded the transport. Say
        # it and refuse rather than sending the credential.
        # `or "starttls"`, which is what `config.py:857` and the
        # Connections page both use. This said `or "none"`, so an
        # empty value meant plain SMTP here and STARTTLS everywhere
        # else — and with `smtp_user` set, the password went out in
        # clear while the interface showed STARTTLS. `Config.from_env`
        # normalises it, so this only bites a value that came in
        # another way: a hand-written `settings.json`, or a restored
        # backup bundle, both of which `setattr` straight on.
        # Stripped BEFORE the default, or a whitespace-only value
        # is "unknown" rather than "unset" and gets refused.
        _tls = (c.smtp_tls or "").strip().lower() or "starttls"
        if _tls not in ("ssl", "starttls", "none"):
            print(f"SMTP error: SMTP_TLS is {c.smtp_tls!r}, which is not "
                  f"one of ssl / starttls / none. Nothing was sent — "
                  f"plain SMTP with a password set would put it on the "
                  f"wire, and that is not something to guess at.")
            return (False, PERMANENT, msgid)

        verify = getattr(c, "smtp_tls_verify", True)
        _has_password = bool(c.smtp_user and c.smtp_password)
        if verify:
            ctx = ssl.create_default_context()
        else:
            ctx = ssl._create_unverified_context()
        # Say what is actually at risk, in this setup, right now. The
        # first version printed one sentence in all six combinations of
        # (transport × password), including `smtp_tls=none`, where
        # there is no certificate to verify at all, and with no
        # credentials, where there is no password to send. @NotRetarded
        # got "the password is sent to whatever answers on that
        # address" for an internal Mailrise he does not authenticate
        # against — a security warning about something he does not
        # have, which teaches people to skip the line.
        if _tls == "none":
            if _has_password:
                # The one that was NOT warned about and is the worst of
                # them: no transport encryption with credentials set
                # puts the password on the wire in clear, with or
                # without `SMTP_TLS_VERIFY`.
                print("SMTP: SMTP_TLS=none and a user is set, so the "
                      "password goes out unencrypted. Use starttls or "
                      "ssl unless the server is on this machine.")
        elif not verify:
            if _has_password:
                print("SMTP: certificate verification is OFF "
                      "(SMTP_TLS_VERIFY=false) — the password is sent to "
                      "whatever answers on that address.")
            else:
                print("SMTP: certificate verification is OFF "
                      "(SMTP_TLS_VERIFY=false) — no credentials are at "
                      "stake, but the mail goes to whatever answers on "
                      "that address.")
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
        # seen nothing at all.
        #
        # Nor is "we called `data()`", which is what it used after that.
        # `smtplib.SMTP.data` sends the DATA command and waits for a
        # `354` before it sends a single byte of the body; a connection
        # that dies at the command has handed over nothing, and a retry
        # there is free. So the flag flips on the `354` itself — the
        # moment the server says "go ahead" — by watching `getreply` for
        # the duration of that one call. Read from the stdlib, not
        # guessed: `data()` is `putcmd("data")`, `getreply()`, then the
        # body.
        handed = {"over": False}
        try:
            if _tls == "ssl":
                server = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port,
                                          timeout=15, context=ctx)
            else:
                server = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15)
            try:
                if _tls == "starttls":
                    server.starttls(context=ctx)
                if c.smtp_user:
                    server.login(c.smtp_user, c.smtp_password)
                _data = getattr(server, "data", None)
                _reply = getattr(server, "getreply", None)
                if callable(_data) and callable(_reply):
                    def _marked_data(arg, _d=_data, _r=_reply):
                        def _watch():
                            code, msg = _r()
                            if code == 354:
                                handed["over"] = True
                            return code, msg

                        server.getreply = _watch
                        try:
                            return _d(arg)
                        finally:
                            server.getreply = _r

                    server.data = _marked_data
                elif callable(_data):
                    def _rough_data(arg, _d=_data):
                        handed["over"] = True
                        return _d(arg)

                    server.data = _rough_data
                else:
                    # Cannot see the boundary at all, so assume the worse
                    # of the two: "it may have arrived" caps the retry,
                    # while "it cannot have" would keep delivering
                    # copies. No `smtplib` transport lacks `data()` —
                    # this is for whatever a stand-in looks like.
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
            return (True, None, msgid)
        except ssl.SSLCertVerificationError as e:
            # Name the escape hatch. Before this change the mail went out
            # regardless, so anyone with a self-signed internal server sees
            # a new failure and deserves to be told why and what to do.
            print(f"SMTP error: the server's certificate could not be "
                  f"verified ({e}). If this is an internal mail server with "
                  f"a self-signed certificate, set SMTP_TLS_VERIFY=false — "
                  f"but be aware that sends the password unverified.")
            return (False, PERMANENT, msgid)
        except Exception as e:
            print(f"SMTP error: {e}")
            why = _why(e, handed["over"])
            if why == TAKEN:
                # The server had answered `354` and the line died
                # while the body was going out, so the mail may be
                # sitting in a queue over there. It is held anyway — a
                # crash alert nobody sees is worse than one that arrives
                # twice.
                #
                # What the same `Message-ID` on both copies buys is not
                # measured here and is not promised: some receivers
                # deduplicate on it, and the ones asked about in #66 do
                # not for separate deliveries. It makes the copy
                # *recognisable*; whether anything collapses it is the
                # receiver's business.
                print(f"SMTP: the server had said 'go ahead' and the "
                      f"connection died while the message was going out, "
                      f"so it may have arrived. Holding it under the same "
                      f"Message-ID ({msgid}): the queue will keep trying "
                      f"while the line is down, but the next attempt the "
                      f"server actually takes is the last one — a third "
                      f"copy is worse than a missing alert.")
            return (False, why, msgid)

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

        This is what the retry queue calls, and the only place that can
        tell the queue why an attempt failed — from out there a failure
        is just a failure. It never registers a retry of its own:
        `flush` decides what happens to an entry it is retrying, and a
        resend that re-queued itself would reset the age and never
        expire.

        * `True` — it landed.
        * `False` — the wire failed, or the server said "later" (4xx).
          Nothing was delivered and another go is free. Greylisting is
          the common case and it is a 4xx: a relay saying "come back in
          five minutes" used to be filed as a refusal and the alert
          thrown away while the server was asking us to retry.
        * `STOP` — either the server had said `354` and the line died
          while the body was going out, so a further attempt is a second
          copy rather than a second chance; or the server refused it
          outright (5xx, bad password, no such recipient), and it will
          refuse it again in thirty seconds.

        `msgid` is the identity of the NOTIFICATION, not of the attempt:
        the queue hands back the one the first try used, so the retry is
        the same mail rather than a second one."""
        ok, why, _m = self._attempt("Docksentry", text.replace("*", ""),
                                    msgid=msgid)
        if ok:
            return True
        if why in (NETWORK, TEMPORARY):
            return False
        return notify_retry.STOP

    def send_message(self, text):
        """One alert by mail, held for later if the wire was the problem.

        Telegram's `*bold*` markers are stripped for a clean plain-text
        mail. A failure once the server said `354` is held too (#66,
        @NotRetarded): we cannot know whether it took the mail, and a
        crash alert nobody sees is worse than one that arrives twice.
        What the retry must not do is deliver a third — `_send_text`
        answers `STOP` for that, so the cap sits where the knowledge is
        rather than being a counter set here in advance.
        """
        ok, why, msgid = self._attempt("Docksentry", text.replace("*", ""))
        if ok or why == PERMANENT:
            # A refusal is not held: the queue would ask the same server
            # the same question every thirty seconds for a quarter of an
            # hour and get the same no. It was logged on the way out.
            return
        notify_retry.remember(
            self.name, text,
            lambda t, _m=msgid: self._send_text(t, msgid=_m),
            still_wanted=lambda: self.enabled)
