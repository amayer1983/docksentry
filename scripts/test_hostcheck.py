#!/usr/bin/env python3
"""The step before adding a host, which did not exist (#7, @LeeNX).

He read the docs, looked at his podman boxes and said it plainly: "I
feel like there should be a agent setup step and tests that can be done
before adding the hub dockSentry pointing at the agent host."

Note what he asked for and what this is: he described an AGENT, and
there is no agent. What exists now is the second half of his sentence —
the tests you can run before pointing anything at anything. Before it
there was nothing: you put an endpoint in `DOCKER_HOSTS`, restarted, and
found out it worked when something failed.

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
        # NOT "no answer": that is also the module's own fallback for an
        # empty stderr, so a check asserting it appears in the output was
        # satisfied either way. Replacing the line with the literal kept
        # the check green — measured.
        rc, out, err = self._a.get(
            key, (1, "", "dial tcp 10.0.0.5:2375: i/o timeout"))
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
checks["…and says what the CLI said"] = (
    "i/o timeout" in dict(f)["unreachable"]["error"])

# …and a raising transport is the same answer, not a crash.
f = hostcheck.preflight("ssh://nas", _Backend({}, raise_on="version"))
checks["a transport that raises is reported, not propagated"] = (
    kinds(f) == ["unreachable"] and "No route to host" in dict(f)["unreachable"]["error"])

# The hint is asked only on failure, and only then.
seen = []
hostcheck.preflight("ssh://nas", _Backend({}),
                    hint=lambda ep, err: seen.append((ep, err)) or "mount ~/.ssh")
checks["a failure gets the ssh hint attached"] = len(seen) == 1
# …and it is in the finding, not merely computed. Throwing the result
# away kept the check above green.
_hinted = hostcheck.preflight(
    "ssh://nas", _Backend({}), hint=lambda ep, err: "mount ~/.ssh read-only")
checks["…and the hint travels with the finding"] = (
    dict(_hinted)["unreachable"].get("hint") == "mount ~/.ssh read-only")
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

# ── a refusal is not silence ─────────────────────────────────────────
# "Nothing answered at `unix:///var/run/docker.sock` — permission
# denied" is the sentence a socket without rights used to produce, and
# it sends the most common case of all — rootless podman, the wrong
# group, SELinux — looking at the network. Something did answer.
denied = {"version --format":
          (1, "", "permission denied while trying to connect to the "
                  "Docker API at unix:///var/run/docker.sock")}
f = hostcheck.preflight("unix:///var/run/docker.sock", _Backend(denied))
checks["a socket that answers and refuses is not called unreachable"] = (
    kinds(f) == ["denied"])
checks["…and a refused socket is not usable"] = hostcheck.usable(f) is False
checks["…while a timeout stays unreachable"] = (
    kinds(hostcheck.preflight("tcp://nas:2375", _Backend({}))) == ["unreachable"])

# …and the three sentences that are NOT a refusal, however much the
# wording looks like one. The docker cli prints "Cannot connect to the
# Docker daemon … Is the docker daemon running?" both when the rights
# are missing and when nothing is listening, so matching that phrase
# reported a stopped daemon as a permission problem and sent the reader
# to his socket group when the answer was `systemctl start docker`.
# Three separate audits found that by three different routes.
for _txt, _want in (
        ("Cannot connect to the Docker daemon at unix:///var/run/"
         "docker.sock. Is the docker daemon running?", "unreachable"),
        ("stat /var/run/docker.sock: no such file or directory", "unreachable"),
        # Both shapes in one sentence, which rootless Podman really
        # does produce. The permission word has to win: there IS
        # something there and it is the rights.
        ("Cannot connect to Podman. Please verify your connection to the "
         "Linux system using `podman system connection list`: permission "
         "denied", "denied"),
        ("ssh: connect to host nas port 22: Connection refused", "unreachable"),
        ("Got permission denied while trying to connect to the Docker "
         "daemon socket", "denied")):
    _f = hostcheck.preflight("unix:///var/run/docker.sock",
                             _Backend({"version --format": (1, "", _txt)}))
    checks[f"'{_txt[:34]}…' is {_want}"] = kinds(_f) == [_want]

# …and WHO refused decides, not which scheme was typed. `ssh://bob@nas`
# where the login works and `bob` is not in the `docker` group produces
# a socket refusal over an ssh connection — the answer is
# `usermod -aG docker bob`, and reading it as a key problem sends the
# reader to his `authorized_keys`. The first version split on the
# scheme and got exactly this backwards, on the most ordinary `ssh://`
# failure there is.
_DOCKER_DENIED = ("permission denied while trying to connect to the "
                  "Docker daemon socket at unix:///var/run/docker.sock")
for _ep, _err, _want in (
        ("ssh://bob@nas", _DOCKER_DENIED, "denied"),
        ("ssh://bob@nas", "bob@nas: Permission denied (publickey).", "denied_ssh"),
        # `context://` reaches a remote Podman over ssh too — the README
        # calls it the only way to give one its own key — so it needs the
        # ssh answer as much as `ssh://` does, and the scheme split left
        # it with the socket advice and no hint at all.
        ("context://pve1", "ssh: Permission denied (publickey).", "denied_ssh"),
        ("unix:///var/run/docker.sock", _DOCKER_DENIED, "denied")):
    _f = hostcheck.preflight(_ep, _Backend({"version --format": (1, "", _err)}))
    checks[f"{_ep.split(':')[0]} + {_err[:28]}… is {_want}"] = (
        kinds(_f) == [_want])

# An ssh host that refuses the KEY is not a socket permission problem,
# and the socket advice (mount, group, SELinux) is the wrong way to send
# that reader — `hostdiag` appends the right sentence directly below it.
_ssh = hostcheck.preflight(
    "ssh://root@nas",
    _Backend({"version --format": (1, "", "root@nas: Permission denied (publickey).")}))
checks["an ssh key refusal is its own finding, not the socket's"] = (
    kinds(_ssh) == ["denied_ssh"])
checks["…and still blocks the host"] = hostcheck.usable(_ssh) is False

# ── a container that vanished says nothing about the host ────────────
# `ps -aq` lists the NEWEST first, which is exactly the `--rm` backup
# job most likely to be gone a moment later. Judging on that one id
# reported a healthy host as a socket proxy that cannot be inspected.
vanished = dict(HEALTHY)
vanished["ps -aq"] = (0, "9f1aa0deadbe\nabc123def456\n789abc012def", "")
vanished["inspect 9f1aa0deadbe"] = (
    1, "", "Error response from daemon: No such container: 9f1aa0deadbe")
f = hostcheck.preflight("tcp://nas:2375", _Backend(vanished))
checks["a container that disappeared does not condemn the host"] = (
    kinds(f) == ["engine", "can_list", "can_inspect", "ready"])
checks["…and the next one is the one reported"] = (
    dict(f)["can_inspect"]["name"] == "web")
# …and when every candidate it tried had finished, that is a host
# running short-lived jobs, not a proxy that will not let us inspect.
# It must not read as one and must not block: the ids[:3] loop healed
# the one-container case and left this one saying "a socket proxy that
# allows listing but not inspecting looks healthy".
all_gone = dict(HEALTHY)
all_gone["ps -aq"] = (0, "aaa111\nbbb222\nccc333", "")
for _i in ("aaa111", "bbb222", "ccc333"):
    all_gone[f"inspect {_i}"] = (
        1, "", f"Error response from daemon: No such container: {_i}")
f = hostcheck.preflight("tcp://ci:2375", _Backend(all_gone))
checks["three for three gone is not a socket proxy"] = (
    kinds(f) == ["engine", "can_list", "gone"])
checks["…and does not block the host"] = hostcheck.usable(f) is True
checks["…and gets the verdict instead of silence"] = (
    hostcheck.verdict(f) == "usable")

# …but a refusal still stops on the first one: asking twice would only
# be two refusals.
refused_all = dict(HEALTHY)
refused_all["inspect abc123def456"] = (1, "", "403 Forbidden")
b_ref = _Backend(refused_all)
hostcheck.preflight("tcp://proxy:2375", b_ref)
checks["…while a refusal is not asked a second time"] = (
    len([a for a in b_ref.asked if a.startswith("inspect")]) == 1)

# ── the conclusion the reader came for ───────────────────────────────
f = hostcheck.preflight("ssh://fresh", _Backend(empty))
checks["a host with no containers gets a verdict of its own"] = (
    hostcheck.verdict(f) == "usable")
checks["…and a host that already said 'ready' needs no second one"] = (
    hostcheck.verdict(hostcheck.preflight("ssh://nas", _Backend(HEALTHY)))
    is None)
checks["…and a refused one gets no verdict at all"] = (
    hostcheck.verdict(hostcheck.preflight("unix:///x", _Backend(denied)))
    is None)
# ── what the "I do not speak that" message offers ────────────────────
# `fd://` is systemd socket activation and `npipe://` is a Windows named
# pipe. Both are accepted — somebody may know better — but neither can
# work from an Alpine container, and advising them is a dead end.
_bad = dict(hostcheck.preflight("http://nas", _Backend({})))["scheme_bad"]
checks["the scheme advice offers nothing that cannot work in here"] = (
    "fd://" not in _bad["schemes"] and "npipe://" not in _bad["schemes"]
    and "context://" in _bad["schemes"] and "ssh://" in _bad["schemes"])
checks["…though both are still accepted if somebody has one"] = (
    "fd://" in hostcheck.SCHEMES and "npipe://" in hostcheck.SCHEMES)

# ── nothing is written anywhere ──────────────────────────────────────
b = _Backend(HEALTHY)
hostcheck.preflight("ssh://nas", b)
# `all()` over an empty list is True, so the old form passed on a
# backend that was never asked anything at all. The count is part of
# the claim, and the whole command is looked at rather than its verb:
# `ps --filter` is a read, `run` with any flag is not.
_WRITES = ("run", "create", "start", "stop", "restart", "rm", "kill",
           "pull", "push", "rename", "exec", "commit", "prune", "login")
checks["a pre-flight only reads"] = (
    len(b.asked) == 4
    and all(a.split()[0] in ("version", "ps", "inspect") for a in b.asked)
    and not any(w in a.split() for a in b.asked for w in _WRITES))

# ── both chats ask the core, and say the same thing ──────────────────
# The three checks that stood here were `"hostcheck.preflight(" in body`
# and friends. An auditor gutted Discord's renderer to `return "nothing
# to see here"`, left the words in a docstring, and all three passed —
# and they never compared the two texts at all. So render both and look.
# The only difference allowed is the markdown dialect; Discord also
# clips at 1900 characters, which this fixture stays well under and
# which is therefore NOT what these checks are about.
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

# The ssh refusal must read as a key problem where the reader sees it,
# not just in the finding list — the socket wording (mount, group,
# SELinux) is the wrong way to send him.
_ssh_answers = {"version --format":
                (1, "", "root@nas: Permission denied (publickey).")}
_ssh_said = _rendered(TelegramBot, "ssh://root@nas", _ssh_answers)
checks["the ssh refusal talks about a key, not about a group"] = (
    "key" in _ssh_said and "SELinux" not in _ssh_said)
checks["…and Discord says the same about it"] = (
    _plain(_rendered(DiscordBot, "ssh://root@nas", _ssh_answers))
    == _plain(_ssh_said))

# The verdict for the no-containers host has to reach the reader, in
# both chats — a conclusion only `hostcheck` knows about is no use.
checks["the verdict reaches the reader in both chats"] = (
    "can be added" in _rendered(TelegramBot, "ssh://fresh", empty)
    and "can be added" in _rendered(DiscordBot, "ssh://fresh", empty))

_tg_bad = _rendered(TelegramBot, "http://nas", {})
checks["…including when the endpoint is one we do not speak"] = (
    "http://nas" in _tg_bad
    and _plain(_rendered(DiscordBot, "http://nas", {})) == _plain(_tg_bad))

# …and the command is reachable at all, which is the half a grep cannot
# see. These three were `'self._hostcheck_text(_parts[2])' in src` and
# friends; commenting the call out — feature dead on both connections —
# left all three green, because the text is still in the file. So the
# real dispatch is driven: a message arrives, a slash command arrives,
# and the pre-flight's own words have to come back out.


def _drive_telegram(text):
    """One message through `_handle_message`, returning what was said."""
    bot = TelegramBot.__new__(TelegramBot)
    said = []
    bot.config = _t.SimpleNamespace(language="en", container_cli="docker",
                                    bot_token="", chat_id="1")
    from i18n import get_translator
    bot.t = get_translator("en")
    bot.send_message = lambda m, **kw: said.append(m)
    bot._check_auth = lambda *a, **kw: True
    bot.audit = None
    bot.hosts = None
    bot.log = lambda *a, **kw: None
    real = _cb.RemoteBackend
    _cb.RemoteBackend = lambda *a, **kw: _Backend(HEALTHY)
    import threading
    before = set(threading.enumerate())
    try:
        bot._handle_message({"text": text, "from": {"id": "1"},
                             "chat": {"id": "1"}}, None, None)
        # The pre-flight answers from a worker thread, so waiting for it
        # is part of driving the command. Without this the fixture
        # happened to finish first and the check was a race that passed.
        for t in set(threading.enumerate()) - before:
            t.join(timeout=30)
    finally:
        _cb.RemoteBackend = real
    return "\n".join(said)


_tg_said = _drive_telegram("/hosts check ssh://nas")
checks["/hosts check reaches the pre-flight in Telegram"] = (
    "27.1.1" in _tg_said and "ssh://nas" in _tg_said)
# …and it does not do it on the thread that listens. Four questions at
# `PROBE_TIMEOUT` seconds each is two minutes, and `_handle_message` runs
# inside the `getUpdates` loop — so a filtered port used to stop the bot
# answering anything, repeatable by anyone allowed to command it.
import threading as _th                                        # noqa: E402
import time as _time                                           # noqa: E402


class _Slow:
    """Every question takes a second."""

    def run(self, args, timeout=None):
        _time.sleep(1.0)
        return _t.SimpleNamespace(returncode=1, stdout="", stderr="timeout")


_slowbot = TelegramBot.__new__(TelegramBot)
_slow_said = []
_slowbot.config = _t.SimpleNamespace(language="en", container_cli="docker",
                                     bot_token="", chat_id="1")
from i18n import get_translator as _gt                       # noqa: E402
_slowbot.t = _gt("en")
_slowbot.send_message = lambda m, **kw: _slow_said.append(m)
_slowbot._check_auth = lambda *a, **kw: True
_slowbot.audit = None
_slowbot.hosts = None
_slowbot.log = lambda *a, **kw: None
_before = set(_th.enumerate())
_real_be = _cb.RemoteBackend
_cb.RemoteBackend = lambda *a, **kw: _Slow()
try:
    _started = _time.monotonic()
    _slowbot._handle_message({"text": "/hosts check ssh://slow",
                              "from": {"id": "1"}, "chat": {"id": "1"}},
                             None, None)
    _blocked = _time.monotonic() - _started
    for _worker in set(_th.enumerate()) - _before:
        _worker.join(timeout=30)
finally:
    _cb.RemoteBackend = _real_be
checks["a slow endpoint does not block the listening thread"] = _blocked < 0.5
checks["…and the answer still arrives afterwards"] = bool(_slow_said)

checks["…and without an endpoint it says how to use it"] = (
    "/hosts check" in _drive_telegram("/hosts check"))


def _drive_discord(options):
    bot = DiscordBot.__new__(DiscordBot)
    bot.config = _t.SimpleNamespace(language="en", container_cli="docker")
    bot.application_id, bot.rest = "", None
    bot.audit = None
    bot.hosts = None
    bot.log = lambda *a, **kw: None
    real = _cb.RemoteBackend
    _cb.RemoteBackend = lambda *a, **kw: _Backend(HEALTHY)
    try:
        return bot._dispatch({"data": {"name": "hosts", "options": options}})
    finally:
        _cb.RemoteBackend = real


_dc_said = _drive_discord([{"name": "check", "value": "ssh://nas",
                            "type": 3}])
checks["…and the option reaches it in Discord"] = (
    "27.1.1" in (_dc_said or "") and "ssh://nas" in (_dc_said or ""))
# A `/hosts` with no option must still be the host list, not a
# pre-flight against the empty string.
checks["…while /hosts without it is still the host list"] = (
    "27.1.1" not in (_drive_discord([]) or ""))
# The registration read as DATA, not as source text. This was the last
# grep left in this file, and it was the same trap as the three above:
# commenting the option block out — feature not typable by anyone — left
# it green, because the words were still in the file.
import discord_bot as _db                                     # noqa: E402

_hosts_cmd = next((c for c in _db.COMMANDS if c.get("name") == "hosts"), None)
_hosts_opts = [o.get("name") for o in (_hosts_cmd or {}).get("options", [])]
checks["…and Discord registers the option so it can be typed"] = (
    "check" in _hosts_opts)
# Discord validates the registration as one document: one
# optional-before-required option makes the bulk PUT fail and the bot
# ends up with NO commands at all. So the option must carry a type and
# must not sit in front of a required one.
checks["…as a string option that cannot break the registration"] = (
    _hosts_cmd is not None
    and all(o.get("type") == 3 for o in _hosts_cmd["options"])
    and not any(_hosts_cmd["options"][i].get("required")
                for i in range(1, len(_hosts_cmd["options"]))))

# ── the claims that were only ever asserted ──────────────────────────
# Each of these survived a revert with the whole suite green.

# `usable([])` — "nothing was measured" is not "nothing is wrong". An
# empty list is the shape a caller gets when the check never ran.
checks["an empty finding list is not 'usable'"] = hostcheck.usable([]) is False

# The engine tie-break. The plain `version` output carries a `Client:`
# block naming the LOCAL cli, so against a podman service a docker
# client prints BOTH names in one answer — measured. "podman" is tested
# first on purpose, because the docker cli says "docker" about itself on
# every host it ever talks to, while nothing says "podman" unless podman
# is in the conversation. No fixture used to contain both, so reversing
# the order changed nothing and the comment described a decision that
# was never exercised.
both = dict(HEALTHY)
both["version --format"] = (0, "4.9.3", "")
both["version"] = (0, "Client: Docker Engine - Community\n Version: 29.5.3\n"
                      "Server: linux/amd64\n Podman Engine:\n  Version: 4.9.3", "")
f = hostcheck.preflight("unix:///run/podman.sock", _Backend(both))
checks["a docker cli talking to podman is reported as podman"] = (
    dict(f)["engine"] == {"engine": "podman", "version": "4.9.3"})

# `EMITTED` against what `preflight` really produces. `KINDS` carries
# one kind `preflight` never emits (`usable`, which is `verdict`'s), so
# a single list stood for two different claims and nothing held either.
# A scenario is `(endpoint, cli answers)`, plus an optional third
# element: what the TLS handshake probe should answer. The `needs_tls`
# scenario below uses the words rather than the probe, so the third
# element is currently unused — it stays because the alternative is a
# caller that has to pass `lambda ep: False` eleven times, and an audit
# flagged it as dead. It is: one entry away from being used again, and
# the default is the safe one.
SCENARIOS = {
    "scheme_bad": ("http://nas", {}),
    "unreachable": ("ssh://nas", {}),
    "needs_tls": ("tcp://nas:2376",
                  {"version --format": (1, "", "Error response from daemon: "
                   "Client sent an HTTP request to an HTTPS server.")}),
    "denied": ("unix:///var/run/docker.sock",
               {"version --format": (1, "", "permission denied")}),
    "denied_ssh": ("ssh://nas",
                   {"version --format": (1, "", "Permission denied (publickey).")}),
    "engine": ("ssh://nas", HEALTHY),
    "engine_unsure": ("ssh://nas", mute),
    "can_list": ("ssh://nas", HEALTHY),
    "cannot_list": ("tcp://p:2375", nolist),
    "can_inspect": ("ssh://nas", HEALTHY),
    "cannot_inspect": ("tcp://p:2375", proxy),
    "no_containers": ("ssh://fresh", empty),
    "gone": ("tcp://ci:2375", all_gone),
    "ready": ("ssh://nas", HEALTHY),
}
_produced = set()
for _scenario in SCENARIOS.values():
    _ep, _answers = _scenario[0], _scenario[1]
    _tls = _scenario[2] if len(_scenario) > 2 else (lambda ep: False)
    _produced |= set(kinds(hostcheck.preflight(_ep, _Backend(_answers),
                                               probe_tls=_tls)))
checks["every kind EMITTED names can actually be produced"] = (
    _produced == set(hostcheck.EMITTED))
if _produced != set(hostcheck.EMITTED):
    print(f"      → never produced: {sorted(set(hostcheck.EMITTED) - _produced)}")
    print(f"      → produced but not listed: {sorted(_produced - set(hostcheck.EMITTED))}")
# ── a TLS port, which used to be called "nothing answered" ──────────
# `tcp://host:2376` is Docker's own recommended hardening, and the
# pre-flight reported it as unreachable — the daemon answered perfectly
# well and we spoke the wrong language at it. Worse, I had told somebody
# in #2 that this is the right form and that Docksentry "then just talks
# to tcp://host:2376", which is not true: there are no client
# certificates anywhere in the program. He was sent down that path by me
# and then told his machine was unreachable.
#
# Both halves are driven here: the words a current Docker prints, and
# the handshake for a CLI that says nothing useful.
_tls_said = {"version --format": (1, "", "Error response from daemon: "
             "Client sent an HTTP request to an HTTPS server.")}
_tls_mute = {"version --format": (1, "", "ping response was 400")}

_f = hostcheck.preflight("tcp://nas:2376", _Backend(_tls_said),
                         probe_tls=lambda ep: False)
checks["a daemon that says we spoke HTTP to HTTPS is understood"] = (
    kinds(_f) == ["needs_tls"])

_f = hostcheck.preflight("tcp://nas:2376", _Backend(_tls_mute),
                         probe_tls=lambda ep: True)
checks["…and a CLI that says nothing useful is answered by the port"] = (
    kinds(_f) == ["needs_tls"])
# `.get`, not `[...]`: with the finding renamed this raised KeyError,
# and a procedure that dies is not a procedure that noticed — the
# mutation probe reported both of these as "did not finish" rather than
# as caught, which is the difference between a guard and a crash.
checks["…naming the endpoint it was asked about"] = (
    dict(_f).get("needs_tls", {}).get("endpoint") == "tcp://nas:2376")

# The phrase that means the opposite. `_TLS_WORDS` first held four
# entries, three of them guessed, and one of those was `"tls
# handshake"` — which matches Go's `tls: first record does not look
# like a TLS handshake`, the message for a port that is **not** TLS.
# So the sentence saying "no TLS here" produced a `needs_tls` finding,
# and anyone with `DOCKER_TLS_VERIFY` set on the container was told his
# plaintext port speaks TLS. Found by an audit the same day I wrote it.
_f = hostcheck.preflight(
    "tcp://nas:2375",
    _Backend({"version --format": (1, "", 'error during connect: Get '
              '"https://nas:2375/v1.50/version": tls: first record does '
              'not look like a TLS handshake')}),
    probe_tls=lambda ep: False)
checks["the words for 'this is not TLS' are not read as TLS"] = (
    kinds(_f) == ["unreachable"])

# A name that never resolved has no port to offer a handshake to, and
# asking anyway costs the resolver timeout a second time for an answer
# that is already known.
_dns = {"version --format": (1, "", "dial tcp: lookup nas: no such host")}
_asked = []
_f = hostcheck.preflight("tcp://nas:2376", _Backend(_dns),
                         probe_tls=lambda ep: _asked.append(ep) or True)
checks["a name that will not resolve is not probed"] = (
    _asked == [] and kinds(_f) == ["unreachable"])

# …and neither is a socket that answered and refused us: that is
# already explained, and three seconds of handshake on top buys the
# reader of `/hosts check` nothing but three seconds.
_asked = []
_f = hostcheck.preflight(
    "tcp://nas:2376",
    _Backend({"version --format": (1, "", "permission denied")}),
    probe_tls=lambda ep: _asked.append(ep) or True)
checks["…nor a socket that answered and said no"] = (
    _asked == [] and kinds(_f) == ["denied"])

# The counter-direction, and it is the one that matters: a port that is
# simply dead must NOT be called a TLS port. Without this the check
# above passes on a module that says `needs_tls` about everything.
_f = hostcheck.preflight("tcp://nas:2375", _Backend(_tls_mute),
                         probe_tls=lambda ep: False)
checks["a port that does not speak TLS is not blamed on TLS"] = (
    kinds(_f) == ["unreachable"])
# …and a refusal stays a refusal rather than being read as TLS.
_f = hostcheck.preflight(
    "tcp://nas:2375",
    _Backend({"version --format": (1, "", "permission denied")}),
    probe_tls=lambda ep: False)
checks["…and a refusal stays a refusal"] = kinds(_f) == ["denied"]

# A host behind TLS cannot be added, so the verdict must say so. It is
# the one case where everything is configured correctly on the far side
# and the answer is still no.
checks["a TLS-only host is not reported as usable"] = (
    not hostcheck.usable([("needs_tls", {})]))

# ── and the probe itself, against the real thing ────────────────────
# `speaks_tls` is the only part that cannot be answered by a stand-in,
# because what is claimed is what a real TLS port does. Measured on
# 11.10.2026 against two dinds: one with `DOCKER_TLS_CERTDIR=/certs`
# completes the handshake, the same image with TLS off refuses it with
# `WRONG_VERSION_NUMBER`. Here only the cheap half runs — a port with
# nothing on it, and the shapes that must not be probed at all.
checks["nothing listening does not look like TLS"] = (
    hostcheck.speaks_tls("tcp://127.0.0.1:1", timeout=1.0) is False)
checks["…and a non-tcp endpoint is never probed"] = (
    hostcheck.speaks_tls("ssh://root@nas") is False
    and hostcheck.speaks_tls("unix:///var/run/docker.sock") is False)
checks["…nor an endpoint with no port to probe"] = (
    hostcheck.speaks_tls("tcp://nocolon") is False)

checks["…and `usable` is a verdict, not something preflight emits"] = (
    "usable" not in _produced and "usable" in hostcheck.KINDS)

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

# ── a very long endpoint must not split the two chats ────────────────
# This is the case the earlier version of this file deliberately did
# NOT cover ("which this fixture stays well under"), and it was the one
# place the two renderers still disagreed: clipping inside `preflight`
# left the header and the closing line using the raw value. Measured
# before the fix — Telegram 8231 characters over five lines, Discord
# clipped to 1900 with not one finding left in it.
_LONG = "tcp://" + "a" * 4000
_tg_long = _rendered(TelegramBot, _LONG, HEALTHY)
_dc_long = _rendered(DiscordBot, _LONG, HEALTHY)
checks["a 4000-character endpoint does not split the two chats"] = (
    _plain(_tg_long) == _plain(_dc_long))
checks["…and the findings survive in both"] = (
    "27.1.1" in _tg_long and "27.1.1" in _dc_long)
checks["…because the endpoint itself is bounded"] = (
    len(_tg_long) < 1000 and len(_dc_long) < 1000)

# ── the texts say what was measured ──────────────────────────────────
# Nothing read these, so `hostcheck_ready` could be changed to "is fully
# writable" and the suite stayed green. The wording of this one finding
# is the whole difference between an honest check and one that promises
# a host "can be managed" after four read-only questions — a socket
# proxy that allows GET and refuses POST gets this far and fails the
# first update with a 403.
_en = json.load(open(os.path.join(ROOT, "app", "lang", "en.json"),
                     encoding="utf-8"))
_ready = _en["hostcheck_ready"].lower()
checks["'ready' does not promise the host can be managed"] = (
    "can be managed" not in _ready and "writable" not in _ready)
checks["…and says that write permission was not tested"] = (
    "write permission" in _ready)
# …and the one for a host with nothing on it must not claim more.
_us = _en["hostcheck_usable"].lower()
checks["'usable' says inspect and write stayed untested"] = (
    "untested" in _us and "write" in _us)
# The ssh refusal must point at a key; the socket one must not.
checks["the ssh refusal points at a key, the socket one does not"] = (
    "key" in _en["hostcheck_denied_ssh"].lower()
    and "key" not in _en["hostcheck_denied"].lower()
    and "selinux" in _en["hostcheck_denied"].lower())

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
