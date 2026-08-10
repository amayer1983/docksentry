#!/usr/bin/env python3
"""Two things @NotRetarded found by using the bot rather than reading it.

**Nothing told you what a `host` was called.** Discord shows the option's
description and then an empty text field; the value it will accept is the
app's business and the app was not saying. With one host that is a small
puzzle (`local`, eventually), with five it is guesswork. Discord has a
mechanism for exactly this — autocomplete — and we had it switched off
everywhere. The answer was never secret: `/hosts` has listed them all
along, just not where the typing happens.

**The bot channel dropped the release link.** The webhook renders the
container name as a clickable `[name](url)` to the release page; the bot
posted the name as plain code. Not a formatting quirk — the method took
`source_url` and used it for nothing. Two hand-written renderings of one
notification, and only one of them was maintained.

The fix for the second is to delete the second rendering: the bot channel
now inherits the webhook's embeds and overrides nothing but the transport.
Which is why most of what is checked below is *sameness* — same embed,
same fields, same link — because that is the property that keeps them from
drifting apart again.
"""

import json
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from discord_bot import COMMANDS, AUTOCOMPLETE_OPTIONS, DiscordBot  # noqa: E402
from notifiers.discord import DiscordNotifier  # noqa: E402
from notifiers.discordbot import DiscordBotNotifier  # noqa: E402

checks = {}


# ── harness ──────────────────────────────────────────────────────────
class FakeREST:
    def __init__(self):
        self.calls = []

    def me(self):
        return {"username": "bot"}

    def register_commands(self, app_id, cmds, guild_id=None):
        self.calls.append(("register", cmds))

    def interaction_response(self, iid, itok, content=None, **kw):
        self.calls.append(("ack", iid, kw.get("deferred", False)))

    def edit_original_response(self, app_id, itok, content, **kw):
        self.calls.append(("edit", content))

    def create_message(self, channel_id, content, **kw):
        self.calls.append(("channel", channel_id, content, kw.get("embeds")))

    def interaction_autocomplete(self, iid, itok, choices):
        self.calls.append(("auto", iid, choices))


class FakeBackend:
    def __init__(self, names):
        self.names = names

    def run(self, args, **kw):
        return types.SimpleNamespace(
            returncode=0, stdout="\n".join(self.names) + "\n", stderr="")


def _host(name, names, local=False):
    h = types.SimpleNamespace(name=name, is_local=local, endpoint="",
                              backend=FakeBackend(names))
    h.checker = types.SimpleNamespace(backend=h.backend)
    return h


class FakeRegistry:
    def __init__(self, hosts):
        self.hosts = hosts

    def __iter__(self):
        return iter(self.hosts)

    def __len__(self):
        return len(self.hosts)

    @property
    def is_multi(self):
        return len(self.hosts) > 1

    @property
    def local(self):
        return next((h for h in self.hosts if h.is_local), self.hosts[0])

    @property
    def names(self):
        return [h.name for h in self.hosts]

    def get(self, n):
        return next((h for h in self.hosts if h.name == n), None)


def wait_for(rest, kind, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hits = [c for c in rest.calls if c[0] == kind]
        if hits:
            return hits
        time.sleep(0.01)
    return []


def autocomplete(bot, option, typed="", options=None, **extra):
    """Send one autocomplete interaction and return the choices sent back."""
    opts = list(options or [])
    opts.append({"name": option, "value": typed, "focused": True})
    data = {"guild_id": "g", "type": 4, "id": "1", "token": "tk",
            "member": {"user": {"id": "42"}},
            "data": {"name": "status", "options": opts}}
    data.update(extra)
    bot.rest.calls.clear()
    bot._on_event("INTERACTION_CREATE", data)
    hits = wait_for(bot.rest, "auto")
    return [c["value"] for c in hits[0][2]] if hits else None


LOCAL = ["docksentry", "nginx", "paperless-web"]
NAS = ["plex", "sonarr"]

cfg = types.SimpleNamespace(discord_bot_token="t", discord_app_id="app",
                            discord_guild_id="g", pending_file="")
engine = types.SimpleNamespace()
reg = FakeRegistry([_host("local", LOCAL, local=True), _host("nas", NAS)])
bot = DiscordBot(cfg, None, engine, hosts=reg,
                 checker=reg.hosts[0].checker, log=lambda *_: None)
bot.rest = FakeREST()


# ── the option is declared as one Discord will suggest for ───────────
_pairs = [(c["name"], o) for c in COMMANDS for o in c.get("options") or []]
checks["every host/container option asks Discord for autocomplete"] = all(
    o.get("autocomplete") for _, o in _pairs
    if o["name"] in AUTOCOMPLETE_OPTIONS)
checks["…and nothing else claims it"] = not any(
    o.get("autocomplete") for _, o in _pairs
    if o["name"] not in AUTOCOMPLETE_OPTIONS)
# It is set in one place for all of them, so command 20 cannot be added
# without it — the same reasoning as the audit seam in `_dispatch`.
checks["…every command with a host is covered, not just /status"] = len(
    {c for c, o in _pairs if o["name"] == "host" and o.get("autocomplete")}) > 5
# Discord rejects the whole registration document when an option carries
# both `autocomplete` and a fixed `choices` list. Neither may appear.
checks["no option mixes autocomplete with fixed choices"] = not any(
    o.get("autocomplete") and o.get("choices") for _, o in _pairs)
# Autocomplete is only valid for STRING/INTEGER/NUMBER options.
checks["…and only string options carry it"] = all(
    o["type"] == 3 for _, o in _pairs if o.get("autocomplete"))


# ── it answers with the names that actually work ─────────────────────
checks["a host option is offered every managed host"] = (
    autocomplete(bot, "host") == ["local", "nas"])
# The one that was asked for: `local` is a name you had to know.
checks["…local first, so the local host names itself"] = (
    autocomplete(bot, "host")[0] == "local")
checks["typing filters the suggestions"] = (
    autocomplete(bot, "host", "na") == ["nas"])
checks["…case-insensitively"] = autocomplete(bot, "host", "NA") == ["nas"]

checks["a container option is offered the containers"] = (
    autocomplete(bot, "container") == LOCAL)
# Substring rather than prefix: people type the distinctive part.
checks["…matched anywhere in the name, not just the start"] = (
    autocomplete(bot, "container", "less") == ["paperless-web"])
# Following the host that has already been filled in is the whole reason
# this is worth doing on a multi-host install.
checks["…from the host already chosen in the same command"] = (
    autocomplete(bot, "container", "",
                 options=[{"name": "host", "value": "nas"}]) == NAS)
checks["…and from the local host when no host is chosen yet"] = (
    autocomplete(bot, "container") == LOCAL)


# ── the deadline and the failure modes ───────────────────────────────
# Three seconds, and no such thing as a deferred autocomplete: the choices
# ARE the response. Sending an acknowledgement first would answer the
# interaction with the wrong type and lose the suggestions.
_ = autocomplete(bot, "host")
checks["autocomplete is not acknowledged first"] = not [
    c for c in bot.rest.calls if c[0] == "ack"]

# A backend that hangs or dies must degrade to "no suggestions" — never
# to an exception on a worker thread, and never to an error in the user's
# face while they are still typing.
class DeadBackend:
    def run(self, *a, **kw):
        raise RuntimeError("host is down")


dead = FakeRegistry([_host("local", [], local=True)])
dead.hosts[0].backend = DeadBackend()
dead.hosts[0].checker = types.SimpleNamespace(backend=DeadBackend())
dbot = DiscordBot(cfg, None, engine, hosts=dead,
                  checker=dead.hosts[0].checker, log=lambda *_: None)
dbot.rest = FakeREST()
checks["an unreachable host answers with no suggestions"] = (
    autocomplete(dbot, "container") == [])
checks["…and still answers, rather than leaving a spinner"] = [
    c for c in dbot.rest.calls if c[0] == "auto"]

# Discord refuses a response carrying more than 25 choices — with the
# whole response, so 26 containers would mean no suggestions at all.
many = FakeRegistry([_host("local", [f"c{i:02}" for i in range(40)],
                           local=True)])
mbot = DiscordBot(cfg, None, engine, hosts=many,
                  checker=many.hosts[0].checker, log=lambda *_: None)
mbot.rest = FakeREST()
checks["more containers than Discord allows are capped at 25"] = len(
    autocomplete(mbot, "container")) == 25

# Suggesting host names to a stranger would give away the estate this bot
# manages — the same reasoning that makes an unauthorised command silent.
wrong = {"guild_id": "somebody-elses-server"}
checks["an interaction from another guild gets no suggestions"] = (
    autocomplete(bot, "host", **wrong) is None)


# ── the bot channel and the webhook send the same message ────────────
def notifier_cfg(**kw):
    base = dict(bot_label="", quiet_hours_start="", quiet_hours_end="",
                discord_webhook="https://discord.com/api/webhooks/1/x",
                discord_bot_token="tok", discord_bot_channel="123",
                channel_discord_enabled=True, channel_discordbot_enabled=True)
    # The other channels have to exist and be empty: `send_weekly_report`
    # walks every plugin, and a bare namespace makes them raise rather
    # than report themselves inactive.
    base.update(dict(
        webhook_url="", smtp_host="", smtp_from="", smtp_to="", smtp_user="",
        smtp_password="", smtp_port=587, smtp_tls="starttls",
        smtp_tls_verify=True, ntfy_url="", ntfy_server="", ntfy_topic="",
        ntfy_token="", ntfy_user="", ntfy_password="", gotify_url="",
        gotify_token="", matrix_homeserver="", matrix_room="",
        matrix_token="", apprise_url="", apprise_urls="", apprise_tag=""))
    base.update(kw)
    return types.SimpleNamespace(**base)


UPDATE = {"name": "Dozzle", "image": "amir20/dozzle:latest",
          "size": "66 MB", "created": "2026-08-09",
          "source_url": "https://github.com/amir20/dozzle/releases",
          "old_version": "10.6.15", "new_version": "10.7.1"}

sent = []
web = DiscordNotifier(notifier_cfg())
web.post = lambda payload: sent.append(("web", payload))
botch = DiscordBotNotifier(notifier_cfg())
botch.post = lambda payload: sent.append(("bot", payload))

for chan in (web, botch):
    chan.send_update_result(UPDATE["name"], UPDATE["image"], True,
                            detail="OK", source_url=UPDATE["source_url"])
    chan.send_updates_available([UPDATE])

by = {}
for who, payload in sent:
    by.setdefault(who, []).append(payload)

checks["the bot channel sends embeds, not plain text"] = all(
    "embeds" in p for p in by["bot"])
# The property worth having: not "the bot also has a link" but "the two
# are the same message". A future change to one is a change to both.
checks["…byte-for-byte the same embeds as the webhook"] = (
    json.dumps(by["bot"], sort_keys=True) ==
    json.dumps(by["web"], sort_keys=True))
# And the thing that was actually missing.
_result = by["bot"][0]["embeds"][0]["description"]
checks["an update result links the container name to the release"] = (
    f'[**Dozzle**]({UPDATE["source_url"]})' in _result)
_avail = by["bot"][1]["embeds"][0]["fields"][0]["value"]
checks["…and a pending update carries its Source link"] = (
    f'[Source ↗]({UPDATE["source_url"]})' in _avail)
checks["…with the version badge the bot used to leave out"] = (
    "v10.6.15" in _avail and "v10.7.1" in _avail)

# Inheriting the transport would post the bot's message to the webhook —
# the one thing this subclass must not share.
checks["the bot channel still speaks through the bot"] = (
    "create_message" in
    __import__("inspect").getsource(DiscordBotNotifier.post))
checks["…and is still discovered as its own channel"] = (
    DiscordBotNotifier.name == "discordbot" and
    DiscordBotNotifier.OWNS == ("discord_bot_channel",))
# It must not inherit the webhook's idea of being configured, or a bot
# channel with no webhook would go quiet.
checks["…configured by its own fields"] = (
    DiscordBotNotifier(notifier_cfg(discord_webhook="")).configured() and
    not DiscordBotNotifier(notifier_cfg(discord_bot_channel="")).configured())


# ── the weekly report reaches it too ─────────────────────────────────
# Wiring the bot channel in was the fix here; #59 then showed the wiring
# itself was the wrong shape, and `test_weekly_report_channels.py` owns
# that. What is left for this file is the narrow claim it made: the bot
# channel is a recipient of the weekly report at all.
from notifier import Notifier  # noqa: E402

posts = []
n = Notifier(notifier_cfg())
n._by_name["discordbot"].post = lambda p: posts.append(p)
n.send_weekly_report({"successes": 1}, "text", {"title": "weekly"})
checks["the weekly report's embed reaches the bot channel"] = (
    len(posts) == 1 and "embeds" in posts[0])
off = Notifier(notifier_cfg(channel_discordbot_enabled=False))
off._by_name["discordbot"].post = lambda p: posts.append(p)
off.send_weekly_report({"successes": 1}, "text", {"title": "weekly"})
checks["…and respects the channel's switch"] = len(posts) == 1


failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
