#!/usr/bin/env python3
"""Test the native e-mail / SMTP notifier (#2).

Monkeypatches smtplib so nothing is actually sent. Verifies channel
detection, the STARTTLS/SSL/none transport selection, auth, multi-recipient
parsing, and the BOT_LABEL subject prefix. Exits non-zero on any failure.
"""
import sys, os, types, smtplib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from notifier import Notifier


class _FakeSMTP:
    last = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.login_args = None
        self.sent = None
        _FakeSMTP.last = self

    def starttls(self):
        self.started_tls = True

    def login(self, u, p):
        self.login_args = (u, p)

    def send_message(self, msg, to_addrs=None):
        self.sent = {"msg": msg, "to_addrs": to_addrs,
                     "subject": msg["Subject"], "from": msg["From"], "to": msg["To"]}

    def quit(self):
        pass


def _cfg(**over):
    base = dict(smtp_host="mail.example.com", smtp_port=587, smtp_user="u",
                smtp_password="p", smtp_from="ds@example.com",
                smtp_to="a@x.com, b@y.com", smtp_tls="starttls", bot_label="pve1",
                discord_webhook="", webhook_url="")
    base.update(over)
    return types.SimpleNamespace(**base)


def main():
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = _FakeSMTP
    smtplib.SMTP_SSL = _FakeSMTP
    checks = {}
    try:
        # has_channels detection
        checks["has_channels true when configured"] = Notifier(_cfg()).has_channels()
        checks["has_channels false when unset"] = not Notifier(
            _cfg(smtp_host="", smtp_from="", smtp_to="")).has_channels()

        # starttls path
        _FakeSMTP.last = None
        Notifier(_cfg())._smtp_send("Update OK: sonarr", "body")
        s = _FakeSMTP.last
        checks["starttls used"] = s.started_tls is True
        checks["auth login called"] = s.login_args == ("u", "p")
        checks["multi-recipient parsed"] = s.sent["to_addrs"] == ["a@x.com", "b@y.com"]
        checks["BOT_LABEL prefixes subject"] = s.sent["subject"] == "[pve1] Update OK: sonarr"
        checks["From set"] = s.sent["from"] == "ds@example.com"

        # ssl path → SMTP_SSL, no starttls
        _FakeSMTP.last = None
        Notifier(_cfg(smtp_tls="ssl", smtp_port=465))._smtp_send("x", "y")
        checks["ssl: no starttls"] = _FakeSMTP.last.started_tls is False

        # none + no user → no starttls, no login
        _FakeSMTP.last = None
        Notifier(_cfg(smtp_tls="none", smtp_user=""))._smtp_send("x", "y")
        checks["none+no-user: no starttls, no login"] = (
            _FakeSMTP.last.started_tls is False and _FakeSMTP.last.login_args is None)
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
