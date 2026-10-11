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


DOORS = (("Discord", _discord), ("Telegram", _telegram))

# ── the ceiling ──────────────────────────────────────────────────────
# A bundle is settings, groups, notes and links. Two megabytes is
# already generous, and the number has to be the same at every door or
# the answer to "will this be refused?" depends on the chat window.
checks["both doors agree on how big a backup may be"] = (
    DiscordBot.RESTORE_MAX_BYTES == TelegramBot.RESTORE_MAX_BYTES)

_CAP = DiscordBot.RESTORE_MAX_BYTES
for _door, _fn in DOORS:
    _r = _fn(size=_CAP + 1)
    checks[f"{_door} refuses a backup over the limit"] = not _r.offered
    checks[f"…and {_door} says why rather than failing silently"] = (
        ("large" in _r.said.lower() or "big" in _r.said.lower())
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

    _r = _fn(name="notes.txt")
    checks[f"…and {_door} does not even read a non-.json file"] = not _r.offered

# ── a real bundle still gets through ─────────────────────────────────
# Without this every check above passes on a door that refuses
# everything, which is a different way of being broken.
for _door, _fn in DOORS:
    _r = _fn()
    checks[f"{_door} still offers a real backup"] = (
        _r.offered and not _r.crashed)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
