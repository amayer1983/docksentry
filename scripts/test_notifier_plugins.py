#!/usr/bin/env python3
"""Notifier plugin layer — registry, best-effort dispatch, ntfy request.

Verifies the v2 plugin split keeps the contract:
  1. the registry collects EXACTLY the configured channels (and no more),
  2. a channel that raises does NOT stop the others (best-effort dispatch),
  3. the new ntfy channel builds the expected HTTP request — topic URL,
     Title / Priority headers, body — with urllib mocked (no real network).

Pure stdlib, no network. Exits non-zero on any failure.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import notifiers
from notifiers import build_configured
from notifier import Notifier
import notifiers.ntfy as ntfy_mod
from notifiers.ntfy import NtfyNotifier


def _cfg(**over):
    base = dict(
        discord_webhook="", webhook_url="",
        smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
        smtp_from="", smtp_to="", smtp_tls="starttls",
        bot_label="")
    base.update(over)
    return types.SimpleNamespace(**base)


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _clear_ntfy_env(saved):
    for k in ("NTFY_URL", "NTFY_SERVER", "NTFY_TOPIC"):
        saved[k] = os.environ.pop(k, None)


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def main():
    checks = {}
    saved = {}
    _clear_ntfy_env(saved)
    orig_urlopen = ntfy_mod.urllib.request.urlopen
    try:
        # 1. registry collects exactly the configured channels ─────────
        names = [p.name for p in build_configured(_cfg(discord_webhook="x"))]
        checks["registry: discord only"] = names == ["discord"]

        names = [p.name for p in build_configured(
            _cfg(smtp_host="h", smtp_from="f@x", smtp_to="t@x"))]
        checks["registry: smtp only"] = names == ["smtp"]

        names = [p.name for p in build_configured(
            _cfg(discord_webhook="x", webhook_url="y"))]
        checks["registry: discord+webhook, stable order"] = names == ["discord", "webhook"]

        checks["registry: nothing configured -> empty"] = \
            build_configured(_cfg()) == []

        # ntfy joins from URL is picked up by the registry via env
        os.environ["NTFY_URL"] = "https://ntfy.sh/topicA"
        names = [p.name for p in build_configured(_cfg(discord_webhook="x"))]
        checks["registry: ntfy discovered from env"] = names == ["discord", "ntfy"]
        del os.environ["NTFY_URL"]

        # every registered class is a BaseNotifier and appears once
        discovered = notifiers.discover()
        checks["registry: expected channel set"] = \
            sorted(c.name for c in discovered) == ["apprise", "discord",
                                                   "discordbot", "gotify",
                                                   "matrix", "ntfy", "smtp",
                                                   "webhook"]

        # 2. a raising channel doesn't stop the others ─────────────────
        n = Notifier(_cfg(discord_webhook="x", webhook_url="y"))
        seen = []

        def boom(*a):
            raise RuntimeError("channel down")

        n._by_name["discord"].send_message = boom
        n._by_name["webhook"].send_message = lambda text: seen.append(text)
        n.send_message("hello")  # must not raise, must reach webhook
        checks["dispatch: raising channel isolated"] = seen == ["hello"]

        # 3. ntfy builds the expected HTTP request ─────────────────────
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["title"] = req.get_header("Title")
            captured["priority"] = req.get_header("Priority")
            captured["method"] = req.get_method()
            captured["body"] = req.data
            return _Resp(200)

        ntfy_mod.urllib.request.urlopen = fake_urlopen

        # NTFY_URL (full topic URL)
        os.environ["NTFY_URL"] = "https://ntfy.sh/docks"
        NtfyNotifier(_cfg()).send_update_result("sonarr", "img:1", False, "it broke")
        checks["ntfy: POST to topic url"] = \
            captured.get("url") == "https://ntfy.sh/docks" and captured.get("method") == "POST"
        checks["ntfy: Title header"] = captured.get("title") == "Update FAILED: sonarr"
        checks["ntfy: Priority high on failure"] = captured.get("priority") == "high"
        checks["ntfy: body carries detail"] = b"it broke" in (captured.get("body") or b"")

        # success -> default priority; bot_label prefixes the title
        NtfyNotifier(_cfg(bot_label="pve1")).send_update_result("radarr", "img:2", True, "done")
        checks["ntfy: Priority default on success"] = captured.get("priority") == "default"
        checks["ntfy: bot_label prefixes title"] = captured.get("title") == "[pve1] Update OK: radarr"
        del os.environ["NTFY_URL"]

        # NTFY_SERVER + NTFY_TOPIC join
        os.environ["NTFY_SERVER"] = "https://ntfy.example.com/"
        os.environ["NTFY_TOPIC"] = "myhost"
        checks["ntfy: server+topic configured"] = NtfyNotifier(_cfg()).configured()
        NtfyNotifier(_cfg()).send_message("*hi*")
        checks["ntfy: server+topic joined url"] = \
            captured.get("url") == "https://ntfy.example.com/myhost"
        checks["ntfy: message strips bold markers"] = captured.get("body") == b"hi"
        del os.environ["NTFY_SERVER"]
        del os.environ["NTFY_TOPIC"]

        # unset -> not configured, no request
        checks["ntfy: unset -> not configured"] = not NtfyNotifier(_cfg()).configured()
    finally:
        ntfy_mod.urllib.request.urlopen = orig_urlopen
        _restore_env(saved)

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
