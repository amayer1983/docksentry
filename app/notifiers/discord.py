#!/usr/bin/env python3
"""Discord webhook channel — rich embeds, unchanged wire format.

Byte-for-byte the same embeds the facade produced before the plugin split
(same titles, colors, fields, footer, version badge, clickable source links).
"""

import notify_retry

from .base import BaseNotifier, post_json_with_retry


class DiscordNotifier(BaseNotifier):
    name = "discord"
    order = 10

    OWNS = ("discord_webhook",)
    REQUIRES = (("discord_webhook", "web_discord_webhook"),)

    def configured(self):
        return bool(self.config.discord_webhook)

    # ── transport ────────────────────────────────────────────────────
    def post(self, payload, on_network_failure=None):
        """POST JSON to Discord webhook."""
        return post_json_with_retry(
            self.config.discord_webhook, payload,
            {"User-Agent": "Docksentry/1.0"}, "Discord webhook",
            on_network_failure=on_network_failure)

    def _post_text(self, text):
        """Render `text` the way this channel does, and post it.

        False ONLY when the network was the reason it did not arrive, so it
        can be handed to the retry queue as-is — the queue calls this again
        with the delay notice prepended, and never goes through
        `send_message`, which is what stops a resend from re-queueing
        itself.
        """
        # Strip Markdown bold (*text*) for Discord
        clean = text.replace("*", "**")
        label = self._bot_label()
        if label:
            clean = f"**{label}** · {clean}"
        reached = [True]
        self.post({"content": clean},
                  on_network_failure=lambda: reached.__setitem__(0, False))
        return reached[0]

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
            # The host belongs in the name: `plex` on the NAS and `plex`
            # at home are two containers, and this embed used to leave
            # the difference out while other channels showed it (#63).
            _host = u.get("host") if isinstance(u, dict) else None
            _where = f" @{_host}" if _host and _host != "local" else ""
            fields.append({
                "name": f"📦 {u['name']}{_where}{compose_tag}",
                "value": f"`{u['image']}`{ver_line}\n📦 {u.get('size', '?')} · 🗓️ {u.get('created', '?')}{link_line}",
                "inline": True,
            })

        label = self._bot_label()
        title_prefix = f"{label} · " if label else ""
        # Discord rejects an embed with more than 25 fields — with a 400,
        # so the whole notification was lost rather than truncated. Anyone
        # coming back from a holiday to 30 pending updates got silence
        # (dc#255, dc#185). Split into messages of 25; the title carries
        # the part number so nobody has to wonder whether they saw it all.
        chunks = [fields[i:i + 25] for i in range(0, len(fields), 25)] or [[]]
        for idx, chunk in enumerate(chunks, 1):
            part = f" ({idx}/{len(chunks)})" if len(chunks) > 1 else ""
            # The embed's shape is Discord's own; its WORDS are the
            # shared ones, so the same event does not read differently
            # here than on ntfy or in an e-mail (#63).
            import notify_text
            _title, _ = notify_text.updates_available(
                updates, lang=notify_text.lang_of(self))
            embed = {
                "title": f"{title_prefix}🔄 {_title}{part}",
                "color": 0x58a6ff,  # Blue
                "fields": chunk,
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
        import notify_text
        _title, _ = notify_text.update_result(
            name, image, success,
            lang=notify_text.lang_of(self))
        if success:
            embed = {
                "title": f"{title_prefix}✅ {_title}",
                "description": f"{name_md} (`{image}`)\n{detail}",
                "color": 0x3fb950,  # Green
                "footer": {"text": self._footer_text()},
            }
        else:
            embed = {
                "title": f"{title_prefix}❌ {_title}",
                "description": f"{name_md} (`{image}`)\n{detail}",
                "color": 0xf85149,  # Red
                "footer": {"text": self._footer_text()},
            }
        self.post({"embeds": [embed]})

    def send_weekly_report(self, stats, text, embed=None):
        """The report as its embed, which is what it has always been here.

        Inherited by the bot channel, which overrides only the transport —
        so the two Discord paths cannot drift apart on this either.
        """
        if embed:
            self.post({"embeds": [embed]})
        else:                                             # pragma: no cover
            self.send_message(text)

    def send_message(self, text):
        """Send plain text to Discord.

        Inherited unchanged by the bot channel, which only swaps the pipe.
        """
        if not self._post_text(text):
            print(f"{self.name} send failed: no answer — holding the message "
                  f"for redelivery")
            notify_retry.remember(self.name, text, self._post_text)
