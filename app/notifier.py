#!/usr/bin/env python3
"""Multi-channel notification dispatcher (Discord, Webhook)."""

import json
import urllib.request

from quiet_hours import is_quiet_now


class Notifier:
    """Sends notifications to Discord and/or generic webhooks."""

    def __init__(self, config):
        self.config = config

    def has_channels(self):
        """Check if any notification channels are configured."""
        return bool(self.config.discord_webhook or self.config.webhook_url)

    def _suppressed(self):
        """True if quiet-hours OR maintenance is active right now — skip
        auto-notifications. Manual sends still go through (the caller would
        use a different code path for those)."""
        if is_quiet_now(self.config):
            return True
        try:
            from maintenance import is_active as _maint_active
            if _maint_active(self.config):
                return True
        except Exception:
            pass
        return False

    def send_updates_available(self, updates):
        """Notify about available updates."""
        if self._suppressed():
            return
        if self.config.discord_webhook:
            self._discord_updates(updates)
        if self.config.webhook_url:
            self._webhook_send("updates_available", {
                "count": len(updates),
                "containers": [
                    {"name": u["name"], "image": u["image"],
                     "size": u.get("size", "?"), "created": u.get("created", "?"),
                     "compose": bool(u.get("compose_project")),
                     # Repo / changelog URL — auto-detected from OCI
                     # labels or manually overridden in the Web UI
                     # (#20). Empty string when no link is available.
                     "source_url": u.get("source_url", "")}
                    for u in updates
                ],
            })

    def send_update_result(self, name, image, success, detail=""):
        """Notify about a completed update (success or failure)."""
        if self._suppressed():
            return
        if self.config.discord_webhook:
            self._discord_update_result(name, image, success, detail)
        if self.config.webhook_url:
            self._webhook_send("update_result", {
                "container": name,
                "image": image,
                "success": success,
                "detail": detail,
            })

    def send_message(self, text):
        """Send a plain text notification (subject to quiet hours)."""
        if self._suppressed():
            return
        if self.config.discord_webhook:
            self._discord_message(text)
        if self.config.webhook_url:
            self._webhook_send("message", {"text": text})

    # ── Discord ──────────────────────────────────────────────

    def _discord_post(self, payload):
        """POST JSON to Discord webhook."""
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                self.config.discord_webhook,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Docksentry/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except Exception as e:
            print(f"Discord webhook error: {e}")
            return None

    def _footer_text(self):
        """Discord-embed footer text. Includes BOT_LABEL when set so
        multiple Docksentry instances posting into the same Discord
        channel can be told apart (e.g. 'Docksentry · pve1')."""
        label = (self.config.bot_label or "").strip()
        return f"Docksentry · {label}" if label else "Docksentry"

    def _discord_updates(self, updates):
        """Send update notification as Discord embed."""
        fields = []
        for u in updates:
            compose_tag = " 🐳" if u.get("compose_project") else ""
            # Discord embed fields don't render links in `name`, but
            # `value` is full markdown — append a clickable
            # "[Source ↗](url)" line when we have a source URL (#20).
            link_line = ""
            if u.get("source_url"):
                link_line = f"\n[Source ↗]({u['source_url']})"
            fields.append({
                "name": f"📦 {u['name']}{compose_tag}",
                "value": f"`{u['image']}`\n📦 {u.get('size', '?')} · 🗓️ {u.get('created', '?')}{link_line}",
                "inline": True,
            })

        label = (self.config.bot_label or "").strip()
        title_prefix = f"{label} · " if label else ""
        embed = {
            "title": f"{title_prefix}🔄 Docker Updates Available ({len(updates)})",
            "color": 0x58a6ff,  # Blue
            "fields": fields,
            "footer": {"text": self._footer_text()},
        }
        self._discord_post({"embeds": [embed]})

    def _discord_update_result(self, name, image, success, detail):
        """Send update result as Discord embed."""
        label = (self.config.bot_label or "").strip()
        title_prefix = f"{label} · " if label else ""
        if success:
            embed = {
                "title": f"{title_prefix}✅ Update Successful",
                "description": f"**{name}** (`{image}`)\n{detail}",
                "color": 0x3fb950,  # Green
                "footer": {"text": self._footer_text()},
            }
        else:
            embed = {
                "title": f"{title_prefix}❌ Update Failed",
                "description": f"**{name}** (`{image}`)\n{detail}",
                "color": 0xf85149,  # Red
                "footer": {"text": self._footer_text()},
            }
        self._discord_post({"embeds": [embed]})

    def _discord_message(self, text):
        """Send plain text to Discord."""
        # Strip Markdown bold (*text*) for Discord
        clean = text.replace("*", "**")
        label = (self.config.bot_label or "").strip()
        if label:
            clean = f"**{label}** · {clean}"
        self._discord_post({"content": clean})

    # ── Generic Webhook ──────────────────────────────────────

    def _webhook_send(self, event, data):
        """POST JSON to generic webhook URL."""
        payload = {
            "event": event,
            "source": "docksentry",
            **data,
        }
        # Add bot label to the payload when set so downstream automations
        # (Home Assistant, Ntfy, custom scripts) can route per-host.
        label = (self.config.bot_label or "").strip()
        if label:
            payload["bot_label"] = label
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                self.config.webhook_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except Exception as e:
            print(f"Webhook error: {e}")
            return None
