#!/usr/bin/env python3
"""Every module a function uses is one it can actually see (#2).

`/restore` on Discord failed with `name 'json' is not defined`, twice,
in front of the person who had volunteered to test it. `discord_bot.py`
has no module-level `import json` — every user imports it inside its own
function, which is a deliberate style in that file — and the new function
did not.

What made it expensive was not the missing line. It was that the original
code caught the NameError in the same `try` as the download and answered
"I could not read that attachment", so the actual fault never surfaced. I
diagnosed Discord's CDN instead, shipped that as the finding, and wrote
it into a changelog. @NotRetarded's second screenshot corrected me: once
the error messages told the truth, the failure moved *past* the fetch and
named itself.

A missing import is not a subtle bug. It is invisible until the line
runs, which for an error path can be weeks, and Python will not say a
word before then. So this walks every function in the app and checks that
each standard-library module it touches is bound somewhere it can see —
module level, its own body, or an enclosing function.

Static, deliberately: the point is to catch the branch nobody executed.
"""

import ast
import os
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")

#: The modules worth checking — the ones actually used in this codebase.
#: A generous list would flag every local variable that happens to share
#: a name with something in the standard library.
STDLIB = {
    "json", "os", "sys", "re", "time", "math", "socket", "shutil",
    "signal", "secrets", "hashlib", "hmac", "base64", "threading",
    "subprocess", "urllib", "datetime", "html", "io", "ipaddress",
    "textwrap", "traceback", "random", "string", "types", "glob",
    "tempfile", "sqlite3", "csv", "uuid", "platform", "logging",
}

checks = {}
problems = []


def bound_names(node):
    """Everything a scope binds: imports, assignments, args, defs."""
    out = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for a in child.names:
                out.add((a.asname or a.name.split(".")[0]))
        elif isinstance(child, ast.ImportFrom):
            for a in child.names:
                out.add(a.asname or a.name)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
            out.add(child.name)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            out.add(child.id)
        elif isinstance(child, ast.arg):
            out.add(child.arg)
        elif isinstance(child, ast.alias):
            out.add(child.asname or child.name.split(".")[0])
        elif isinstance(child, ast.ExceptHandler) and child.name:
            out.add(child.name)
    return out


def check_module(path):
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        problems.append(f"{os.path.basename(path)}: will not parse — {e}")
        return

    module_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                module_level.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                module_level.add(a.asname or a.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in ast.walk(node):
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    module_level.add(t.id)

    def walk(node, visible):
        here = visible | bound_names(node)
        for child in ast.walk(node):
            # `json.loads(...)` — a module used through an attribute.
            if (isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and isinstance(child.value.ctx, ast.Load)
                    and child.value.id in STDLIB
                    and child.value.id not in here):
                problems.append(
                    f"{os.path.basename(path)}:{child.lineno} "
                    f"{node.name if hasattr(node, 'name') else '<module>'}() "
                    f"uses `{child.value.id}.{child.attr}` with no import "
                    f"it can see")
        for child in node.body if hasattr(node, "body") else []:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                walk(child, here)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            walk(node, module_level)


for name in sorted(os.listdir(APP)):
    if name.endswith(".py"):
        check_module(os.path.join(APP, name))
notifiers = os.path.join(APP, "notifiers")
if os.path.isdir(notifiers):
    for name in sorted(os.listdir(notifiers)):
        if name.endswith(".py"):
            check_module(os.path.join(notifiers, name))

checks["every module used is one the code can see"] = not problems
for p in problems[:20]:
    print(f"  → {p}")

# The check has to be able to fail, or it proves nothing.
_probe = ast.parse("def f():\n    return json.loads('1')\n")
_before = len(problems)
problems.clear()
_saved = check_module
try:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("def f():\n    return json.loads('1')\n")
        probe_path = fh.name
    check_module(probe_path)
    checks["…and this check would notice if one were missing"] = bool(problems)
finally:
    os.unlink(probe_path)
    problems.clear()

# The specific one that got out. `discord_bot.py` imports json inside
# each function that needs it; a new function that forgot cost a tester
# two rounds and me a wrong diagnosis in a changelog.
dsrc = open(os.path.join(APP, "discord_bot.py"), encoding="utf-8").read()
i = dsrc.index("def _cmd_restore")
body = dsrc[i:dsrc.index("\n    def ", i + 10)]
checks["Discord's /restore imports the json it uses"] = "import json" in body

# ═══ a host that drops out is reported, not just logged ══════════════
# @famewolf added two hosts, the SSH out of the container could not
# authenticate, and his nightly check quietly covered one machine of
# three. The manual /check reported it; the scheduled one printed to a
# log he had no reason to open. Backwards — the unattended run is the
# one nobody is watching.
import types  # noqa: E402

sys.path.insert(0, APP)
from scheduler import Scheduler  # noqa: E402


def sched():
    s = Scheduler.__new__(Scheduler)
    s.sent = []
    s.bot = types.SimpleNamespace(
        enabled=True,
        send_message=lambda text, **kw: s.sent.append(text),
        t=lambda key, **kw: key + " " + repr(sorted(kw.items())),
        notifier=types.SimpleNamespace(has_channels=lambda: False))
    return s


s = sched()
s._host_failed("nas", RuntimeError("ssh: connect to host nas port 22"))
checks["a host dropping out of the scheduled run is reported"] = any(
    "host_check_failed" in m for m in s.sent)
checks["…naming the host and what went wrong"] = (
    "nas" in s.sent[0] and "ssh" in s.sent[0])

before = len(s.sent)
s._host_failed("nas", RuntimeError("still down"))
s._host_failed("nas", RuntimeError("still down"))
checks["…once, not every night"] = len(s.sent) == before

s._host_recovered("nas")
checks["…and again when it comes back"] = any(
    "host_check_recovered" in m for m in s.sent)
before = len(s.sent)
s._host_recovered("nas")
checks["…which is also said once"] = len(s.sent) == before

s2 = sched()
s2._host_recovered("never-failed")
checks["a host that never failed says nothing"] = s2.sent == []

# It must reach every channel, not only Telegram.
ssrc = open(os.path.join(APP, "scheduler.py"), encoding="utf-8").read()
_tell = ssrc[ssrc.index("    def _tell(self"):]
_tell = _tell[:_tell.index("\n    def ", 10)]
checks["the report goes to every channel"] = (
    "notifier.send_message" in _tell and "self.bot.send_message" in _tell)
checks["…and a failure to report cannot stop the check"] = (
    "except Exception" in _tell)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
