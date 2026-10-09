#!/usr/bin/env python3
"""The exit code Docker acts on, which nothing had ever run.

`Dockerfile:90` runs `python3 /app/healthcheck.py` every sixty seconds,
and its exit code is what makes the container healthy or unhealthy. On
06.10.2026 a sweep found no procedure loads the file at all — not one of
185 — so a syntax error in it would have shipped, and a container that
reports healthy while the bot is dead is worse than no healthcheck,
because it is the signal somebody built their restart policy on.

Three modes, from the module's own docstring, and the precedence
between them is a decision rather than an accident:

* **Web UI configured** → the local socket decides, and it decides
  alone. A TCP connect on 127.0.0.1 is deterministic and cheap, while
  the Telegram check talks to api.telegram.org and can fail on a
  network blip when the bot is perfectly fine. So a healthy Web UI is
  enough, and an unhealthy one is a definite failure — it was supposed
  to be listening.
* **Telegram only** → `getMe` answers or it does not. That covers an
  expired token, lost DNS, an outage.
* **Webhook-only, headless** → there is nothing to probe locally, so
  being alive to be asked is the whole signal. Exit 0.
* **Nothing configured** → exit 1. `main.py` would have refused to
  start, and saying so here too costs one line.

Run as a subprocess, with the environment it reads, because the exit
code IS the interface — importing `main()` and catching `SystemExit`
would test a different thing than Docker runs.
"""
import os
import socket
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "app", "healthcheck.py")

checks = {}


def _run(env, timeout=30, want_output=False):
    """The exit code, with only the variables given set.

    `want_output` also returns what it printed, because the exit code
    alone cannot tell "exited 1 because the Web UI is down" from
    "exited 1 because the file does not parse" — and the first check
    below claimed to catch the second.
    """
    clean = {k: v for k, v in os.environ.items()
             if k not in ("BOT_TOKEN", "CHAT_ID", "WEB_UI", "WEB_PORT",
                          "DISCORD_WEBHOOK", "WEBHOOK_URL")}
    clean.update({k: str(v) for k, v in env.items()})
    try:
        r = subprocess.run([sys.executable, SCRIPT], env=clean,
                           timeout=timeout, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        out = (r.stdout or b"").decode("utf-8", "replace")
        return (r.returncode, out) if want_output else r.returncode
    except subprocess.TimeoutExpired:
        return (None, "") if want_output else None


def _listening():
    """A socket that accepts one connection, like the Web UI's would."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def _accept():
        try:
            while True:
                c, _ = srv.accept()
                c.close()
        except OSError:
            pass

    threading.Thread(target=_accept, daemon=True).start()
    return srv, srv.getsockname()[1]


def _free_port():
    """A port number nobody is listening on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── it runs at all ───────────────────────────────────────────────────
# The cheapest and, until now, missing check: the file Docker executes
# every minute parses and runs.
# `is not None` was only false on a timeout, so this passed on a file
# with a syntax error in it — which is the one thing the docstring
# above says it exists to catch. The interpreter's own complaint is
# what distinguishes the two.
_rc, _out = _run({"DISCORD_WEBHOOK": "https://example.invalid/hook"},
                 want_output=True)
checks["the script Docker runs every minute parses and runs"] = (
    _rc is not None
    and "SyntaxError" not in _out
    and "Traceback (most recent call last)" not in _out)

# ── Web UI: the local socket decides ─────────────────────────────────
_srv, _port = _listening()
try:
    checks["a Web UI that is listening is healthy"] = (
        _run({"WEB_UI": "true", "WEB_PORT": _port}) == 0)
    # …and it decides ALONE. A bogus token must not make a healthy
    # container unhealthy: the whole reason the socket takes precedence
    # is that api.telegram.org can fail when nothing is wrong here.
    checks["…even with a bot token that would never answer"] = (
        _run({"WEB_UI": "true", "WEB_PORT": _port,
              "BOT_TOKEN": "0:nonexistent", "CHAT_ID": "1"}) == 0)
finally:
    _srv.close()

_dead = _free_port()
checks["a Web UI that was meant to listen and does not is unhealthy"] = (
    _run({"WEB_UI": "true", "WEB_PORT": _dead}) == 1)
checks["…and a working bot token does not paper over it"] = (
    _run({"WEB_UI": "true", "WEB_PORT": _dead,
          "BOT_TOKEN": "0:nonexistent", "CHAT_ID": "1"}) == 1)

# ── headless: alive is the signal ────────────────────────────────────
# Nothing to probe locally, and Docker only asked because the process
# is running, so saying "unhealthy" here would make every webhook-only
# install flap forever.
checks["a webhook-only install is healthy"] = (
    _run({"WEBHOOK_URL": "https://example.invalid/hook"}) == 0)
checks["…and so is a Discord-webhook-only one"] = (
    _run({"DISCORD_WEBHOOK": "https://example.invalid/hook"}) == 0)

# ── nothing configured ───────────────────────────────────────────────
checks["an install with nothing configured is unhealthy"] = _run({}) == 1
# A token without a chat id is not a configured Telegram surface, so it
# falls through to "nothing configured" rather than being probed.
checks["…and so is half a Telegram setup"] = (
    _run({"BOT_TOKEN": "0:nonexistent"}) == 1)

# ── Telegram only ────────────────────────────────────────────────────
# A token that cannot answer is the failure this mode exists to catch:
# an expired token, lost DNS, an outage. Not asserted as a network
# round-trip — the sandbox may have no route at all, and either way the
# answer is the same: it did not answer, so it is unhealthy.
checks["a Telegram-only install with an unanswerable token is unhealthy"] = (
    _run({"BOT_TOKEN": "0:nonexistent", "CHAT_ID": "1"}) == 1)

# ── a port that is not a number ──────────────────────────────────────
# `int(os.environ[...])` raises, and an exception out of a healthcheck
# is a non-zero exit — which is the right answer for a misconfigured
# port, but worth knowing rather than discovering.
checks["an unreadable WEB_PORT is unhealthy rather than silently healthy"] = (
    _run({"WEB_UI": "true", "WEB_PORT": "not-a-port"}) != 0)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
