#!/usr/bin/env python3
"""Updating Docksentry itself lives in the core, not on a front end (#63).

Fifth and largest step of the core extraction: 486 lines — eleven
functions that pull an image and recreate our own container — were
methods of the Telegram bot. Nothing about them is Telegram's, which is
why Discord could only self-update by reaching into that instance, and
why the report then went to Telegram rather than to whoever asked.

This step moves the machinery. It pins two things: that the machinery is
gone from the bot and callable in the module, and that the module's whole
dependency on its caller is the small `ctx` contract — because that
contract is what lets a second front end drive it at all.
"""
import ast
import os
import sys
import types

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

checks = {}

import selfupdate  # noqa: E402

MOVED = ("run", "resolve_target", "latest_released_version",
         "should_retag_moving", "version_line", "save_history", "swap",
         "build_script", "write_marker", "host_docker_socket",
         "host_mount_source")
missing = [n for n in MOVED if not callable(getattr(selfupdate, n, None))]
checks["every moved function is in the module"] = not missing
if missing:
    print("  → fehlt: " + ", ".join(missing))

OLD = ("_selfupdate_locked", "_resolve_selfupdate_target",
       "_latest_released_version", "_should_retag_moving",
       "_selfupdate_version_line", "_save_selfupdate_history",
       "_do_selfupdate", "_build_selfupdate_script",
       "_write_selfupdate_marker", "_host_docker_socket",
       "_host_mount_source")
from telegram_bot import TelegramBot  # noqa: E402
left = [n for n in OLD if hasattr(TelegramBot, n)]
checks["…and none of them is still on the Telegram bot"] = not left
if left:
    print("  → noch am Bot: " + ", ".join(left))

# ── the ctx contract, counted from the module rather than trusted ────
# The whole point is that the module needs almost nothing from whoever
# drives it. If that list grows, a second front end gets harder to
# attach — so the test states the contract and fails when it widens.
src = open(os.path.join(APP, "selfupdate.py"), encoding="utf-8").read()
tree = ast.parse(src)
needed = sorted({n.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute)
                 and isinstance(n.value, ast.Name) and n.value.id == "ctx"})
ALLOWED = ["_swap_in_flight", "config", "notifier", "send_message", "t"]
checks["the module asks its caller for exactly five names"] = needed == ALLOWED
if needed != ALLOWED:
    print(f"  → verlangt: {needed}")
    print(f"  → erlaubt : {ALLOWED}")

checks["…and never imports a front end"] = (
    "telegram_bot" not in src and "discord_bot" not in src)

# ── behavioural: the pure ones run with no bot anywhere in sight ─────
script = selfupdate.build_script("ds", "-e X=1", "img:latest")
checks["build_script works as a plain function"] = (
    isinstance(script, str) and "docker rename" in script
    and "img:latest" in script)
checks["…and honours the stop timeout it is given"] = (
    "stop -t 99 " in selfupdate.build_script("a", "b", "c", stop_timeout=99))

# `config` here is our own INSPECT dict, not the settings object — the
# parameter name is inherited from the method it was.
checks["host_docker_socket falls back to the default socket"] = (
    selfupdate.host_docker_socket({}) == "/var/run/docker.sock")
checks["…and otherwise mounts the socket we ourselves are given"] = (
    selfupdate.host_docker_socket(
        {"Mounts": [{"Destination": "/var/run/docker.sock",
                     "Source": "/run/podman/podman.sock"}]})
    == "/run/podman/podman.sock")

inspect_cfg = {"Mounts": [{"Destination": "/data", "Source": "/srv/ds"}]}
checks["host_mount_source finds the host side of a mount"] = (
    selfupdate.host_mount_source(inspect_cfg, "/data") == "/srv/ds")
checks["…and answers None for one that is not mounted"] = (
    selfupdate.host_mount_source(inspect_cfg, "/nope") is None)

# ── the ctx-driven ones take any object answering the contract ───────
class Voice:
    """Not a bot. Just the five names the module asks for."""
    def __init__(self):
        self.said = []
        self.config = types.SimpleNamespace(docker_stop_timeout=60)
        self.notifier = None
        self._swap_in_flight = False
    def t(self, key, **kw):
        return key
    def send_message(self, text, **kw):
        self.said.append(text)

v = Voice()
image, err = selfupdate.resolve_target(v, "repo/ds:latest", "1.2.3")
checks["resolve_target pins an explicit version"] = (
    image == "repo/ds:1.2.3" and err is None)
image, err = selfupdate.resolve_target(v, "repo/ds:latest", None)
checks["…and leaves the running tag alone when none is asked for"] = (
    image == "repo/ds:latest" and err is None)
_, err = selfupdate.resolve_target(v, "repo/ds:latest", "not a version")
checks["…and refuses something that is not a version"] = bool(err)

checks["should_retag_moving: a moving tag is re-pinned"] = (
    selfupdate.should_retag_moving(v, "latest", "repo/ds:1.2.3",
                                   "repo/ds:latest") is True)
checks["…and an explicit version is not"] = (
    selfupdate.should_retag_moving(v, "1.2.3", "repo/ds:latest",
                                   "repo/ds:1.2.3") is False)

failed = [k for k, val in checks.items() if not val]
for k, val in checks.items():
    print(f"  {'PASS' if val else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
