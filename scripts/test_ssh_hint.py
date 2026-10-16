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
    # The host path on the left, the container path on the right. Both
    # sides were `{dir}` — the path *inside* the container — so a reader
    # who is not root on the host got a left-hand side that does not
    # exist, Docker made an empty directory, the keys were still
    # missing, and the sentence had looked right.
    checks["…and giving the mount that fixes it"] = f"-v ~/.ssh:{empty}:ro" in h
    checks["…with the host's own path on the left, not the container's"] = (
        f"-v {empty}:{empty}:ro" not in h)

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

# The hint must never be the reason a check fails — at EVERY call
# site, not at the first one. `"except Exception" in src[i:i+700]`
# after the first `import hostdiag` was the check, and Telegram has
# three: stripping the try/except from the third left it green.
#
# As a tree: every `hostdiag.hint` call has to sit inside a `Try` that
# catches something. A window of 700 characters says nothing about
# which call it landed near.
def _inside_lambda(tree, target):
    """Is this call the body of a lambda handed to somebody else?"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda):
            for sub in ast.walk(node):
                if sub is target:
                    return True
    return False


_unprotected = []
for _file in SITES:
    _src = open(os.path.join(APP, _file), encoding="utf-8").read()
    _t = ast.parse(_src)
    _protected = set()
    for _node in ast.walk(_t):
        if not isinstance(_node, ast.Try):
            continue
        for _sub in ast.walk(_node):
            if (isinstance(_sub, ast.Call)
                    and isinstance(_sub.func, ast.Attribute)
                    and _sub.func.attr == "hint"
                    and isinstance(_sub.func.value, ast.Name)
                    and _sub.func.value.id == "hostdiag"):
                _protected.add(_sub.lineno)
    for _node in ast.walk(_t):
        if (isinstance(_node, ast.Call)
                and isinstance(_node.func, ast.Attribute)
                and _node.func.attr == "hint"
                and isinstance(_node.func.value, ast.Name)
                and _node.func.value.id == "hostdiag"
                and _node.lineno not in _protected
                # The two pre-flight sites hand the hint to
                # `hostcheck.preflight` as a lambda, and preflight
                # catches it there instead. Driven below rather than
                # assumed, because "somebody else catches it" is the
                # kind of belief this file exists to stop.
                and not isinstance(_node, ast.Lambda)
                and not _inside_lambda(_t, _node)):
            _unprotected.append(f"{_file}:{_node.lineno}")

checks["a broken hint cannot break the sites that call it directly"] = (
    not _unprotected)
if _unprotected:
    print(f"      → unprotected: {_unprotected}")

# …and the two that pass it on: `preflight` has to swallow a hint that
# throws, or the pre-flight dies on the one host it was asked about.
def _raises(ep, why, **kw):
    raise RuntimeError("the hint blew up")


class _Dead:
    def build(self, args):
        return ["docker"] + args

    def run(self, args, **kw):
        import types as _t2
        return _t2.SimpleNamespace(returncode=1, stdout="",
                                   stderr="Permission denied (publickey).")


import hostcheck as _hc  # noqa: E402

try:
    _found = [k for k, _p in _hc.preflight("ssh://nas", _Dead(),
                                           hint=_raises)]
    checks["…and a hint that throws does not take the pre-flight down"] = (
        _found == ["denied_ssh"])
except Exception as _e:                                     # noqa: BLE001
    checks["…and a hint that throws does not take the pre-flight down"] = False
    print(f"      → preflight raised: {_e}")

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
