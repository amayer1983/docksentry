#!/usr/bin/env python3
"""What one front end can do, the other can too (#61, @NotRetarded).

He photographed Discord and Telegram side by side during the same
auto-update run. Discord had the per-container results; Telegram had
those *and* the two lines around them — "⚡ Auto-updating 2
container(s)…" and "⚡ Auto-update complete: 2 updated".

Measured, it was exactly two `self.send_message(..., auto=True)` calls
that never had a second recipient. Which would have been a two-line fix,
except this was the **third** time:

  * #57 — the release link the Discord bot channel never got;
  * v2.9.0 — "restarted on vX", Telegram-only, so no Discord, e-mail or
    ntfy user ever saw it;
  * #61 — these two.

Each was fixed where it was found, which is precisely why there was a
third. So there is one seam now — `broadcast.Broadcast.announce`, which
both front ends hold — and this test fails if an unattended message goes
anywhere else.

The command lists had drifted the same way: seven commands Telegram had
and Discord did not, three the other way round. Two front ends that
answer different questions is a support burden nobody signed up for.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from discord_bot import COMMANDS  # noqa: E402
from telegram_bot import _BOT_COMMANDS  # noqa: E402

checks = {}
APP = os.path.join(os.path.dirname(__file__), "..", "app")

# ═══ the same commands on both sides ═════════════════════════════════
tg = {c[0] for c in _BOT_COMMANDS}
dc = {c["name"] for c in COMMANDS}
checks["both front ends offer the same number of commands"] = len(tg) == len(dc)
checks["…and the same ones"] = tg == dc
if tg != dc:
    print(f"  → only Telegram: {sorted(tg - dc)}")
    print(f"  → only Discord : {sorted(dc - tg)}")

# Every Telegram command reaches a handler, and every Discord one too.
tsrc = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
# `/stop`, `/start` and `/restart` are matched with a trailing space —
# they take a container name, and the bare form is a different command
# (or nothing). Accepting that spelling is not looseness: it is the
# spelling the code uses, and the first version of this check reported
# `stop` as unhandled because of it.
missing_tg = [n for n in sorted(tg)
              if f'text.startswith("/{n}")' not in tsrc
              and f'text.startswith("/{n} ")' not in tsrc
              and f'text == "/{n}"' not in tsrc]
checks["every Telegram command has a handler"] = not missing_tg
if missing_tg:
    print(f"  → no Telegram handler: {missing_tg}")

dispatch = dsrc[dsrc.index("    def _dispatch(self, data):"):]
dispatch = dispatch[:dispatch.index("\n    def ", 10)]
missing_dc = [n for n in sorted(dc) if f'"{n}"' not in dispatch]
checks["every Discord command is dispatched"] = not missing_dc
if missing_dc:
    print(f"  → not dispatched: {missing_dc}")

# And every Discord handler it dispatches to exists — a typo here is a
# command that registers with Discord and then raises when pressed.
tree = ast.parse(dsrc)
methods = {m.name for c in tree.body if isinstance(c, ast.ClassDef)
           for m in c.body if isinstance(m, ast.FunctionDef)}
called = set()
for cls in tree.body:
    if not isinstance(cls, ast.ClassDef):
        continue
    for m in cls.body:
        if isinstance(m, ast.FunctionDef) and m.name == "_dispatch":
            for node in ast.walk(m):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "self"
                        and node.attr.startswith("_cmd_")):
                    called.add(node.attr)
checks["…to a handler that exists"] = called <= methods
if called - methods:
    print(f"  → dispatched into nothing: {sorted(called - methods)}")

# The new handlers must not call helpers that are not there either —
# the first draft of them called four methods this class does not have.
new = ("_cmd_help", "_cmd_changelog", "_cmd_selfupdate", "_cmd_debug",
       "_cmd_lang", "_cmd_setlink", "_cmd_audit", "_cmd_restore",
       "_cmd_backup", "_cmd_restart_self")
ghosts = set()
for cls in tree.body:
    if not isinstance(cls, ast.ClassDef):
        continue
    for m in cls.body:
        if isinstance(m, ast.FunctionDef) and m.name in new:
            for node in ast.walk(m):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "self"
                        and node.attr.startswith("_")
                        and node.attr not in methods):
                    ghosts.add(f"{m.name} → self.{node.attr}")
checks["the new commands call helpers that exist"] = not ghosts
if ghosts:
    print(f"  → {sorted(ghosts)}")

# ═══ an unattended message reaches every channel ═════════════════════
lines = tsrc.splitlines()
stray = []
in_announce = False
for i, line in enumerate(lines):
    if line.startswith("    def announce("):
        in_announce = True
    elif line.startswith("    def ") and in_announce:
        in_announce = False
    if in_announce:
        continue
    if "self.send_message(" in line and "auto=True" in line:
        stray.append(f"{i + 1}: {line.strip()[:70]}")
checks["no unattended message goes to Telegram alone"] = not stray
if stray:
    print("  → " + "\n  → ".join(stray))

# The seam itself moved into `broadcast.py` (#63, third core-extraction
# step) — it was never Telegram's, and while it sat on the Telegram bot
# the Discord side had to reach into that instance to be heard at all.
# Same checks, pointed at where the seam lives now.
bsrc = open(os.path.join(APP, "broadcast.py"), encoding="utf-8").read()
i = bsrc.index("    def announce(self")
ann = bsrc[i:]
checks["the seam sends to Telegram"] = "send_message(text" in ann
checks["…and to every other channel"] = "notifier.send_message(" in ann
checks["…and asks whether there are any first"] = "has_channels()" in ann
checks["…and a failing channel cannot stop the others"] = (
    "except Exception" in ann)
# A button is Telegram's alone; inventing a second-class version of it
# for e-mail would be worse than leaving it out.
checks["a keyboard is not forced on channels that have none"] = (
    "reply_markup" in ann and "reply_markup" not in ann.split(
        "notifier.send_message(")[1][:80])
# And the Telegram bot's `announce` is now the adapter onto it, so every
# existing caller (and every test that builds a bare bot) still works.
i = tsrc.index("    def announce(self")
tg_ann = tsrc[i:tsrc.index("\n    def ", i + 10)]
checks["TelegramBot.announce delegates to the seam"] = (
    "seam.announce(" in tg_ann and "notifier.send_message(" not in tg_ann)

# ═══ what a delivery-only channel can still do ═══════════════════════
# E-mail is the only one of the seven that can carry a file, and a
# backup in your inbox is the copy that survives the machine it came
# from — the point of the ones @famewolf asked for. Nothing can *ask*
# for it there (no back channel); the Web UI and the scheduled copy are
# what trigger it.
import smtplib  # noqa: E402
import types as _t  # noqa: E402

_sent = {}


class _FakeSMTP:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, **k): pass
    def login(self, *a): pass
    def send_message(self, msg, **kw): _sent["msg"] = msg
    def quit(self): pass


smtplib.SMTP = _FakeSMTP
smtplib.SMTP_SSL = _FakeSMTP
from notifiers.smtp import SmtpNotifier  # noqa: E402

_cfg = _t.SimpleNamespace(
    smtp_host="mail", smtp_port=587, smtp_user="", smtp_password="",
    smtp_from="a@b.c", smtp_to="d@e.f", smtp_tls="starttls",
    smtp_tls_verify=True, bot_label="nas", channel_smtp_enabled=True)
_n = SmtpNotifier(_cfg)
_n.send_document("docksentry-backup-nas.json", b'{"schema_version": 1}',
                 "Docksentry backup")
_m = _sent.get("msg")
_att = [p for p in _m.iter_attachments()] if _m else []
checks["e-mail can carry a backup"] = len(_att) == 1
checks["…under its real filename"] = (
    _att and _att[0].get_filename() == "docksentry-backup-nas.json")
checks["…as JSON, not as an opaque blob"] = (
    _att and _att[0].get_content_type() == "application/json")
checks["…with the bytes intact"] = (
    _att and _att[0].get_payload(decode=True) == b'{"schema_version": 1}')

# An ordinary message must not have grown an empty attachment.
_sent.clear()
_n.send_message("plain")
checks["a plain message is unchanged"] = not list(
    _sent["msg"].iter_attachments())

# ═══ counts written into prose go stale ═════════════════════════════
# The Connections page advertised "/status, /update, /logs and 19 more"
# while there were 35 commands — spotted in a screenshot taken for an
# unrelated change. A number in a sentence has no way of noticing that
# the thing it counts has moved.
import json as _json  # noqa: E402
import re as _re  # noqa: E402

_en = _json.load(open(os.path.join(APP, "lang", "en.json"), encoding="utf-8"))
_intro = _en.get("web_discord_bot_intro", "")
_m = _re.search(r"\b(\d+) more\b", _intro)
checks["the advertised command count is the real one"] = (
    bool(_m) and int(_m.group(1)) == len(COMMANDS) - 3)

# The README said 27 in three places, stale since v1.63, and a new doc
# page copied the number rather than counting. Same class, wider blast
# radius — a prose number cannot notice the thing it counts has moved.
_docs = {}
for _p in ("README.md", "docs/notifications.md", "docs/discord-bot.md"):
    _f = os.path.join(os.path.dirname(__file__), "..", _p)
    if os.path.exists(_f):
        _docs[_p] = open(_f, encoding="utf-8").read()
_wrong = []
for _p, _text in _docs.items():
    for _n in _re.findall(r"\b(\d{1,3}) slash[- ]commands?\b", _text):
        if int(_n) != len(COMMANDS):
            _wrong.append(f"{_p}: {_n}")
checks["no document advertises a stale command count"] = not _wrong
if _wrong:
    print("  → " + ", ".join(_wrong))

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
