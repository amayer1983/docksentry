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

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request

#: NOTE — this module depends on `main.py` having forced IPv4-only
#: resolution (it patches `socket.getaddrinfo` at import, unless
#: `DOCKSENTRY_IPV6=true`). That is not a nicety here: on a host without
#: working IPv6 routing, Python tries the AAAA record first and each call
#: costs ~5 seconds before falling back. Discord kills an unacknowledged
#: interaction after **three**, so every command would fail with
#: "Unknown interaction" (10062) while every other part of Docksentry
#: merely felt slow. Import this module through the app, not standalone,
#: or apply the same patch — a test harness that skips `main.py`
#: reproduces exactly that failure.
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

    def create_message(self, channel_id, content, *, components=None,
                       embeds=None):
        """An unsolicited message — update notifications, alerts.

        `embeds` so the bot channel can send the very same embeds the
        webhook does. That is not decoration: a masked `[name](url)` link
        renders inside an embed for certain, which is how the webhook has
        always shown the release link (#57, @NotRetarded noticed the bot
        was missing it).
        """
        payload = {"content": content}
        if components:
            payload["components"] = components
        if embeds:
            payload["embeds"] = embeds
        return self.request("POST", f"/channels/{channel_id}/messages", payload)

    def upload(self, path, filename, data, content="", *, method="POST"):
        """Attach a file to a message. Returns the decoded body.

        Discord takes attachments as multipart/form-data: the ordinary
        JSON body under `payload_json`, and each file under `files[n]`
        with a matching entry in `attachments`. That is a different body
        format from every other call this client makes, so it builds its
        own request rather than teaching `request()` a second shape and
        risking the path every notification already goes through.

        `.json` is attached as-is, with its real name. @famewolf and
        @NotRetarded both expected Discord would need it renamed to
        `.txt` or wrapped in a zip (#2); the documented API takes an
        arbitrary filename and there is no extension whitelist for bot
        uploads, so it should not.

        **Not verified against the live API.** The body below is built to
        Discord's documented multipart shape and checked structurally,
        but nothing here has posted a real `.json` to a real channel — I
        have no bot of my own, and using somebody else's channel to find
        out is not mine to do. If Discord does refuse it, the fix is a
        zip rather than a `.txt` rename: renaming a file to hide what it
        is only moves the problem to whatever has to open it later.

        Not retried. A duplicated 30 kB upload into a channel is worse
        than an error the user can act on by asking again.
        """
        boundary = "----docksentry" + hashlib.sha1(
            (filename + str(len(data))).encode()).hexdigest()[:16]
        payload = {"content": content,
                   "attachments": [{"id": 0, "filename": filename}]}
        parts = [
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="payload_json"\r\n'
            f"Content-Type: application/json\r\n\r\n"
            f"{json.dumps(payload)}\r\n".encode("utf-8"),
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files[0]"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: application/json\r\n\r\n".encode("utf-8"),
            data if isinstance(data, bytes) else data.encode("utf-8"),
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)
        req = urllib.request.Request(
            f"{self.base}{path}", data=body, method=method,
            headers={"Authorization": f"Bot {self.token}",
                     "User-Agent": USER_AGENT,
                     "Content-Type":
                         f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60)) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise DiscordRESTError(e.code, e.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DiscordRESTError(0, str(e))

    def upload_to_channel(self, channel_id, filename, data, content=""):
        return self.upload(f"/channels/{channel_id}/messages",
                           filename, data, content)

    def upload_followup(self, application_id, interaction_token,
                        filename, data, content=""):
        """A file as the answer to a deferred slash command."""
        return self.upload(
            f"/webhooks/{application_id}/{interaction_token}",
            filename, data, content)

    def interaction_autocomplete(self, interaction_id, interaction_token,
                                 choices):
        """Answer an autocomplete interaction with up to 25 suggestions.

        Type 8 = APPLICATION_COMMAND_AUTOCOMPLETE_RESULT. There is no
        deferring this one: Discord's three-second deadline applies and a
        late answer is simply dropped, which is why the caller keeps the
        work cheap.

        Discord rejects the whole response over 25 choices, so the cap is
        applied here rather than trusted to every caller.
        """
        payload = {"type": 8, "data": {"choices": list(choices)[:25]}}
        return self.request(
            "POST",
            f"/interactions/{interaction_id}/{interaction_token}/callback",
            payload)
