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
        from email.utils import make_msgid
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
        # copy is byte-identical where it counts: receivers that honour
        # `Message-ID` collapse it, and the ones that do not show two
        # crash alerts instead of none.
        msgid = msgid or make_msgid(domain=(c.smtp_from.split("@")[-1]
                                            or "docksentry").strip() or None)
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
        # Everything up to `send_message` can be retried safely: nothing
        # has been handed to the server yet. From that call onwards it
        # cannot — if the connection dies after DATA, the server may well
        # have accepted the mail, and e-mail has no transaction id to
        # dedupe a second copy with. So a lost mail there stays lost,
        # which is the lesser of the two (#66, @NotRetarded).
        handed_over = False
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
                handed_over = True
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
                if handed_over:
                    # The far end went away after DATA, so the mail may
                    # already be sitting in a queue there. It is held
                    # anyway: the retry carries the same `Message-ID`,
                    # which is what makes a duplicate recognisable — and
                    # a crash alert nobody sees is worse than one some
                    # mail clients show twice. This used to be dropped.
                    print(f"SMTP: the connection died after handing the "
                          f"mail over, so it may have arrived. Holding it "
                          f"for a retry under the same Message-ID "
                          f"({msgid}) — a receiver that honours it will "
                          f"collapse the copy.")
                on_network_failure(msgid)
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
        """One plain mail, `True` when it landed. No retry registration:
        `notify_retry` decides what happens to an entry it is retrying,
        and a resend that re-queued itself would reset the age and never
        expire.

        `msgid` is the identity of the NOTIFICATION, not of the attempt:
        the queue hands back the one the first try used, so the retry is
        the same mail rather than a second one."""
        return bool(self.send_raw("Docksentry", text.replace("*", ""),
                                  msgid=msgid))

    def send_message(self, text):
        # Strip Telegram *bold* markers for a clean plain-text mail.
        #
        # A failure after `DATA` is held too now. We still cannot know
        # whether the server took the mail — but the retry carries the
        # same `Message-ID`, so a receiver that honours it collapses the
        # copy, and the ones that do not show a crash alert twice rather
        # than not at all (#66, @NotRetarded).
        def _hold(msgid=None):
            notify_retry.remember(
                self.name, text,
                lambda t, _m=msgid: self._send_text(t, msgid=_m),
                still_wanted=lambda: self.enabled)

        self.send_raw("Docksentry", text.replace("*", ""),
                      on_network_failure=_hold)
