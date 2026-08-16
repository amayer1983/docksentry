#!/usr/bin/env python3
"""Why the bot went quiet, and how it says so now (#2, @famewolf).

He upgraded, and his bots stopped answering. The log was clean: listener
started, 27 commands registered, bot identified, "Waiting for Telegram
messages…". Everything looked healthy. Commands went nowhere.

**The cause**, once he turned debug on, was in his log four times over:

    Telegram API 400: Bad Request: group chat was upgraded to a
    supergroup chat

His group had been converted to a supergroup, which changes its id from
`-52…` to `-100…`. Both directions break at once, which is why it looked
so total: sends are refused, and incoming messages now carry the new id,
so they no longer match `CHAT_ID` and are dropped as unauthorised.

And Telegram had been telling us the new id the whole time. The 400 body
carries `parameters.migrate_to_chat_id`; we printed the description and
threw the parameters away. The answer was inside the error message we
were already showing him.

So: follow it. Learn the new id, resend what was dropped, accept commands
from it, and say — in the log and once in the chat — what to change so it
does not have to be rediscovered on every restart. Deliberately **not**
written to settings.json: a saved value outranks the environment, so
persisting it would swap this problem for the one where a corrected
CHAT_ID in the compose file is silently ignored (#53, and again with
WEB_PASSWORD in his own log).

**And the silence itself was a defect.** Both rejections were mute unless
`DEBUG=true`, which produces exactly his dead end: the bot announces
itself, registers its command list so the `/` picker even offers the
commands, and then nothing happens, with no error anywhere. There is no
thread to pull unless you already suspect the setting. It says why now —
once per reason per boot, because the silence was not arbitrary either:
in a shared group, messages from people off the allow-list are ordinary
and a line each would bury the log.

Log only, never a reply into the chat. Answering an unauthorised chat
would confirm the bot exists and name the machine it watches, and
refusing quietly is exactly what that check is for.
"""

import io
import json
import os
import sys
import types
import urllib.error
import urllib.parse
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import telegram_bot  # noqa: E402

checks = {}

MIGRATED = {
    "ok": False, "error_code": 400,
    "description": "Bad Request: group chat was upgraded to a supergroup chat",
    "parameters": {"migrate_to_chat_id": -1005212345678},
}


def bot(chat_id="-52123456", allowed=None, debug=False):
    b = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    b.config = types.SimpleNamespace(
        chat_id=chat_id, telegram_allowed_users=allowed or [], debug=debug,
        bot_token="123:abc", bot_label="", telegram_topic_id=None)
    b.calls = []
    return b


def auth(b, chat, user):
    out = io.StringIO()
    with redirect_stdout(out):
        ok = b._check_auth(chat, user)
    return ok, out.getvalue()


def offline(b):
    """No network: every api_call is recorded and answers ok."""
    def fake(method, data=None, **kw):
        b.calls.append((method, dict(data or {})))
        return {"ok": True}
    b.api_call = fake


# ═══ the group that changed its id ═══════════════════════════════════
# A send that fails with the migration error is retried against the new
# chat, so the message the user was waiting for still arrives.
class Net:
    """Telegram, for one bot, before and after the rename.

    Patched in at the socket end (`urlopen`) rather than over `api_call`,
    so the retry, the id-swapping seam and the error parsing being tested
    are the real ones. A fake sitting on top of `api_call` would have
    proved only that the fake works.
    """

    def __init__(self, old, new):
        self.old, self.new, self.seen = str(old), str(new), []

    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        body = req.data.decode() if req.data else ""
        data = dict(urllib.parse.parse_qsl(body))
        self.seen.append((method, data))
        if str(data.get("chat_id", "")) == self.old:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(json.dumps(MIGRATED).encode()))
        return Resp({"ok": True, "result": {"message_id": 7}})


class Resp:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def wire(net):
    telegram_bot.urllib.request.urlopen = net


b = bot()
net = Net(old="-52123456", new="-1005212345678")
wire(net)
out = io.StringIO()
with redirect_stdout(out):
    res = b.send_message("Update available: nginx")
log = out.getvalue()

checks["a send into a migrated group is not lost"] = bool(res and res.get("ok"))
checks["…it is retried against the new id"] = any(
    str(d.get("chat_id")) == "-1005212345678" for _, d in net.seen)
checks["…and the old id was tried first, i.e. nothing was guessed"] = (
    str(net.seen[0][1].get("chat_id")) == "-52123456")
checks["the log names the new id, not just the error"] = (
    "-1005212345678" in log and "supergroup" in log)
checks["…and says which setting to change"] = "CHAT_ID" in log

# The next message carries the notice into the chat, where he is looking.
with redirect_stdout(io.StringIO()):
    b.send_message("Update available: sonarr")
notice = [d for _, d in net.seen if "supergroup" in d.get("text", "")]
checks["the chat is told once, on the next message"] = len(notice) == 1
checks["…naming both the dead id and the live one"] = (
    "-52123456" in notice[0]["text"] and "-1005212345678" in notice[0]["text"])
checks["…without swallowing the message it rode along with"] = (
    "sonarr" in notice[0]["text"])
with redirect_stdout(io.StringIO()):
    b.send_message("Update available: radarr")
checks["…and never again"] = len(
    [d for _, d in net.seen if "supergroup" in d.get("text", "")]) == 1

# Followed once. `api_call` retries on the back of this, so a body that
# keeps naming new ids would recurse; a supergroup cannot be upgraded
# again, so a second one means something we do not understand.
again = dict(MIGRATED, parameters={"migrate_to_chat_id": -1009999999999})
with redirect_stdout(io.StringIO()):
    checks["a second migration is not followed"] = (
        b._note_migration(again) is None)
checks["…and the first one still stands"] = (
    b._migrated_chat_id == "-1005212345678")

# It is not persisted — that would make a corrected CHAT_ID unusable.
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "telegram_bot.py"), encoding="utf-8").read()
i = src.index("def _note_migration")
note = src[i:src.index("\n    def ", i + 10)]
checks["the new id is not written to persistent settings"] = (
    "save_persistent" not in note and "settings" not in note.split('"""')[2])

# ── and commands from the new id are accepted ───────────────────────
b = bot()
b._migrated_chat_id = "-1005212345678"
offline(b)
ok, _ = auth(b, "-1005212345678", "4242")
checks["commands from the migrated group are accepted"] = ok is True
ok, _ = auth(b, "-52123456", "4242")
checks["…and the dead id no longer counts as the chat"] = ok is False

# A listen-only bot never sends, so it would never meet the 400. It asks.
b = bot()
probes = []


def probing(method, data=None, **kw):
    probes.append(method)
    if method == "getChat":
        b._note_migration(MIGRATED)
    return {"ok": True}


b.api_call = probing
ok, log = auth(b, "-1005212345678", "4242")
checks["a rejection asks Telegram whether the chat merely moved"] = (
    probes == ["getChat"])
checks["…and the command is then honoured, not dropped"] = ok is True
b2 = bot()
b2.api_call = probing
probes.clear()
auth(b2, "-999", "4242")
auth(b2, "-999", "4242")
auth(b2, "-888", "4242")
checks["…asked once per boot, not once per stray message"] = len(probes) == 1

# ═══ and when it really is misconfigured, it says so ═════════════════
b = bot()
offline(b)
ok, log = auth(b, "-999", "4242")
checks["a command from the wrong chat is still refused"] = ok is False
checks["…and now says why, without DEBUG"] = "does not match" in log
checks["…naming both ids, so it can be acted on"] = (
    "-999" in log and "-52123456" in log)

_, log2 = auth(b, "-999", "4242")
_, log3 = auth(b, "-888", "77")
checks["…but only once per boot"] = log2 == "" and log3 == ""

# ── the allow-list too ───────────────────────────────────────────────
b = bot(allowed=["1111"])
offline(b)
ok, log = auth(b, "-52123456", "4242")
checks["a user off the allow-list is still refused"] = ok is False
checks["…and says why"] = "TELEGRAM_ALLOWED_USERS" in log
checks["…naming the id that was refused"] = "4242" in log
checks["…and warns that a saved value beats the compose file"] = (
    "settings.json" in log)
_, log2 = auth(b, "-52123456", "5555")
checks["…once, not per stranger"] = log2 == ""

# The two reasons are counted apart: hitting one must not mask the other.
b = bot(allowed=["1111"])
offline(b)
_, first = auth(b, "-999", "4242")          # wrong chat
_, second = auth(b, "-52123456", "4242")    # right chat, wrong user
checks["the two reasons are reported independently"] = (
    "does not match" in first and "TELEGRAM_ALLOWED_USERS" in second)

# ── what must NOT change ─────────────────────────────────────────────
b = bot(allowed=["4242"])
offline(b)
ok, log = auth(b, "-52123456", "4242")
checks["a legitimate command still passes"] = ok is True
checks["…and says nothing at all"] = log == ""
checks["…and costs no API call"] = b.calls == []

b = bot()          # no allow-list configured
offline(b)
ok, _ = auth(b, "-52123456", "9999")
checks["an empty allow-list still means everyone in the chat"] = ok is True

# DEBUG keeps its detailed per-message line — the diagnostic is a
# superset of the new notice, not a replacement for it.
b = bot(allowed=["1111"], debug=True)
offline(b)
_, log = auth(b, "-52123456", "4242")
checks["DEBUG still prints its per-message detail"] = "Auth fail" in log

# ── it stays in the log, never in the chat ───────────────────────────
i = src.index("def _warn_rejected_once")
warn = src[i:src.index("\n    def ", i + 10)]
checks["the rejection notice is printed, not sent to the chat"] = (
    "print(" in warn and "send_message" not in warn)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
