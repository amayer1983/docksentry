#!/usr/bin/env python3
"""Who did what, through which front end — and no secrets in the file.

The last open item on the v2.1 axis. Docksentry can be driven from four
places and none of them left a trace that survived a restart, so "someone
stopped the database last night" was unanswerable.

Two properties carry the whole design, and both are asserted here rather
than assumed.

**One seam per front end.** There are 26 state-changing HTTP endpoints and
19 Discord commands. Instrumenting them individually is not merely tedious
— the 27th gets added without a line and nobody notices, and a gap in an
audit log is worse than no log, because a missing entry reads as evidence
that nothing happened. So the recording sits in `do_POST` before the path
dispatch, in `_handle_message` after the auth check, and at the top of
Discord's `_dispatch`.

**No secrets on disk.** A settings save carries the Web UI password, the
bot token and both webhook URLs in its body. Writing those to a plaintext
file next to the config would hand anyone who can read `/data` the lot.
Measured against the running instance before this was written:

    web_password    -> <redacted:9>
    discord_webhook -> <redacted:48>

Redaction is a key denylist and errs towards hiding: an unrecognised key
whose *name* suggests a secret is redacted rather than kept.

No hash chain, deliberately. It would prove nothing here — the file, the
algorithm and the absence of a key are all on the same box, so anyone able
to edit an entry can recompute the chain. Tamper *evidence* needs an
anchor outside the machine (syslog, a webhook); a self-contained chain
would only look like security.
"""

import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from audit import AuditLog, redact, MAX_ENTRIES


def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "audit.json")
    log = AuditLog(types.SimpleNamespace(audit_file=path))
    checks = {}

    # ── nothing secret reaches the file ──────────────────────────
    secret_form = {
        "web_password": ["hunter2"],
        "discord_webhook": ["https://discord.com/api/webhooks/1/TOKEN"],
        "webhook_url": ["https://example.com/hook?key=abc"],
        "bot_token": ["123:AAExample"],
        "api_tokens": ["prom:xyz"],
        "smtp_password": ["pw"],
        "ntfy_token": ["tk_1"],
        "telegram_allowed_users": ["1,2"],
        "language": ["de"],
        "cron_schedule": ["0 18 * * *"],
    }
    out = redact(secret_form)
    leaked = [k for k, v in out.items()
              if any(bad in str(v) for bad in
                     ("hunter2", "TOKEN", "abc", "AAExample", "xyz", "tk_1"))]
    checks["no secret value survives redaction"] = not leaked
    if leaked:
        print(f"    leaked: {leaked}")
    # The ordinary fields must still be readable, or the log says nothing.
    checks["harmless fields are kept"] = (
        out["language"] == "de" and out["cron_schedule"] == "0 18 * * *")
    # Length is safe and occasionally the answer ("they saved an empty one").
    checks["redaction keeps the length"] = out["web_password"] == "<redacted:7>"
    checks["an empty secret is marked empty"] = (
        redact({"web_password": [""]})["web_password"] == "<empty>")
    # A key we have never seen, whose NAME suggests a secret.
    checks["an unknown secret-looking key is hidden"] = (
        "sesame" not in str(redact({"future_api_key": ["sesame"]})))

    # ── it actually writes, and reads back ───────────────────────
    log.record("web", "andreas", "/api/pin", "gitlab", {"name": ["gitlab"]})
    log.record("telegram", "4711", "/stop", "db", {"args": "db"})
    log.record("discord", "leenx", "/update", "web", None)
    log.record("schedule", "system", "auto-update", "nginx", None)
    saved = json.load(open(path))
    checks["every entry is written"] = len(saved) == 4
    checks["the front end is recorded"] = (
        [e["source"] for e in saved] == ["web", "telegram", "discord", "schedule"])
    checks["the actor is recorded"] = saved[0]["actor"] == "andreas"
    checks["the target is recorded"] = saved[0]["target"] == "gitlab"
    checks["newest first when read back"] = (
        log.entries(10)[0]["action"] == "auto-update")

    # An action with nothing to add carries no empty keys — a log full of
    # `"detail": {}` is noise in every reader that touches it.
    checks["no empty detail key"] = "detail" not in saved[2]
    checks["no empty target key"] = "target" not in json.loads(
        json.dumps(_record_bare(path)))

    # ── it cannot grow without bound ─────────────────────────────
    for i in range(MAX_ENTRIES + 40):
        log.record("web", "x", f"/api/{i}")
    saved = json.load(open(path))
    checks["the log is capped"] = len(saved) == MAX_ENTRIES
    checks["the cap drops the OLDEST"] = saved[-1]["action"].endswith(
        str(MAX_ENTRIES + 39))

    # ── it never takes the application down with it ──────────────
    # An audit trail that can cause an outage is its own worst failure.
    broken = AuditLog(types.SimpleNamespace(audit_file="/proc/nonexistent/x"))
    try:
        broken.record("web", "x", "/api/pin")
        checks["an unwritable path is survived"] = True
    except Exception:
        checks["an unwritable path is survived"] = False
    with open(path, "w") as f:
        f.write("{ this is not json")
    try:
        log.record("web", "x", "/api/pin")
        checks["a corrupt file is survived"] = json.load(open(path))[-1][
            "action"] == "/api/pin"
    except Exception:
        checks["a corrupt file is survived"] = False
    # Not configured at all: no file, no crash, no silent exception storm.
    off = AuditLog(types.SimpleNamespace())
    off.record("web", "x", "/api/pin")
    checks["an unconfigured log is a no-op"] = off.entries() == []

    # ── one seam per front end, not one per endpoint ─────────────
    root = os.path.join(os.path.dirname(__file__), "..", "app")
    web = open(os.path.join(root, "web_ui.py"), encoding="utf-8").read()
    checks["the web seam is in do_POST"] = (
        "self._audit_post(path)" in web
        and web.index("def do_POST") < web.index("self._audit_post(path)"))
    # Exactly one call site. More than one means someone started
    # instrumenting endpoints individually, which is the failure this
    # design exists to prevent.
    checks["the web records in exactly one place"] = (
        web.count("self._audit_post(") == 1)
    tg = open(os.path.join(root, "telegram_bot.py"), encoding="utf-8").read()
    checks["telegram records in one place"] = tg.count('audit.record(') == 1
    dc = open(os.path.join(root, "discord_bot.py"), encoding="utf-8").read()
    checks["discord records in one place"] = dc.count('audit.record(') == 1
    # And the shim that lets the 26 handlers keep reading the body.
    checks["the body is replayed to the handlers"] = "_ReplayedBody" in web

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


def _record_bare(path):
    """An entry with no target, to check the key is omitted rather than empty."""
    log = AuditLog(types.SimpleNamespace(audit_file=path + ".bare"))
    log.record("web", "x", "/api/cleanup")
    return json.load(open(path + ".bare"))[-1]


if __name__ == "__main__":
    sys.exit(main())
