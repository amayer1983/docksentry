#!/usr/bin/env python3
"""A restore refuses the same things whichever door it came through.

`README:315` — a backup bundle can be restored from Telegram, from
Discord or from the Web UI. Three doors into the one operation that
overwrites settings, groups, pins, notes and links in place.

On 10.10.2026 a sweep found the Discord door's two guards unprotected:
the size ceiling and the "is this actually a backup" check could both
be removed with no procedure going red, while the word-for-word
identical Telegram lines were both caught by
`test_backup_channels.py`. A pure parity hole — the same rule, written
twice, tested once.

That is worth closing on its own terms. A restore is not an update: it
replaces state rather than adding to it, and the thing standing between
a mistyped drag-and-drop and somebody's groups is these few lines. If
one door is stricter than another, the answer to "is my configuration
safe from a bad file?" depends on which chat window happened to be
open.

So the rules are asked of every door at once, and a new door that
forgets one of them fails here rather than being found by whoever
drags the wrong file into it.
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from discord_bot import DiscordBot  # noqa: E402
from i18n import get_translator  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402

checks = {}

GOOD = json.dumps({"schema_version": 1, "settings": {"language": "en"}})


class _Door(types.SimpleNamespace):
    """What came back from one door: the words, and what it did.

    `offered` is the one that carries the promise — a refusal that still
    hands out a confirmation token has refused nothing.
    """

    def __init__(self, said, offered, crashed):
        super().__init__(said=said, offered=offered, crashed=crashed)


# What is asserted is whether the restore was OFFERED, not what the
# answer said. The first version of this file read the reply text for
# the words "backup" and "json" — and `restore_offer` opens with "A
# backup arrived" while the reply quotes the filename `backup.json`.
# Both refusal checks were satisfied by the acceptance path, and the
# counter-probe said so: removing the `schema_version` guard altogether
# left this file green. The guard either stops the offer or it does
# not, and that is what a confirmation token being handed out measures.


def _discord(name="backup.json", size=1000, body=GOOD):
    """Drop a file on `/restore` in Discord; was a restore offered?"""
    bot = DiscordBot.__new__(DiscordBot)
    bot.config = types.SimpleNamespace(language="en", container_cli="docker")
    bot.application_id, bot.rest = "", None
    bot.log = lambda *a, **kw: None
    bot.store = None
    offered = []
    bot._new_confirmation = lambda kind, payload, data: (
        offered.append(kind) or "tok")
    import urllib.request
    real = urllib.request.urlopen

    class _Resp:
        def read(self, n=-1):
            return body.encode() if isinstance(body, str) else body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    urllib.request.urlopen = lambda *a, **kw: _Resp()
    data = {"data": {"resolved": {"attachments": {
        "1": {"filename": name, "size": size,
              "url": "https://cdn.example/x.json"}}}}}
    try:
        said = str(bot._cmd_restore({"file": "1"}, data) or "")
    except Exception as e:                                  # noqa: BLE001
        return _Door(f"raised {type(e).__name__}: {e}", False, True)
    finally:
        urllib.request.urlopen = real
    return _Door(said, bool(offered), False)


def _telegram(name="backup.json", size=1000, body=GOOD):
    """The same file, dropped into the Telegram chat."""
    bot = TelegramBot.__new__(TelegramBot)
    said, offered = [], []
    bot.config = types.SimpleNamespace(language="en", bot_token="", chat_id="1")
    bot.t = get_translator("en")
    bot.send_message = lambda m, **kw: said.append(m)
    bot.log = lambda *a, **kw: None
    bot.store = None
    bot._download_file = lambda fid: (body.encode()
                                      if isinstance(body, str) else body)
    # Telegram keeps its own pending-restore table rather than going
    # through `_new_confirmation`, so that table is where "was it
    # offered?" is read. Patching `_new_confirmation` here measured
    # nothing: it is never called on this path.
    bot._new_confirmation = lambda *a, **kw: "tok"
    bot.send_message_with_buttons = lambda *a, **kw: said.append("buttons")
    crashed = False
    try:
        bot._offer_restore({"file_name": name, "file_size": size,
                            "file_id": "f1"})
    except Exception as e:                                  # noqa: BLE001
        said.append(f"raised {type(e).__name__}: {e}")
        crashed = True
    offered = bool(getattr(bot, "_pending_restores", None))
    return _Door("\n".join(said), offered, crashed)


def _web(name="backup.json", size=1000, body=GOOD):
    """The third door: `POST /api/backup_import` from the browser.

    It was missing from this file, and it was the door with the hole.
    `backup.restore` has no `schema_version` sentinel of its own, so
    any JSON object was applied section by section — and `settings`
    carries the Web UI password, the bot token and
    `TELEGRAM_ALLOWED_USERS`, because `save_persistent` writes every
    persistent key at once. The docstring above said "three doors" and
    the table asked two of them.
    """
    import web_ui
    raw = body.encode() if isinstance(body, str) else body
    # Declared length wins where the caller gave one: the size checks
    # above are about `Content-Length`, which is the client's word.
    declared = size if size != 1000 else len(raw)
    # An oversized *claim* is what the chats are asked about; the Web
    # door reads the body, so give it one that really is too big.
    if size > 1000:
        raw = (b'{"schema_version": 1, "pad": "' + b"x" * size + b'"}')
        declared = len(raw)
    applied = []

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
                                bot=types.SimpleNamespace(t=None),
                                store=None,
                                restart_discord=lambda *a, **kw: None)
    h = cls.__new__(cls)
    h.path = "/api/backup_import"
    h.headers = {"Content-Length": str(declared), "Host": "x",
                 "Origin": "http://x", "Content-Type": "application/json"}
    h.rfile = types.SimpleNamespace(read=lambda n: raw[:n])
    h.client_address = ("127.0.0.1", 0)
    h._check_auth = lambda: True
    h._check_csrf = lambda: True
    h._audit = lambda *a, **k: None
    h._audit_post = lambda *a, **k: None
    seen = {}
    h.send_response = lambda code: seen.__setitem__("code", code)
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    out = []
    h.wfile = types.SimpleNamespace(write=lambda b: out.append(b))

    import backup as _backup
    _real = _backup.restore
    _backup.restore = lambda bundle, *a, **kw: (
        applied.append(bundle) or (["settings"], [], 0))
    try:
        h.do_POST()
    except Exception as e:                                  # noqa: BLE001
        return _Door(f"raised {type(e).__name__}: {e}", False, True)
    finally:
        _backup.restore = _real
    said = b"".join(out).decode("utf-8", "replace")
    # "Offered" has no meaning here — the Web UI has no confirmation
    # step, the POST *is* the decision. So what is measured is whether
    # the bundle reached `backup.restore`, which is the same question:
    # did this door let it through?
    return _Door(said, bool(applied), False)


DOORS = (("Discord", _discord), ("Telegram", _telegram), ("Web", _web))

# ── the ceiling ──────────────────────────────────────────────────────
# A bundle is settings, groups, notes and links. Two megabytes is
# already generous, and the number has to be the same at every door or
# the answer to "will this be refused?" depends on the chat window.
checks["both doors agree on how big a backup may be"] = (
    DiscordBot.RESTORE_MAX_BYTES == TelegramBot.RESTORE_MAX_BYTES)

_CAP = DiscordBot.RESTORE_MAX_BYTES
# Over the cap by a real margin, not by one byte: the Web door allows
# a few kilobytes for the multipart envelope a browser wraps the file
# in, because measuring that made it refuse a bundle both chats take.
# The chats weigh the file itself, so the margin changes nothing there.
for _door, _fn in DOORS:
    _r = _fn(size=_CAP + 64 * 1024)
    checks[f"{_door} refuses a backup over the limit"] = not _r.offered
    checks[f"…and {_door} says why rather than failing silently"] = (
        ("large" in _r.said.lower() or "big" in _r.said.lower()
         or "2 MB" in _r.said)
        and not _r.crashed)
    checks[f"…while {_door} accepts one under it"] = _fn(size=1000).offered

# ── is this actually a backup? ───────────────────────────────────────
# `schema_version` is the sentinel. Without it any JSON object would be
# applied section by section, and a file that happens to have a
# `settings` key would overwrite the real one.
for _door, _fn in DOORS:
    _r = _fn(body=json.dumps({"settings": {"language": "de"}}))
    checks[f"{_door} does not offer to restore JSON that is not a backup"] = (
        not _r.offered)
    checks[f"…and {_door} names the missing schema_version"] = (
        "schema_version" in _r.said and not _r.crashed)

    _r = _fn(body="this is not json at all")
    checks[f"…{_door} does not offer a file that is not JSON"] = not _r.offered
    checks[f"…and {_door} says so"] = (
        "not json" in _r.said.lower() and not _r.crashed)

    # The filename rule is the one thing that genuinely has no Web
    # equivalent: a POST body has no name to look at. Stated rather
    # than faked — a check that quietly skips a door is how this file
    # came to claim three and ask two.
    if _door != "Web":
        _r = _fn(name="notes.txt")
        checks[f"…and {_door} does not even read a non-.json file"] = (
            not _r.offered)

# ── a real bundle still gets through ─────────────────────────────────
# Without this every check above passes on a door that refuses
# everything, which is a different way of being broken.
for _door, _fn in DOORS:
    _r = _fn()
    checks[f"{_door} still offers a real backup"] = (
        _r.offered and not _r.crashed)

# ── and who may press the button, and for how long ──────────────────
# `_offer_stop` recorded the asker and the time from the start, with a
# comment saying why: "a question left unanswered on Monday was still
# live on Friday when someone scrolled up and tapped". `_offer_restore`
# recorded neither — the more destructive of the two operations had the
# weaker guard. An audit found the asymmetry on 11.10.2026.
import time as _time                                          # noqa: E402


def _press(asker="4711", presser="4711", age=0.0, cancel=False,
           second_offer=False, other_button=False):
    """Drop a bundle as `asker`, then have `presser` tap a button.

    Returns `(applied, said, buttons_removed, tokens_left)`.
    """
    said, applied, removed = [], [], []
    bot = TelegramBot.__new__(TelegramBot)
    bot.config = types.SimpleNamespace(language="en", bot_token="",
                                       chat_id="1")
    bot.t = get_translator("en")
    bot.store = None
    bot.log = lambda *a, **kw: None
    bot.send_message = lambda m, **kw: said.append(m)
    bot.send_message_with_buttons = lambda *a, **kw: None
    bot.answer_callback = lambda *a, **kw: said.append(str(a[1:]))
    bot.remove_buttons = lambda c, m: removed.append(m)
    bot._check_auth = lambda chat_id, uid, kind="message": True
    bot._download_file = lambda fid: GOOD.encode()
    bot._offer_restore({"file_name": "backup.json", "file_size": 900,
                        "file_id": "f1"}, asker=asker)
    token = next(iter(bot._pending_restores))
    if second_offer:
        # A newer file arrives; the older message keeps its buttons.
        bot._offer_restore({"file_name": "tuesday.json", "file_size": 900,
                            "file_id": "f2"}, asker=asker)
    if age:
        for _rec in bot._pending_restores.values():
            _rec["created"] -= age
    import backup as _backup
    _real = _backup.restore
    _backup.restore = lambda bundle, *a, **kw: (
        applied.append(bundle) or (["settings"], [], 0))
    try:
        _data = ("stop_cancel" if other_button
                 else f"restore_cancel:{token}" if cancel
                 else f"restore_go:{token}")
        bot._handle_callback({"id": "c1", "data": _data,
                              "from": {"id": presser},
                              "message": {"message_id": 5,
                                          "chat": {"id": 1}}},
                             None, None)
    finally:
        _backup.restore = _real
    return (bool(applied), "\n".join(said), removed,
            set(bot._pending_restores or {}))


_applied, _said, _removed, _left = _press()
checks["the person who dropped the file may restore it"] = _applied

_applied, _said, _removed, _left = _press(asker="4711", presser="9999")
checks["…and nobody else may"] = not _applied
checks["…and is told why"] = "confirm" in _said.lower() or "can" in _said.lower()

# The deadline. A bundle dropped on Monday must not still be armed on
# Friday, which is the case the stop path already refused.
#
# `not _applied` alone is not enough and the counter-probe said so:
# with the TTL check in `_do_restore` removed, the sweep at the top of
# the callback had already dropped the record, so nothing was applied
# for a different reason and this stayed green. What is asked now is
# that the reader is TOLD it expired — only the TTL path says that.
_applied, _said, _removed, _left = _press(
    age=TelegramBot.STOP_CONFIRM_TTL + 60)
checks["a question left unanswered expires"] = (
    not _applied and "offer is gone" in _said)
# …and is not told "Restoring…" first. The toast used to go out before
# the record was looked at, so an expired press got two sentences that
# contradict each other.
# `"Restoring…"` with the ellipsis, not the bare word: the offer text
# itself opens with "Restoring overwrites the settings…", so a check
# for "Restoring" would have matched the question rather than the
# answer.
checks["…without being told it is restoring"] = (
    "Restoring…" not in _said and "⏳" in _said)

_applied, _said, _removed, _left = _press(
    age=TelegramBot.STOP_CONFIRM_TTL - 60)
checks["…while one answered inside the window still works"] = _applied

# ── and a refused press leaves the owner something to press ─────────
# The gate used to run after the buttons came off, so a bystander's
# tap took the owner's only way to confirm with it and the file had to
# be uploaded again. Measured on 17.10.2026.
_applied, _said, _removed, _left = _press(asker="4711", presser="9999")
checks["a stranger's press leaves the buttons for the owner"] = (
    _removed == [] and len(_left) == 1)
_applied, _said, _removed, _left = _press()
checks["…while the owner's own press takes them off"] = (
    _removed == [5] and _applied)

# Cancel clears its own offer and not a newer one. A bare `clear()`
# was the first fix and it was wrong: an older message keeps its
# buttons, so cancelling Monday's file wiped Tuesday's live token.
_applied, _said, _removed, _left = _press(cancel=True)
checks["cancelling an offer forgets it"] = _left == set() and not _applied
_applied, _said, _removed, _left = _press(cancel=True, second_offer=True)
checks["…and leaves a newer offer alone"] = len(_left) == 1

# An offer nobody answered is not kept in memory for ever: the bundle
# carries the source instance's settings. Measured on a button press
# that has nothing to do with the offer — a cancel or a restore pops
# its own record anyway, so either of those would pass with no sweep
# at all. The counter-probe said exactly that.
_applied, _said, _removed, _left = _press(
    age=TelegramBot.STOP_CONFIRM_TTL + 60, other_button=True)
checks["an expired offer is swept on the next press, not just refused"] = (
    _left == set())
# …and a live one is not swept out from under its owner.
_applied, _said, _removed, _left = _press(other_button=True)
checks["…while a live offer survives an unrelated press"] = len(_left) == 1

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
