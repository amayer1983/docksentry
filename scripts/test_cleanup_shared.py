#!/usr/bin/env python3
"""/cleanup: one host walk, one wording, and no doubled checkmarks.

Three things were wrong, and the first one had been wrong in every
language since the wording changed underneath it:

  * Both chats guarded the "nothing to clean" case with `"Nothing" in
    msg`. The message has said "✅ No unused images found." for a long
    time — no "Nothing" in it, in English or in any of the other fifteen
    — so the branch never fired and the fallback added a SECOND ✅. The
    most common outcome of the most-run maintenance command rendered as
    "✅ ✅ No unused images found." everywhere.
  * Discord ran the cleanup on the local host only. The reasoning was
    sound (cleanup is a write, writes stay local) and the conclusion was
    still wrong: @famewolf's dockmox was the box that was full, and it
    was not the local one (#2). Telegram walked every host; Discord did
    not.
  * `cleanup_images` built three of its own sentences in English —
    "Backed up N local image(s) → …", "Removed: …", "Cleanup error: …" —
    so a German reader got a German summary with English fragments in it.

Icons now live in the keys, like every other message, and no caller
prefixes one.
"""
import glob
import json
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import container_flags as cf                      # noqa: E402
from i18n import get_translator                   # noqa: E402
from update_checker import UpdateChecker          # noqa: E402

checks = {}
LANGS = sorted(glob.glob(os.path.join(APP, "lang", "*.json")))
checks["all 16 languages are examined"] = len(LANGS) == 16


class Prune:
    """A backend whose prune returns whatever docker would have said."""
    def __init__(self, stdout="", rc=0, stderr=""):
        self.stdout, self.rc, self.stderr = stdout, rc, stderr

    def image_prune(self, **kw):
        return types.SimpleNamespace(returncode=self.rc, stdout=self.stdout,
                                     stderr=self.stderr)


def checker(backend, lang="en"):
    cls = type("Ck", (UpdateChecker,), {"backend": backend})
    c = cls.__new__(cls)
    c.config = types.SimpleNamespace(cleanup_grace_hours=24,
                                     cleanup_backup_local_only=False,
                                     debug=False)
    c._t = get_translator(lang)
    return c


# ── the message says one thing, once ─────────────────────────────────
for lang in ("en", "de", "ja"):
    ok, msg = checker(Prune(), lang).cleanup_images()
    checks[f"{lang}: an empty cleanup is one ✅, not two"] = (
        ok is True and msg.count("✅") == 1)

RECLAIMED = ("Untagged: nginx:1.2\nUntagged: redis:7\nUntagged: a:1\n"
             "Untagged: b:1\nUntagged: c:1\nUntagged: d:1\nUntagged: e:1\n"
             "Total reclaimed space: 662.9MB")
ok, msg = checker(Prune(RECLAIMED), "de").cleanup_images()
checks["a successful cleanup carries its own ✅"] = ok and msg.startswith("✅")
checks["…and exactly one"] = msg.count("✅") == 1
# The three fragments that used to stay English whatever you had set.
checks["…and the removed-images line is translated"] = "Entfernt:" in msg
checks["…including the overflow count"] = "weitere" in msg
# Docker writes "8.534MB", where a dot is the decimal point. The owner
# read one of those as 8 GB (#63), so we reformat rather than pass through.
checks["…and the size is ours, not docker's"] = (
    "663 MB" in msg and "662.9MB" not in msg)

ok, msg = checker(Prune(rc=1, stderr="daemon gone"), "de").cleanup_images()
checks["a failed prune carries ❌ and the reason"] = (
    ok is False and msg.startswith("❌") and "daemon gone" in msg)


class Boom:
    def image_prune(self, **kw):
        raise RuntimeError("connection refused")


ok, msg = checker(Boom(), "de").cleanup_images()
checks["a raising prune is reported in the reader's language"] = (
    ok is False and "Fehler beim Aufräumen" in msg)

# The dead branch itself: no front end may test the message for English.
for f in ("telegram_bot.py", "discord_bot.py", "web_ui.py"):
    src = open(os.path.join(APP, f), encoding="utf-8").read()
    checks[f"{f} does not sniff the cleanup message for English"] = (
        '"Nothing" in' not in src)

# Nobody prefixes an icon onto a message that already has one.
web = open(os.path.join(APP, "web_ui.py"), encoding="utf-8").read()
checks["the Web UI does not prefix its own ✅/❌ either"] = (
    "'✅' if ok else '❌'" not in web)


# ── the host walk ────────────────────────────────────────────────────
class Host:
    def __init__(self, name, local=False):
        self.name, self.is_local = name, local


hosts = [Host("local", local=True), Host("nas"), Host("dockmox")]
seen = []


def guarded(ck):
    seen.append(ck.name)
    return ck.result


def mk(name, result):
    return types.SimpleNamespace(name=name, result=result)


by = {h.name: mk(h.name, (True, "✅ done")) for h in hosts}
o = cf.cleanup(hosts, checker_for=lambda h: by[h.name], guarded_run=guarded)
checks["every managed host is cleaned, not just the local one"] = (
    seen == ["local", "nas", "dockmox"])
checks["…and each answers separately"] = len(o.replies) == 3
checks["…tagged with the host it came from"] = (
    [r.host.name for r in o.replies] == ["local", "nas", "dockmox"])

# One dead host must not stop the rest — the box you need to reach is
# usually the broken one.
seen.clear()
def flaky(ck):
    seen.append(ck.name)
    if ck.name == "nas":
        raise RuntimeError("connection refused")
    return ck.result

o = cf.cleanup(hosts, checker_for=lambda h: by[h.name], guarded_run=flaky)
checks["an unreachable host does not stop the walk"] = (
    seen == ["local", "nas", "dockmox"] and len(o.replies) == 3)
bad = [r for r in o.replies if not r.ok]
checks["…and is reported as the failure it is"] = (
    len(bad) == 1 and bad[0].key == "cleanup_error"
    and "connection refused" in bad[0].params["error"])

# A skipped cleanup (an update holds the mutex) is not a failure.
o = cf.cleanup([None], checker_for=lambda h: mk("x", (None, "⏳ busy")),
               guarded_run=lambda ck: ck.result)
checks["a skip is reported, but not as an error"] = (
    o.replies[0].ok is True and not o.changed)
checks["…and says what it says, not a second version of it"] = (
    o.replies[0].text == "⏳ busy")

o = cf.cleanup([None], checker_for=lambda h: None, guarded_run=guarded)
checks["a host with no backend says so"] = (
    o.replies[0].key == "chan_no_backend" and not o.replies[0].ok)


# ── both front ends go through it ────────────────────────────────────
tb = open(os.path.join(APP, "telegram_bot.py"), encoding="utf-8").read()
db = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
i = tb.index('elif text == "/cleanup":')
tg_block = tb[i:tb.index("\n        elif text ==", i + 10)]
checks["Telegram /cleanup calls the core"] = "container_flags.cleanup(" in tg_block
checks["…and keeps no per-host loop of its own"] = "for host" not in tg_block

j = db.index("def _cmd_cleanup")
dc_block = db[j:db.index("\n    def ", j + 10)]
checks["Discord /cleanup calls the core"] = "container_flags.cleanup(" in dc_block
checks["…and no longer stops at the local host"] = "_hosts_for(None)" in dc_block

# The four new keys exist everywhere, with their placeholders intact —
# a key that exists but drops {size} renders a sentence with a hole in it.
PLACEHOLDERS = {"cleanup_backed_up": ("{count}", "{dir}"),
                "cleanup_removed": ("{images}",),
                "cleanup_more": ("{count}",),
                "cleanup_error": ("{error}",)}
for key, needed in PLACEHOLDERS.items():
    missing = [os.path.basename(f) for f in LANGS
               if key not in json.load(open(f, encoding="utf-8"))]
    checks[f"{key} exists in every language"] = missing == []
    holes = [os.path.basename(f) for f in LANGS
             for d in [json.load(open(f, encoding="utf-8"))]
             if key in d and any(p not in d[key] for p in needed)]
    checks[f"…and keeps its placeholders"] = holes == []

# And update_checker builds none of those sentences itself any more.
# Parsed, not grepped: the first version of this check flagged the
# function's own docstring and a comment quoting docker's output, which
# is how a scan certifies a problem that is not there — the mirror image
# of the scan that certified none while there were 78 (#63).
import ast  # noqa: E402

uc_src = open(os.path.join(APP, "update_checker.py"), encoding="utf-8").read()
fn = next(n for n in ast.walk(ast.parse(uc_src))
          if isinstance(n, ast.FunctionDef) and n.name == "cleanup_images")
doc = ast.get_docstring(fn, clean=False)
# A debug line is for the log, not for the reader — subtract anything
# sitting inside a `_debug(...)` / `print(...)` call.
logged = set()
for node in ast.walk(fn):
    if (isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("_debug", "print", "log")):
        for sub in ast.walk(node):
            logged.add(id(sub))
english = []
for node in ast.walk(fn):
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        continue
    v = node.value
    if v == doc or len(v) < 12 or len(v.split()) < 3 or id(node) in logged:
        continue
    english.append(v)
checks["cleanup_images writes no English of its own"] = english == []

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
