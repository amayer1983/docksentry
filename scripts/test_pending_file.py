#!/usr/bin/env python3
"""The pending-updates file is read and written in one place, and every caller still answers what it answered before.

`pending_updates.json` is one flat list holding every managed host's
entries. Telegram, Discord and the Web UI each had their own reader and
Telegram and Discord their own read-modify-write around it, each with
its own idea of a missing, corrupt or wrongly-shaped file. `app/pending.py`
holds the reading, the filtering by host, the removal by `(host, name)`
and the atomic write; the callers keep their own filters on top.

Two halves. The first drives `pending` itself: a missing file, no path,
an empty list, corrupt JSON, bytes that are not UTF-8, a file that is
not a list, entries that are not dicts, and a valid file with the same
name on two hosts. The second drives each caller as it stands — Telegram
`read_pending` and `_remove_from_pending`, Discord `_pending`,
`_pending_for` and `_drop_pending`, the Web UI's `_get_pending` — and
pins what each one returns and what the file looks like afterwards.

Measured on the tree before Q5 (scratchpad q5/measure.py): every caller
already agreed on a missing file, corrupt JSON and the valid file, and
both removals delete the file when nothing is left — `[]` included.
They differed only where one of them crashed: Telegram's removal raised
on a file that is not a list, on a non-dict entry, on bytes that are not
UTF-8 and on a `None` path; the Web UI's reader raised on the last two.
Those rows are marked "(raised before Q5)". Every other row is the old
answer, unchanged.

The Web update worker removes through Telegram's `_remove_from_pending`,
so its delete-when-empty is the Telegram row here; the worker itself is
driven by test_web_action_matrix (pending file at the end of each row).

`--app DIR` runs the second half against another app/ tree.
"""
import json
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app")
if "--app" in sys.argv:
    APP = sys.argv[sys.argv.index("--app") + 1]
sys.path.insert(0, os.path.abspath(APP))
sys.path.insert(0, HERE)

import srcslice                                     # noqa: E402

checks = {}
tmp = tempfile.mkdtemp(prefix="pendingfile-")
path = os.path.join(tmp, "pending_updates.json")

VALID = [{"name": "web"}, {"name": "web", "host": "nas"},
         {"name": "db", "host": "nas"}]
JUNK = [{"name": "web"}, "junk", 3, {"name": "db", "host": "nas"},
        {"host": "nas"}]
STATES = {
    "missing": None,
    "empty": b"[]",
    "corrupt": b"{ not json",
    "not UTF-8": b'[{"name": "w\xff"}]',
    "not a list": b'{"a": 1}',
    "junk": json.dumps(JUNK).encode(),
    "valid": json.dumps(VALID).encode(),
}


def put(state):
    if os.path.exists(path):
        os.remove(path)
    if STATES[state] is not None:
        with open(path, "wb") as fh:
            fh.write(STATES[state])


def on_disk():
    """What the file holds now: `None` when it is gone, the raw bytes
    when it is not JSON, the parsed value otherwise."""
    if not os.path.exists(path):
        return None
    raw = open(path, "rb").read()
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return raw


def call(fn, *a, **k):
    """(`"ok"`, value) or (`"raise"`, exception name) — a crash is a red
    check here, never a traceback."""
    try:
        return "ok", fn(*a, **k)
    except Exception as e:                          # noqa: BLE001
        return "raise", type(e).__name__


def leftovers():
    return sorted(n for n in os.listdir(tmp)
                  if n != os.path.basename(path))


# ── first half: the core ─────────────────────────────────────────────
try:
    import pending
except ImportError:
    pending = None
checks["app/pending.py exists"] = pending is not None

if pending is not None:
    read = srcslice.attr(pending, "read")
    entries_for = srcslice.attr(pending, "entries_for")
    remove = srcslice.attr(pending, "remove")
    write = srcslice.attr(pending, "write")
    key_of = srcslice.attr(pending, "key_of")
    have = None not in (read, entries_for, remove, write, key_of)
    checks["…with read, entries_for, remove, write and key_of"] = have
else:
    have = False

if have:
    # reading: every way of having nothing is an empty list
    for state in ("missing", "empty", "corrupt", "not UTF-8", "not a list"):
        put(state)
        before = on_disk()
        checks[f"read: {state} is nothing pending"] = call(read, path) == ("ok", [])
        checks[f"…and reading leaves the {state} file as it was"] = on_disk() == before
    checks["read: no path is nothing pending"] = call(read, "") == ("ok", [])
    checks["read: a None path is nothing pending"] = call(read, None) == ("ok", [])
    put("junk")
    checks["read: every entry comes back, junk included"] = call(read, path) == ("ok", JUNK)
    checks["read: named_only keeps the dicts that carry a name"] = (
        call(read, path, named_only=True)
        == ("ok", [{"name": "web"}, {"name": "db", "host": "nas"}]))
    put("valid")
    checks["read: a valid file comes back as written"] = call(read, path) == ("ok", VALID)

    # entries_for: by host name, the local host implied, non-dicts skipped
    checks["entries_for: an entry without a host is the local host's"] = (
        call(entries_for, JUNK, ["local"]) == ("ok", [{"name": "web"}]))
    checks["entries_for: a remote host's entries, the nameless one too"] = (
        call(entries_for, JUNK, ["nas"])
        == ("ok", [{"name": "db", "host": "nas"}, {"host": "nas"}]))
    checks["entries_for: several hosts at once, in file order"] = (
        call(entries_for, VALID, ["nas", "local"]) == ("ok", VALID))
    checks["entries_for: a host with nothing pending gets nothing"] = (
        call(entries_for, VALID, ["pve1"]) == ("ok", []))

    checks["key_of: the pair, the local host implied"] = (
        call(key_of, {"name": "web"}) == ("ok", ("local", "web")))
    checks["key_of: a remote entry carries its host"] = (
        call(key_of, {"name": "db", "host": "nas"}) == ("ok", ("nas", "db")))

    # remove: by (host, name), the other host's entry of the same name stays
    put("valid")
    r = call(remove, path, [("nas", "web")], delete_when_empty=True)
    checks["remove: one host's entry goes, the other host's same name stays"] = (
        r == ("ok", [{"name": "web"}, {"name": "db", "host": "nas"}])
        and on_disk() == r[1])
    put("valid")
    r = call(remove, path, ["web"], delete_when_empty=True)
    checks["remove: a bare name is the local host's entry only"] = (
        r == ("ok", VALID[1:]) and on_disk() == VALID[1:])
    put("valid")
    r = call(remove, path, [("pve1", "web")], delete_when_empty=True)
    checks["remove: a key that matches nothing leaves every entry"] = (
        r == ("ok", VALID) and on_disk() == VALID)
    put("junk")
    r = call(remove, path, [("local", "web"), ("nas", "db")],
             delete_when_empty=True)
    checks["remove: entries that are not dicts are kept, not dropped"] = (
        r == ("ok", ["junk", 3, {"host": "nas"}])
        and on_disk() == ["junk", 3, {"host": "nas"}])

    put("valid")
    every = [("local", "web"), ("nas", "web"), ("nas", "db")]
    r = call(remove, path, every, delete_when_empty=True)
    checks["remove: the last entry gone deletes the file"] = (
        r == ("ok", []) and on_disk() is None)
    put("valid")
    r = call(remove, path, every, delete_when_empty=False)
    checks["remove: …or leaves [] when the caller asks for that"] = (
        r == ("ok", []) and on_disk() == [])
    put("empty")
    r = call(remove, path, [("local", "web")], delete_when_empty=True)
    checks["remove: a file that already holds [] is deleted too"] = (
        r[0] == "ok" and on_disk() is None)

    for state in ("missing", "corrupt", "not UTF-8", "not a list"):
        put(state)
        before = on_disk()
        r = call(remove, path, [("local", "web")], delete_when_empty=True)
        checks[f"remove: a file that is {state} is left exactly as it was"] = (
            r[0] == "ok" and on_disk() == before)
    checks["remove: no path at all is not an error"] = (
        call(remove, "", [("local", "web")], delete_when_empty=True)[0] == "ok")

    # write: atomic, through container_store.atomic_write_json
    seen = []
    real = getattr(pending, "atomic_write_json", None)
    if real is not None:
        def _spy(p, data, **kw):
            seen.append(p)
            return real(p, data, **kw)
        pending.atomic_write_json = _spy
    try:
        put("missing")
        r = call(write, path, VALID)
        checks["write: what is written reads back"] = (
            r[0] == "ok" and call(read, path) == ("ok", VALID))
        checks["write: through atomic_write_json"] = seen == [path]
        put("valid")
        seen.clear()
        call(remove, path, [("nas", "db")], delete_when_empty=True)
        checks["remove: the rewrite is atomic as well"] = seen == [path]
    finally:
        if real is not None:
            pending.atomic_write_json = real
    checks["write: no temp file is left beside the target"] = leftovers() == []


# ── second half: every caller, as it answers ─────────────────────────
import telegram_bot                                 # noqa: E402
import discord_bot                                  # noqa: E402
import web_ui                                       # noqa: E402

cfg = types.SimpleNamespace(pending_file=path, language="en")
tg = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
tg.config = cfg
dc = discord_bot.DiscordBot.__new__(discord_bot.DiscordBot)
dc.config = cfg
_handler = web_ui.create_handler(cfg, None, None, None)
web = _handler.__new__(_handler)

NAS_HOST = types.SimpleNamespace(name="nas")       # a ManagedHost's face
READERS = [
    ("Telegram read_pending", lambda: telegram_bot.read_pending(cfg.pending_file)),
    ("Discord _pending", lambda: dc._pending()),
    ("Discord _pending_for(nas)", lambda: dc._pending_for(NAS_HOST)),
    ("Discord _pending_for(None)", lambda: dc._pending_for(None)),
    ("Web _get_pending()", lambda: web._get_pending()),
    ("Web _get_pending(nas)", lambda: web._get_pending("nas")),
]
NAMED_JUNK = [{"name": "web"}, {"name": "db", "host": "nas"}]
#: (state, reader) → what it answers. Everything not listed answers [].
READ_ANSWERS = {
    ("junk", "Telegram read_pending"): JUNK,
    ("junk", "Discord _pending"): NAMED_JUNK,
    ("junk", "Discord _pending_for(nas)"): [{"name": "db", "host": "nas"}],
    ("junk", "Discord _pending_for(None)"): [{"name": "web"}],
    ("junk", "Web _get_pending()"): [{"name": "web"}],
    ("junk", "Web _get_pending(nas)"): [{"name": "db", "host": "nas"},
                                        {"host": "nas"}],
    ("valid", "Telegram read_pending"): VALID,
    ("valid", "Discord _pending"): VALID,
    ("valid", "Discord _pending_for(nas)"): VALID[1:],
    ("valid", "Discord _pending_for(None)"): VALID[:1],
    ("valid", "Web _get_pending()"): VALID[:1],
    ("valid", "Web _get_pending(nas)"): VALID[1:],
}
#: Answers that were a crash before Q5, measured on 6c4c474.
RAISED_BEFORE = {
    ("not UTF-8", "Web _get_pending()"), ("not UTF-8", "Web _get_pending(nas)"),
    ("None path", "Web _get_pending()"),
    ("not UTF-8", "Telegram _remove_from_pending"),
    ("not a list", "Telegram _remove_from_pending"),
    ("junk", "Telegram _remove_from_pending"),
    ("None path", "Telegram _remove_from_pending"),
}


def label(state, who):
    tail = " (raised before Q5)" if (state, who) in RAISED_BEFORE else ""
    return f"{who}: {state}{tail}"


for state in STATES:
    for who, fn in READERS:
        put(state)
        before = on_disk()
        want = READ_ANSWERS.get((state, who), [])
        checks[label(state, who) + " → " + json.dumps(want)[:60]] = (
            call(fn) == ("ok", want) and on_disk() == before)
put("missing")
for who, pf in (("Telegram read_pending", None), ("Discord _pending", None),
                ("Web _get_pending()", None)):
    cfg.pending_file = pf
    fn = dict(READERS)[who]
    checks[label("None path", who) + " → []"] = call(fn) == ("ok", [])
    cfg.pending_file = ""
    checks[label("no path", who) + " → []"] = call(fn) == ("ok", [])
cfg.pending_file = path

REMOVERS = [
    ("Telegram _remove_from_pending", lambda keys: tg._remove_from_pending(keys)),
    ("Discord _drop_pending", lambda keys: dc._drop_pending(keys)),
]
EVERY = [("local", "web"), ("nas", "web"), ("nas", "db")]
for who, rm in REMOVERS:
    put("valid")
    r = call(rm, [("nas", "web")])
    checks[f"{who}: drops nas's web, local's web stays"] = (
        r == ("ok", None)
        and on_disk() == [{"name": "web"}, {"name": "db", "host": "nas"}])
    put("valid")
    r = call(rm, [("local", "web")])
    checks[f"{who}: drops local's web, nas's web stays"] = (
        r == ("ok", None) and on_disk() == VALID[1:])
    put("valid")
    r = call(rm, [("pve1", "web")])
    checks[f"{who}: a key that matches nothing keeps every entry"] = (
        r == ("ok", None) and on_disk() == VALID)
    put("valid")
    r = call(rm, EVERY)
    checks[f"{who}: the last entry gone deletes the file"] = (
        r == ("ok", None) and on_disk() is None)
    put("empty")
    r = call(rm, [("local", "web")])
    checks[f"{who}: a file holding [] is deleted"] = (
        r == ("ok", None) and on_disk() is None)
    put("junk")
    r = call(rm, [("local", "web"), ("nas", "db")])
    checks[label("junk", who) + ": non-dict entries are kept"] = (
        r == ("ok", None) and on_disk() == ["junk", 3, {"host": "nas"}])
    for state in ("missing", "corrupt", "not UTF-8", "not a list"):
        put(state)
        before = on_disk()
        r = call(rm, [("local", "web")])
        checks[label(state, who) + ": file left as it was"] = (
            r == ("ok", None) and on_disk() == before)
    for pf, st in ((None, "None path"), ("", "no path")):
        cfg.pending_file = pf
        checks[label(st, who) + ": nothing happens"] = (
            call(rm, [("local", "web")]) == ("ok", None))
    cfg.pending_file = path
    checks[f"{who}: no temp file left behind"] = leftovers() == []

put("valid")
r = call(tg._remove_from_pending, ["web"])
checks["Telegram _remove_from_pending: a bare name is the local host"] = (
    r == ("ok", None) and on_disk() == VALID[1:])

# ── the callers do not open the file themselves ─────────────────────
SOURCES = [
    ("Telegram read_pending", telegram_bot, "read_pending", "_pending.read("),
    ("Telegram _remove_from_pending", telegram_bot.TelegramBot,
     "_remove_from_pending", "_pending.remove("),
    ("Discord _pending", discord_bot.DiscordBot, "_pending", "_pending.read("),
    ("Discord _pending_for", discord_bot.DiscordBot, "_pending_for",
     "_pending.entries_for("),
    ("Discord _drop_pending", discord_bot.DiscordBot, "_drop_pending",
     "_pending.remove("),
]
for who, owner, name, door in SOURCES:
    src = srcslice.method_source(owner, name, label=who)
    checks[f"{who} goes through pending"] = src is not None and door in src
    checks[f"…and does not open or parse the file itself ({who})"] = (
        src is not None and "open(" not in src and "json." not in src
        and "os.remove" not in src)
_web_src = open(os.path.join(APP, "web_ui.py"), encoding="utf-8").read()
_gp = srcslice.section(_web_src, "        def _get_pending(self, host=None):",
                       "\n        def ", label="Web _get_pending")
checks["Web _get_pending goes through pending"] = (
    _gp is not None and "_pending.read(" in _gp
    and "_pending.entries_for(" in _gp)
checks["…and does not open or parse the file itself (Web _get_pending)"] = (
    _gp is not None and "open(" not in _gp and "json." not in _gp)

shutil.rmtree(tmp, ignore_errors=True)
for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
