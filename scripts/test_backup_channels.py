#!/usr/bin/env python3
"""Getting a backup out, and back in, without a browser (#2).

@famewolf asked for `/backup` on Telegram and, once it worked, said the
thing that explains why it matters: *"No more having to jump from webui
to webui."* Three hosts, one command, three files that say which is
which. Four hours later @NotRetarded asked why Discord did not have it,
and then asked for the other half:

    I'd love to see if it's possible to perform a /restore for Telegram
    by attaching that file from the backup. That will keep you out of
    the GUI even for restores.

Which is the case that actually counts. The day you need a restore is
the day the Web UI is the thing you cannot reach.

Three decisions worth pinning:

**One restore, two callers.** The apply logic moved out of the Web UI's
import endpoint into `backup.restore` rather than being written a second
time for the chat. A second implementation starts identical and quietly
stops being — and this one carries the security rules: settings through
the PERSISTENT_KEYS allow-list so a bundle cannot inject attributes, and
links through the same validator the live path uses, because a backup is
a file and nothing about "the user picked it" says the user wrote it.

**A file arriving is not a decision.** Dropping a backup into the chat
reports what it would restore and hands back a button. The press
restores. Somebody showing somebody else a backup file must not lose
their configuration to it.

**And `.json` keeps its name.** Both of them expected Discord would need
a `.txt` rename or a zip. If it turns out to, the answer is the zip:
renaming a file to hide what it is only moves the problem to whatever
has to open it later.
"""

import io
import json
import os
import sys
import types
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import backup  # noqa: E402
import discord_rest  # noqa: E402
import telegram_bot  # noqa: E402

checks = {}

BUNDLE = {
    "schema_version": 1,
    "generated_at": "2026-08-17T19:04:07",
    "docksentry_version": "2.11.1",
    "instance": "dockmox.lan",
    "settings": {"language": "de", "disk_warn_percent": 60,
                 "totally_made_up": "nope"},
    "pinned": ["nginx"],
    "groups": {"vpn": {"name": "vpn", "containers": ["gluetun"]}},
    "links": {"ok": "https://example.com/notes",
              "bad": "javascript:alert(1)"},
    "notes": {}, "autoupdate": [], "ask_major": [], "update_windows": {},
}


# ═══ Discord: the same bundle, a different transport ═════════════════
sent = {}


class Resp:
    status = 200

    def read(self):
        return json.dumps({"id": "1", "attachments": [
            {"filename": "docksentry-backup-dockmox.lan-20260817-190407.json"}]}).encode()

    def __enter__(self): return self

    def __exit__(self, *a): return False


def fake_open(req, timeout=None):
    sent["url"] = req.full_url
    sent["headers"] = {k.lower(): v for k, v in req.headers.items()}
    sent["body"] = req.data
    return Resp()


discord_rest.urllib.request.urlopen = fake_open
rest = discord_rest.DiscordREST("tok")
out = rest.upload_followup("appid", "itoken", "docksentry-backup-x.json",
                           json.dumps(BUNDLE).encode(), "📦 backup")

body = sent["body"].decode("utf-8", "replace")
boundary = sent["headers"]["content-type"].split("boundary=")[1]
checks["Discord uploads to the interaction's own webhook"] = (
    sent["url"].endswith("/webhooks/appid/itoken"))
checks["…as multipart"] = sent["headers"]["content-type"].startswith(
    "multipart/form-data; boundary=")
checks["…with the JSON body under payload_json"] = (
    'name="payload_json"' in body)
checks["…the file under files[0]"] = 'name="files[0]"' in body
checks["…and the two tied together by an attachments entry"] = (
    '"attachments": [{"id": 0' in body)
checks["the file keeps its real name and extension"] = (
    'filename="docksentry-backup-x.json"' in body)
checks["the boundary cannot occur in the payload"] = (
    boundary not in json.dumps(BUNDLE))
checks["…and the body closes properly"] = body.rstrip().endswith(
    f"--{boundary}--")
checks["a successful upload is parsed"] = out["attachments"][0][
    "filename"].endswith(".json")


class Failing:
    status = 500


def fake_fail(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 413, "Payload Too Large",
                                 {}, io.BytesIO(b'{"message":"too big"}'))


discord_rest.urllib.request.urlopen = fake_fail
try:
    rest.upload_followup("a", "b", "x.json", b"{}")
    ok = False
except discord_rest.DiscordRESTError as e:
    # The class formats code and body into one message and exposes the
    # code as `.status` — checked rather than assumed, because asserting
    # against the wrong attribute is how a test passes while the caller
    # cannot tell a 413 from a 401.
    ok = getattr(e, "status", None) == 413 or "413" in str(e)
checks["a refused upload raises rather than looking successful"] = ok
# Uploads are deliberately not retried: a duplicated 30 kB file in a
# channel is worse than an error the user can act on.
src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                        "discord_rest.py"), encoding="utf-8").read()
up = src[src.index("def upload("):src.index("def upload_to_channel")]
checks["…and is not retried into a duplicate"] = "for attempt in" not in up
checks["the untested part is labelled as untested"] = (
    "Not verified against the live API" in up)

dsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "discord_bot.py"), encoding="utf-8").read()
checks["Discord has the command"] = '{"name": "backup"' in dsrc
checks["…dispatched like the rest"] = 'return self._cmd_backup(data)' in dsrc
checks["…and it says it already answered"] = (
    "return ANSWERED" in dsrc and "if text is ANSWERED:" in dsrc)
# Returning "" instead would post "(no output)" under the file.
checks["…because an empty answer would post a second message"] = (
    '"(no output)"' in dsrc)


# ═══ Telegram: a file dropped into the chat ══════════════════════════
def bot(**over):
    b = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    b.config = types.SimpleNamespace(
        bot_token="1:x", chat_id="-100", telegram_allowed_users=[],
        debug=False, bot_label="", telegram_topic_id=None,
        data_dir="/tmp", settings_file="/tmp/nope.json")
    b.sent = []
    b.markups = []
    b.send_message = lambda text, reply_markup=None, **kw: (
        b.sent.append(text), b.markups.append(reply_markup))
    b.t = lambda key, **kw: key + (" " + json.dumps(kw, sort_keys=True)
                                   if kw else "")
    b.downloaded = json.dumps(BUNDLE).encode()
    b._download_file = lambda fid: b.downloaded
    for k, v in over.items():
        setattr(b, k, v)
    return b


b = bot()
b._offer_restore({"file_name": "docksentry-backup-dockmox.lan.json",
                  "file_size": 900, "file_id": "f1"})
checks["a dropped backup is recognised"] = any(
    "restore_offer" in s for s in b.sent)
checks["…and nothing is restored yet"] = b._pending_restores != {}
checks["…the offer names where it came from"] = "dockmox.lan" in b.sent[0]
checks["…and what it would overwrite"] = "settings" in b.sent[0]
checks["…with a confirm and a cancel"] = (
    len(b.markups[-1]["inline_keyboard"][0]) == 2)
checks["…the button carries a token, not the bundle"] = all(
    len(btn["callback_data"]) < 64
    for btn in b.markups[-1]["inline_keyboard"][0])

# Things that are not a backup are refused before anything is written.
b2 = bot()
b2._offer_restore({"file_name": "holiday.jpg", "file_size": 10, "file_id": "f"})
checks["a non-JSON attachment is ignored in silence"] = b2.sent == []
b3 = bot()
b3._offer_restore({"file_name": "huge.json",
                   "file_size": 9 * 1024 * 1024, "file_id": "f"})
checks["an implausibly large file is refused before downloading"] = any(
    "restore_too_big" in s for s in b3.sent)
b4 = bot(downloaded=b"not json at all")
b4._download_file = lambda fid: b"not json at all"
b4._offer_restore({"file_name": "x.json", "file_size": 10, "file_id": "f"})
checks["…and something that is not JSON says so"] = any(
    "restore_not_json" in s for s in b4.sent)
b5 = bot()
b5._download_file = lambda fid: json.dumps({"hello": "world"}).encode()
b5._offer_restore({"file_name": "x.json", "file_size": 10, "file_id": "f"})
checks["…JSON without a schema_version is not a backup"] = any(
    "restore_not_a_backup" in s for s in b5.sent)

# The press is what restores, and it only works once.
class Store:
    def __init__(self):
        self.saved = {}
        self.pinned_file = "pinned"; self.groups_file = "groups"
        self.notes_file = "notes"; self.links_file = "links"
        self.update_windows_file = "windows"
        self.ask_before_major_file = "major"

    def save_pinned(self, v): self.saved["pinned"] = v
    def save_autoupdate(self, v): self.saved["autoupdate"] = v
    def _save(self, f, v): self.saved[f] = v
    def _save_dict(self, f, v): self.saved[f] = v


b = bot()
b.store = Store()
b.config.save_persistent = lambda: b.__dict__.setdefault("saved_settings", True)
b._offer_restore({"file_name": "b.json", "file_size": 900, "file_id": "f1"})
token = list(b._pending_restores)[0]
msg = b._do_restore(token)
checks["the press restores"] = "restore_done" in msg
checks["…settings among them"] = b.__dict__.get("saved_settings") is True
checks["…and a second press finds nothing left"] = (
    "restore_expired" in b._do_restore(token))

# The bundle's own claims are not trusted.
checks["an unknown settings key is dropped"] = not hasattr(
    b.config, "totally_made_up")
checks["…and an unsafe link never reaches the store"] = (
    "bad" not in (b.store.saved.get("links") or {}))
checks["…while the safe one does"] = (
    (b.store.saved.get("links") or {}).get("ok") == "https://example.com/notes")

tsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "telegram_bot.py"), encoding="utf-8").read()
checks["the confirm button strips itself before acting"] = (
    "self.remove_buttons(chat_id, msg_id)" in tsrc.split(
        'if data.startswith("restore_go:")')[1][:400])
checks["a command is never mistaken for an attachment"] = (
    'if doc and not text.startswith("/")' in tsrc)

# One implementation, two callers.
wsrc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "web_ui.py"), encoding="utf-8").read()
checks["the Web UI import goes through the same restore"] = (
    "_backup.restore(" in wsrc)
checks["…and no longer keeps its own copy of it"] = (
    wsrc.count("dropped_links += 1") == 0)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
