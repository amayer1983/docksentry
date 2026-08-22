#!/usr/bin/env python3
"""E-mail / SMTP channel (#2) — unchanged transport + subject/body format.

STARTTLS/SSL/none transport selection, multi-recipient parsing, BOT_LABEL
subject prefix — all exactly as before the plugin split.
"""

from .base import BaseNotifier


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
        """Send a plain-text e-mail via SMTP. `smtp_tls` selects the
        transport: "starttls" (default, 587), "ssl" (implicit, 465) or
        "none". SMTP_TO may be a comma/semicolon-separated list. Best-effort:
        logs and returns on any failure, never raising into the caller —
        same contract as the Discord/webhook channels."""
        import smtplib
        import ssl
        from email.message import EmailMessage
        c = self.config
        recipients = [r.strip() for r in c.smtp_to.replace(";", ",").split(",") if r.strip()]
        if not recipients:
            return
        label = self._bot_label()
        msg = EmailMessage()
        msg["Subject"] = (f"[{label}] " if label else "") + subject
        msg["From"] = c.smtp_from
        msg["To"] = ", ".join(recipients)
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
                server.send_message(msg, to_addrs=recipients)
            finally:
                server.quit()
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

    def send_message(self, text):
        # Strip Telegram *bold* markers for a clean plain-text mail.
        self.send_raw("Docksentry", text.replace("*", ""))
