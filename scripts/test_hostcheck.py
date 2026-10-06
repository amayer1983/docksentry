#!/usr/bin/env python3
"""The step before adding a host, which did not exist (#7, @LeeNX).

He read the docs, looked at his podman boxes and said it plainly: "I
feel like there should be a setup step and tests that can be done before
adding the hub pointing at the agent host." There was not. You put an
endpoint in `DOCKER_HOSTS`, restarted, and found out it worked when
something failed.

The case that makes this worth having is not the dead host — that one
announces itself. It is the socket proxy that allows `GET
/containers/json` and nothing else: it answers a ping, lists containers,
looks perfectly healthy, and cannot be updated from. So the check does
the things we actually do, in order, and names the one that failed.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import hostcheck                                            # noqa: E402

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")


class _Backend:
    """A daemon whose answers we choose, keyed on the first two args."""

    def __init__(self, answers, raise_on=None):
        self._a, self._raise = answers, raise_on
        self.asked = []

    def run(self, args, timeout=None):
        key = " ".join(args[:2])
        self.asked.append(key)
        if self._raise and key.startswith(self._raise):
            raise OSError("ssh: connect to host nas port 22: No route to host")
        rc, out, err = self._a.get(key, (1, "", "no answer"))
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


HEALTHY = {"version --format": (0, "27.1.1", ""),
           # Plain `version`, which is what the engine probe asks now:
           # the first version asked for `{{.Server.Components}}`, a
           # field Podman does not have, so every Podman host came back
           # as "docker" — and this file only passed because it made up
           # an answer no podman build gives. Measured on podman 4.9.3:
           # "can't evaluate field Components in type *define.Version".
           "version": (0, "Client: Docker Engine - Community\n"
                          "Server: Docker Engine - Community", ""),
           "ps -aq": (0, "abc123def456\n789abc012def", ""),
           "inspect abc123def456": (0, '[{"Name": "/web"}]', "")}


def kinds(findings):
    return [k for k, _p in findings]


# ── an endpoint we do not speak is caught before anything is tried ───
b = _Backend({})
f = hostcheck.preflight("http://nas:2375", b)
checks["an endpoint we do not speak is named as such"] = (
    kinds(f) == ["scheme_bad"])
checks["…without asking the network anything"] = b.asked == []
checks["…and it is not usable"] = hostcheck.usable(f) is False

# ── nothing answering is one finding, not a stack trace ──────────────
f = hostcheck.preflight("ssh://nas", _Backend({}))
checks["an endpoint nobody answers is unreachable"] = kinds(f) == ["unreachable"]
checks["…and says what the CLI said"] = "no answer" in dict(f)["unreachable"]["error"]

# …and a raising transport is the same answer, not a crash.
f = hostcheck.preflight("ssh://nas", _Backend({}, raise_on="version"))
checks["a transport that raises is reported, not propagated"] = (
    kinds(f) == ["unreachable"] and "No route to host" in dict(f)["unreachable"]["error"])

# The hint is asked only on failure, and only then.
seen = []
hostcheck.preflight("ssh://nas", _Backend({}),
                    hint=lambda ep, err: seen.append((ep, err)) or "mount ~/.ssh")
checks["a failure gets the ssh hint attached"] = len(seen) == 1
seen.clear()
hostcheck.preflight("ssh://nas", _Backend(HEALTHY),
                    hint=lambda ep, err: seen.append(1) or "x")
checks["…and a working host is not asked for one"] = seen == []

# ── the healthy host answers all four, in order ──────────────────────
f = hostcheck.preflight("ssh://nas", _Backend(HEALTHY))
checks["a healthy host answers every question"] = (
    kinds(f) == ["engine", "can_list", "can_inspect", "ready"])
checks["…naming the engine and version that answered"] = (
    dict(f)["engine"] == {"engine": "docker", "version": "27.1.1"})
checks["…and how many containers it saw"] = dict(f)["can_list"]["count"] == 2
checks["…and it is usable"] = hostcheck.usable(f) is True

# Podman names itself in the plain output, which is the only form both
# engines answer. Taken verbatim from `podman version` on 4.9.3.
pod = dict(HEALTHY)
pod["version --format"] = (0, "4.9.3", "")
pod["version"] = (0, "Client:       Podman Engine\nVersion:      4.9.3", "")
f = hostcheck.preflight("context://nas", _Backend(pod))
checks["podman is recognised from what it calls itself"] = (
    dict(f)["engine"] == {"engine": "podman", "version": "4.9.3"})
checks["…and context:// is an endpoint we speak"] = "scheme_bad" not in kinds(f)

# An engine that answers a version and will not say what it is gets an
# honest finding rather than a guess. The first version guessed "docker".
mute = dict(pod)
mute["version"] = (1, "", "nope")
f = hostcheck.preflight("ssh://nas", _Backend(mute))
checks["an engine that will not name itself is not guessed at"] = (
    "engine_unsure" in kinds(f) and "engine" not in kinds(f))

# ── the one that matters: lists fine, cannot inspect ─────────────────
proxy = dict(HEALTHY)
proxy["inspect abc123def456"] = (1, "", "403 Forbidden")
f = hostcheck.preflight("tcp://proxy:2375", _Backend(proxy))
checks["a socket proxy that only lists is caught"] = (
    kinds(f) == ["engine", "can_list", "cannot_inspect"])
checks["…and is not called usable"] = hostcheck.usable(f) is False
checks["…with the server's own words"] = (
    "403" in dict(f)["cannot_inspect"]["error"])

# …and one that cannot even list stops there.
nolist = {"version --format": (0, "27.1.1", ""),
          "version": (0, "Server: Docker Engine", ""),
          "ps -aq": (1, "", "permission denied")}
f = hostcheck.preflight("tcp://proxy:2375", _Backend(nolist))
checks["a daemon that refuses to list stops at that"] = (
    kinds(f) == ["engine", "cannot_list"])

# ── an empty host leaves the permission question open, and says so ───
empty = {"version --format": (0, "27.1.1", ""),
         "version": (0, "Server: Docker Engine", ""), "ps -aq": (0, "", "")}
f = hostcheck.preflight("ssh://fresh", _Backend(empty))
checks["a host with no containers says the inspect right is untested"] = (
    kinds(f) == ["engine", "can_list", "no_containers"])
checks["…and is not blocked for it"] = hostcheck.usable(f) is True
checks["…and is not called ready either"] = "ready" not in kinds(f)

# ── nothing is written anywhere ──────────────────────────────────────
b = _Backend(HEALTHY)
hostcheck.preflight("ssh://nas", b)
checks["a pre-flight only reads"] = all(
    a.split()[0] in ("version", "ps", "inspect") for a in b.asked)

# ── both chats ask the core, and say the same thing ──────────────────
# The three checks that stood here were `"hostcheck.preflight(" in body`
# and friends. An auditor gutted Discord's renderer to `return "nothing
# to see here"`, left the words in a docstring, and all three passed —
# and they never compared the two texts at all, which differ: Discord
# clips, Telegram does not. So render both and look.
import types as _t                                            # noqa: E402

from discord_bot import DiscordBot                            # noqa: E402
from telegram_bot import TelegramBot                          # noqa: E402
import container_backend as _cb                               # noqa: E402


def _rendered(cls, endpoint, answers):
    bot = cls.__new__(cls)
    bot.config = _t.SimpleNamespace(language="en", container_cli="docker",
                                    bot_token="", chat_id="")
    bot.application_id, bot.rest = "", None
    bot.log = lambda *_a: None
    # `t` is set in __init__ on the Telegram side and is a property on
    # the Discord one, so give the instance one either way.
    from i18n import get_translator
    try:
        bot.t = get_translator("en")
    except AttributeError:
        pass
    real = _cb.RemoteBackend
    _cb.RemoteBackend = lambda *a, **kw: _Backend(answers)
    try:
        return bot._hostcheck_text(endpoint)
    finally:
        _cb.RemoteBackend = real


_tg_text = _rendered(TelegramBot, "ssh://nas", HEALTHY)
_dc_text = _rendered(DiscordBot, "ssh://nas", HEALTHY)
checks["Telegram renders the findings, not a placeholder"] = (
    "27.1.1" in _tg_text and "web" in _tg_text)
# Same findings, different markdown dialect: Telegram writes *bold*
# and Discord **bold**. That difference is the connection's business —
# anything beyond it is not.
def _plain(t):
    return t.replace("**", "*")


checks["…and Discord says the same thing about the same endpoint"] = (
    _plain(_dc_text) == _plain(_tg_text))

_tg_bad = _rendered(TelegramBot, "http://nas", {})
checks["…including when the endpoint is one we do not speak"] = (
    "http://nas" in _tg_bad
    and _plain(_rendered(DiscordBot, "http://nas", {})) == _plain(_tg_bad))

# …and the command is reachable at all. Make the dispatch unreachable
# and nothing used to notice.
_tgsrc = open(os.path.join(ROOT, "app", "telegram_bot.py"),
              encoding="utf-8").read()
_dcsrc = open(os.path.join(ROOT, "app", "discord_bot.py"),
              encoding="utf-8").read()
_tg_branch = _tgsrc.split('elif text.startswith("/hosts")')[1][:700]
checks["/hosts check reaches the pre-flight in Telegram"] = (
    '_parts[1] == "check"' in _tg_branch
    and "self._hostcheck_text(_parts[2])" in _tg_branch)
_dc_branch = _dcsrc.split("def _cmd_hosts")[1][:600]
checks["…and the option reaches it in Discord"] = (
    'opts or {}).get("check")' in _dc_branch
    and "self._hostcheck_text(_ep)" in _dc_branch)
checks["…and Discord registers the option so it can be typed"] = (
    '"name": "check"' in _dcsrc.split('{"name": "hosts"')[1][:400])

# every kind has wording, in every language
import json                                                  # noqa: E402

LANGS = sorted(f for f in os.listdir(os.path.join(ROOT, "app", "lang"))
               if f.endswith(".json"))
# The module's own list, not a copy of it: a new finding kind with no
# wording would otherwise slip past, because the check would only ever
# look at the copy.
KINDS = set(hostcheck.KINDS)
missing = []
for f_ in LANGS:
    d = json.load(open(os.path.join(ROOT, "app", "lang", f_), encoding="utf-8"))
    for k in KINDS | {"header", "usage"}:
        if f"hostcheck_{k}" not in d:
            missing.append(f"{f_}:{k}")
checks["every finding has wording in all 16 languages"] = (
    len(LANGS) == 16 and not missing)
if missing:
    print("      → " + ", ".join(missing[:8]))

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
