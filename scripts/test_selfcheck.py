#!/usr/bin/env python3
"""Docksentry answers "what can you see?" itself, without a shell.

On 20.09. I asked @NotRetarded twice for `docker exec … ls -la` to learn
whether one of his mounts covered a compose path. The first attempt
failed because I had typed my own container's name into his terminal;
the second worked and told me something Docksentry already knew. Every
Compose mount question in #2 and #65 has ended the same way — somebody
pasting shell output back at me for facts the program reads anyway.

`/audit` with no container name now reports them: who we are, how many
mounts we have, and for every Compose container whether its file is
readable — and when it is not, whether a mount already covers it. That
last distinction is the whole point: "mount it" and "your mount points
at the wrong directory" are different instructions, and the difference
was only ever visible from inside.

The findings live in the core so both chats say the same thing. A
finding implemented twice is two findings (#63).
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import selfcheck                                           # noqa: E402

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")


class _Reply:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode = stdout, returncode


class _Backend:
    """A daemon that answers `ps -a` and `inspect` with what we choose.

    Entries are `(name, image, [(src, dest)])`, or a fourth item with a
    volume name for the case a directory path cannot express — Portainer
    keeps its stacks in `portainer_data` and that is half of #2.
    """

    def __init__(self, containers):
        self._c = [tuple(c) + (None,) * (4 - len(c)) for c in containers]
        self.asked_all = False
        #: A daemon that will not talk to us at all.
        self.refuse = False

    def ps(self, *, all=False, quiet=False, fmt=None, timeout=None):
        self.asked_all = all
        if self.refuse:
            return _Reply("", 1)
        return _Reply(" ".join(n for n, _i, _m, _v in self._c))

    def inspect(self, refs, *, fmt=None, timeout=None):
        import json as _j
        if self.refuse:
            return _Reply("", 1)
        return _Reply(_j.dumps([
            {"Name": "/" + n, "Config": {"Image": i},
             "Mounts": [{"Type": "volume" if v else "bind", "Source": s,
                         "Name": v or "", "Destination": d}
                        for s, d in ms]}
            for n, i, ms, v in self._c]))


class _Checker:
    """A checker whose answers we choose, so the findings are the test."""

    def __init__(self, mounts, files, exists, containers=None,
                 own="DockSentry", data_dir="/docksentry"):
        self._m, self._f, self._e = mounts, files, exists
        self._own, self.config = own, types.SimpleNamespace(data_dir=data_dir)
        if containers is not None:
            self.backend = _Backend(containers)

    def _own_container_name(self):
        return self._own

    def _own_mounts(self):
        return self._m

    def get_running_containers(self):
        # What the Telegram handler asks for before anything else. A
        # stand-in without it makes the report come back empty and the
        # check pass on two header lines.
        return [{"name": n} for n in self._f]

    def _get_compose_info(self, name):
        # The real keys, asserted below — a stand-in that invents its own
        # is a stand-in that proves nothing. This one did: it answered
        # `config_file` while the checker returns `compose_file`, the
        # test stayed green, and every path came back empty the first
        # time it ran against a real daemon.
        return {"compose_file": self._f[name]} if name in self._f else {}

    def _compose_files(self, cfg, wd=None):
        return [cfg] if cfg else []


# ── the stand-in speaks the checker's language ───────────────────────
_uc = open(os.path.join(ROOT, "app", "update_checker.py"),
           encoding="utf-8").read()
import re                                                    # noqa: E402
# The LAST return block: the function opens with a bare `return {}` for
# "not a compose container", and splitting on the first one yields
# nothing at all — which would have made this check pass vacuously.
_body = _uc.split("def _get_compose_info")[1].split("\n    def ")[0]
_returns = _body.split("return {")[-1].split("}")[0]
_real_keys = set(re.findall(r'"([a-z_]+)"\s*:', _returns))
_sc = open(os.path.join(ROOT, "app", "selfcheck.py"), encoding="utf-8").read()
checks["the report reads the keys the checker really returns"] = (
    "compose_file" in _real_keys and "compose_dir" in _real_keys
    and 'info.get("compose_file")' in _sc
    and 'info.get("compose_dir")' in _sc)

# The live case: a mount that covers the path, and the file still absent.
DOCKMON = "/app/data/stacks/QNAP/dockmon/compose.yaml"
c = _Checker([("/share/stacks", "/app/data/stacks")], {"dockmon": DOCKMON}, set())
state = selfcheck.collect(c, ["dockmon"])
kinds = dict(selfcheck.findings(state))

checks["it knows its own name"] = kinds.get("self_name", {}).get("name") == "DockSentry"
checks["…and how many mounts it has"] = kinds.get("self_mounts", {}).get("count") == 1
checks["a covered path that is still missing reads as a wrong source"] = (
    "compose_wrong" in kinds)
checks["…naming the mount it found"] = (
    kinds["compose_wrong"]["src"] == "/share/stacks"
    and kinds["compose_wrong"]["dest"] == "/app/data/stacks")
checks["…and not telling them to mount it again"] = (
    "compose_no_mount" not in kinds)

# Nothing mounted at all: the old advice is still the right advice.
c2 = _Checker([], {"dockmon": DOCKMON}, set())
k2 = dict(selfcheck.findings(selfcheck.collect(c2, ["dockmon"])))
checks["with nothing mounted it says to mount it"] = "compose_no_mount" in k2
checks["…and names what to mount"] = (
    k2["compose_no_mount"]["mount"] == "/app/data/stacks")

# A path no manager claims still gets a directory. It printed
# "Mount  to reach it" with a hole in it — twelve times in one /audit,
# because Dockge's own `/opt/stacks` is deliberately not in KNOWN
# (it is a valid host path, so there is nothing to map).
c2b = _Checker([], {"Plex": "/opt/stacks/plex/compose.yaml"}, set())
k2b = dict(selfcheck.findings(selfcheck.collect(c2b, ["Plex"])))
checks["an unrecognised path names its own directory"] = (
    k2b["compose_no_mount"]["mount"] == "/opt/stacks/plex")
checks["…so the sentence never has a hole in it"] = all(
    p.get("mount") for kind, p in selfcheck.findings(selfcheck.collect(
        _Checker([], {"a": "/opt/stacks/plex/compose.yaml",
                      "b": DOCKMON}, set()), ["a", "b"]))
    if kind == "compose_no_mount")

# A readable file is not a finding to act on.
here = os.path.abspath(__file__)
c3 = _Checker([("/x", os.path.dirname(here))], {"self": here}, set())
k3 = dict(selfcheck.findings(selfcheck.collect(c3, ["self"])))
checks["a readable compose file reports as fine"] = "compose_ok" in k3
checks["…and raises neither complaint"] = (
    "compose_wrong" not in k3 and "compose_no_mount" not in k3)

# Containers that are not Compose at all are simply not in the report.
c4 = _Checker([], {}, set())
checks["a non-Compose container is not reported"] = (
    selfcheck.collect(c4, ["plain"])["compose"] == [])

# The headline counts what the lines say.
c5 = _Checker([("/share/stacks", "/app/data/stacks")],
              {"a": DOCKMON, "b": "/opt/other/compose.yml", "c": here}, set())
checks["the headline counts match the findings"] = (
    selfcheck.summary(selfcheck.collect(c5, ["a", "b", "c"])) == (3, 1, 1, 1))


# ── who DOES have it ─────────────────────────────────────────────────
# "Your source is wrong" ends the sentence one word before the answer.
# @NotRetarded read that line three times on 21.09. and wrote back
# "nothing in that section tells me where it really is" — and he was
# right: the daemon knows which container holds the file, and we were
# not asking (#63).
OURS = ("DockSentry", "amayer1983/docksentry:latest",
        [("/share/Container/stacks", "/app/data/stacks")])
DOCKGE = ("dockge", "louislam/dockge:1",
          [("/share/CACHEDEV1_DATA/stacks", "/app/data/stacks")])

c6 = _Checker([("/share/Container/stacks", "/app/data/stacks")],
              {"dockmon": DOCKMON}, set(), containers=[OURS, DOCKGE])
f6 = selfcheck.findings(selfcheck.collect(c6, ["dockmon"]))
k6 = dict(f6)
checks["the daemon is asked who really holds the file"] = "compose_holder" in k6
checks["…and it names the directory that would work"] = (
    k6.get("compose_holder", {}).get("src") == "/share/CACHEDEV1_DATA/stacks"
    and k6["compose_holder"]["dest"] == "/app/data/stacks")
checks["…and the container it read that off"] = (
    k6.get("compose_holder", {}).get("who") == "dockge")
checks["…and then does not claim it is on another machine"] = (
    "compose_elsewhere" not in k6)
checks["every container is inspected, not only the running ones"] = (
    c6.backend.asked_all is True)

# Our own mount is not an answer. It covers the path and demonstrably
# does not hold the file — that is what made the finding in the first
# place — so naming it back would hand them the directory they are
# trying to replace.
c7 = _Checker([("/share/Container/stacks", "/app/data/stacks")],
              {"dockmon": DOCKMON}, set(), containers=[OURS])
f7 = selfcheck.findings(selfcheck.collect(c7, ["dockmon"]))
k7 = dict(f7)
checks["our own mount is never named as the holder"] = "compose_holder" not in k7
checks["…instead it says the stack was made somewhere else"] = (
    "compose_elsewhere" in k7)

# Three stacks behind the same absent manager are one problem, not
# three lines of it.
c8 = _Checker([("/share/Container/stacks", "/app/data/stacks")],
              {"dockmon": DOCKMON,
               "tailscale": "/app/data/stacks/QNAP/tailscale/compose.yaml",
               "dockge": "/app/data/stacks/QNAP/dockge/compose.yaml"},
              set(), containers=[OURS])
f8 = selfcheck.findings(
    selfcheck.collect(c8, ["dockmon", "tailscale", "dockge"]))
checks["three missing stacks say it once"] = (
    sum(1 for kind, _p in f8 if kind == "compose_elsewhere") == 1)
checks["…while each still gets its own wrong-source line"] = (
    sum(1 for kind, _p in f8 if kind == "compose_wrong") == 3)

# A daemon that will not answer must not turn into "it is elsewhere".
# The check under this comment used to feed a READABLE file, which
# short-circuits before the lookup ever runs — it passed with the whole
# thing deleted. This is the case the comment names:
c9 = _Checker([("/share/Container/stacks", "/app/data/stacks")],
              {"dockmon": DOCKMON}, set(), containers=[OURS])
c9.backend.refuse = True
k9 = dict(selfcheck.findings(selfcheck.collect(c9, ["dockmon"])))
checks["a daemon that will not answer is reported as unknown"] = (
    "compose_holder_unknown" in k9)
checks["…and never as 'it is somewhere else'"] = "compose_elsewhere" not in k9

# Two containers mounted equally deep and nothing to choose between
# them is the same answer: I will not guess.
c10 = _Checker([("/mnt", "/app/data/stacks")], {"dockmon": DOCKMON}, set(),
               containers=[("boxA", "plainimage", [("/a", "/app/data/stacks")]),
                           ("boxB", "otherimage", [("/b", "/app/data/stacks")])])
k10 = dict(selfcheck.findings(selfcheck.collect(c10, ["dockmon"])))
checks["an ambiguous lookup is unknown too, not an absence"] = (
    "compose_holder_unknown" in k10 and "compose_elsewhere" not in k10)

# Mounting over our own data directory is what the web page has refused
# since #2, and the audit was handing it out: Portainer keeps its stacks
# in a volume at /data, and the legacy default put ours there too.
c11 = _Checker([("/x", "/data")], {"stack1": "/data/compose/1/docker-compose.yml"},
               set(), containers=[("portainer", "portainer/portainer-ce",
                                   [("", "/data")], "portainer_data")],
               data_dir="/data")
k11 = dict(selfcheck.findings(selfcheck.collect(c11, ["stack1"])))
checks["a holder that sits on our data directory is refused"] = (
    "compose_clash" in k11 and "compose_holder" not in k11)
checks["…and the refusal names the volume that has it"] = (
    k11["compose_clash"]["src"] == "portainer_data")
# …but only when it really is ours.
c12 = _Checker([("/x", "/data")], {"stack1": "/data/compose/1/docker-compose.yml"},
               set(), containers=[("portainer", "portainer/portainer-ce",
                                   [("", "/data")], "portainer_data")],
               data_dir="/docksentry")
checks["…and an unrelated data directory is not refused"] = (
    "compose_holder" in dict(selfcheck.findings(selfcheck.collect(c12, ["stack1"]))))

# Our own container is struck out by its MOUNTS as well as its name:
# `_own_container_name()` comes back empty often enough, and the name
# test alone then excluded nothing.
c13 = _Checker([("/wrong", "/app/data/stacks")], {"dockmon": DOCKMON}, set(),
               containers=[("me", "amayer1983/docksentry",
                            [("/wrong", "/app/data/stacks")])], own="")
k13 = dict(selfcheck.findings(selfcheck.collect(c13, ["dockmon"])))
checks["our own mount is not handed back when we do not know our name"] = (
    "compose_holder" not in k13)

# Nothing mounted at all, and the file is inside Portainer's volume: the
# generic line would name a host directory that does not exist.
c14 = _Checker([], {"stack1": "/data/compose/1/docker-compose.yml"}, set(),
               containers=[("portainer", "portainer/portainer-ce",
                            [("", "/data")], "portainer_data")],
               data_dir="/docksentry")
k14 = dict(selfcheck.findings(selfcheck.collect(c14, ["stack1"])))
checks["with nothing mounted it still names the volume that holds it"] = (
    k14.get("compose_holder", {}).get("src") == "portainer_data")

# One found and one positively not found: neither summary is true of the
# whole set, so neither is said.
c15 = _Checker([("/mnt", "/app/data/stacks"), ("/o", "/opt/x")],
               {"a": DOCKMON, "b": "/opt/x/compose.yml"}, set(),
               containers=[OURS, DOCKGE])
f15 = selfcheck.findings(selfcheck.collect(c15, ["a", "b"]))
checks["a mixed answer claims neither 'elsewhere' nor 'unknown'"] = not any(
    k in ("compose_elsewhere", "compose_holder_unknown") for k, _p in f15)
checks["…while still naming the one it did find"] = any(
    k == "compose_holder" for k, _p in f15)

checks["a readable compose file raises none of them"] = (
    not any(kind in ("compose_holder", "compose_elsewhere")
            for kind, _p in selfcheck.findings(selfcheck.collect(
                _Checker([("/x", os.path.dirname(here))], {"self": here},
                         set(), containers=[OURS]), ["self"]))))

# Every kind the core can emit has words in every language.
import json as _json                                         # noqa: E402
_langs = sorted(f for f in os.listdir(os.path.join(ROOT, "app", "lang"))
                if f.endswith(".json"))
# Every kind the core can emit, gathered from all the cases above —
# the earlier version of this looked at three of them and so never saw
# the two kinds added last.
_kinds = {kind for kind, _p in
          f6 + f7 + f8 + f15 + list(k2.items()) + list(k2b.items())
          + list(k9.items()) + list(k10.items()) + list(k11.items())
          + list(k14.items())}
# `compose_ok` is the one kind with no wording anywhere, on purpose:
# both chats drop it before translating, because a file we can read is
# not something to act on. Asserted just below, so the exclusion cannot
# quietly start hiding a real gap.
_kinds.discard("compose_ok")
_missing = []
for f in _langs:
    d = _json.load(open(os.path.join(ROOT, "app", "lang", f), encoding="utf-8"))
    for kind in _kinds:
        if f"selfaudit_{kind}" not in d:
            _missing.append(f"{f}:{kind}")
checks["every finding has wording in all 16 languages"] = (
    len(_langs) == 16 and not _missing
    # …and the set under test really contains the new ones, or this
    # check would pass by looking at nothing.
    # and the one kind that has no wording is the one both chats skip
    and "selfaudit_compose_ok" not in _json.load(
        open(os.path.join(ROOT, "app", "lang", "en.json"), encoding="utf-8"))
    and {"compose_holder", "compose_elsewhere", "compose_clash",
         "compose_holder_unknown", "compose_wrong", "compose_no_mount"}
    <= _kinds)

# ── the Telegram command actually runs ───────────────────────────────
# `/audit <name>` worked and a bare `/audit` answered with nothing at
# all. The handler reached for `self.checker`, which TelegramBot does
# not have: the AttributeError went up into the poll loop and the
# silence looked like a command that was never built (#63,
# @NotRetarded, 22.09.). The comment on `restart_self` in that same
# file had already said the attribute does not exist — a grep of the
# source would not have caught this, so the bot gets built here the way
# `main.py` builds it and the command gets called for real.
import ast                                                  # noqa: E402
import tempfile                                             # noqa: E402
import types                                                # noqa: E402

from container_store import ContainerStore                   # noqa: E402
from telegram_bot import TelegramBot                          # noqa: E402

_d = tempfile.mkdtemp()
_cfg = types.SimpleNamespace(
    bot_token="x", chat_id="1", language="en", debug=False,
    container_cli="docker", auto_update_all=False, update_policy="all",
    data_dir=_d, pending_file=os.path.join(_d, "p.json"),
    history_file=os.path.join(_d, "h.json"))
for _n in ("pinned", "autoupdate", "update_windows", "ask_before_major",
           "trust_running", "cooldown", "protect_stop", "major_pending",
           "groups", "notes", "links"):
    setattr(_cfg, f"{_n}_file", os.path.join(_d, f"{_n}.json"))
_bot = TelegramBot(_cfg, ContainerStore(_cfg))

checks["the bot really has no `checker` attribute"] = not hasattr(_bot, "checker")
try:
    _text = _bot._self_audit_text(_Checker([], {"Plex": "/opt/stacks/plex/compose.yaml"},
                                           set()))
    _err = ""
except Exception as _e:                                      # noqa: BLE001
    _text, _err = "", f"{type(_e).__name__}: {_e}"
checks["a bare /audit produces a report rather than an exception"] = (
    not _err and bool(_text.strip()))
checks["…and the report has the container in it"] = "Plex" in _text

# The rule behind it: nothing in this file may read `self.checker`.
_tg_src = open(os.path.join(ROOT, "app", "telegram_bot.py"),
               encoding="utf-8").read()
_tree = ast.parse(_tg_src)
_reads = [n.lineno for n in ast.walk(_tree)
          if isinstance(n, ast.Attribute) and n.attr == "checker"
          and isinstance(n.value, ast.Name) and n.value.id == "self"]
checks["no handler reaches for an attribute the class does not have"] = not _reads

# The handler passes on what it was given, rather than finding its own.
_branch = _tg_src.split(
    'text.startswith("/audit")')[1].split("\n        elif ")[0]
checks["the bare branch hands the checker down"] = (
    "_self_audit_text(checker)" in _branch)

# ── both chats, one answer ───────────────────────────────────────────
tg = open(os.path.join(ROOT, "app", "telegram_bot.py"), encoding="utf-8").read()
dc = open(os.path.join(ROOT, "app", "discord_bot.py"), encoding="utf-8").read()
for label, src in (("Telegram", tg), ("Discord", dc)):
    body = src.split("def _self_audit_text")[1].split("\n    def ")[0]
    checks[f"{label} asks the core for the findings"] = (
        "selfcheck.collect(" in body and "selfcheck.findings(" in body)
    checks[f"…{label} words them with its own translator"] = (
        'self.t(f"selfaudit_{kind}"' in body)
    checks[f"…{label} stays quiet about what is fine"] = (
        'kind == "compose_ok"' in body)

# Sliced at the function boundary rather than a character count — a
# docstring long enough to push the call out of view would have made
# this pass or fail for a reason that has nothing to do with the code.
checks["a bare /audit audits us, in Telegram"] = (
    "self._self_audit_text(checker)"
    in tg.split('startswith("/audit")')[1].split("\n        elif ")[0])
checks["…and in Discord"] = (
    "self._self_audit_text()"
    in dc.split("def _cmd_audit")[1].split("\n    def ")[0])
checks["Discord no longer demands a container name"] = (
    '"name": "container",' in dc.split('{"name": "audit"')[1][:400]
    and '"required": False' in dc.split('{"name": "audit"')[1][:400])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
