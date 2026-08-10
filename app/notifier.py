#!/usr/bin/env python3
"""Multi-channel notification dispatcher (Discord, Webhook, e-mail/SMTP, ntfy).

Thin facade over the channel plugins in ``app/notifiers/``. The dispatch code
no longer knows anything about individual channels: it asks the registry for
the configured plugins and forwards each payload best-effort. Adding a channel
is one new file under ``app/notifiers/`` — no edits here.

``import time`` / ``import urllib.request`` are kept at module scope so the
notifier retry test can patch ``notifier.time.sleep`` /
``notifier.urllib.request.urlopen`` (they mutate the shared module objects the
plugins' transport helper uses).
"""

import time  # noqa: F401  (kept for test monkeypatch surface)
import urllib.request  # noqa: F401  (kept for test monkeypatch surface)

from quiet_hours import is_quiet_now
from notifiers import build_all, version_str


class Notifier:
    """Dispatches notifications to every configured channel plugin."""

    def __init__(self, config):
        self.config = config
        # Instantiate all registered channels once; filter by `configured()`
        # at dispatch time so a live config change (Web UI) is picked up.
        self._plugins = build_all(config)
        self._by_name = {p.name: p for p in self._plugins}

    def _configured_plugins(self):
        # `active()`, not `configured()`: a channel that is switched off
        # still has everything it needs and must still not be sent to.
        # The two were the same thing until the switch existed.
        return [p for p in self._plugins if p.active()]

    def has_channels(self):
        """True when at least one channel is switched on AND complete."""
        return any(p.active() for p in self._plugins)

    def channel_states(self):
        """`(name, enabled, configured, missing)` for every channel.

        For the Connections page, which has to distinguish "off" from
        "incomplete" — they need different things done about them, and
        one boolean answers neither question.
        """
        return [(p.name, p.enabled, p.configured(), p.missing())
                for p in self._plugins]

    def isolated_config(self, name):
        """A copy of config with every channel but `name` blanked.

        What the "send a test" button needs: build a Notifier from this
        and only the channel under test can be active, so the test says
        something about that channel and nothing else. The list of what
        to blank comes from each plugin's own OWNS rather than a second
        table here, which is the table that would go stale.
        """
        from copy import copy as _copy
        test_config = _copy(self.config)
        for plugin in self._plugins:
            if plugin.name == name:
                continue
            for attr in plugin.OWNS:
                setattr(test_config, attr, "" if not isinstance(
                    getattr(test_config, attr, ""), bool) else False)
        # The switch must not veto a deliberate test either: someone
        # testing a channel they have just turned off is asking whether
        # it works, not whether it is on.
        setattr(test_config, f"channel_{name}_enabled", True)
        # And quiet hours must not swallow the one message the user is
        # standing there waiting for.
        test_config.quiet_hours_start = ""
        test_config.quiet_hours_end = ""
        return test_config

    def _smtp_configured(self):
        """E-mail is going to be sent: complete AND switched on (#2).

        `active()`, like the dispatch list above — the callers use this to
        decide whether to *say* an e-mail went out, and saying so about a
        channel that is switched off would be a lie.
        """
        p = self._by_name.get("smtp")
        return bool(p and p.active())

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

    @staticmethod
    def _version_str(u):
        """`v_old → v_new` when both are known and differ, else the single
        known version, else "". Mirrors the Telegram badge (#44) so Discord /
        webhook / e-mail show the same version info."""
        return version_str(u)

    def _dispatch(self, method, *args):
        """Call `method` on every configured plugin, best-effort: a channel
        that raises must not stop the others (each existing channel is already
        internally best-effort; this also contains any new/buggy channel)."""
        for p in self._configured_plugins():
            try:
                getattr(p, method)(*args)
            except Exception as e:
                print(f"{p.name} error: {e}")

    def send_weekly_report(self, stats, text, embed=None):
        """The weekly report, to every channel that is on.

        Returns True if it went anywhere. Quiet hours deliberately do not
        apply: the report is sent at an hour the operator picked, and
        suppressing it there would mean skipping the week rather than
        delaying it.
        """
        targets = self._configured_plugins()
        for p in targets:
            try:
                p.send_weekly_report(stats, text, embed)
            except Exception as e:
                print(f"{p.name} error: {e}")
        return bool(targets)

    def send_updates_available(self, updates):
        """Notify about available updates."""
        if self._suppressed():
            return
        self._dispatch("send_updates_available", updates)

    def send_update_result(self, name, image, success, detail="", source_url=""):
        """Notify about a completed update (success or failure).

        ``source_url`` is the resolved repo / changelog link for the
        container (manual override → OCI label → registry fallback,
        resolved once by the caller via
        ``TelegramBot._enrich_with_source_url``). When present, the
        Discord embed wraps the container name as a clickable link
        and the generic webhook payload carries the URL alongside
        the other fields. Closes a parity gap reported by @NotRetarded
        in #2 — v1.19.2 fixed the Telegram side, this fixes Discord
        and the webhook payload.
        """
        if self._suppressed():
            return
        self._dispatch("send_update_result", name, image, success, detail, source_url)

    def send_message(self, text):
        """Send a plain text notification (subject to quiet hours)."""
        if self._suppressed():
            return
        self._dispatch("send_message", text)

    # ── Backwards-compat delegators ──────────────────────────────────
    # weekly_report.py (not part of this change) and the notifier tests
    # reach into these internal entry points directly. They forward to the
    # matching plugin so the wire behaviour and return values are unchanged.

    def _discord_post(self, payload):
        """POST JSON to Discord webhook (returns HTTP status or None)."""
        return self._by_name["discord"].post(payload)

    def _discordbot_post(self, payload):
        """The same payload through the bot channel, when it is on.

        Returns True when it was sent, False when the channel isn't
        configured or is switched off — so the caller needs no second
        condition, and doesn't depend on what the transport returns.
        """
        p = self._by_name.get("discordbot")
        if p is None or not p.active():
            return False
        p.post(payload)
        return True

    def _webhook_send(self, event, data):
        """POST JSON to the generic webhook (returns HTTP status or None)."""
        return self._by_name["webhook"].send_raw(event, data)

    def _smtp_send(self, subject, body):
        """Send a plain-text e-mail via SMTP (best-effort)."""
        return self._by_name["smtp"].send_raw(subject, body)
