#!/usr/bin/env python3
"""Healthcheck - verifies the bot process is running and responsive."""

import json
import os
import sys
import urllib.request

def main():
    bot_token = os.environ.get("BOT_TOKEN", "")
    if not bot_token:
        sys.exit(1)

    try:
        url = f"https://api.telegram.org/bot{bot_token}/getMe"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                sys.exit(0)
    except Exception:
        pass

    sys.exit(1)

if __name__ == "__main__":
    main()
