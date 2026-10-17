#!/usr/bin/env python3
"""Who did what, through which front end — and no secrets in the file.

The last open item on the v2.1 axis. Docksentry can be driven from four
places and none of them left a trace that survived a restart, so "someone
stopped the database last night" was unanswerable.

Two properties carry the whole design, and both are asserted here rather
than assumed.

**One seam per front end.** There are 31 state-changing HTTP endpoints and
35 Discord commands — counted on 16.10.2026, after an audit pointed out
that the 26 and 19 standing here were from the v2.1 era and had been
quoted ever since. Instrumenting them individually is not merely tedious:
the next one gets added without a line and nobody notices, and a gap in an
audit log is worse than no log, because a missing entry reads as evidence
that nothing happened. So the recording sits in `do_POST` before the path
dispatch, in `_handle_message` after the auth check, at the top of
Discord's `_dispatch`, and — since 11.10.2026 — in `_handle_callback`,
which is Telegram's second entry path and had none.

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

from audit import AuditLog, redact, MAX_ENTRIES, MAX_VALUE


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
        # Its four siblings on the same form were all caught and this
        # one matched no hint, so it went into the file in the clear.
        "apprise_urls": ["pover://user@apptok,mailto://u:pw@smtp.example"],
        "telegram_allowed_users": ["1,2"],
        "language": ["de"],
        "cron_schedule": ["0 18 * * *"],
    }
    out = redact(secret_form)
    leaked = [k for k, v in out.items()
              if any(bad in str(v) for bad in
                     ("hunter2", "TOKEN", "abc", "AAExample", "xyz",
                      "tk_1", "apptok", "u:pw@"))]
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
    # Driven, both of them. What stood here was `"self._audit_post(path)"
    # in web` plus a `.count()` — and an audit commented the call out
    # with the text left standing: all four checks green, both seams
    # dead. Exactly the defect this file's own comment above claims to
    # have fixed, fixed for Telegram only.
    _web = _web_entries("/api/pin", {"name": ["gitlab"]})
    checks["a Web UI POST leaves a trace"] = len(_web) == 1
    checks["…as the web, naming the endpoint and its target"] = (
        bool(_web) and _web[0][0] == "web" and _web[0][2] == "/api/pin"
        and _web[0][3] == "gitlab")
    # Before the dispatch, so the 27th endpoint cannot be added without
    # one. The point is not the count in the source, it is that an
    # endpoint nobody instrumented is recorded anyway.
    _unknown = _web_entries("/api/something-nobody-instrumented", {})
    checks["…including an endpoint nobody thought about"] = (
        len(_unknown) == 1)
    # A GET is not a state change and must not fill the log.
    checks["…while a read leaves none"] = _web_entries("/status", {},
                                                       method="GET") == []

    # Telegram's buttons, which had no seam at all: `restore_go:`
    # overwrites every setting and `stop_go:` stops a container, and
    # neither left a trace while the typed command that only *asked*
    # did.
    _btn = _callback_entries("restore_go:abc123")
    checks["a button press leaves a trace"] = len(_btn) == 1
    checks["…naming the button, not the one-time token"] = (
        bool(_btn) and _btn[0][2] == "button:restore_go"
        and "abc123" not in str(_btn[0]))
    checks["…and the person who pressed it"] = (
        bool(_btn) and _btn[0][1] == "4711")
    # After the auth check, like the command seam: a press from a
    # stranger is not something this instance did.
    checks["…while a press from a stranger is not recorded"] = (
        _callback_entries("stop_go:abc", authorised=False) == [])

    _dc = _discord_entries("stop", "gitlab")
    checks["a Discord command leaves a trace"] = len(_dc) == 1
    checks["…as discord, naming the command"] = (
        bool(_dc) and _dc[0][0] == "discord" and "stop" in _dc[0][2])

    web = open(os.path.join(root, "web_ui.py"), encoding="utf-8").read()
    tg = open(os.path.join(root, "telegram_bot.py"), encoding="utf-8").read()
    # What the design is about is not how often `audit.record` appears —
    # that was the check, and it broke the moment a legitimate third
    # seam was added — but that a handler nobody instrumented is
    # recorded anyway. Driven: two commands that share nothing, and one
    # the program does not have.
    checks["every command goes through the one seam"] = all(
        len(_telegram_entry(cmd)[0]) == 1
        for cmd in ("/help", "/status", "/notacommand"))
    # The shim that lets the handlers keep reading the body after the
    # seam has consumed it — driven, because a POST that records and
    # then loses its own body would pass everything above.
    checks["the body is replayed to the handlers"] = _body_survives()

    # ── the scheduler leaves a trail too ─────────────────────────────
    # `web_audit_intro` says the log covers "the Web UI, Telegram,
    # Discord and the scheduler", and `record` documents the source
    # `"schedule"` with the actor `"system"`. Every call site in the
    # program was a front end — so the unattended update, the only kind
    # nobody watched happen, was the one kind that left no record.
    _ups = [{"name": "web", "image": "nginx:1.25", "host": "srv30"},
            {"name": "db", "image": "postgres:16", "host": "srv30"}]
    _sched = _scheduler_entries(_ups)
    checks["an unattended update is recorded"] = bool(_sched)
    checks["…as the scheduler, not as a person"] = (
        bool(_sched) and _sched[0][1] == "system")
    # Named rather than counted: "2 containers" answers nothing anyone
    # asks of an audit log a week later.
    checks["…naming the containers it touched"] = (
        bool(_sched) and "web" in _sched[0][3] and "db" in _sched[0][3])
    checks["…with the host, since two machines may run the same name"] = (
        bool(_sched) and "srv30" in _sched[0][3])
    # …and a tick with nothing to do writes nothing, or the log fills
    # with entries that say an hour passed.
    checks["a tick with nothing to update writes nothing"] = (
        _scheduler_entries([]) == [])

    # ── the Telegram seam, driven ────────────────────────────────────
    # What stood here was `tg.count('audit.record("telegram"') == 1`:
    # a count of occurrences in the source. A commented-out call
    # satisfies that, and so does a call sitting in a branch nothing
    # reaches — which is exactly how `/hosts check` was dead on both
    # connections with three checks green. So the command goes through
    # `_handle_message` and the entry is read back.
    _entries, _ok, _crash = _telegram_entry("/stop portainer",
                                            user_id="4711")
    checks["a Telegram command is recorded"] = len(_entries) == 1
    checks["…as telegram, by the user who sent it"] = (
        bool(_entries) and _entries[0][0] == "telegram"
        and _entries[0][1] == "4711")
    checks["…naming the command and its target"] = (
        bool(_entries) and _entries[0][2] == "/stop"
        and _entries[0][3] == "portainer")
    # After the auth check, not before: a command from a stranger is not
    # something this instance did, and a log that records refused
    # attempts as actions reads as if they happened.
    checks["a command from a stranger is not recorded as an action"] = (
        _telegram_entry("/stop portainer", authorised=False)[0] == [])
    # An ordinary chat message is not a command and leaves no entry, or
    # the log fills with conversation.
    checks["plain chat text leaves no entry"] = (
        _telegram_entry("good morning")[0] == [])
    # …and the dispatch still happens. A seam that records and then
    # swallows the command would pass everything above. `/help` is the
    # one command that needs nothing but the table it prints, so this
    # is the counter-direction without six stubs holding it up: a
    # `/stop` fixture needed the engine, the hosts and the lifecycle
    # module before it reached anything observable, and a prop that
    # deep measures its own scaffold.
    _h_entries, _h_answered, _h_crash = _telegram_entry("/help")
    checks["…and a command recorded is still a command answered"] = (
        _h_answered and _h_crash == [])
    checks["…and that one is recorded too"] = (
        len(_h_entries) == 1 and _h_entries[0][2] == "/help")

    # ── who the actor is when a token did it ─────────────────────────
    # `record` documents the actor as "user id, token name, or system".
    # A Prometheus scrape and a person clicking in the browser are not
    # the same event, and an unattributed API call is the one entry
    # somebody will ask about.
    checks["a token is recorded by its name, not as a person"] = (
        _web_actor(token="prom") == ("token:prom", "api"))
    checks["…and a signed-in person by their user name"] = (
        _web_actor(basic=("andreas", "pw")) == ("andreas", "web"))
    checks["…and the two are told apart by the source"] = (
        _web_actor(token="prom")[1] != _web_actor(basic=("andreas", "pw"))[1])

    # ── the history page shows it ────────────────────────────────────
    # A record nobody can read is a file, not a trail. The page is
    # rendered and the bytes are read back.
    _html = _history_html([
        {"timestamp": "2026-10-11 09:00:00", "source": "web",
         "actor": "andreas", "action": "/api/pin", "target": "gitlab",
         "detail": {"web_password": "<redacted:9>"}},
    ])
    checks["the history page lists the entry"] = (
        "/api/pin" in _html and "andreas" in _html)
    checks["…with the time and the target"] = (
        "2026-10-11 09:00:00" in _html and "gitlab" in _html)
    # The section has to be findable by the word everyone uses: the
    # heading is translated in all 16 languages and none says "audit".
    checks["…and the section can be found by searching for 'Audit'"] = (
        "Audit-Trail" in _html and 'id="audit"' in _html)
    # Nothing on the page is a secret, and nothing is raw HTML either.
    _html2 = _history_html([
        {"timestamp": "t", "source": "web", "actor": "<script>x</script>",
         "action": "/settings", "detail": {"web_password": "<redacted:9>"}},
    ])
    checks["the page escapes what it shows"] = "<script>" not in _html2
    checks["…and shows the redaction marker, never a value"] = (
        "redacted:9" in _html2)

    # ── a body that is not a form is not read as one ────────────────
    # `_audit_post` ran `parse_qs` over whatever arrived, so a JSON or
    # multipart upload came back as one key built out of the file's own
    # bytes with a window of file content as its value — past the
    # redaction, which matches on key NAMES. `/api/backup_import`
    # carries a backup bundle, and a bundle carries every persistent
    # key there is: the Web UI password, the bot token, the SMTP
    # password. What landed in the log depended on where `&` and `=`
    # happened to fall in the file, which is worse than always or never.
    _bundle = json.dumps({"schema_version": 1, "settings": {
        "web_password": "hunter2", "smtp_password": "swordfish",
        "apprise_urls": "pover://user@secrettoken"}})
    _e = _web_json("/api/backup_import", _bundle)
    checks["a JSON upload is not parsed as a form"] = len(_e) == 1
    _blob = json.dumps(_e)
    checks["…so no part of the file reaches the log"] = (
        "hunter2" not in _blob and "swordfish" not in _blob
        and "secrettoken" not in _blob)
    checks["…and what IS recorded says what arrived"] = (
        bool(_e) and "application/json" in _blob and "bytes" in _blob)
    # A real form still gets its fields, or the fix above is just
    # switching the detail off.
    _e = _web_entries("/api/pin", {"name": ["gitlab"], "scope": ["host"]})
    # `record` is handed the parsed form as `parse_qs` returns it —
    # values are lists — and flattens and redacts on the way to the
    # file. What matters here is that the fields arrived at all.
    checks["…while a form body is still recorded field by field"] = (
        bool(_e) and (_e[0][4] or {}).get("scope") == ["host"])

    # ── and the redaction is on the path to the file ────────────────
    # `redact()` itself is checked above, thoroughly. What was not: that
    # `record` calls it. The line `clean = redact(detail)` could be
    # `clean = detail` and nothing went red — the function was guarded
    # and its use was not, which is the same shape as the health
    # outcome and the session clear. Debt entry, now closed.
    log.record("web", "andreas", "/settings", "",
               {"web_password": ["hunter2"],
                "discord_webhook": ["https://discord.com/api/webhooks/1/TK"]})
    _last = json.load(open(path))[-1]
    _written = json.dumps(_last)
    checks["a secret in the form never reaches the file"] = (
        "hunter2" not in _written and "/1/TK" not in _written)
    checks["…while the entry is still written"] = (
        _last["action"] == "/settings")
    checks["…and says a secret was there"] = (
        "redacted" in _written)

    # ── a value is a pointer, not a copy ────────────────────────────
    # `MAX_VALUE` exists because a settings save can carry a 4 kB
    # `EXCLUDE_CONTAINERS` list, and 500 of those is the whole ring
    # buffer spent on one paste.
    _long = redact({"exclude": ["x" * 400]})["exclude"]
    checks["a long value is cut"] = len(_long) <= MAX_VALUE + 1
    checks["…and marked as cut, not silently shortened"] = _long.endswith("…")
    checks["…while a short one is kept whole"] = (
        redact({"exclude": ["nginx,redis"]})["exclude"] == "nginx,redis")
    # It is the stored entry that has to be short, not just `redact`'s
    # return value — the cut has to be on the path to the file.
    log.record("web", "x", "/settings", "", {"exclude": ["y" * 5000]})
    checks["the entry on disk is short too"] = len(
        json.load(open(path))[-1]["detail"]["exclude"]) <= MAX_VALUE + 1

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


def _scheduler_entries(updates):
    """What an unattended update run writes to the audit log.

    Driven through `handle_autoupdates`, because the promise is about
    the scheduler's path and not about a function anyone could call.
    """
    import tempfile
    import threading
    from i18n import get_translator
    from telegram_bot import TelegramBot
    import update_engine as _ue

    written = []

    class _Recorder:
        def record(self, source, actor, action, target="", detail=None):
            written.append((source, actor, action, target, detail))

    class _Cfg:
        language = "en"
        auto_update_all = True
        bot_token = ""
        chat_id = "1"
        pending_file = os.path.join(tempfile.mkdtemp(), "pending.json")

        def __getattr__(self, name):
            return ""

    bot = TelegramBot.__new__(TelegramBot)
    bot.config = _Cfg()
    bot.t = get_translator("en")
    for n in ("send_message", "announce", "log", "_say_once"):
        setattr(bot, n, lambda *a, **kw: None)
    bot.audit = _Recorder()
    bot.hosts = None
    bot._policy_decision = lambda u, c: None
    bot._age_decision = lambda u, c: None
    bot._display_name = lambda u: f"{u['name']} @{u.get('host', 'local')}"
    bot._process_update_batch = lambda ups, checker, auto=False: ([], 0, [])
    bot.engine = types.SimpleNamespace(_update_lock=threading.Lock(),
                                       _queued_selfupdate=None,
                                       _swap_in_flight=False)

    class _Store:
        def get_autoupdate(self):
            return [u["name"] for u in updates]

        def get_update_windows(self):
            return {}

    real = _ue.host_store
    _ue.host_store = lambda scope, host: _Store()
    try:
        bot.handle_autoupdates(list(updates), None)
    finally:
        _ue.host_store = real
    return [e for e in written if e[0] == "schedule"]


def _web_entries(path, params, method="POST"):
    """What one Web UI request writes to the audit log.

    The seam sits in `do_POST` before the path dispatch, so this runs
    `do_POST` for real and reads the recorder back.
    """
    import web_ui
    from urllib.parse import urlencode

    written = []

    class _Recorder:
        def record(self, source, actor, action, target="", detail=None):
            written.append((source, actor, action, target, detail))

    class _Cfg:
        _CALLABLE = ("env_override", "save", "load", "apply", "write",
                     "set_", "has_", "reload")
        language = "en"

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            if name.startswith(self._CALLABLE):
                return lambda *a, **kw: None
            return ""

    cls = web_ui.create_handler(_Cfg(), checker=None,
                                bot=types.SimpleNamespace(t=None), store=None,
                                restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    body = urlencode(params, doseq=True)
    h.path = path
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x",
                 "Content-Type": "application/x-www-form-urlencoded"}
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode()[:n])
    h.client_address = ("127.0.0.1", 0)
    h.server = types.SimpleNamespace(audit=_Recorder())
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._send_redirect = lambda loc: None
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.send_error = lambda *a, **k: None
    h.end_headers = lambda: None
    h.wfile = types.SimpleNamespace(write=lambda b: None)
    h._send_html = lambda *a, **k: None
    try:
        (h.do_POST if method == "POST" else h.do_GET)()
    except Exception:                                       # noqa: BLE001
        # The endpoint itself may well fall over on a stand-in store;
        # the seam runs before the dispatch, so the entry is already
        # written and that is what is being read.
        pass
    return [e for e in written if e[0] in ("web", "api")]


def _web_json(path, body, content_type="application/json"):
    """One non-form POST through the Web seam; what was recorded?"""
    import web_ui

    written = []

    class _Recorder:
        def record(self, source, actor, action, target="", detail=None):
            written.append((source, actor, action, target, detail))

    class _Cfg:
        _CALLABLE = ("env_override", "save", "load", "apply", "write",
                     "set_", "has_", "reload")
        language = "en"

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            if name.startswith(self._CALLABLE):
                return lambda *a, **kw: None
            return ""

    cls = web_ui.create_handler(_Cfg(), checker=None,
                                bot=types.SimpleNamespace(t=None), store=None,
                                restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    raw = body.encode() if isinstance(body, str) else body
    h.path = path
    h.headers = {"Content-Length": str(len(raw)), "Host": "x",
                 "Origin": "http://x", "Content-Type": content_type}
    h.rfile = types.SimpleNamespace(read=lambda n: raw[:n])
    h.client_address = ("127.0.0.1", 0)
    h.server = types.SimpleNamespace(audit=_Recorder())
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._send_redirect = lambda loc: None
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.send_error = lambda *a, **k: None
    h.end_headers = lambda: None
    h.wfile = types.SimpleNamespace(write=lambda b: None)
    h._send_html = lambda *a, **k: None
    try:
        h.do_POST()
    except Exception:                                       # noqa: BLE001
        pass
    return [e for e in written if e[0] in ("web", "api")]


def _discord_entries(command, target=""):
    """What one Discord slash command writes."""
    from discord_bot import DiscordBot

    written = []

    class _Recorder:
        def record(self, source, actor, action, target="", detail=None):
            written.append((source, actor, action, target, detail))

    bot = DiscordBot.__new__(DiscordBot)
    bot.config = types.SimpleNamespace(language="en", container_cli="docker",
                                       discord_guild_id="")
    bot.audit = _Recorder()
    bot.log = lambda *a, **kw: None
    bot.store = None
    bot.application_id, bot.rest = "", None
    data = {"data": {"name": command,
                     "options": ([{"name": "name", "value": target}]
                                 if target else [])},
            "member": {"user": {"id": "77", "username": "leenx"}},
            "guild_id": ""}
    try:
        bot._dispatch(data)
    except Exception:                                       # noqa: BLE001
        pass
    return [e for e in written if e[0] == "discord"]


def _body_survives():
    """Does a handler still see its body after the seam consumed it?"""
    import web_ui

    read_back = []

    class _Cfg:
        _CALLABLE = ("env_override", "save", "load", "apply", "write",
                     "set_", "has_", "reload")
        language = "en"

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            if name.startswith(self._CALLABLE):
                return lambda *a, **kw: None
            return ""

    cls = web_ui.create_handler(_Cfg(), checker=None,
                                bot=types.SimpleNamespace(t=None), store=None,
                                restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    body = "name=gitlab&settings_page=1"
    h.path = "/api/pin"
    h.headers = {"Content-Length": str(len(body)), "Host": "x",
                 "Origin": "http://x",
                 "Content-Type": "application/x-www-form-urlencoded"}
    h.rfile = types.SimpleNamespace(read=lambda n: body.encode()[:n])
    h.client_address = ("127.0.0.1", 0)
    h.server = types.SimpleNamespace(
        audit=types.SimpleNamespace(record=lambda *a, **kw: None))
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._send_redirect = lambda loc: None
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.send_error = lambda *a, **k: None
    h.end_headers = lambda: None
    h.wfile = types.SimpleNamespace(write=lambda b: None)
    # Whatever the handler reads from `rfile` after the seam ran.
    _orig_post = cls.do_POST

    def _spy(self):
        try:
            _orig_post(self)
        finally:
            read_back.append(self.rfile.read(1024))

    try:
        _spy(h)
    except Exception:                                       # noqa: BLE001
        pass
    return bool(read_back) and b"gitlab" in (read_back[0] or b"")


def _callback_entries(data, user_id="4711", authorised=True):
    """What one Telegram button press writes to the audit log."""
    from i18n import get_translator
    from telegram_bot import TelegramBot

    written = []

    class _Recorder:
        def record(self, source, actor, action, target="", detail=None):
            written.append((source, actor, action, target, detail))

    class _Cfg:
        language = "en"
        bot_token = ""
        chat_id = "1"

        def __getattr__(self, name):
            return ""

    bot = TelegramBot.__new__(TelegramBot)
    bot.config = _Cfg()
    bot.t = get_translator("en")
    bot.audit = _Recorder()
    bot.store = None
    bot._check_auth = lambda chat_id, uid, kind="message": authorised
    for n in ("send_message", "log", "answer_callback", "remove_buttons"):
        setattr(bot, n, lambda *a, **kw: None)
    try:
        bot._handle_callback({"id": "c1", "data": data,
                              "from": {"id": user_id},
                              "message": {"message_id": 5,
                                          "chat": {"id": 1}}}, None)
    except Exception:                                       # noqa: BLE001
        # The branch behind the button may well need more than a
        # stand-in; the seam runs before the dispatch.
        pass
    return [e for e in written if e[0] == "telegram"]


def _telegram_entry(text, user_id="4711", authorised=True):
    """Send one message through `_handle_message`; what was recorded?

    The seam is what is measured, so `_handle_message` runs for real.
    Only the environment is stood in for: the auth answer, the command
    dispatch, and sending.
    """
    from i18n import get_translator
    from telegram_bot import TelegramBot

    written, ran = [], []

    class _Recorder:
        def record(self, source, actor, action, target="", detail=None):
            written.append((source, actor, action, target, detail))

    class _Cfg:
        language = "en"
        bot_token = ""
        chat_id = "1"

        def __getattr__(self, name):
            return ""

    bot = TelegramBot.__new__(TelegramBot)
    bot.config = _Cfg()
    bot.t = get_translator("en")
    bot.audit = _Recorder()
    bot._check_auth = lambda chat_id, uid, kind="message": authorised
    bot._help_alias = lambda t: t
    bot.bot_username = ""
    bot.hosts = None                     # single-host install
    bot.send_message = lambda *a, **kw: ran.append(a[0] if a else "")
    for n in ("log", "announce", "_say_once"):
        setattr(bot, n, lambda *a, **kw: None)
    try:
        bot._handle_message({"text": text, "from": {"id": user_id},
                             "chat": {"id": 1}}, None, None)
    except Exception as e:                                  # noqa: BLE001
        written.append(("raised", type(e).__name__, str(e), "", None))
    return ([e for e in written if e[0] == "telegram"], bool(ran),
            [e for e in written if e[0] == "raised"])


def _web_actor(token=None, basic=None):
    """Who the Web UI thinks is calling, and through which front end."""
    import web_ui

    class _Cfg:
        language = "en"
        api_tokens = ["prom:secrettoken"]

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            if name.startswith(("env_override", "save", "load", "apply")):
                return lambda *a, **kw: None
            return ""

    cls = web_ui.create_handler(_Cfg(), checker=None,
                                bot=types.SimpleNamespace(t=None), store=None,
                                restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    headers = {}
    if token:
        headers["Authorization"] = "Bearer secrettoken"
    if basic:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"{basic[0]}:{basic[1]}".encode()).decode()
    h.headers = headers
    h.path = "/api/pin"
    h.client_address = ("127.0.0.1", 0)
    return h._audit_actor()


def _history_html(rows):
    """The /history page, rendered over a given set of entries."""
    import web_ui

    class _Cfg:
        language = "en"

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            if name.startswith(("env_override", "save", "load", "apply")):
                return lambda *a, **kw: None
            return ""

    sent = []

    class _Audit:
        def entries(self, n=100):
            return rows

    cls = web_ui.create_handler(_Cfg(), checker=None,
                                bot=types.SimpleNamespace(t=None), store=None,
                                restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    h.server = types.SimpleNamespace(audit=_Audit(), monitor=None, events=None)
    h.headers, h.path = {}, "/history"
    h.client_address = ("127.0.0.1", 0)
    h._send_html = lambda html, code=200: sent.append(html)
    h._render_page = lambda content, active: content
    try:
        h._page_history()
    except Exception as e:                                  # noqa: BLE001
        return f"raised {type(e).__name__}: {e}"
    return "\n".join(sent)


def _record_bare(path):
    """An entry with no target, to check the key is omitted rather than empty."""
    log = AuditLog(types.SimpleNamespace(audit_file=path + ".bare"))
    log.record("web", "x", "/api/cleanup")
    return json.load(open(path + ".bare"))[-1]


if __name__ == "__main__":
    sys.exit(main())
