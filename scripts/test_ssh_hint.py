#!/usr/bin/env python3
"""Say why the SSH host refuses, not just that it did (#2, @famewolf).

He set up key-based login between his three machines, tested it, added
`DOCKER_HOSTS`, and lost two days to an instance that reported three
managed hosts and could reach one. When the error finally arrived
untruncated it said `Permission denied (publickey)` — correct, and
useless from where he was standing, because the keys *did* work.

They did. On the host. Docksentry runs in a container, and a container
has its own filesystem: `ssh-copy-id` wrote to `/root/.ssh` on the
machine, and the image has no `/root/.ssh` at all. He is the second
person to hit this, which is the point at which a message should stop
making people deduce it.

Everything checked here is a **measurement taken at the moment of
failure** — does that directory exist, does that file exist — not a
guess from the shape of the error. An error we cannot explain gets no
guess attached to it, because a confident wrong hint is worse than
none: it sends somebody looking in the place we named.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import hostdiag  # noqa: E402

checks = {}

REFUSED = ("could not list containers (rc=1): error during connect: command "
           "ssh -l root -- docknas.lan docker system dial-stdio has exited "
           "with exit status 255, stderr=ssh: Permission denied "
           "(publickey,password).")
HOSTKEY = ("error during connect: command ssh … has exited with exit status "
           "255, stderr=Host key verification failed.")
REFUSED_CONN = ("error during connect: ssh: connect to host nas port 22: "
                "Connection refused")

with tempfile.TemporaryDirectory() as tmp:
    empty = os.path.join(tmp, "nothing")            # no .ssh at all
    keyonly = os.path.join(tmp, "keyonly")          # key, no known_hosts
    full = os.path.join(tmp, "full")                # both
    os.makedirs(keyonly); os.makedirs(full)
    open(os.path.join(keyonly, "id_ed25519"), "w").write("k")
    open(os.path.join(full, "id_ed25519"), "w").write("k")
    open(os.path.join(full, "known_hosts"), "w").write("h")

    # ── his case ─────────────────────────────────────────────────────
    h = hostdiag.hint("ssh://root@docknas.lan", REFUSED, ssh_dir=empty)
    checks["a refused key with no .ssh in the container is explained"] = bool(h)
    checks["…saying the container has its own filesystem"] = (
        "container has its own filesystem" in h)
    checks["…and giving the mount that fixes it"] = f"-v {empty}:{empty}:ro" in h

    # ── mounted, but only the key ────────────────────────────────────
    h = hostdiag.hint("ssh://root@nas", REFUSED, ssh_dir=keyonly)
    checks["a mount without known_hosts is a different answer"] = (
        "known_hosts" in h and "-v" not in h.split("known_hosts")[0])

    # ── mounted properly: then it really is the key ──────────────────
    h = hostdiag.hint("ssh://root@nas", REFUSED, ssh_dir=full)
    checks["with both present it points at authorized_keys instead"] = (
        "authorized_keys" in h)

    # ── host key, which is a different failure ───────────────────────
    h = hostdiag.hint("ssh://root@nas", HOSTKEY, ssh_dir=empty)
    checks["an unknown host key gets its own explanation"] = (
        "unknown to the container" in h)
    h = hostdiag.hint("ssh://root@nas", HOSTKEY, ssh_dir=full)
    checks["…and with known_hosts present it does not suggest mounting"] = (
        "-v " not in h and "changed" in h)
    checks["…and never suggests turning the check off"] = (
        "StrictHostKeyChecking" not in h and "rather than removing" in h)

    # ── what must stay silent ────────────────────────────────────────
    checks["a refused connection is not blamed on keys"] = (
        hostdiag.hint("ssh://root@nas", REFUSED_CONN, ssh_dir=empty) == "")
    checks["a tcp host gets no ssh advice"] = (
        hostdiag.hint("tcp://nas:2375", REFUSED, ssh_dir=empty) == "")
    checks["…nor does a context endpoint"] = (
        hostdiag.hint("context://nas", REFUSED, ssh_dir=empty) == "")
    checks["an error we cannot place gets no guess"] = (
        hostdiag.hint("ssh://root@nas", "something entirely else",
                      ssh_dir=empty) == "")
    checks["…and neither does an empty one"] = (
        hostdiag.hint("ssh://root@nas", "", ssh_dir=empty) == "")

# ── the sentence comes out in the reader's language ─────────────────
# It did not, until 11.10.2026: five hard-coded English sentences hung
# under a finding that is translated in all sixteen languages. A German
# reader got the diagnosis in German and the one line telling him what
# to do in English.
from i18n import available_languages, get_translator  # noqa: E402

KEYS = ("hostdiag_ssh_no_dir", "hostdiag_ssh_no_known_hosts",
        "hostdiag_ssh_key_refused", "hostdiag_hostkey_unknown",
        "hostdiag_hostkey_changed")

_langs = available_languages()
checks["all sixteen languages carry the five sentences"] = (
    len(_langs) == 16
    and all(get_translator(lg)(k) != k for lg in _langs for k in KEYS))
_missing = [(lg, k) for lg in _langs for k in KEYS
            if get_translator(lg)(k) == k]
if _missing:
    print(f"      → missing: {_missing[:6]}")

# The in-module English floor and `en.json` have to say the same thing,
# or a caller with no translator gets a different sentence from one with.
_en = get_translator("en")
checks["the English floor matches the language file"] = all(
    hostdiag._EN[k] == _en(k) for k in KEYS)

# Driven, per language: the branch is reached and the sentence that
# comes back is the one from that language's file.
with tempfile.TemporaryDirectory() as _d:
    _empty = os.path.join(_d, "nothing")
    _mounted = os.path.join(_d, "ssh")
    os.makedirs(_mounted)
    _cases = [(REFUSED, _empty, "hostdiag_ssh_no_dir"),
              (REFUSED, _mounted, "hostdiag_ssh_no_known_hosts"),
              (HOSTKEY, _mounted, "hostdiag_hostkey_unknown")]
    for _lang in ("de", "ja", "ru", "en"):
        _t = get_translator(_lang)
        _ok = all(
            hostdiag.hint("ssh://root@nas", _err, ssh_dir=_dir, t=_t)
            == _t(_key, dir=_dir)
            for _err, _dir, _key in _cases)
        checks[f"a {_lang} reader gets the {_lang} sentence"] = _ok
    # …and the sentences really differ, or the check above would pass on
    # a translator that returns English for everything.
    checks["the languages are not all the same text"] = (
        hostdiag.hint("ssh://root@nas", REFUSED, ssh_dir=_empty,
                      t=get_translator("de"))
        != hostdiag.hint("ssh://root@nas", REFUSED, ssh_dir=_empty,
                         t=get_translator("ja")))
    # A translator that throws must not cost the reader the sentence.
    def _broken(key, **kw):
        raise RuntimeError("no")

    checks["a broken translator still yields English"] = (
        hostdiag.hint("ssh://root@nas", REFUSED, ssh_dir=_empty,
                      t=_broken).startswith("There is no"))

# ── and every place that reports a host failure asks in the language ─
# Read as a tree, not as a grep. `"hostdiag.hint" in src` was what
# stood here, and a commented-out call satisfies it — which is how
# `/hosts check` was dead on both connections with three checks green.
import ast  # noqa: E402

APP = os.path.join(os.path.dirname(__file__), "..", "app")
SITES = {"scheduler.py": 1, "telegram_bot.py": 3, "discord_bot.py": 1}
_found, _untranslated = {}, []
for _file in SITES:
    _src = open(os.path.join(APP, _file), encoding="utf-8").read()
    _calls = [n for n in ast.walk(ast.parse(_src))
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "hint"
              and isinstance(n.func.value, ast.Name)
              and n.func.value.id == "hostdiag"]
    _found[_file] = len(_calls)
    _untranslated += [f"{_file}:{n.lineno}" for n in _calls
                      if not any(kw.arg == "t" for kw in n.keywords)]

checks["every reporting surface asks for the hint"] = _found == SITES
checks["…and every one of them asks in the reader's language"] = (
    not _untranslated)
if _untranslated:
    print(f"      → no translator at: {_untranslated}")
if _found != SITES:
    print(f"      → call sites: {_found}, expected {SITES}")

# The hint must never be the reason a check fails.
for _file in ("scheduler.py", "telegram_bot.py"):
    _src = open(os.path.join(APP, _file), encoding="utf-8").read()
    i = _src.index("import hostdiag")
    checks[f"a broken hint cannot break {_file[:-3]}"] = (
        "except Exception" in _src[i:i + 700])

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
