#!/usr/bin/env python3
"""Discord webhook channel — rich embeds, unchanged wire format.

Byte-for-byte the same embeds the facade produced before the plugin split
(same titles, colors, fields, footer, version badge, clickable source links).
"""

from .base import BaseNotifier, post_json_with_retry


class DiscordNotifier(BaseNotifier):
    name = "discord"
    order = 10

    def configured(self):
        return bool(self.config.discord_webhook)

    # ── transport ────────────────────────────────────────────────────
    def post(self, payload):
        """POST JSON to Discord webhook."""
        return post_json_with_retry(
            self.config.discord_webhook, payload,
            {"User-Agent": "Docksentry/1.0"}, "Discord webhook")

    def _footer_text(self):
        """Discord-embed footer text. Includes BOT_LABEL when set so
        multiple Docksentry instances posting into the same Discord
        channel can be told apart (e.g. 'Docksentry · pve1')."""
        label = self._bot_label()
        return f"Docksentry · {label}" if label else "Docksentry"

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
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
            ver = self.version_str(u)
            ver_line = f"\n🔖 {ver}" if ver else ""
            fields.append({
                "name": f"📦 {u['name']}{compose_tag}",
                "value": f"`{u['image']}`{ver_line}\n📦 {u.get('size', '?')} · 🗓️ {u.get('created', '?')}{link_line}",
                "inline": True,
            })

        label = self._bot_label()
        title_prefix = f"{label} · " if label else ""
        embed = {
            "title": f"{title_prefix}🔄 Docker Updates Available ({len(updates)})",
            "color": 0x58a6ff,  # Blue
            "fields": fields,
            "footer": {"text": self._footer_text()},
        }
        self.post({"embeds": [embed]})

    def send_update_result(self, name, image, success, detail="", source_url=""):
        """Send update result as Discord embed."""
        label = self._bot_label()
        title_prefix = f"{label} · " if label else ""
        # Discord embed `description` is full markdown — render the
        # container name as a clickable [name](url) when we have a
        # source URL (matches the "Updates Available" embed already
        # does this for fields, and the Telegram side does it for
        # both pre/post-update message types since v1.19.2).
        name_md = f"[**{name}**]({source_url})" if source_url else f"**{name}**"
        if success:
            embed = {
                "title": f"{title_prefix}✅ Update Successful",
                "description": f"{name_md} (`{image}`)\n{detail}",
                "color": 0x3fb950,  # Green
                "footer": {"text": self._footer_text()},
            }
        else:
            embed = {
                "title": f"{title_prefix}❌ Update Failed",
                "description": f"{name_md} (`{image}`)\n{detail}",
                "color": 0xf85149,  # Red
                "footer": {"text": self._footer_text()},
            }
        self.post({"embeds": [embed]})

    def send_message(self, text):
        """Send plain text to Discord."""
        # Strip Markdown bold (*text*) for Discord
        clean = text.replace("*", "**")
        label = self._bot_label()
        if label:
            clean = f"**{label}** · {clean}"
        self.post({"content": clean})
