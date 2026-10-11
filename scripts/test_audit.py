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
    # Two, and both on purpose: the command seam, and the scheduler —
    # which the Connections page has always promised and which nothing
    # wrote until 10.10.2026. Counting call sites was the wrong
    # question anyway; what the design is about is that the COMMAND
    # path has one seam rather than one per handler.
    checks["telegram records commands in one place"] = (
        tg.count('audit.record("telegram"') == 1)
    checks["…and the scheduler writes its own, separately"] = (
        tg.count('audit.record(\n') + tg.count('_audit.record(') >= 1
        and '"schedule", "system"' in tg)
    dc = open(os.path.join(root, "discord_bot.py"), encoding="utf-8").read()
    checks["discord records in one place"] = dc.count('audit.record(') == 1
    # And the shim that lets the 26 handlers keep reading the body.
    checks["the body is replayed to the handlers"] = "_ReplayedBody" in web

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
