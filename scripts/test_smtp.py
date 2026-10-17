#!/usr/bin/env python3
"""Test the native e-mail / SMTP notifier (#2).

Monkeypatches smtplib so nothing is actually sent. Verifies channel
detection, the STARTTLS/SSL/none transport selection, auth, multi-recipient
parsing, and the BOT_LABEL subject prefix. Exits non-zero on any failure.
"""
import sys, os, types, smtplib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app",
                                "notifiers"))
from notifier import Notifier
from notifiers import smtp as smtp_mod


class _FakeSMTP:
    last = None

    def __init__(self, host, port, timeout=None, context=None):
        # `context` is the certificate-verification context. It is a
        # required part of the signature now: passing None here would let
        # the unverified default back in through the test, which is the
        # bug this fake is supposed to help prevent (wud#352).
        self.context = context
        self.host, self.port = host, port
        self.started_tls = False
        self.login_args = None
        self.sent = None
        _FakeSMTP.last = self

    def starttls(self, context=None):
        self.context = context
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
                discord_webhook="", webhook_url="",
                language="en", channel_smtp_enabled=True,
                smtp_tls_verify=True, smtp_subject_prefix="")
    base.update(over)
    return types.SimpleNamespace(**base)


def _capture(text, **cfg_over):
    """Send one alert to a stand-in server; return its headers and body."""
    import email
    import socket
    import threading

    got = []

    def _sink(srv):
        conn, _ = srv.accept()
        f = conn.makefile("rwb")
        conn.sendall(b"220 sink ESMTP\r\n")
        data_mode, body = False, []
        while True:
            line = f.readline()
            if not line:
                break
            if data_mode:
                if line.strip() == b".":
                    got.append(b"".join(body))
                    conn.sendall(b"250 ok\r\n")
                    data_mode = False
                    continue
                body.append(line)
                continue
            u = line.upper()
            if u.startswith((b"EHLO", b"HELO")):
                conn.sendall(b"250-sink\r\n250 OK\r\n")
            elif u.startswith((b"MAIL", b"RCPT")):
                conn.sendall(b"250 ok\r\n")
            elif u.startswith(b"DATA"):
                data_mode = True
                conn.sendall(b"354 go\r\n")
            elif u.startswith(b"QUIT"):
                conn.sendall(b"221 bye\r\n")
                break
            else:
                conn.sendall(b"250 ok\r\n")
        conn.close()

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(2)
    threading.Thread(target=_sink, args=(srv,), daemon=True).start()
    try:
        # No credentials: the stand-in does not advertise AUTH, and a
        # login attempt against it fails before anything is sent. The
        # auth path has its own checks above.
        n = smtp_mod.SmtpNotifier(_cfg(smtp_host="127.0.0.1", smtp_port=port,
                                       smtp_tls="none", smtp_user="",
                                       smtp_password="", **cfg_over))
        n.send_message(text)
    finally:
        srv.close()
    if not got:
        return {"Subject": "", "body": ""}
    parsed = email.message_from_bytes(got[0])
    # Decoded, which is what a mail client shows. A subject with a
    # non-ASCII character is RFC 2047 encoded and folded on the wire,
    # so the raw header of an 80-character subject measures 115 — and
    # a length check against the raw form fails for the wrong reason.
    import email.header
    raw = parsed["Subject"] or ""
    subject = str(email.header.make_header(email.header.decode_header(raw)))
    return {"Subject": subject,
            "body": parsed.get_payload(decode=True).decode("utf-8", "replace")}


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

    # version string for cross-channel "Updates Available" badge (#44)
    vs = Notifier._version_str
    checks["version: old → new arrow"] = vs({"old_version": "1.2.3", "new_version": "1.3.0"}) == "v1.2.3 → v1.3.0"
    checks["version: single known"] = vs({"new_version": "1.3.0"}) == "v1.3.0"
    checks["version: none → empty"] = vs({}) == ""

    # ── the subject says which container and what happened ─────────
    # Every alert used to arrive as `Subject: Docksentry`, so a relay
    # or a mail rule had nothing to sort on — @NotRetarded hit exactly
    # that with Mailrise (#63), and only the update notifications
    # carried a real title. Driven through the real `send_message`
    # against a stand-in server, so what is read is the header that
    # actually went on the wire.
    sent = _capture("💥 *gitlab* exited with code 137.\n\nHost: 7.2 GiB",
                    bot_label="")
    checks["the subject names the container"] = "gitlab" in sent["Subject"]
    checks["…and what happened to it"] = "exited" in sent["Subject"]
    checks["…and still says who is writing"] = (
        sent["Subject"].startswith("Docksentry"))
    # The emoji is noise in a subject column and some relays mangle it;
    # it stays in the body, which is where it reads as a status.
    checks["…without the headline's emoji"] = "💥" not in sent["Subject"]
    checks["…while the body keeps it"] = "💥" in sent["body"]

    # Two alerts about different containers must not share a subject,
    # or sorting on it buys nothing.
    _a = _capture("💥 *gitlab* exited with code 137.", bot_label="")
    _b = _capture("🧠 *db* was OOM-killed (exit code 137).", bot_label="")
    checks["two different alerts get two different subjects"] = (
        _a["Subject"] != _b["Subject"])

    # A subject has to exist and has to fit. An alert with nothing
    # usable in its first line falls back rather than sending an empty
    # one, and a very long headline is cut.
    checks["an empty alert still has a subject"] = (
        _capture("   \n\n  ", bot_label="")["Subject"] == "Docksentry")
    _long = _capture("💥 " + "y" * 300, bot_label="")["Subject"]
    checks["…and a very long one is cut, not sent whole"] = (
        len(_long) <= smtp_mod.MAX_SUBJECT and _long.endswith("…"))

    # `SMTP_SUBJECT_PREFIX` still goes in front, because that is what
    # people already filter on.
    checks["the configured prefix still comes first"] = (
        _capture("💥 *gitlab* exited.", bot_label="prod")["Subject"]
        .startswith("[prod] Docksentry"))

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
