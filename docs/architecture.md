# Architecture: core and connections

This describes how Docksentry is put together after the refactor that ran
through August 2026 (#63), and — just as importantly — which half of that
refactor is still unfinished. It is written for someone about to add a
connection or move code between the two layers.

## The rule

> Wir haben Verbindungen, die nur noch Verbindungen sind. Egal was raus und
> rein geht, sollte zu 100% identisch sein (bis auf Darstellungsthemen, die
> wären ok), aber der Inhalt / Text muss in allen Verbindungen gleich sein.

A connection is a transport. It receives, it renders, it sends. Everything
else — what Docksentry knows, decides and says — lives in the core, once,
and every connection asks the same core the same question.

**Presentation is what a connection may decide for itself.** The markup
dialect (Telegram writes bold as `*one*`, Discord as `**two**`, Matrix
sends real HTML), the length cap and how to handle overflow (Discord hard-
rejects a body over 2000 characters and `DiscordBot._clip` cuts at 1900
with a marker; Telegram's ceiling is 4096 and it splits into chunks), the
bullet glyph, whether a payload is an embed or a plain string, whether a
reply is ephemeral.

**Content is not.** The wording, the language, which facts appear and in
what order, and the host tag. `plex` on the NAS and `plex` at home are two
different containers, so the `@nas` suffix is a fact, not decoration —
four of the six notification channels used to leave it out, and which
container had an update depended on which channel you happened to read.

The test is simple: if a person on Telegram and a person on Discord could
come away believing different things about the same system, that is a
content difference, and it belongs in the core.

## The map

### The core

Ten modules were pulled out of `telegram_bot.py` from 20 August onward.
Each one answers a question that was never Telegram's:

| Module | What it answers |
|---|---|
| `app/changelog.py` | What does `/changelog` have to say — are releases waiting, or are you newest and here is what that version brought? Also the version comparison, including prereleases. |
| `app/container_info.py` | What is one container's live state, stats and disk footprint, on a given host's backend? |
| `app/status_render.py` | Which fields does a container detail show, in what order, as chat lines? |
| `app/selfupdate.py` | How does Docksentry update itself — resolve the target, pull, write the marker, build the swap helper, record the history? |
| `app/selfrestart.py` | May we stop ourselves, and will anything bring us back? |
| `app/broadcast.py` | One text to every channel that is switched on. |
| `app/notify_text.py` | What does an unattended notification *say*, translated? |
| `app/backup.py` | What is in a backup bundle, and what does restoring one do? |
| `app/container_flags.py` | Every per-container flag, note, link, cooldown and audit — set it, clear it, list it, on whichever hosts were named. |
| `app/lifecycle.py` | May this container be stopped, started or restarted, and what happened when we tried? Glob matching lives here too. |

Shared underneath them: `app/i18n.py` with 16 language files under
`app/lang/` (923 keys in `en.json`), the `UpdateEngine`, the per-host
`container_store` state views, and the host registry.

`app/notifier.py` is a thin facade over the channel plugins in
`app/notifiers/` — eight of them today (Discord webhook, the Discord bot
channel, generic webhook, SMTP, ntfy, Gotify, Matrix, Apprise). It asks
the registry which are `active()` — switched on *and* complete — and
forwards each payload best-effort, so one channel that raises cannot take
the others down. `app/notifiers/base.py` holds the interface plus the two
things every channel shares: the bounded POST-with-retry and the version
badge. Adding a channel is one new file; `app/notifiers/__init__.py`
discovers it by walking the package.

### The connections

`app/telegram_bot.py` and `app/discord_bot.py`. What each legitimately
does on its own:

- **Receive and parse its own syntax.** Discord gets structured slash-command
  interactions off a gateway websocket, with autocomplete and buttons;
  Telegram gets free text off long polling and parses `/cmd arg arg`
  itself. These are genuinely different protocols.
- **Transport-specific auth and limits.** Discord needs a bot token, an
  application id and a guild id, and registers its command table with the
  API; Telegram needs a bot token and an allow-list of chat ids.
- **Render and send.** Convert the core's answer into the markup the
  client reads, fit it into the client's size limit, and deal with the
  failure modes of sending.

`DiscordBot.t` is worth looking at as the model: it resolves the shared
translator for the configured language, then wraps every string in
`_tg_bold_to_discord`, which rewrites `*one*` into `**two**` and leaves
backticked spans alone. The sentence is the core's; the asterisks are the
connection's.

## How a message travels

### Outward: an unattended notification

Nobody asked for this one — a scheduled check found updates.

The checker hands the list to `Notifier.send_updates_available(updates)`.
The facade drops it if quiet hours or a maintenance window are active,
then dispatches to every active plugin. Each plugin calls
`notify_text.updates_available(updates, lang=notify_text.lang_of(self),
version_of=…, bullet=…)` and gets back `(title, body)` already translated,
with the host tag already in each line. What the plugin then does with it
is its own business: ntfy sets a title header, SMTP makes it a subject,
Matrix builds a `<ul>` alongside the plain text, Discord assembles an
embed. Six channels, one wording.

For free text rather than a structured payload there is
`broadcast.Broadcast`, built once in `main.py` and handed to both front
ends. `announce(text)` sends to Telegram *and* through the notifier
facade — marked as unattended, so quiet hours can suppress it. `tell(text)`
is the same fan-out with the Telegram message sent as an answer instead,
for the report on something a person started: a self-update at 23:00 must
still say how it went. Which of the two a message is, is a property of the
message, which is why it is two methods and not a boolean parameter.

This seam exists because the same bug happened three times — a
notification written against Telegram's `send_message` that quietly
reached Telegram alone (#57, #61). Each was fixed where it was found,
which is precisely why there was a third.

### Inward: a command

Discord: the gateway event arrives at `DiscordBot._on_event`, which acks
within Discord's three-second window and hands the work to a worker
thread; `_dispatch` routes to a `_cmd_*` method, which returns a string;
`_deliver` clips it and posts it. Telegram: `_handle_message` matches the
text against a long `if`/`elif` chain and calls `send_message`.

In between, both are supposed to ask the same core. Where they do, it
looks like this — `/status <name>`, in both front ends:

```
container_info.state(backend, name)        # inspect-derived facts
container_info.stats(backend, name)        # cpu, memory, net, block io
container_info.disk_facts(backend, name)   # image size, age, writable layer
status_render.collect(name, info, stats=…, store=…, probe=…, disk=…)
status_render.lines(detail, bold="**", host_tag=…)   # "*" on Telegram
```

The only difference either front end is allowed is the bold marker.
`/changelog` has the same shape: `changelog.fetch()`, then
`changelog.report(content, VERSION)` decides which of three things to say
("newer", "current", "unknown"), then `changelog.render_body(body,
bold=…)` lays an entry out. `/restart` asks `selfrestart.policy(backend,
checker, lang)` whether stopping is safe, calls
`selfrestart.record_request(config, by=…)`, delivers its answer, and only
then calls `selfrestart.go_down()` — the three are separate because the
caller has to get its reply out before the process disappears.
`/selfupdate` runs `selfupdate.start(ctx, target, reply=…)` on a thread,
against the single `selfupdate.Context` that `main.py` builds and both
front ends hold.

## What is not done yet

The send side is shared. **The receive side is roughly half shared.**

Both front ends implement the same 35 commands. Sixteen of them now do
nothing but parse their own syntax, call a core function and render the
`Outcome` that comes back:

`/status`, `/audit`, `/logs`, `/history`, `/changelog`, `/checkimages`,
`/pin`, `/unpin`, `/autoupdate`, `/protect`, `/trustrunning`,
`/askmajor`, `/cooldown`, `/note`, `/setlink`, and the lifecycle trio
`/stop` `/start` `/restart`.

The rest still carry their own logic, twice: `/update`, `/updateall`,
`/updates`, `/check`, `/cleanup`, `/groups`, `/maintenance`, `/events`,
`/settings`, `/backup`, `/restore`, `/selfupdate`, `/hosts`, `/lang`,
`/help`, `/debug`, `/testchannel`, `/restart` (the self-restart one).
Some of those are thin and instance-global and will stay that way;
`/update`, `/updateall`, `/check` and `/cleanup` are the ones that
matter, because they are the ones that act.

This matters for a reason that is not aesthetic. Every extraction so far
has found something wrong that had been wrong for a long time:

* Three Discord commands — `/changelog`, `/selfupdate` and `/restart` —
  were broken on every setup while the same three worked in Telegram.
  `/selfupdate` called `bot.check_selfupdate`, a method that does not
  exist, and simply raised.
* Discord's `/note`, `/trustrunning` and `/askmajor` silently acted on
  the local host only, on a multi-host install, with no way to say
  otherwise. Discord had no `@all` at all.
* Telegram's `/logs` and `/audit` did the same in the other direction —
  local-only where their Discord twins already walked every host.
* `/stop web*` worked in one chat and not the other, because the glob
  matching sat inside a front end.
* A stop refused during an update said two different sentences depending
  on which app you had open.

Nothing caught any of it, because there were two implementations and
only one of each pair was ever exercised.

Finishing the receive side means a command becomes a declaration — name,
parameters, permissions, and a core function returning an `Outcome` —
with each connection responsible only for parsing its own syntax into
that call and rendering what comes back.

## Adding a new connection

What the code actually requires today, in order:

1. **Decide what it is.** A notification channel (send only) is one file in
   `app/notifiers/` subclassing `BaseNotifier`: set `name`, `order`, `OWNS`
   and `REQUIRES`, implement `configured()` and the payload methods you can
   carry. The registry finds it; there is no list to edit. A bot that also
   answers commands is a connection, and the rest of this list applies.
2. **Take your dependencies from `main.py`, do not build your own.** One
   config, one store, one `UpdateEngine` (which owns the single update
   mutex), one host registry, one `Broadcast`, one `selfupdate.Context`.
   `DiscordBot.__init__` is the reference for the argument list. Never
   reach into another connection's instance for machinery — if you need
   something that only lives there, extract it to the core first.
3. **Take every user-facing sentence from `i18n`.** Resolve the translator
   per call from `config.language`, not once at construction, or `/lang`
   will not apply until a restart. If a string you need does not exist,
   add the key to all of `app/lang/*.json` — do not write the sentence in
   your file.
4. **Call the core for the answer.** `container_info` + `status_render`
   for status, `changelog.report` + `changelog.render_body` for the
   changelog, `selfrestart.policy` before stopping anything,
   `selfupdate.start` for a self-update, `backup.build` / `backup.restore`
   for backups.
5. **Speak to everyone through `Broadcast`.** `announce()` for unattended
   messages, `tell()` for the report on something a person started. Your
   own channel-posting method is for your own channel only.
6. **Render last, and only render** — markup, size limit, embed shape.
7. **Pass the guards**, and extend them to name your file: a guard that
   only looks at `discord_bot.py` says nothing about a third connection.

## The guards

The rule is machine-checked. All four live in `scripts/` and exit non-zero
on failure.

**`test_no_hardcoded_replies.py`** — a connection carries no sentences of
its own. It walks the AST of `discord_bot.py` looking for literal strings
of twelve characters or more containing a space, by three routes: a plain
`return "…"`, a ternary or boolean chain inside a return, and
`lines.append("…")` where a reply is assembled piece by piece. The third
route alone was hiding twenty-one English sentences *after* the check
first reported none. It also asserts the file actually reads the shared
translations (at least 40 `self.t(` calls; there are 82 distinct keys
today), that the translator is resolved per call, that the markup
conversion stays in the connection, and that every key asked for exists in
`en.json` — a typo'd key renders as the key itself, which is worse than
the hardcoded sentence it replaced.

Worth knowing about its reach: it sees those three routes and no others. A
literal wrapped in a call (`return self._clip("…")`), built by
concatenation, or placed in a list literal is outside them. If you are
adding a connection, do not read a passing run as proof that nothing was
missed.

**`test_notify_parity.py`** — every channel announces the same thing in
your language. It renders all six text-composing channels side by side (67
checks) and asserts each one carries the shared title in German, follows a
switch back to English, names the host the container is on, distinguishes
success from failure, and — for Matrix — says the same thing in its plain
text as in its HTML. It also greps each channel file for the old English
sentences, so none can quietly come back. Two real bugs were found by
rendering all six together rather than by reading the diff.

**`test_discord_borrows_exist.py`** — Discord borrows no behaviour from
the Telegram bot. It scans the AST for `bot.NAME` and
`self.telegram.NAME` in every shape: a call, a bare reference handed to a
thread, and a lookup by string via `getattr`/`hasattr`. The expected list
is now empty. The single remaining thread is one attribute *write* —
`bot.t`, when `/lang` switches the language — which the test names
explicitly so it cannot quietly grow company. It also self-tests its own
scanner against a probe containing all three shapes, so it cannot go blind
while reporting "nothing borrowed", and it pins the three formerly broken
commands to their core calls.

**`test_changelog_parity.py`** — both front ends say the same thing about
the changelog. It exercises all three cases of `report()`, checks that the
two renderings differ only in the bold marker (strip the asterisks and
they are identical), and asserts both front ends actually route through
`changelog.report` and `changelog.render_body` rather than deciding for
themselves.
