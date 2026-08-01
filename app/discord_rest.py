#!/usr/bin/env python3
"""Discord REST calls — the half the gateway can't do.

The gateway delivers events; everything a bot *says* goes over REST:
answering an interaction, editing that answer, registering the slash
commands in the first place.

The one thing that must not be got wrong here is rate limiting. Discord
answers an over-eager client with 429 and a `retry_after`, and a client
that retries immediately instead of waiting gets its whole application
temporarily banned — not the request, the application. So 429 is handled
here, once, rather than trusted to every call site.

`urllib` only, like the rest of the project.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://discord.com/api/v10"

#: Discord asks for a descriptive User-Agent and can throttle generic
#: ones harder. Ours names the project and links it, per their docs.
USER_AGENT = ("DiscordBot (https://github.com/amayer1983/docksentry, 1.0)")

#: A 429 asking us to wait longer than this means something is badly
#: wrong (usually a global limit from another instance sharing the
#: token). Waiting it out inside a request would hang the caller, so we
#: give up and let it retry later instead.
MAX_RETRY_AFTER = 60.0


class DiscordRESTError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class DiscordREST:
    """Thin REST client bound to one bot token."""

    def __init__(self, token, *, base=API_BASE, timeout=15, log=print,
                 sleep=time.sleep):
        self.token = token
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.log = log
        #: Injectable so tests don't actually sit out a rate limit.
        self._sleep = sleep

    def request(self, method, path, payload=None, *, attempts=3):
        """One API call, with 429 handling. Returns the decoded body, or
        None for 204. Raises DiscordRESTError on any other 4xx/5xx."""
        url = f"{self.base}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Authorization": f"Bot {self.token}",
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(attempts):
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    if r.status == 204 or not raw:
                        return None
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                if e.code == 429 and attempt < attempts - 1:
                    wait = self._retry_after(raw, e)
                    if wait is None or wait > MAX_RETRY_AFTER:
                        raise DiscordRESTError(e.code, raw)
                    self.log(f"Discord rate limited; waiting {wait:.1f}s")
                    self._sleep(wait)
                    continue
                # 5xx is Discord having a bad day — worth one more go.
                if 500 <= e.code < 600 and attempt < attempts - 1:
                    self._sleep(1 + attempt)
                    continue
                raise DiscordRESTError(e.code, raw)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < attempts - 1:
                    self._sleep(1 + attempt)
                    continue
                raise DiscordRESTError(0, str(e))
        raise DiscordRESTError(0, "retries exhausted")

    @staticmethod
    def _retry_after(raw, err):
        """Seconds to wait, from the body or the header. Discord sends
        both; the body is authoritative and float-valued."""
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "retry_after" in data:
                return float(data["retry_after"])
        except (ValueError, TypeError):
            pass
        header = None
        try:
            header = err.headers.get("Retry-After")
        except Exception:
            pass
        try:
            return float(header) if header is not None else None
        except (TypeError, ValueError):
            return None

    # ── the calls a bot actually makes ────────────────────────────
    def me(self):
        """The bot's own user object. Cheapest way to prove a token
        works, so it doubles as the startup check."""
        return self.request("GET", "/users/@me")

    def register_commands(self, application_id, commands, guild_id=None):
        """Replace the registered slash commands (bulk overwrite).

        Guild-scoped registration appears instantly; global registration
        can take up to an hour to propagate. That difference matters
        enough that the caller chooses — for a bot serving one server,
        guild scope is almost always what you want.
        """
        if guild_id:
            path = f"/applications/{application_id}/guilds/{guild_id}/commands"
        else:
            path = f"/applications/{application_id}/commands"
        return self.request("PUT", path, commands)

    def interaction_response(self, interaction_id, interaction_token,
                             content=None, *, ephemeral=True, deferred=False,
                             components=None):
        """Answer an interaction.

        Discord gives a **three second** deadline for the first response
        and shows the user an error if it's missed. Anything slower than
        that — which for us is most things, since they shell out to the
        container CLI — must send `deferred=True` first and edit the
        message afterwards.

        Ephemeral by default: a container list is noise for everyone else
        in the channel, and update output can name internal hosts.
        """
        # 5 = CHANNEL_MESSAGE_WITH_SOURCE, 6 = DEFERRED_UPDATE,
        # 4 = immediate message, 5 = deferred message
        payload = {"type": 5 if deferred else 4}
        if not deferred:
            data = {"content": content or ""}
            if ephemeral:
                data["flags"] = 64      # EPHEMERAL
            if components:
                data["components"] = components
            payload["data"] = data
        elif ephemeral:
            payload["data"] = {"flags": 64}
        return self.request(
            "POST",
            f"/interactions/{interaction_id}/{interaction_token}/callback",
            payload)

    def edit_original_response(self, application_id, interaction_token,
                               content, *, components=None):
        """Fill in (or replace) a deferred answer."""
        payload = {"content": content}
        if components is not None:
            payload["components"] = components
        return self.request(
            "PATCH",
            f"/webhooks/{application_id}/{interaction_token}/messages/@original",
            payload)

    def create_message(self, channel_id, content, *, components=None):
        """An unsolicited message — update notifications, alerts."""
        payload = {"content": content}
        if components:
            payload["components"] = components
        return self.request("POST", f"/channels/{channel_id}/messages", payload)
