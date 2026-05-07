#!/usr/bin/env python3
"""Healthcheck — verifies that the configured surface(s) are reachable.

Docksentry can run in three modes (or any combination):

  1. **Telegram bot** — BOT_TOKEN + CHAT_ID set. Health = the bot process
     can still reach `api.telegram.org/getMe` (covers expired tokens, DNS
     loss, network outage).
  2. **Web UI** — WEB_UI=true. Health = the local HTTP server on
     WEB_PORT (default 8080) responds. The Web UI being up implies the
     in-process scheduler thread is also alive.
  3. **Webhook-only headless** — DISCORD_WEBHOOK or WEBHOOK_URL set, no
     Telegram, no Web UI. There's no socket to probe — Docker's normal
     process supervision (PID 1 alive = container running) is the best
     signal we have, so we exit 0.

Exit codes:
  0 — at least one configured surface is reachable
  1 — a configured surface failed (Telegram getMe error, Web UI socket
      closed, or no surface configured at all → misconfiguration)
"""

import json
import os
import socket
import sys
import urllib.request


def _check_telegram(bot_token: str) -> bool:
    """True if the Telegram Bot API answers ok=true for getMe."""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getMe"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            return bool(data.get("ok"))
    except Exception:
        return False


def _check_web_ui(port: int) -> bool:
    """True if the local Web UI socket accepts a TCP connection.

    We don't need to issue an HTTP request — a successful connect on
    127.0.0.1 means the ThreadingHTTPServer thread is alive. The /
    handler always either renders or redirects; either way the listener
    is up. Keeping the check at the socket layer also avoids depending
    on auth state (WEB_PASSWORD) and stays cheap.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return True
    except OSError:
        return False


def main():
    bot_token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    web_ui = os.environ.get("WEB_UI", "false").lower() in ("true", "1", "yes")
    web_port = int(os.environ.get("WEB_PORT", "8080"))
    discord = os.environ.get("DISCORD_WEBHOOK", "").strip()
    webhook = os.environ.get("WEBHOOK_URL", "").strip()

    telegram_configured = bool(bot_token and chat_id)
    headless_only = (discord or webhook) and not telegram_configured and not web_ui

    # Web UI takes precedence when both are configured: the Telegram check
    # talks to api.telegram.org, which can fail transiently (network blip,
    # rate limiting) even when the bot is fine. The local socket check is
    # deterministic and cheap, so a healthy Web UI alone is enough to call
    # the container healthy.
    if web_ui:
        if _check_web_ui(web_port):
            sys.exit(0)
        # Web UI was supposed to be up but isn't — definite failure
        sys.exit(1)

    if telegram_configured:
        sys.exit(0 if _check_telegram(bot_token) else 1)

    if headless_only:
        # Webhook-only mode: nothing to probe locally. If we got here the
        # process is running (otherwise Docker wouldn't have invoked us),
        # so report healthy.
        sys.exit(0)

    # Nothing configured — main.py would have refused to start, but be
    # explicit here too.
    sys.exit(1)


if __name__ == "__main__":
    main()
