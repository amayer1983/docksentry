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
           "ps -aq": (0, "abc123def456\n789abc012def", ""),
           "inspect abc123def456": (0, "[]", "")}


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

# Podman says so about itself rather than being guessed from the endpoint.
pod = dict(HEALTHY)
pod["version --format"] = (0, "5.2.0", "")
b = _Backend(pod)
b._a["version --format"] = (0, "5.2.0", "")


class _Podman(_Backend):
    def run(self, args, timeout=None):
        if args[:2] == ["version", "--format"] and "Components" in args[2]:
            return types.SimpleNamespace(returncode=0,
                                         stdout="[{Name:podman Engine}]",
                                         stderr="")
        return super().run(args, timeout=timeout)


f = hostcheck.preflight("unix:///run/podman/podman.sock", _Podman(pod))
checks["podman is recognised from what it calls itself"] = (
    dict(f)["engine"]["engine"] == "podman")

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
          "ps -aq": (1, "", "permission denied")}
f = hostcheck.preflight("tcp://proxy:2375", _Backend(nolist))
checks["a daemon that refuses to list stops at that"] = (
    kinds(f) == ["engine", "cannot_list"])

# ── an empty host leaves the permission question open, and says so ───
empty = {"version --format": (0, "27.1.1", ""), "ps -aq": (0, "", "")}
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
ROOT = os.path.join(os.path.dirname(__file__), "..")
for label, f_ in (("Telegram", "telegram_bot.py"), ("Discord", "discord_bot.py")):
    src = open(os.path.join(ROOT, "app", f_), encoding="utf-8").read()
    body = src.split("def _hostcheck_text")[1].split("\n    def ")[0]
    checks[f"{label} asks the core for the findings"] = (
        "hostcheck.preflight(" in body)
    checks[f"…{label} words them with its own translator"] = (
        'self.t(f"hostcheck_{kind}"' in body)
    checks[f"…{label} passes the ssh hint along"] = "hostdiag.hint" in body

# every kind has wording, in every language
import json                                                  # noqa: E402

LANGS = sorted(f for f in os.listdir(os.path.join(ROOT, "app", "lang"))
               if f.endswith(".json"))
KINDS = {"scheme_bad", "unreachable", "engine", "can_list", "cannot_list",
         "can_inspect", "cannot_inspect", "no_containers", "ready"}
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
