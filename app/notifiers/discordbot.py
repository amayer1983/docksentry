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

from .discord import DiscordNotifier


class DiscordBotNotifier(DiscordNotifier):
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
    def post(self, payload, on_network_failure=None):
        """Send one payload the bot's way instead of the webhook's.

        The ONLY thing this channel changes about a Discord message is
        how it gets there. Everything above — the embeds, their colors,
        the version badge, the clickable `[name](url)` release link, the
        25-field chunking — is inherited from the webhook channel, which
        is the entire point: @NotRetarded compared the two side by side
        and found the bot's version poorer (#57). It shouldn't have been
        a different message at all; it was only ever a different pipe.

        Written by hand before this, it lost the source link — the method
        took `source_url` and never used it. Two renderings of the same
        notification is one too many to keep in step.

        Best-effort, like every other channel: logs and returns on
        failure, never raises into the facade.
        """
        from discord_rest import DiscordREST, DiscordRESTError
        try:
            DiscordREST(self._token()).create_message(
                self._channel(),
                (payload.get("content") or "")[:1900],
                embeds=payload.get("embeds"))
        except DiscordRESTError as e:
            print(f"Discord bot channel error: {e}")
            # `DiscordREST` uses status 0 for "the request never got an
            # answer at all" — its own word for a network failure, and the
            # same line the webhook path draws: a 403 is a no, an
            # unreachable host is a not-yet (#66).
            if e.status == 0 and on_network_failure is not None:
                on_network_failure()
        except Exception as e:                                # pragma: no cover
            print(f"Discord bot channel error: {e}")
