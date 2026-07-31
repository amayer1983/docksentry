#!/usr/bin/env python3
"""E-mail / SMTP channel (#2) — unchanged transport + subject/body format.

STARTTLS/SSL/none transport selection, multi-recipient parsing, BOT_LABEL
subject prefix — all exactly as before the plugin split.
"""

from .base import BaseNotifier


class SmtpNotifier(BaseNotifier):
    name = "smtp"
    order = 30

    def configured(self):
        """E-mail is active once host + from + to are all set (#2)."""
        c = self.config
        return bool(c.smtp_host and c.smtp_from and c.smtp_to)

    # ── transport ────────────────────────────────────────────────────
    def send_raw(self, subject, body):
        """Send a plain-text e-mail via SMTP. `smtp_tls` selects the
        transport: "starttls" (default, 587), "ssl" (implicit, 465) or
        "none". SMTP_TO may be a comma/semicolon-separated list. Best-effort:
        logs and returns on any failure, never raising into the caller —
        same contract as the Discord/webhook channels."""
        import smtplib
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
        try:
            if c.smtp_tls == "ssl":
                server = smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15)
            try:
                if c.smtp_tls == "starttls":
                    server.starttls()
                if c.smtp_user:
                    server.login(c.smtp_user, c.smtp_password)
                server.send_message(msg, to_addrs=recipients)
            finally:
                server.quit()
        except Exception as e:
            print(f"SMTP error: {e}")

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
        lines = [f"- {u['name']} ({u['image']})"
                 + (f" {self.version_str(u)}" if self.version_str(u) else "")
                 + f" — {u.get('size','?')}, {u.get('created','?')}"
                 for u in updates]
        self.send_raw(f"{len(updates)} Docker update(s) available",
                      "Docksentry found updates for:\n\n" + "\n".join(lines))

    def send_update_result(self, name, image, success, detail="", source_url=""):
        status = "OK" if success else "FAILED"
        body = f"{name} ({image})\n\n{detail}"
        if source_url:
            body += f"\n\n{source_url}"
        self.send_raw(f"Update {status}: {name}", body)

    def send_message(self, text):
        # Strip Telegram *bold* markers for a clean plain-text mail.
        self.send_raw("Docksentry", text.replace("*", ""))
