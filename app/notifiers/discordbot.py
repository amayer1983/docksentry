#!/usr/bin/env python3
"""The Discord BOT as a notification channel (#57, @NotRetarded).

Distinct from `discord.py`, which posts through a webhook URL. This one is
the bot itself speaking into a channel it was given the id of — which is
what was actually asked for:

> I do like the channel ID for the simple reason that it will eliminate the
> webhook option and keep everything within the bot settings.

Why it was needed at all: every slash-command answer is *ephemeral* —
visible only on the device that sent the command, and it deletes itself
after a while. That is deliberate for answers, because a container listing
names your internal services, and it is no use whatever for "bot started"
or a crash alert. Before this, the bot could only ever answer; it could not
speak.

The mechanism is not new. `DiscordREST.create_message` posts an ordinary,
permanent, everyone-can-see-it message and has been running in production
for a while — as the fallback for an answer that overran Discord's
15-minute interaction window. It simply was not wired to anything else.

A plugin rather than a method on the bot, so it goes through the same
facade as every other channel: quiet hours, the per-channel switch and the
"send a test" button all work without a special case. It builds its own
REST client from the token in config, which is why it does not need a
running bot to be useful — configure it and it works, whether or not the
gateway happens to be connected.
"""

from .base import BaseNotifier


class DiscordBotNotifier(BaseNotifier):
    name = "discordbot"
    #: After the Discord webhook, so a setup carrying both keeps the
    #: historical ordering in its logs.
    order = 25

    OWNS = ("discord_bot_channel",)
    REQUIRES = (("discord_bot_token", "web_discord_token"),
                ("discord_bot_channel", "web_discord_bot_channel"))

    def configured(self):
        """A token to speak with and a channel to speak into.

        Deliberately NOT requiring the application id: that one is for
        registering slash commands, and posting a message needs no
        commands. Someone who wants notifications from the bot and no
        commands at all is a coherent setup.
        """
        return bool(self._token() and self._channel())

    def _token(self):
        return (self.setting("discord_bot_token", "DISCORD_BOT_TOKEN") or "").strip()

    def _channel(self):
        return (self.setting("discord_bot_channel", "DISCORD_BOT_CHANNEL") or "").strip()

    # ── transport ────────────────────────────────────────────────────
    def _post(self, text):
        """Post one message. Best-effort, like every other channel: logs
        and returns on failure, never raises into the facade."""
        from discord_rest import DiscordREST, DiscordRESTError
        label = self._bot_label()
        body = (f"{label} · " if label else "") + text
        try:
            DiscordREST(self._token()).create_message(self._channel(), body[:1900])
        except DiscordRESTError as e:
            print(f"Discord bot channel error: {e}")
        except Exception as e:                                # pragma: no cover
            print(f"Discord bot channel error: {e}")

    # ── payloads ─────────────────────────────────────────────────────
    def send_updates_available(self, updates):
        if not updates:
            return
        lines = [f"• `{u['name']}` → {u.get('image', '?')}"
                 + (f"  ({self.version_str(u)})" if self.version_str(u) else "")
                 for u in updates]
        self._post("🔄 **Updates available**\n" + "\n".join(lines))

    def send_update_result(self, name, image, success, detail="", source_url=""):
        mark = "✅" if success else "❌"
        text = f"{mark} `{name}` — {image}"
        if detail:
            text += f"\n{detail}"
        self._post(text)

    def send_message(self, text):
        self._post(text)
