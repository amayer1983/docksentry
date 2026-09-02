"""One unattended message, to every channel that is switched on (#63).

Three times a notification was written against Telegram's `send_message`
and quietly reached Telegram alone: the release link the bot channel
never got (#57), the "restarted on vX" line that no Discord, e-mail or
ntfy user ever saw, and the auto-update batch notices @NotRetarded
photographed side by side (#61) — Discord had the per-container results
and neither the "starting" nor the "complete" line around them. Each was
fixed where it was found, which is precisely why there was a third.

So there is one seam, and this is it. It lived on the Telegram bot, and
the Discord bot reached across into that instance to borrow it
(`bot.announce`) — which made the seam look like Telegram's, when what it
actually does is hand one text to every channel. It lives here now, both
front ends hold the same instance, and neither depends on the other.

Third step of the core extraction agreed on 2026-08-20. The seam holds
*senders*, not logic: `telegram` is whatever can put a message in the
Telegram chat, `notifier` is the multi-channel dispatcher. main.py builds
it once and hands it to both front ends.
"""


class Broadcast:
    """Every switched-on channel, behind one call."""

    def __init__(self, telegram=None, notifier=None, log=print):
        #: The Telegram bot, when one is running. Held as a sender — the
        #: only thing asked of it is `send_message`, and whether it is
        #: switched on.
        self.telegram = telegram
        #: The multi-channel dispatcher (Discord, webhook, e-mail, ntfy).
        self.notifier = notifier
        self.log = log

    def announce(self, text, reply_markup=None):
        """Send `text` to every channel that is switched on.

        `reply_markup` is Telegram's alone; the other channels get the
        text. A button is not something an e-mail can carry, and leaving
        it out is better than inventing a second-class version of it.

        The fan-out is guarded, the Telegram send is not — exactly as it
        was on the bot. Worth revisiting (a throwing Telegram currently
        takes the other channels down with it, which is the bug class
        this seam exists to prevent), but that is a change, and this step
        only moves the seam.
        """
        tg = self.telegram
        if tg is not None and getattr(tg, "enabled", False):
            tg.send_message(text, reply_markup=reply_markup, auto=True)
        notifier = self.notifier
        try:
            if notifier is not None and notifier.has_channels():
                notifier.send_message(text)
        except Exception as e:
            self.log(f"Could not fan out an announcement: {e}")

    def tell(self, text, skip=()):
        """Every channel, and not silenced by quiet hours.

        `skip` names channels this one message must not reach. It exists
        for a single case: a self-update somebody asked for privately
        announces its own restart, and posting that in the Discord
        channel publishes exactly what the private reply was hiding
        (#63, @NotRetarded). Every other channel is the operator's own
        and keeps it.

        `announce` marks its Telegram message as unattended, which is
        what quiet hours suppress — right for a notification nobody
        asked for. Wrong for the report on something a person just
        started: a self-update at 23:00 must still say how it went, and
        it always has. So this is the same fan-out with the Telegram
        message sent as an answer rather than an announcement.

        The difference is one flag, and it is deliberately not a
        parameter on `announce`: which of the two a message is, is a
        property of the message, and a caller that has to pass a boolean
        gets it wrong eventually.
        """
        tg = self.telegram
        if tg is not None and getattr(tg, "enabled", False):
            tg.send_message(text)
        notifier = self.notifier
        try:
            if notifier is not None and notifier.has_channels():
                notifier.send_message(text, skip=skip)
        except Exception as e:
            self.log(f"Could not fan out a report: {e}")
