#!/usr/bin/env python3
"""Generic webhook channel — JSON POST, unchanged field contract.

Same envelope (`event`/`source`/`bot_label`) and same per-event fields
(`old_version`/`new_version`/`source_url`, …) downstream automations rely on.
"""

from .base import BaseNotifier, post_json_with_retry


class WebhookNotifier(BaseNotifier):
    name = "webhook"
    order = 20

    OWNS = ("webhook_url",)
    REQUIRES = (("webhook_url", "web_webhook_url"),)

    def configured(self):
        return bool(self.config.webhook_url)

    # ── transport ────────────────────────────────────────────────────
    def send_raw(self, event, data):
        """POST JSON to generic webhook URL."""
        payload = {
            "event": event,
            "source": "docksentry",
            **data,
        }
        # Add bot label to the payload when set so downstream automations
        # (Home Assistant, Ntfy, custom scripts) can route per-host.
        label = self._bot_label()
        if label:
            payload["bot_label"] = label
        # Same retry contract as Discord and Telegram — a transient blip after
        # a self-update restart shouldn't drop a notification. Note the
        # trade-off is slightly different for a generic webhook: it may point
        # at a user automation (Home Assistant, ntfy, custom script), so a
        # duplicate could double-trigger something. Documented in the README.
        return post_json_with_retry(
            self.config.webhook_url, payload, None, "Webhook")

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
        self.send_raw("updates_available", {
            "count": len(updates),
            "containers": [
                {"name": u["name"], "image": u["image"],
                 "size": u.get("size", "?"), "created": u.get("created", "?"),
                 "compose": bool(u.get("compose_project")),
                 # Version info (#44) — read from OCI image.version
                 # labels (old=local, new=remote). Empty when the image
                 # doesn't carry the label.
                 "old_version": u.get("old_version", ""),
                 "new_version": u.get("new_version", ""),
                 # Repo / changelog URL — auto-detected from OCI
                 # labels or manually overridden in the Web UI
                 # (#20). Empty string when no link is available.
                 "source_url": u.get("source_url", "")}
                for u in updates
            ],
        })

    def send_update_result(self, name, image, success, detail="", source_url=""):
        self.send_raw("update_result", {
            "container": name,
            "image": image,
            "success": success,
            "detail": detail,
            "source_url": source_url,
        })

    def send_message(self, text):
        self.send_raw("message", {"text": text})
