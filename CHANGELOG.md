# Changelog

All notable changes to Docksentry (formerly Docker Telegram Updater) are documented here.

## [2.18.0-beta.23] - 2026-08-27

One regression from beta.21, reported by @famewolf in #2.

### Fixed
- **A compose update no longer runs out of time because we gave it more grace.** beta.21 passed `--timeout <DOCKER_STOP_TIMEOUT>` to the compose recreate so a slow container would stop on its own terms instead of being SIGKILLed after Compose's 10s default. The grace was right; the wait around it stayed a flat 120 seconds, so the budget left for pull, create and start fell from 110s to 60s — and a stack whose container has to rejoin a VPN network namespace ran out of it, reporting `timed out after 120 seconds`. The wait now contains the grace it grants (`stop_grace + 120`), the same shape the standalone path has always used. 2.17.1 is unaffected — it never passed `--timeout` at all.

## [2.18.0-beta.22] - 2026-08-26

One regression from beta.20, found while mapping the dispatcher.

### Fixed
- **`/restart` restarts Docksentry again.** The lifecycle branch matches `/restart` without requiring a space after it, so once bare `/stop` and `/restart` were routed there to answer with a usage line instead of silence (beta.20), it swallowed the bare form as well — and the branch that restarts Docksentry itself sat below it, unreachable. `/restart` answered "usage: /restart <name>" and restarted nothing. It is tested for first now, on an exact match, so `/restart <name>` and `/restartx` go exactly where they went before. 2.17.1 is unaffected; only beta.20 and beta.21 carry this.

## [2.18.0-beta.21] - 2026-08-26

Two follow-ups from @NotRetarded's #62.

### Fixed
- **An update tells you when a container had to be force-killed.** When we recreate a container we stop the old one; if the app ignores SIGTERM, Docker SIGKILLs it after the grace and it exits 137. `docker stop` reports success either way, so the result used to read a bare "✅ updated" and you'd only learn of the hard-kill from an external monitor. The recreate now checks the old container's exit code and adds "⚠️ old container force-killed (ignored SIGTERM)" when it happened.

### Changed
- **A Compose recreate gets the same stop grace as a `docker run` one.** A standalone recreate gave the old container ~60s to stop (`DOCKER_STOP_TIMEOUT`); the compose recreate passed no `--timeout`, so Compose fell back to its own 10s default — making a 137 far likelier on compose containers for no reason other than the shorter default. It passes the configured timeout now.

## [2.18.0-beta.20] - 2026-08-25

The multi-host pass, tested end to end on a real five-host bed — local Docker, local Podman, a box behind a TCP socket-proxy, a box over SSH, and one host left deliberately dead. Every host-touching command was walked through by hand and checked on the far side. Three bugs turned up on the way and are fixed here.

### Changed
- **Reads reach every host, writes stay home, `@host` decides.** A flag with no container name lists what is set on *every* managed host, not just the local one. `/cleanup` and `/checkimages` take a `@host` like the commands beside them — the box that fills up is rarely the local one. Writes still default to the local machine; `@host` narrows and `@all` widens, one rule everywhere.
- **All sixteen languages are fully translated.** Fourteen of them were still half English; they are not any more.
- **`/checkimages` tells you when it could not look.** Behind a socket-proxy that does not expose `system df`, it used to answer "nothing to reclaim" — about a host it never actually measured. It reports "could not be checked" now, and names the missing permission (`SYSTEM=1`).

### Fixed
- **An update button kept the whole fleet's containers after one tap.** A host-scoped `/check @srv30` showed srv30's buttons, but tapping one rebuilt the keyboard from the global pending list — so every other host's containers reappeared as live buttons, and tapping one recreated the wrong host's container. "Update all" went global too. The scope, and the per-notification snapshot token, survive the tap now.
- **A silent container read as an unreachable host.** When several containers stop at once the alerts collapse into one digest with a log file; a container that had logged nothing printed "host unreachable, or the container is gone" in it — an alarming claim about a host that was fine. It says "no log output" now, and keeps the unreachable wording for a fetch that actually failed.
- **`/status` died on a dead host, and made the rest wait.** One unreachable box threw the whole overview, and where it did answer it spent 30 seconds doing it. A dead host is reported now, in its place, after 10 seconds, and the reachable hosts answer regardless.
- **Bare `/stop` and `/restart` went silent.** With no container name they matched nothing and said nothing; they answer with the usage line now, the way `/logs` and `/audit` already did.
- **Help text showed a literal `\n`.** Three `/help` details carried the two characters instead of the line break they were meant to be.

## [2.18.0-beta.19] - 2026-08-24

### Changed
- **`/stop` asks first, in Telegram too.** Discord already did. A container that comes back up is a decision you can take back; a stopped one stays stopped until somebody notices. Both chats ask the same question now, and the refusals run *before* it — being asked "are you sure about `gluetun`?", pressing yes and only then being told it is stop-protected is a worse answer than being told straight away. That holds for patterns too: `/stop *` says up front which ones it will skip and asks about the rest. `/start` and `/restart` do not ask.

- **`/stop web*` works in Discord.** Globs only ever worked in Telegram because the matching lived inside that file. Stop, start and restart are one implementation now, shared by both chats and the Web UI — same three refusals, same wording, same two CLI calls.

- **`/cleanup` reaches every host from Discord.** It cleaned the local machine only, on the reasoning that cleanup is a write and writes stay local. Sound reasoning, wrong conclusion: the box that fills up is rarely the local one (#2, @famewolf). Telegram walked them all; both do now, and one unreachable host reports instead of stopping the rest.

- **`/cooldown` with no container lists what is set**, in both chats. Telegram could; Discord required a container and a number.

- **`/checkimages` says it is an estimate.** It reports an upper bound and names what the grace period is holding back, rather than promising a figure `/cleanup` then does not deliver. Sizes are formatted by us in both chats now — Docker writes `8.534MB`, which reads as 8534 MB wherever a dot is the thousands separator.

### Fixed
- **The Web UI's Start / Stop / Restart buttons did nothing.** They had stopped working when the lifecycle code moved into the shared core: the Web UI still called a method that no longer existed, the error was swallowed, and the page reloaded as if it had worked. Fixed, and on the way it gained the two guards it never had — it only ever checked "would this stop Docksentry itself?", so its Stop button could take down a container both chats refuse to touch, which for most people is the VPN carrying their remote access.

- **A recreate could lose its log driver (#2, @famewolf).** A container on `json-file` with a `max-size` option, on a host whose daemon default is `journald`, got the option without the driver — and journald refuses `max-size`, so the recreate failed outright. Log options now always carry their driver.

- **`/note web something` confirmed with an empty note.** It saved correctly and reported "📝 Note on `web`: —", in both chats, in every language.

- **`/updateall @nas` updated every host.** The host was never parsed. On a multi-host setup "just the NAS please" meant everything, everywhere, without a confirmation. It filters by host now, and a container name gets a usage line instead of a silent full run.

- **`/note`, `/trustrunning`, `/askmajor` and `/setlink` were permanently local in Discord.** All four accepted a host internally; none of them offered the option, so it was always empty.

- **`/cleanup` said ✅ twice.** Both chats and the Web UI checked the result for an English word the message stopped containing a while ago, so the check never fired and a second checkmark got stuck in front. The most common outcome of the most-run maintenance command, in every language. Four more places put their own icon in front of that message, and their labels were hardcoded English in an otherwise translated report — five new keys.

- **A crash-restart loop printed its stack trace five times.** That is what a restart loop writes: the same error on every attempt. It is folded to one copy with a count now, so the lines that differ are the ones you see. Crash messages no longer show a bare `health=` for containers that have no health probe.

- **A stop refused during an update** said two different sentences depending on which chat you had open.

- **Telegram's stop confirmation now expires** after fifteen minutes, like Discord's, and only the person who asked can press it.

## [2.18.0-beta.18] - 2026-08-22

### Changed
- **Every channel announces the same thing, in your language.** ntfy, e-mail, Gotify, Matrix, Apprise and the Discord webhook each wrote their own English for the two structured notifications — "updates available" and the result of one update — in six slightly different wordings. An instance set to German announced updates in German on Telegram and in English everywhere else. They all read the same translated sentences now; each channel still decides its own bullet character, embed shape and whether it sends HTML, which is presentation and stays where it belongs.

### Fixed
- **Four of six channels left out which host a container is on.** `plex` on the NAS and `plex` at home are different containers, and which one had an update depended on which channel you happened to read. The host tag is part of the shared line now, so every channel carries it.
- **Matrix said one thing in its plain text and another in its HTML** — the intro line was in one and not the other. Both carry it now.

## [2.18.0-beta.17] - 2026-08-22

### Changed
- **Every line Docksentry says now comes from one place, in your language, whichever chat you asked in.** The Discord bot carried seventy-two of its own hardcoded English sentences while Telegram read the shared translations — so an instance set to German answered German in Telegram and English in Discord to the same question. All of them read the same wording now, and the thirty-three sentences Discord had that Telegram never said went into the translations in all sixteen languages, where any connection can use them. Each chat still chooses its own markup and length limit; that part is genuinely its own.

  The same sweep found English left in the machinery behind the chats: the status header's uptime label, Docker's own "Total reclaimed space" passed straight through from `image prune`, the maintenance-window skip, the major-update prompt, the auto-cleanup line, the self-update failures and the restart-policy refusals. Fifteen more shared keys. A check now scans every module that sends text and fails on any sentence written into the code.

- **`/changelog` says the same thing in both chats (#63, @NotRetarded).** He put them side by side and they disagreed: Telegram showed what your current version brought and adapted GitHub's markdown, Discord printed headings raw and cut mid-entry without saying so. The decision — which of the three things to say — is made once now, and each chat lays it out.

- **The health-check line wears a rescue helmet instead of a stethoscope**, which washed out on both clients' backgrounds. It only appears when a container is actually unhealthy, so the alarm colour fits.

### Fixed
- **A self-update answered in both chats at once.** Pulling the shared machinery apart routed every one of its reports through the all-channel seam, so one `/selfupdate` reported fourteen times, everywhere. A reply goes back to whoever asked now; the events — an update was found, restarting — still reach every channel, which is what they are for.

## [2.18.0-beta.16] - 2026-08-21

### Fixed
- **Discord's `/selfupdate` works again.** It called a method that never existed (`check_selfupdate`), so the command just errored out — the same kind of latent break as `/changelog` had. It triggers the real self-update now, in the background so the "started" reply goes out right away. Found during the tidy-up below, and a test now checks that every method the Discord side borrows from the Telegram side actually exists.

### Changed
- **Container state, stats and disk facts moved into a neutral `container_info` module.** They had lived on the Telegram bot, and the Discord bot reached across into that instance to borrow all three; both call the shared module as equals now, so the two front ends stop depending on each other. What `/status` shows is unchanged. (Continuing the code tidy-up that started with the changelog module.)

## [2.18.0-beta.15] - 2026-08-21

### Fixed
- **Discord's `/changelog` works again.** It joined a list of `(version, date, body)` tuples as though they were strings and raised a TypeError, so the command just errored out. Found while moving the changelog code into a shared module (below); the Discord side renders the entries properly now.

### Changed
- **Changelog reading and version comparison moved into a neutral `changelog.py`.** It had lived in the Telegram bot, and the Discord bot reached across into that instance to borrow it — but neither is Telegram's: it fetches a file and compares versions. Both front ends call the shared module as equals now, so neither depends on the other. What `/changelog` shows on Telegram is unchanged.

## [2.18.0-beta.14] - 2026-08-20

### Changed
- **The `/status` header icon is a 🔎 now (#63, @NotRetarded).** The stethoscope washed out on Discord's lighter ephemeral background — and, as he spotted, Discord even renders it mirrored. He screenshotted the candidates against the real background and narrowed it to a few; the magnifier won: it reads clearly, and it says "look at this container" rather than "emergency", which a rescue helmet did. The stethoscope stays on the "Health check said:" line, where it belongs and only shows when something is actually unhealthy — so the header no longer doubles it either.

### Removed
- **The temporary `/iconcheck` command is gone** — it existed only to pick the icon above.

## [2.18.0-beta.13] - 2026-08-20

### Fixed
- **`/changelog` compares versions correctly on a beta (#63).** Run on `2.18.0-beta.12` it reported "206 new versions", v2.17.0 — an older release — among them. The heading pattern matched only `## [x.y.z]`, so every `-beta.N` entry was invisible; and parsing the running version as three integers threw on "0-beta", fell back to (0,0,0), and made all 206 historical stable entries look newer. Both are replaced by one comparable key that understands prereleases: a final release ranks above its own betas, and both above the previous version.

## [2.18.0-beta.12] - 2026-08-20

### Added
- **A temporary `/iconcheck` command (#63, @NotRetarded).** The `/status` header icon washes out on Discord's lighter ephemeral background, and it can only be judged in a real ephemeral reply. This dumps every candidate icon, each labelled by a letter, for him to screenshot and pick one. It will be removed once an icon is chosen.

## [2.18.0-beta.11] - 2026-08-20

### Fixed
- **With debug on, `/check` no longer floods the chat with the registry trace (#63).** The debug build sent `check_all`'s entire HTTP trace to Telegram in code blocks — on every check, for every container. On an instance with debug left on and 21 containers, a routine `/check` came back as pages of code blocks. The trace is only ever useful for diagnosing *why a specific container's check failed*, so it now fires only when a check actually failed — a local-only image with no registry is a clean skip, not a failure, so an ordinary check is silent. The full trace is still in the console (`docker logs`) and the Web UI Logs page either way, so nothing is lost.

## [2.18.0-beta.10] - 2026-08-20

### Fixed
- **The Status page shows every container on a phone again (#63, @NotRetarded).** In the table layout, the mobile card list had been placed *inside* the `.table-scroll` wrapper — the very element CSS sets to `display:none` below 700px to hide the wide table. So on a phone the cards inherited that and vanished: the header still counted "16 containers running", but the list beneath it was empty and there was no table either. Measured with a headless browser at 390px — the card list computed to `display:grid` yet had zero height, because its parent was hidden. The scroll wrapper now closes right after the table, so the cards are a sibling of it rather than a child, and the phone view fills in. Desktop is unchanged. A test holds the wrapper to closing before the card list opens, so it cannot nest again unnoticed.

## [2.18.0-beta.9] - 2026-08-20

### Fixed
- **A host going down is one message now, not one per container (#63, @famewolf).** He rebooted a monitored host (planned, from Proxmox) and ran `systemctl restart docker` on another; each time the monitoring host sent one 💥 crash alert — with a full log dump — for *every* container that stopped. A dozen messages for one shutdown, which he called, fairly, unsustainable. Deaths in a single monitor tick are coalesced: up to three stay individual, full-detail alerts (two unrelated crashes in a minute are two incidents), but above that it is a host going away, not that many incidents — so **one** digest (`host X appears to have gone down — N of M stopped`) plus **one file** carrying every container's log tail. The logs go in the file because a dozen inline dumps *is* the flood being removed, and they are captured at detection, since a host that stays down can no longer be asked (the same reason the writable layer is measured before a recreate). A single container crashing still alerts on its own with its logs inline — the fix must never hide a real problem. The wording is honest about scale: only a majority of the host going at once reads as "the host went down"; a smaller cluster reads as "N stopped at once". The event log keeps every death individually — only the notification collapses. Reproduced end to end against a throwaway remote host before shipping. Behind `MONITOR_MASS_STOP` (default on).

### Changed
- **`/logs` and `/audit` reach every host now (#2, @famewolf).** Completing the command pass he asked for: both resolved the container on the *local* daemon only, so a container on a remote host came back "not found" — while their Discord twins were host-aware all along. Both take the same `@host` sweep as `/status` now, and a coverage test holds every container-touching command to it so the gap cannot reopen unnoticed.

## [2.18.0-beta.8] - 2026-08-19

### Changed
- **`/checkimages` answers for every managed host now (#2, @famewolf).** It reported the reclaimable image space on the local box only, while its sibling `/cleanup` had walked every host since beta.4 — the quiet kind of inconsistency that makes a remote problem look local. It now takes the same host walk: one line per host, tagged with its name, and one unreachable host is reported without stopping the rest. Discord's `/checkimages` already did this; this brings Telegram level with it. Prompted by his ask to *"take a pass across all the commands and ensure they act on the appropriate host"* — the audit found this was the one remaining command that did not.

- **The `/status` detail reads less cramped, and two icons carry better (#63, @NotRetarded).** The plug on the ports line was nearly invisible on Discord's dark theme, so ports now use 🌐; the live-cost line moved from 📈 to 📊; and the header carries 🩺. The lower half also had its paired lines (CPU next to net/disk, ports next to volumes) sitting flush against the airy header and image blocks above — every fact now gets its own blank line, so the whole detail breathes at one rhythm rather than clumping toward the bottom.

## [2.18.0-beta.7] - 2026-08-19

### Fixed
- **A restore no longer wipes the hosts a backup never knew about (#2).** `restore()` replaced every list and dictionary wholesale, so importing a bundle taken from a single-host install — or from before a second host existed — into a multi-host instance silently erased every `dock8520/` pin, note, group and update window in it. Nothing said so; the toast reported success.

  The rule now: **a bundle replaces state only for the hosts it speaks for, and state for hosts it never saw is kept.** New bundles record the hosts they were taken from; older ones are inferred from their keys, and the inference errs toward keeping — being stale is recoverable, being wiped is not. What survived is counted and named in the result (`kept 4 current entries for dock8520 (not covered by this bundle)`), because a restore that quietly decides what lives is only one step better than one that quietly wipes.

  The mirror case is reported too: a bundle carrying entries for a host this instance does not manage restores them anyway — they take effect if the host is later added — with a warning saying which host and why nothing appears yet.

## [2.18.0-beta.6] - 2026-08-19

### Added
- **Disk pressure is handled where it happens, container by container (#2, @famewolf).** His words after the routing fix: *"It should never get to 'no space left on device' if docksentry is doing its job. It needs to do cleanup on a container by container basis as it updates them."* Two halves, because the two kinds of host allow different honesty. **Reactive, any host:** an update that fails on ENOSPC gets that host's image cleanup immediately, mid-batch — `🧹 dock8520: emergency cleanup after ENOSPC — freed …` — and the batch carries on. It is the only disk signal a remote host gives us at all: free space is a filesystem question, and the Docker API does not answer it. **Proactive, local only:** between containers the local disk is checked against `DISK_WARN_PERCENT`, and past it a cleanup runs before the next update — behind the existing `DISK_WARN_AUTO_CLEANUP` opt-in, at most once per batch (a prune walks every image; pruning after every container would spend longer pruning than updating). The usual grace-hours filter applies throughout, so an image pulled seconds ago for the next entry is never eligible.

- **A big writable layer is named before the update destroys it.** A recreate throws the container's writable layer away by design — below half a gigabyte that is caches and temp files, above it an application has been storing data in a place the next update deletes. Said twice now: in the `/status` detail (`✍ +9.8GB layer ⚠ lost on next update`, before the fact) and in the update result itself (*"⚠ 9.8 GB had been written inside the old container (not in a volume) — discarded with it. If that was data, it belongs in a volume."*). Measured **before** the stop, because afterwards the evidence is gone with the layer; one measurement shared by the standalone and compose paths.

### Changed
- **The Discord bot's silences explain themselves now (#63, @NotRetarded).** His bot took seven minutes from container start to first answer, with a log that explained none of them — and a silence the log cannot explain is a defect of the log. Every step now says what it cost: gateway connects are timestamped and say whether they resume or identify fresh, READY reports its seconds since connect start, and a successful RESUME — which never gets a READY and used to be **completely silent** — logs too. On the REST side, authentication and the bulk slash-command registration (the first rate-limit candidate on frequent restarts) are timed, and an interaction acknowledgement that takes longer than two of Discord's three seconds logs that the user may have seen "did not respond". I can't reproduce Discord's side live; what I can do is make the next silence name its slow step itself.

## [2.18.0-beta.5] - 2026-08-19

### Changed
- **`/check` answers per host, as each one finishes (#2, @famewolf).** His observation after the first real multi-host run: *"You currently wait until it's checked all hosts before responding."* Half true, measured: a host **with** updates already answered the moment it finished — but a host that was up to date said nothing until every host was done, so the first feedback sat on the slowest machine's SSH round-trip. Every host now produces exactly one thing when it completes: its updates, its `✅ everything up to date`, or its failure. No aggregate line repeats what each host already said. A single-host install keeps its original messages untouched, byte for byte.

  Discord's `/check` keeps its single collected reply — an interaction is one editable answer, and that is a property of the transport, not a drift.

## [2.18.0-beta.4] - 2026-08-19

### Fixed
- **A manual multi-host update batch now runs every container on the host it belongs to (#2, @famewolf).** His first real remote update ended with dockmox's disk full. The batch takes one checker for the whole list; the scheduled path passes the right one per host, but the manual paths — "Update all", `/updates` — handed over the *local* checker with a mixed list. For an `@dock8520` entry that meant: the remote-compose guard read `backend.name`, saw "local", stood down — it guards the right thing, but it was never asked — and `docker compose` ran dockmox's copy of the compose file against the *local* daemon. dock8520's 2.4 GB CUDA images were pulled onto dockmox until the disk was full; his local `/cleanup` afterwards reclaimed 14 GB of images that were never meant to be there.

  Reproduced end to end against a dind "remote host" before the fix — producing his error byte for byte — and again after: the container is recreated on the remote host, nothing appears locally. Every batch entry now resolves its own host's checker; an entry whose host the registry no longer knows is refused with its name, never run on the wrong machine.

- **"✅ Image pulled. Container inspect failed." is a ❌ now.** The image is on disk but the container was never touched — it still runs the old version, and a success mark there is how "pulled" gets read as "updated". It reports as a failure, saying plainly that the container was NOT updated, with the actual error attached.

- **`/cleanup` walks every managed host (#2, @famewolf).** He ran it while dockmox was drowning and it answered for one machine out of three — "the cleanup is only running locally?" Same host walk as `/check` now, each answer tagged with its host, one unreachable box reported without stopping the rest.

## [2.18.0-beta.3] - 2026-08-18

### Added
- **Disk facts in the `/status` detail — the two that cost milliseconds.** The image line now states its size and age (`73.4MB · built 2026-08-01, 17d old`), and the wiring line names the container's writable layer (`✍ +2.45MB layer`) — the part that `system prune` reclaims and the one that balloons when something logs into the container filesystem. Both measured at ~13 ms together, detail view only, never the overview loop.

  **Volume sizes are deliberately absent.** The only way Docker surfaces them is `system df -v`, which walks every volume on the host — measured at 7 seconds on this machine — and a status command that stalls for seconds answers a different question than it was asked.

## [2.18.0-beta.2] - 2026-08-18

### Changed
- **The `/status` detail reads in stanzas instead of a label list.** Eleven `Label: value` lines carry no hierarchy, so nothing leads — the owner's reaction to beta.1 was "übersichtlicher?" and he was right. Now: who and how it is doing (header with state and uptime), what it runs, what it costs right now, how it is wired, what Docksentry knows about it — a blank line between each, because a gap is the cheapest heading there is. Net I/O carries direction arrows (`↓11MB ↑7.15MB` — docker reports received/sent in that order), disk says `R`/`W`, and a port mapped identically over tcp and udp shows once instead of twice.

## [2.18.0-beta.1] - 2026-08-18

First release through the beta channel — features settle on `:beta` before they move to `:latest`. See the README's "Trying new features early" section.

### Added
- **Network and disk I/O in the `/status` detail.** `docker stats` hands them over in the same call that already fetches CPU and memory, so the two extra fields cost nothing. A runtime that reports fewer fields still yields the two that matter.
- **The `beta` channel, documented.** New features land on `amayer1983/docksentry:beta` first and move to `:latest` once they have settled — `:latest` is never moved by a pre-release, so `AUTO_SELFUPDATE` only ever pulls settled versions.
## [2.17.7] - 2026-09-01

One bad warning, out within hours of 2.17.6. @famewolf saw it on all three of his hosts.

### Fixed
- **Docksentry told working installs their data directory was wrong, and the fix it offered would have destroyed it.** 2.17.6 moved the default data directory to `/docksentry` and declared a `VOLUME` there — so Docker creates an anonymous volume at that path on every install whose data sits somewhere else, which is every install that existed before. `/docksentry` was also on the list of paths that look like a misplaced data directory, so the storage check flagged our own volume and announced "your data directory is not where you think it is" to people whose data was exactly where they think it is.

  The mount it suggested was that anonymous volume's own path — the one place the data really would be thrown away on the next recreate, which is what the same message warns about two lines further up. Nobody should follow that advice; nobody has to any more.

  Two rules now: the current data directory is never a suspect, the same way the old one already wasn't. And an anonymous volume is never treated as a data directory somebody meant — Docker created it, not the user, so its path can never be the answer. The genuine case this check exists for (#2, @famewolf, a bind at `/app/data` that nothing read) is still caught, with the same message.

## [2.17.6] - 2026-08-31

The release where several things stopped being quiet about themselves.

### Added
- **The self-update sits in the icon bar now.** It was in Settings, under *Cleanup*, which is an odd place to keep the one button that updates Docksentry itself — @LeeNX said he battled to find it every time (#2). It is in the header with the same "update now" the containers have, pointed at the same self-updater, and it still asks before it fires. The old place still works; this is a second door, not a move.
- **A container being updated says so.** The yellow `update` badge kept claiming an update was merely *available* while the log already said it was running (#2, @LeeNX). It now reads `updating to 1.26` for as long as the update holds the lock, falls back to plain `updating` when the target version is not known rather than inventing one, and the row's own update button is inert while it runs.
- **A notification survives a short network outage.** @NotRetarded lost the network on two machines at once — a brief power cut, both boxes on UPS, both offline for about half a minute (#66). Discord's gateway reconnected on its own. Telegram got three tries over six seconds and then dropped the crash alert with nothing written down anywhere. Failed sends are now held and delivered when the connection comes back, carrying a `⏳ Delayed 12m` line so a late alert cannot read as a fresh one. Held at most 15 minutes and at most 20 messages, and never written to disk — an alert that outlives a restart is a lie the interface can never take back. Covers Telegram, the Discord bot, the Discord webhook and the generic webhook. **Not** ntfy, Gotify, Matrix, Apprise or SMTP: each has its own transport and none of them tells a network failure apart from a rejection yet.
- **A private self-update answers privately.** Running `/selfupdate` with ephemeral replies still announced the restart to the whole channel afterwards — publishing exactly what the private mode exists to hide (#63, @NotRetarded). The result now arrives as a direct message and the channel hears nothing. If Discord will not open that DM, the message goes to the channel rather than vanishing, and says why.

- **An unreachable Compose file now shows the volume line that is missing.** The container detail page names the path Docksentry actually opens and whether it is there — and underneath, the mount that would make it resolve. Where the files live inside another container, that line is read off the running container itself, so it names a *volume* when that is what holds them: @NotRetarded's Portainer keeps its stacks in `portainer_data`, which means "mount that directory" was never an instruction anyone could follow. It works for a stack manager I have never heard of, as long as it runs on the same machine, and it stays quiet rather than guess when several containers mount the same depth. It also refuses to suggest a mount that would land where Docksentry already keeps something: `/data` is our own state directory *and* Portainer's, so the obliging line would have read-only-mounted a stranger's volume over our own database. Marked experimental on the page — measured against 27 Compose files on one host, all 27 right, but that is one host.

### Changed
- **A fresh install keeps its data in `/docksentry`, not `/data`.** `/data` is a busy name: Portainer keeps its stacks there, and our own shipped compose file offered to mount them at `/data/compose` — straight over our own state directory, where they would have been invisible. The image no longer reserves `/data` either. **Nothing moves for an existing install**: if something is already mounted at `/data`, that is deliberate and it still wins, so upgrading changes nothing and no volume needs touching. Which directory is ours is decided by our own files being in it, not by something being mounted there — mounting a stack manager's volume at `/data` must not turn it into our database. `DATA_DIR` overrides everything, as before.

### Fixed
- **The rollback copy of a running update was reported as litter.** Mid-update both containers exist — the new one is already up, and `<name>_old` is still what a rollback would restore from — so the status banner counted it as "left behind from an interrupted update" and offered a `docker rm` for it. Following that removes the one thing a failed update could fall back to. It cleared itself when the update finished, which made it look like a glitch rather than the advice it was.
- **"updating" read as "updated" in eleven languages.** German said `aktualisiert`, which is the past tense — the badge announcing that an update is *running* looked like one that had finished. Same in Dutch, and the eight languages still carrying the English placeholder now have their own word. French, Italian, Spanish and Portuguese were already right.
- **A denied pull says which of the two things it probably is.** `pull access denied … repository does not exist` is the daemon's one answer to two unrelated situations: an image built on this machine, which has nothing to pull from at all, and a private registry that wants credentials. It is also the first thing anyone who builds Docksentry themselves sees on `/selfupdate`. Both causes are now named — guessing one of them would be a confident wrong answer.
- **The storage check accused a volume somebody else mounted.** It looks for a mount whose name suggests it was *meant* to be the data directory — the `/app/data` case from #2. Once the data directory moved off `/data`, that heuristic started firing on whatever the user had mounted there, and told somebody who had deliberately mounted Portainer's volume at `/data` to make it Docksentry's database instead. Following that buries our state inside another tool's volume. `/data` is no longer treated as a candidate; every other spelling of the mistake still is.
- **An ssh:// host pays for its connection once, not once per command.** A bare `ssh … true` to a managed host costs 355 ms, so three quarters of every `docker -H ssh://…` call was the handshake — and a page render makes several per host. The image now multiplexes: the same call goes from 475 ms to 148 ms, and the status page from 2.55 s to **1.32 s**. Configured in the image's own `ssh_config`, deliberately — your `~/.ssh` is yours.
- **The managed hosts are asked side by side, and asked once.** Every host was queried in turn, so the status page paid the sum of all of them; and each was probed with one `ps` and then listed with a second, asking the same question twice. Measured across four hosts, one of them over ssh: 3.66 s for the status page and 3.02 s for the V2 document, against **2.55 s and 1.93 s** after. A host that fails still becomes a line in the table rather than an exception, and the order on screen still follows the configuration, not whichever answered first.
- **A host that keeps failing is asked less and less often.** One minute is the right patience for a machine rebooting; it is the wrong one for an endpoint typo'd into `DOCKER_HOSTS` months ago, which then spent its full timeout on every page load a minute apart. The wait now doubles per consecutive failure up to fifteen minutes, and any success puts it straight back to one minute.
- **One unreachable host made every page load wait ten seconds.** The status page probes each managed host before listing it, and a dead endpoint spends the full timeout every single time. Measured on an install with one host down: 13.6 seconds for the status page and 13.0 for the V2 document, against 0.08 for a page that does not build that list — and reloading to see whether the host came back, which is exactly what a reader does, paid the wait again. A host that just failed is now remembered for a minute and skipped, with the reason it gave. Same install after the change: 3.7 and 3.0 seconds. Long enough that reloading is free, short enough that a host coming back is noticed within the minute.
- **One unreachable host took the whole V2 status page down.** `/api/v2/status` read every host view's `host` key, but a host that cannot be reached is deliberately recorded as `{"unreachable": …}` instead — so the endpoint raised a `KeyError` and answered nothing. The V2 page is drawn entirely from that document and polls it every 30 seconds, which means on a multi-host install with one host down — the normal case, not the exotic one — the page simply never filled in. The dead host is now listed and marked, the way the classic table has always shown it.
- **Discord's `/selfupdate` never worked.** It called `bot.check_selfupdate`, a method that does not exist — so every invocation since v2.13.0 promised "Self-update started" and then answered "Something went wrong". Nobody reported it, which is its own small lesson.
- **`/changelog` read the container's labels on the wrong machine.** The lookup ran a hardcoded `docker inspect` with no host routing: on Podman it answered nothing, and on a multi-host install it always asked the local daemon (#7). The host was being passed in and quietly dropped halfway through, so two containers with the same name on different hosts meant the local one answered — not an error, just the wrong repository linked.
- **The Compose mount example could not work.** The docs suggested `- /path/to/your/stacks:/stacks:ro`. Docksentry opens the absolute path recorded in the container's own label, so the mount has to land on that same path — anything else counts as unreachable and quietly takes the rebuild path instead. Same class of mistake as the README line that cost someone a week.

### Changed
- **The Compose fallback only speaks up when the rebuild actually lost something.** It used to fire for every Compose container whose file was out of reach, whether or not anything was worse off for it — @LeeNX asked whether healthchecks were even the point, and on one real host with 22 containers, 18 got the note while 3 were losing anything (#65). Docksentry now looks at the container in front of it and names what it is about to drop: a Compose healthcheck in exec form, long-form `tmpfs` volumes, `blkio_config`, `cgroup_parent`, `device_cgroup_rules`, `storage_opt`, `-P`. Nothing from that list set means no message. Two paths that fell into the rebuild in total silence — a remote host, and Compose labels without a file list — now say the same thing as the rest.
- **A crash alert says when it measured.** Two lines both read "at event time" and meant different moments: the top-consumer lists come from the snapshot taken as the container died, the line about the container itself from the sweep afterwards, when it was already booting again. @NotRetarded read 59% CPU there and reasonably took it for the state before the crash (#66). They are worded apart now. And the CPU line no longer disappears when nothing was busy — below the threshold it says so, because "nothing was going on" and "not measured" should not look identical.
- **A stable-window that looked like a setting is a constant.** `crashloop_stable_seconds` was read through a `getattr` against the config, appeared in no config file and no documentation, and nothing has ever set it. A knob nobody can reach is worse than a number in the open.

## [2.17.5] - 2026-08-28

One crash, and three lines of documentation that were simply wrong.

### Fixed
- **Docksentry could refuse to start after a self-update.** A stray line sent the startup banner a third time, from inside the one-shot migration that removes Docksentry's own name from the auto-update list — outside the block where that message is built. Both conditions together (own name still in the list, and a start right after a self-update) meant `UnboundLocalError` before the bot listener came up. Reproduced on the released 2.17.4 image and confirmed fixed on the same fixture. It also took less than it looked: the self-update check only asks whether the marker file *exists*, so a leftover or unreadable `deferred_check.json` was enough. Short of the crash, it sent the same banner twice to every non-Telegram channel.

### Changed
- **`TZ` is documented as `Europe/Berlin`, which is what the image actually sets.** The README said `UTC` while `docs/configuration.md` said the truth — and every schedule, quiet-hours window and update window runs on that clock.
- **The health check waits up to 600 seconds, not 30.** The documented 30 was a different setting entirely (the stability window *after* a container already looks healthy). A slow-starting container was never being given 30 seconds and failed for it.
- **The Web UI password is hashed with scrypt, not SHA-256** — and a password supplied as `WEB_PASSWORD` is not hashed at all, because an environment variable is plain text by nature. Both documents claimed SHA-256 and "never stored in plain text"; one of them was the security document.

## [2.17.4] - 2026-08-28

One message and one page of documentation, made honest.

### Fixed
- **The README said the compose label holds the host path. It does not.** It holds the path *whatever created the stack* saw — a host path when you ran `docker compose` yourself, and a container-internal one when a stack manager did. The example alongside it (`/opt/stacks:/opt/stacks`) reinforced the wrong idea, and at least one person set up their mount from it and then spent a week wondering why nothing matched (#2, @NotRetarded). The section now says which case is which, and shows the one command that answers it: read the label first, then mount so that this exact path resolves.

### Changed
- **When a compose file cannot be reached, the message says whose path it is.** `com.docker.compose.project.config_files` records the path the thing that *created* the stack saw — and that is usually another container: Portainer keeps stacks at `/data/compose/<id>/` inside itself, Dockge and Dockhand at `/app/data/stacks/`. None of those exist on the host, so "mount that directory into the Docksentry container" sent people looking for a directory that is not there. Three did in one week, each with a different manager, and each concluded their own mount was wrong. It was not. A recognised path now names the manager and the exact mount point to use; an unrecognised one keeps the general advice, because a confident wrong name is worse than no name. Only managers whose internal path actually differs from the host's are listed — Dockge mounts its stacks at the identical path, so there is nothing to map and nothing to warn about.

## [2.17.3] - 2026-08-28

One fix, from @LeeNX's report in #65.

### Fixed
- **A Compose stack whose file label is relative is no longer rebuilt as standalone.** Docker records the compose file on the container as `com.docker.compose.project.config_files`. That is usually an absolute path — but not always, and a label written as plain `compose.yml` turns up in the wild. Docksentry checked it against its own working directory, which inside a container is `/app`, so the file was never found and the update dropped silently into the standalone `docker run` recreate. That path rebuilds the container from its inspect data and can lose the healthcheck, which is the failure that was reported. A relative label is now resolved against `com.docker.compose.project.working_dir`, which Docker records absolute beside it. An absolute label, or a container with no `working_dir`, behaves exactly as before.

## [2.17.2] - 2026-08-27

One fix, reported by @famewolf in #2.

### Fixed
- **A compose update is no longer cut off after two minutes.** `docker compose up -d --force-recreate` ran under a fixed 120-second wait. That wait is there to stop a hung command from blocking the update loop forever — it was never meant to bound normal work, and at 120 seconds it was doing the second job badly: the number never scaled with anything, and a service that has to rejoin a VPN network namespace on start runs past it. The failure reads `timed out after 120 seconds`, which is Docksentry's own message rather than Docker's. The wait is 600 seconds now, the same reasoning the compose *pull* beside it has always followed at 1800.

## [2.17.1] - 2026-08-24

### Fixed
- **A recreate could lose its log driver (#2, @famewolf).** A container created with the `json-file` log driver and a `max-size` option, on a host whose daemon default is `journald`, got the option without the driver — and journald refuses `max-size`, so the recreate failed outright. `json-file` is only the factory default, not necessarily the daemon's; log options now always carry their driver.
- **A Compose stack rebuilt as standalone now says so.** Docksentry runs in a container, so a compose file living on the host is invisible unless the directory is mounted in. When it is, the update quietly switches strategy and rebuilds the container from its inspect data with `docker run` instead of `docker compose up` — a different code path with different failure modes, recorded only in a debug line nobody sees. The result now names the unreachable file and says that mounting its directory restores the Compose path.

## [2.17.0] - 2026-08-18

### Fixed
- **`/status` on Discord no longer hides Docksentry itself (#2, @NotRetarded).** It listed everything except the one container answering the question. The self-filter exists for the *update* path, where it protects PID 1 (#16) — Discord's status borrowed that listing and inherited a filter that makes no sense for a read. Readers now ask for the whole truth; the update path keeps its guard.

### Added
- **One `/status` detail, assembled once, rendered per front end.** The owner's diagnosis of the drift was better than the report that prompted it: he assumed a reply was generated once and then sent per connection — which has been true for notifications since `announce()`, and was false for command replies, where each front end kept its own assembly. Two assemblies is drift by construction. The detail view is now one collector and one renderer (`status_render`), and the only thing a front end may choose is its bold marker. Verified live: both outputs are identical to the byte once markdown is stripped.

  What the detail shows now — the questions you actually have when a container misbehaves: state, health and uptime; the **exit code** when it is not running (the field #62 was diagnosed from); **live CPU and memory**; **what the health probe said** when unhealthy (the 2.15.0 lesson — the probe's words, not the container's); image, **version label** and image ID; ports, volumes, restart policy; and Docksentry's own knowledge — pinned, auto-update, protected, trust-running, ask-major, group, note, pending update.

- **The overview says the version, not just the tag (#2, @NotRetarded).** `ollama/ollama:latest` tells you nothing; `(v0.32.14)` sits right in the image label. Both front ends' overviews now lead with a health icon, name the version when the image carries one, and show uptime.

## [2.16.1] - 2026-08-18

### Fixed
- **`/audit` no longer reports what Docker does to every container.** The owner ran the new audit against a stock `ollama` and got four findings — all four of them values Docker writes into every container unasked: `CgroupnsMode "private"`, `ConsoleSize [0,0]`, and the standard `MaskedPaths`/`ReadonlyPaths` lists. Measured against a plain nginx here: the same four. An audit that flags the baseline is an audit people learn to ignore, which is precisely the defect the section was built to fix — one release earlier.

  Three different truths, now separated. `MaskedPaths` and `ReadonlyPaths` are **derived**: Docker computes them from `--privileged` and `--security-opt`, both of which the recreate carries, so the recreated container gets the same values recomputed — they were never lost, and saying they would be was wrong. `ConsoleSize [0,0]` and `CgroupnsMode "private"` are **defaults** and no longer count as findings. And an explicit `--cgroupns host` turned out to be **genuinely dropped** on recreate — it is carried now, which the audit rework surfaced by accident.

  The "please open an issue" plea appears only under genuinely unknown fields. The deliberately-skipped section is a statement of policy, not a coverage gap, and asking people to file issues about it invites reports that would be closed as intended.

## [2.16.0] - 2026-08-18

### Fixed
- **A recreated container keeps its GPU.** The owner's `ollama`, deployed from a Portainer stack with a GPU, was updated by Docksentry — and the recreate dropped `HostConfig.DeviceRequests`. The NVIDIA runtime therefore never injected `nvidia-smi` into the new container, his healthcheck probes exactly that binary, and every update failed and rolled back, forever. The rollback was the only thing keeping him off CPU inference — and the skip list's comment literally said *"may add in a future release if requested"*. His server requested.

  All four shapes `docker run --gpus` produces round-trip now: `all`, a count, a device list (as a quoted CSV field — the quotes are part of the value, not shell quoting), and extra capabilities. Two `DeviceRequests` entries have no CLI spelling, so that case emits nothing and the audit reports it instead — carrying half a GPU config silently would be this bug all over again.

  Said plainly: this was built against Docker's documented shapes, not a live NVIDIA box — this development machine has none. The owner's ollama is the live verification, with the rollback as the net.

- **`/audit` no longer keeps the skip list to itself.** Fields we deliberately do not carry (`DeviceCgroupRules`, `StorageOpt`, …) were invisible to the one command built to find recreate gaps — `DeviceRequests` sat in that silence while the ollama failed every update. A field that is *set* on the container and *knowingly skipped* now shows up in `/audit` under its own heading, on both Telegram and Discord. Unset skip-fields stay quiet; the section is for what would actually be lost.

## [2.15.0] - 2026-08-18

### Fixed
- **A failed health check now shows what the health check said.** The owner's `ollama` was rolled back with `health=unhealthy`, and underneath it ten lines of a textbook-clean startup — listening on its port, discovering GPUs, model cache hydrated. Nothing to act on, because those were the wrong lines. What failed was the **probe**, and a probe's output does not go to the container's stdout: it goes to `.State.Health.Log[].Output`, with the exit code of the command Docker ran, and we were not looking there.

  Both failure paths report it now, above the container log rather than instead of it — the two answer different questions. On the rollback path it is read *before* the rollback, because restoring the previous container under the same name would otherwise have us quoting the old container's health log as the reason the new one failed.

  It stays quiet where there is nothing to say: a container with no healthcheck, a runtime that does not report one, a probe that passed. Long output is trimmed and multi-line output flattened, so a probe printing a stack trace does not turn one message into forty lines. Podman's older spelling of the field is tried as well.

### Added
- **The Discord bot setup guide, written by @NotRetarded (#57).** He set the bot up from nothing, screenshotted every step while he was doing it, and corrected the two places that turned out to be wrong once we held them against the code. It is his walkthrough, with commentary in the indented notes, and it is better than anything written from the source would have been. `docs/discord-bot.md`, linked from the README and the notifications page, with all sixteen screenshots in the repository rather than hotlinked.

## [2.14.2] - 2026-08-18

### Fixed
- **A self-update no longer kills Docksentry halfway through shutting down (#62, @NotRetarded).** His instance updated from 2.12.3 to 2.14.0, announced it cheerfully, and died with **exit 137** — SIGKILL — with, in his words, "nothing about the exit code 137".

  The helper that performs the swap ran a bare `docker stop`. No `-t`, so Docker's own default of ten seconds applied and then it killed us. Shutting *ourselves* down is not faster than shutting anything else down: the web server, the scheduler and the Discord gateway all have to come to a halt, and the Discord one deliberately waits for a command still in flight. Every other stop in this project moved onto `DOCKER_STOP_TIMEOUT` in v2.8.3 after @famewolf hit exactly this on slow containers; this one was missed, the same way the `rename` calls were missed then and had to be fixed again in 2.8.4. It follows the setting now, floored at 30 seconds.

- **And we could not tell afterwards, which is why nothing was said.** The shutdown handler writes its exit marker *first*, deliberately, so it survives a SIGKILL — which means the marker proved a signal had arrived, not that shutting down ever finished. A killed shutdown and a clean one looked identical. It is written a second time once every service has stopped, and the difference between those two writes is the answer: a run that was killed partway through now says so, in the log as well as on your channels. Reproduced against a real container — `docker stop -t 0`, exit 137, marker `done: false`, message on the next boot.

- **`BOT_LABEL` moved out of the Telegram section (#2, @NotRetarded).** It sat between "Telegram Topic ID" and "Allowed users" on the Connections page, and seven of the nine channels use it — it prefixes every message on all of them. He uses Discord, read it as a Telegram setting and never touched it, which is entirely reasonable. Its own card now, above the per-channel ones, saying what it actually does.

- **The Discord bot card no longer advertises a command count from three months ago.** It said "/status, /update, /logs and 19 more" while there were 35. Spotted in a screenshot taken for something else; a test now holds that number against the command table, because a number written into a sentence cannot notice that the thing it counts has moved.

## [2.14.1] - 2026-08-18

### Added
- **An `ssh://` host that refuses now says why (#2, @famewolf).** He set up key-based login between his three machines, tested it, added `DOCKER_HOSTS`, and lost two days to an instance that reported three managed hosts and could reach one. The error, once it stopped being truncated, said `Permission denied (publickey)` — correct, and useless from where he was standing, because the keys *did* work.

  They did, on the host. Docksentry runs in a container, and a container has its own filesystem: `ssh-copy-id` writes to `/root/.ssh` on the machine, and the image has no `/root/.ssh` in it at all. He is the second person to hit this, which is where a message should stop leaving it to be deduced.

  A refused `ssh://` host now carries a sentence naming what is actually missing — no `.ssh` in the container, no `known_hosts` in a mounted one, or neither, in which case it points at the remote `authorized_keys` instead. Every branch is a check made at the moment of failure, not an inference from the wording of the error: a connection refused on port 22 is not blamed on keys, a `tcp://` host gets no SSH advice, and an error we cannot place gets no guess. A confident wrong hint is worse than none, because it sends somebody looking where we pointed.

## [2.14.0] - 2026-08-18

### Added
- **Five things the Web UI could do and the chat could not.** `/note`, `/trustrunning`, `/askmajor` and `/testchannel` on both Telegram and Discord — 35 commands each, still identical on both sides. All four are container state, which is what the chat is for; the Web UI is where you go when you are already at a desk.

  `/testchannel` is the one that is genuinely better here than there: it sends a test through every channel that is switched on, and you are already standing where the message has to arrive, so "did it work?" answers itself. A channel that stays quiet is a channel to look at.

- **The auto-update notice names the containers (#56, @LeeNX).** It said "⚡ Auto-updating 2 container(s)…" and nothing else. His words: *"I prefer knowing what is about to change or upgrade at a glance, so if something breaks and could be related, I have an idea of where to look."* Names now, capped at twelve with an "and N more" — past a dozen it stops being a glance, and the per-container results follow in the same conversation anyway.

- **E-mail can carry a backup.** Of the seven delivery-only channels it is the one that can hold a file, and a backup in your inbox is the copy that survives the machine it came from. Nothing can *ask* for it there — e-mail has no back channel — so the Web UI and the automatic local copy are what trigger it.

### Fixed
- **The configuration reference says, per variable, whether the Web UI can change it (#2, @NotRetarded).** He read that a setting was editable in the interface on one page and saw its default on another, and could not tell whether the two agreed. Fair: the tables never said, and a paragraph eighty lines further down described "roughly" which settings were editable. Sixty-six variables now carry a ⚙, with a legend that also says the interface wins — and that since 2.13.0 a save only writes what you actually changed, so a variable set in your compose file keeps working unless you change that same setting in the interface.

## [2.13.0] - 2026-08-18

### Fixed
- **A saved setting no longer swallows a variable you never touched (#2, @NotRetarded).** He set `BOT_LABEL=QNAP` in his compose file, rebuilt, and nothing changed — because `bot_label: ""` sat in `settings.json`, written by some unrelated save months earlier, and a saved value beats the environment. His words: *"A blank entry in settings.json should not exist so it doesn't report the way it did and the compose entry then sticks."* He is right, and this is the root of a trap that has bitten repeatedly (#53, and three times in one night for @famewolf).

  A save used to write all eighty-odd persistent keys, freezing the then-current value of every setting including the ones nobody had ever opened. It now writes two things: what actually changed since the process started, and what `settings.json` already carried and is not at its default — so a value you set on purpose stays yours, and a blank that was swept in by an unrelated save is simply dropped. Existing files shed their accumulated blanks the next time anything is saved.

- **The reason an error happened is no longer the part that gets cut off (#2, @famewolf).** His host-unreachable message stopped at 200 characters, exactly where `…dial-stdio ha` becomes `…has exited with status 255, stderr=ssh: Permission denied (publickey)`. Wrapped errors put the context first and the cause last, all the way down, so taking the first 200 characters takes the least useful 200. Long errors are now trimmed from the middle: what was attempted, then what went wrong.

- **`/status @host` says when a host is unreachable.** It answered `📊 0 Containers` — a failing `ps` was indistinguishable from a host with nothing running, and the reply named neither the problem nor the host. @famewolf asked exactly that of an instance whose SSH could not authenticate.

- **The auto-update batch notices reach every channel (#61, @NotRetarded).** He photographed Discord and Telegram side by side: Discord had the per-container results, Telegram had those *plus* "⚡ Auto-updating 2 container(s)…" and "⚡ Auto-update complete: 2 updated". Two `send_message` calls that never had a second recipient — and the third time this has happened, after the release link in #57 and the "restarted on vX" line in v2.9.0. Each was fixed where it was found, which is why there was a third. There is one seam now, and a test fails if an unattended message goes anywhere else.

### Added
- **Telegram and Discord offer the same commands.** Seven existed on Telegram only (`help`, `changelog`, `selfupdate`, `debug`, `lang`, `setlink`, `audit`) and three on Discord only (`hosts`, `updateall`, `restore`). Thirty-one each now, same names, with a test that fails if they drift again — two front ends answering different questions is a support burden nobody signed up for.

## [2.12.3] - 2026-08-17

### Fixed
- **A host that drops out of the *scheduled* check now says so (#2, @famewolf).** The manual `/check` has always reported an unreachable host. The nightly run printed it to the container log and stopped there — which is backwards, because the unattended run is the one nobody is watching. He added two hosts, the SSH out of the container could not authenticate, and his nightly check quietly covered one machine out of three with the reason sitting in a log he had no reason to open.

  Said once per outage, and again when the host comes back. A message every night about the same dead box is a message people learn to ignore, which is the same defect wearing a different coat. It goes to every channel, not just Telegram.

- **`/restore` on Discord failed with `name 'json' is not defined`.** `discord_bot.py` has no module-level `import json` — every function that needs it imports it itself, which is the style in that file — and the new one did not.

  **And the 2.12.2 diagnosis was wrong.** The original code caught this NameError in the same `try` as the download and answered "I could not read that attachment", so the real fault was invisible and I blamed Discord's CDN, in a changelog and in the issue. @NotRetarded's second screenshot settled it: once the messages told the truth, the failure moved *past* the fetch and named itself, which means the download had been working all along. The User-Agent added in 2.12.2 is still correct — every other request this bot makes sends one — but it fixed nothing.

  There is now a check across the whole app that every standard-library module a function touches is one it can actually see. A missing import is invisible until the line runs, which on an error path can be weeks.

## [2.12.2] - 2026-08-17

### Fixed
- **`/restore` on Discord could not read the file it was given (#2, @NotRetarded).** He ran it ten minutes after 2.12.0 shipped and got "I could not read that attachment", which is a message that says nothing.

  The attachment does not come from the API — it comes from Discord's CDN, which sits behind Cloudflare, and Cloudflare answers `Python-urllib/3.x` with a refusal. Every other request this bot makes already identifies itself; this one fetch did not. It does now.

  And the message is no longer a dead end. An HTTP refusal names its status code, a network failure quotes what went wrong, and a file that turns out not to be JSON says which part failed — rather than a blank sentence and a reason buried in a log the user has no reason to open. That is the failure mode this whole thread has been about.

- Confirmed along the way: Discord takes a `.json` attachment with its real filename and previews it inline. No `.txt` rename, no zip — the question 2.12.0 shipped with unanswered.

## [2.12.1] - 2026-08-17

### Fixed
- **`/restart` was declared twice on Telegram, and one of the two was silently dropped.** 2.12.0 added "restart Docksentry itself" as a second table entry under a name that already meant "restart a container". `setMyCommands` was handed 29 commands and stored 28 — Telegram deduplicates by name without complaining — so the picker kept one description for a command that no longer matched it. One entry now, covering both meanings: `/restart <name>` restarts that container, bare `/restart` restarts Docksentry. The behaviour was correct by accident of handler ordering; the declaration was not.

### Added
- **Discord caught up with Telegram.** `/restart` with no container restarts Docksentry there too, with the same refusal when the container has no restart policy to bring it back — asked of the same code rather than reimplemented, so the two cannot disagree about when stopping is safe. A Discord restore now points at it instead of merely mentioning that a restart is needed.

## [2.12.0] - 2026-08-17

### Added
- **`/backup` and `/restore` on Discord too, and `/restore` on Telegram (#2).** @famewolf, once Telegram's `/backup` worked: *"No more having to jump from webui to webui."* @NotRetarded four hours later, asking why Discord did not have it — and then for the other half, a restore from the chat. That is the case that counts: the day you need a restore is the day the Web UI is the thing you cannot reach.

  On Telegram you simply drop the backup into the chat. On Discord it is `/restore file:<attachment>`. Either way **the file arriving is not the decision**: it reports which instance the bundle came from, when it was made and what it would overwrite, and hands back a confirm button. The press restores, once. Somebody showing somebody else a backup file must not cost them their configuration.

  A bundle is not trusted just because a person picked it: settings go through the persistent-keys allow-list so nothing can inject arbitrary attributes, links go through the same validator the live write path uses, and rejects are counted rather than swallowed. The apply logic moved out of the Web UI's import endpoint into one shared function rather than being written a second time — it is the half that carries those rules, and having it twice means maintaining it once.

  Discord keeps the real `.json` name. Both of them expected it would need a `.txt` rename or a zip; the documented API has no extension whitelist for bot uploads. **That part is not verified against the live API** — no bot of my own, and somebody else's channel is not mine to test in. If Discord does refuse it, the answer is a zip, not a rename: disguising a file only moves the problem to whatever opens it later.

- **A restart you can ask for, and that refuses when it would not come back.** Restoring a backup ends with settings that only apply at boot, so the reply now offers the restart instead of describing it — the owner's point on seeing the message. There is a `/restart` command too, because a restart you can only reach by restoring something first is a restart you cannot reach.

  It checks the container's restart policy first. Without one, stopping would leave the container down and you without the bot you would have used to bring it back — so it stays up and says so. If the check itself cannot be answered, that counts as no: going down with no way back is the worse mistake. And it is our own `SIGTERM` rather than `docker restart` on ourselves, which would ask the daemon to stop the process making the request and kill the answer mid-sentence.

### Fixed
- **A restart you asked for no longer reports itself as an external stop signal.** The banner said *"Restarted after an external stop signal (SIGTERM) — e.g. a host reboot… Docksentry did not restart itself"*, which was true of the mechanism and backwards about what happened. It is our own SIGTERM. A marker written just before going down keeps the next boot honest, and is ignored if it is more than an hour old so an abandoned request cannot mask a genuine external restart later.

## [2.11.1] - 2026-08-17

### Fixed
- **A failed self-update check wedged every update on the instance (affects 2.9.0 – 2.11.0).** Found in this developer's own log while @famewolf and @NotRetarded were testing something else on #2. With `AUTO_SELFUPDATE` on, the scheduled check takes the shared update lock, and the `finally` that gives it back first reads `_swap_in_flight` to see whether the helper container is about to stop the process. That attribute was never set on any real engine — so the read raised `AttributeError` **inside the finally, before `release()`** — and the lock stayed held for the life of the process.

  Everything after that answered "an update is already running". Tapping a container in the notification queued it behind a batch that had finished forty minutes earlier, where it waited until the container restarted and the in-memory queue died with it. From the outside: the bot answers, the buttons work, and nothing ever updates.

  The cause was a line in the wrong place. v2.9.0 added the update queue by inserting three methods into the middle of `UpdateEngine.__init__`, which left the tail of the constructor — `_swap_in_flight` and `notifier` — sitting after a `return` inside the last of them. Dead code. Both are back in the constructor, and neither `finally` can raise before releasing any more: a lock release must not depend on an attribute lookup succeeding.

  No test caught it because every test builds its engine with `UpdateEngine.__new__` and sets the handful of attributes it needs by hand — a reasonable fixture, and a blind spot the size of the constructor. There is now one that builds the real thing.

## [2.11.0] - 2026-08-17

### Added
- **A second way to draw the status page, and a setting to pick it.** The old one was measurably the dense page in the product: 25 containers produced 236 forms, 380 fields and 289 buttons — about nine forms and eleven buttons per row — and 426 of the page's explanations sat in `title` tooltips, which a phone cannot show at all. Looking at a screenshot settled the rest: every row carried six identical emoji buttons at the same size and weight, so you could not tell the routine one from the one that stops your database. And in a tool whose whole job is telling you when a container needs updating, **the row never said whether it needed updating**. That lived in a counter at the top of the page.

  The new **list** layout leads with the answer: state, name, image, and then either "update available" with the button that applies it, or nothing at all. An up-to-date container is quiet. Everything else — check, pin, auto-update, restart, logs, stop — moved into a detail panel behind one control per row, with its explanations as text on the page instead of in a tooltip. Colour means something again: *Stop* is the only red thing on the screen. It also renders as two-line cards on a phone, and refreshes itself in place rather than reloading.

  Under it, the page is data instead of markup: the server sends one JSON document built from the same view the table renders from — so the two cannot disagree about what is pinned or pending — and the browser draws it. 27 kB instead of 173 kB at 25 containers. No framework and no build step; this image still has no npm anywhere near it.

  **Settings › General › Status page**, and the default is unchanged: `table`, the layout you already have. An upgrade must not rearrange a page somebody knows how to use. `STATUS_VIEW=list` seeds it for a fresh install; the setting wins after that.

  A note on what this deliberately is not. It began as a whole rebuilt interface behind a hidden switch, for testers. That was over-scoped, and the same measurements said so: Settings, at four tabs and 45 fields, was the tidiest page there was. One page had the problem, so one page got the fix.

- **The status JSON says what the caller is allowed to do.** A read-only API token now gets a read-only page: the actions it would be refused are not offered. Groundwork rather than a feature — the day roles and users arrive, the server sends a shorter list and the interface needs no change. To be clear about the boundary: hiding a button is not a permission, and every endpoint still enforces its own access.

### Fixed
- **The status page no longer changes width when you arrive on it.** It has been 1400px since #46, because the old seven-column table scrolled sideways on a wide monitor. The list layout has no such problem, and inheriting the exception made the header, the navigation and the content jump between two widths as you moved between pages. The table keeps its extra width; the list matches every other page.

## [2.10.0] - 2026-08-17

### Added
- **`/backup` in Telegram sends the backup as a file (#2, @famewolf).** *"Can we get a /backup option in telegram that sends the backup as a file VIA telegram?"* — the same bundle the Web UI exports, uploaded straight into the chat. Restoring used to mean reaching a browser on the machine you are trying to repair, which is exactly the machine you cannot reach when it matters. The chat is already the trusted channel: `CHAT_ID` plus the allow-list gate every command, and the file goes back to that same chat and nowhere else. It still carries webhook URLs and the Web UI password hash, so the command says so rather than shipping it quietly.

- **Automatic local copies in `/data/backups`, five kept.** *"I would REALLY REALLY like it if backups stored a local copy so restores are not dependent on another machine to get going again […] In every case I've lost a config having a copy in the docksentry container directory would have solved the issue."* A copy is written on startup and after anything that changes state, debounced so a burst of saves produces one file rather than ten, and the oldest are pruned. The obvious objection — a backup inside the volume it backs up protects against nothing — is answered by what actually keeps happening: a `settings.json` lost while the rest of the directory survives, or a restore needed from a browser you cannot reach. It is not a substitute for the copy you keep elsewhere and does not pretend to be.

- **And a way back from one.** When `settings.json` is missing on startup *and* this directory has held one before, the newest local copy is restored automatically and the log says which file it came from. Only the settings half: groups, pins and links live in their own files and survive independently, so pulling those from an older backup would overwrite live state to fix a problem it never had. A boot that lost settings and could not repair them writes no new copy — archiving the damage would evict the good ones.

- **"Restore from a backup" on the first page of the setup wizard.** *"Why force the user to go through the setup wizard if they plan to import a backup? It should be on the first page as an option to skip the click-through's."* Quite right — by the time you are restoring, you are usually having a bad day already. The file carries `web_setup_done`, so a successful restore ends the wizard as well as filling it in.

- **A `beta` tag for pre-releases.** *"Please consider a seperate developmental chain (beta) that is opt in for future large changes. We could have ran them in parallel with seperate containers/directories."* `beta` now moves with pre-releases the way `latest` moves with stable ones, so running one alongside is a single compose change rather than a new pin per release. `latest` is untouched by them, as before.

### Fixed
- **Backup filenames no longer fall back to a container id.** With no `BOT_LABEL`, `HOSTNAME` inside a container is normally the container id, and `docksentry-backup-9cef9348bc8f-…` is worse than no name at all — it looks like it means something. Only a hostname somebody actually chose is used. Found by looking at the first file this wrote on a real instance.

## [2.9.3] - 2026-08-17

### Fixed
- **Simple mode no longer hides settings that are switched on (#2, @famewolf and @NotRetarded).** Two people spent four hours on this. He had auto-cleanup running at 85% on three hosts and could see it on one; the other two were in simple mode, where that control lives in a block the stylesheet hides. The setting was working the whole time — it just was not there to look at. His conclusion is the right one: *"If I have things enabled but can't see them in the gui how would I know to change them?"*

  Simple mode is meant to be fewer knobs, not "your server is doing things you cannot see". Hiding an option nobody has touched is fine; hiding one that is active is not. In simple mode the Settings page now opens with a short list of exactly those — every hidden setting that is not at its default, with its value — and a button that switches to advanced. It reads the list back out of the page it just rendered rather than from a hand-kept list of "advanced" keys, so moving a block around cannot make the two disagree. An untouched install sees nothing at all.

- **An overruled environment variable can be taken back with one click.** He hit this three times in one night — `DISK_WARN_AUTO_CLEANUP`, `WEB_PASSWORD` and `BOT_LABEL` — and asked the fair question: *"I would think environment variables should have priority?"*

  The precedence stays as it is, because flipping it would silently reset everyone who set something in the environment once and later changed it in the Web UI. What was missing was a way back that is not "hand-edit settings.json inside the volume". The Settings page lists every variable a saved value is overruling, with both values side by side and a button to take the environment's. It writes the value in rather than deleting the key, which is what makes it stick — `save_persistent()` writes every persistent key, so a key merely removed comes back the next time anything is saved. Secrets are named and never shown, and the value itself never leaves the config object; an earlier draft carried it in the entries the interface iterates, and the existing env-override test caught the leak.

## [2.9.2] - 2026-08-16

### Fixed
- **Docksentry now checks whether its data directory is somewhere that survives (#2, @famewolf).** He lost his settings on a recreate, then after a `compose down`/`up`, then again — restored from backup, reconfigured three hosts, and wrote "I'm afraid to restart them". Every time, all we said was "possible data loss", which named the symptom and stopped there.

  The cause was one line of his compose file: `- /mnt/.../docksentry/config:/app/data`. We use `/data`, and nothing in this image has ever read `/app/data`, so his bind mount held nothing and the real `/data` fell to the anonymous volume that our `VOLUME ["/data"]` creates — a fresh one per container, discarded with the old one. Which is exactly why it "worked all this time" and then lost everything on every recreate: within a single container's life the settings really were there.

  All of that is visible from inside the container, in its own mounts, so it now looks on startup instead of describing the loss afterwards. A bind mount at a path we never read is named, along with the corrected line to put in your compose file. An anonymous volume at `/data` is named, because it will not survive the next recreate — including a self-update's. Sockets, timezone files and the documented `/data/compose` mount are left alone.

- **And the alert stopped crying wolf.** Measured on a fresh env-only install across three boots: `/data` ends up holding `version_state.json` and nothing else, because `save_persistent()` only ever runs from a user action. So anyone who configures Docksentry purely through environment variables has no `settings.json`, has lost nothing, and was being warned about "possible data loss" on **every single restart**. Whether settings were ever saved here is now recorded rather than guessed, with a marker written alongside every save — so a real loss is still reported, in the log as well as your notification channels, and a first boot or an env-only install gets one quiet line instead of an alarm. The marker is a plain file and never a setting: nothing about it can end up outranking an environment variable (#53).

- **`Skipped (self): Docksentry` and friends left the ordinary log (#2, @NotRetarded).** He asked what was being skipped and why, which is the tell that a line costs more attention than it is worth. Nothing was wrong: we exclude our own container from the regular update path because updating yourself through it kills PID 1 mid-swap (#16). That bookkeeping is behind `DEBUG=true` now. The failure diagnostics stay unconditional on purpose — `Stop …: effective_stop=60s, subprocess=90s` came out of a debug-OFF log and is what made #2 readable at all.

### Added
- **Backups are named after the instance they came from (#2, @famewolf).** "I backup 3 hosts to my pc currently and end up with this: […] No clue what host they are from." The file now carries your `BOT_LABEL` (or the container hostname) — `docksentry-backup-dockmox.lan-20260816-152818.json` — which matters more than tidiness, because restoring the wrong one puts another machine's groups and pins on this one. Nothing set on either: the old name.

## [2.9.1] - 2026-08-16

### Fixed
- **A group that becomes a supergroup no longer takes the bot down with it (#2, @famewolf).** His bots stopped answering after an upgrade. Clean log, listener running, 27 commands registered, nothing happening. With `DEBUG=true` the cause was in there four times over: `Bad Request: group chat was upgraded to a supergroup chat`. Telegram had changed the group's id from `-52…` to `-100…`, and that breaks both directions at once — sends are refused, and incoming messages carry the new id, so they no longer match `CHAT_ID` and are dropped as unauthorised. Which is why it looked total rather than partial.

  We had the answer the whole time and threw it away. That 400 carries `parameters.migrate_to_chat_id` — the new id, from Telegram, in the same response we were already printing. We printed the description and dropped the parameters. So the bot now follows the rename: it picks up the new id, **resends the message that was refused** instead of losing it, accepts commands from the group under its new id, and says in the log and once in the chat exactly which value to change. It is deliberately not written to `settings.json` — a saved value outranks the environment, so persisting it would just swap this problem for the one where a corrected `CHAT_ID` in your compose file is silently ignored. And a bot that only ever listens never meets that 400, so the first rejected command asks Telegram once whether the chat merely moved.

- **The two places a command can be dropped now say so.** Both were silent unless `DEBUG=true`: an incoming `chat.id` that does not match `CHAT_ID`, and a sender who is not on `TELEGRAM_ALLOWED_USERS`. That produces a very specific dead end — the bot announces itself and registers its command list, so the `/` picker even offers you the commands, and then nothing happens with no error anywhere. There is nothing to pull on unless you already suspect the setting. Each reason is now named once per boot, with the actual ids, and the allow-list one points out that a value in `settings.json` overrules your compose file. Once, not per message: the silence was there to stop drive-by messages in a shared group burying the log, and that still holds. Log only, never a reply into the chat — answering an unauthorised chat would confirm the bot is there and name the machine it watches, which is the whole point of refusing.

## [2.9.0] - 2026-08-16

### Added
- **Tapping several containers in the update notification now updates several containers.** Only the first one ran. The rest answered "an update is already running" and were then thrown away, so you had to come back after each one finished and tap the next again — and nothing told you which had never run. They queue now, with their position, and work off one after the other.

  The lock stays: two updates recreating containers at the same time is what v1.23.1 was added to prevent. It is re-taken per entry rather than held across the whole queue, so the scheduler, "update all" and a queued self-update can still get in between — holding it for five containers would lock everything else out for ten minutes.

  A failure carries on to the next container, **unless they belong to the same group**: group order exists because those containers depend on each other, and updating the next one against a head that just failed is how an app ends up talking to a database that rolled back. The skipped ones are named. And because the queue lives in memory, a pending self-update stops the drain and says which containers will not run rather than letting the restart swallow them — dropping work quietly is the bug this whole change exists to fix.

## [2.8.4] - 2026-08-16

### Fixed
- **A timed-out command is not a command that did not happen (#2, @famewolf).** He sent the log, and it turned his ten-day outage into a chain that reads end to end. The `stop` before it was given 60 seconds; the `rename` right after it had a hard-coded **10**. It hit that limit — and Docker completed the rename anyway, because our timeout stops *us* waiting, not the daemon working. His words: "the rm times out after 15 seconds but the delete actually works". The `TimeoutExpired` then escaped `recreate_dependent`, which had no `try` around that call, so the rebuild **and** the rollback were both skipped; the container existed from then on only as `<name>_old`; and every later run found no such container, fell through to `restart`, and printed the same line. That is why it was consistent rather than intermittent: after the first failure the state was permanently wrong and nothing ever looked again.

  Three repairs. The renames follow `DOCKER_STOP_TIMEOUT` like the rest of the shutdown path (2.8.3 fixed `kill` and `rm` and missed these — this is the other half of that). A rename that times out now **checks what actually happened** before deciding it failed. And a dependent left behind as `<name>_old` is put back under its own name and started on the next run, instead of being reported as missing forever. `recovery.py` has healed exactly this shape for the main update path all along, but it runs off an in-flight note that only that path writes, so the dependent recreate was never covered by it.

  Deliberately narrow: it only touches `<name>_old`, only when `<name>` itself is absent, and only for a container already known to be a group dependent. A stray `*_old` belonging to someone else is not ours to move.

## [2.8.3] - 2026-08-16

### Fixed
- **A failed dependent said which container, never why — and said it at the end of a line of good news (#2, @famewolf).** One of ten containers behind his Gluetun sidecar failed to be recreated on 6 August and every time after. His whole arr stack depends on it, so it sat broken for **ten days**, while the notification read `9 ok (nine names…), failed 1 (gluetun-nzbhydra2)`. Two of our failures in that one line. The reason was thrown away: `recreate_dependent` returns it, and the caller printed it to the container's own log and put only the name in the message — so it could tell him something was wrong and never what, and the line that would have let him act sat somewhere he had no reason to look. And a failure appended to nine successes reads as good news at a glance, which is exactly how it was missed. Failures now come first, on their own lines, each with its reason attached; the successes follow. An all-good run is unchanged.
- **`kill`, `rm -f` and `rename` timed out after a hard-coded 15 seconds.** Not enough for a container that is slow to die: he hit it repeatedly on `ollama` (a model loaded in VRAM), and on `byparr` and `metube`. They follow `DOCKER_STOP_TIMEOUT` now — `max(30, that)` — so the whole shutdown path is governed by the one setting somebody with slow containers has already raised, instead of only its first step. The default rises from 15 to 60 seconds.

## [2.8.2] - 2026-08-11

### Added
- **Documentation for the VPN-sidecar case (Gluetun and friends).** The README has advertised this as a headline feature from the start — "Gluetun before the containers sharing its network namespace", "the case that breaks naive updaters" — and `docs/` did not contain the word "gluetun" once, across all nine files. The strongest thing the tool does was the worst-documented thing in it. There is now a section under Container Groups explaining why `network_mode: container:gluetun` breaks on update (Docker stores it as the head's container *ID*, which dies with the head), what Docksentry does about it (recreates the dependents against the head's *name*, which survives), and the three things you have to get right: the head is the group's first member, the "restart dependents" tick is what switches the mechanism on, and nothing else needs configuring because the namespace check runs per update rather than being stored. A test pins each of those claims against the code so the page cannot quietly go stale.

### Fixed
- **The browser's own password box came back after an expired session (#60, @NotRetarded).** He left a tab open overnight, came back to it, and got the native login dialog before a refresh took him to the proper login page. Cause: our pages fetch in the background — the Settings page asks `/api/cron_preview` as it loads — and a `fetch()` sends `Accept: */*`, so it took the branch meant for scripts and got a 401 **with `WWW-Authenticate`**, which is exactly the header that summons that dialog. It is the thing the login page exists to replace, handed back by our own page. Browsers label those calls with `Sec-Fetch-Mode` and no command-line client sends it, so a background call from a page now gets a plain 401 with no such header, and `app.js` turns that into a trip to the login page and back. `curl -u` and every existing scraper are unchanged, and a normal navigation still redirects as before.

## [2.8.1] - 2026-08-11

### Added
- **First-run setup now makes you set a password (#60, @NotRetarded and the owner).** Two people asked for the same thing from opposite ends: @NotRetarded went looking for an initial "create a username and password" screen and there wasn't one, and the request was to build exactly that — a fresh boot with no password where the first thing you do is set one. The setup wizard has a password step now, and it is the first step: you leave it either with a password (typed twice, stored as a scrypt hash, never in the clear) or by deliberately ticking a "run without a password" box for the reverse-proxy or trusted-LAN case. Enforced on the server, not just in the browser — a missing or mismatched password sends you back to the wizard rather than into an accidentally-open dashboard, and the endpoint that used to skip the whole wizard now refuses to open a passwordless one. Not retroactive: an existing install has already been through setup and is left alone, so upgrading does not suddenly demand a password from anyone running open on purpose.

### Fixed
- **The logout button rendered as an empty box (#60, @NotRetarded).** It used the U+23FB power symbol, which a lot of system fonts do not have, so it showed as a tofu box rather than an icon. It is an inline SVG now, like the theme toggle beside it that was already one for the same reason.

## [2.8.0] - 2026-08-10

### Added
- **A login page, a username, and the password is no longer stored in plaintext (#60, @NotRetarded).** He asked for all three and checking his claims was the whole of the design work, because every one of them was true. There was no login page: it was HTTP Basic Auth, so what you saw was the browser's dialog rather than a page of this application, and a password manager had no form to fill in. There was no username either, and not in the sense of "not configurable" — the Basic Auth header was split into user and password and the user half was then never looked at again, so any name got in. And the password sat in cleartext in `settings.json` as well as in your compose file, with the file's 0600 as the whole of the protection.

  Now: a real form at `/login`, marked up so a password manager recognises it; `WEB_USERNAME`, optional and empty by default because every existing install has a password and no username and an upgrade must not lock anyone out; and scrypt from the standard library, with an existing plaintext password migrated to a hash on the first start after the upgrade. `WEB_PASSWORD` keeps working: an environment variable is plaintext by nature, so a plaintext stored value still verifies. So does `curl -u` — a browser is redirected to the form, a script still gets its 401 with `WWW-Authenticate`, because redirecting scripts to an HTML form would break every scraper silently.

  Sessions have two clocks, `WEB_SESSION_HOURS` (idle, default 8) and `WEB_SESSION_MAX_DAYS` (absolute, default 7): the first ends a session on a machine somebody walked away from, the second ends one a background tab has been keeping alive, which no idle timeout ever catches. They live in memory, so a restart signs everyone out — writing them down would put live credentials on disk, which is most of what this was getting away from. Changing the password ends every session.

  **No HTTPS**, and that is a decision rather than something pending. Anyone exposing this beyond their own network puts a reverse proxy in front of it, and Caddy or Traefik does certificates, including renewal, better than we would.
- **The Connections page lists your API tokens.** `API_TOKENS` was the one setting the interface said nothing at all about — not the values, which is right, but not even whether any existed. "Is my scraper authorised, or is it getting 401s?" could only be answered by reading the compose file and then the container's logs. The card shows each token's **name** and when it was last used, never the token itself, and it is deliberately not a form: the value lives in the environment, and a field that silently fails to save is worse than no field. Two things it will now tell you that nothing did before — that `API_TOKENS=prom` with no colon can never match anything, and that a saved empty list **overrules** a set `API_TOKENS`, which is a variable sitting in your compose file doing nothing while every request is refused. That last one was found by hitting it while testing the card.
- **The audit trail can be found by searching for "audit".** Its heading is translated in all 16 languages and not one of them contains the word — which the docs, the changelog and every issue use. It keeps the translated heading, plus a quieter `· Audit-Trail` beside it and an `#audit` anchor.

### Changed
- **`docs/configuration.md` is grouped and sorted.** It was one flat table of 81 rows in no discernible order; @NotRetarded went looking for the Discord variables at the top and found them at row 39, with the webhook ones near the beginning (#57). Now twelve sections by purpose, alphabetical within each. No description changed.

### Fixed
- **Security audit of the new login code, and the four things it turned up.** After building the login page, sessions and password hashing, the whole lot went through an adversarial review. The one that mattered: the `next=` parameter after login rejected `//host` but not a backslash (`/\host`, which the browser reads as `//host`) and not an embedded CR/LF — so it was an open redirect and, because the value went straight into a `Location:` header, an HTTP header-injection. Closed by rejecting backslashes and control characters outright, with a real behavioural test in place of the source-grep that let it through. The login page also no longer pre-fills a configured `WEB_USERNAME`, which would have handed the name to any unauthenticated visitor and made the deliberately vague "wrong username or password" pointless. Three smaller ones alongside: the session store is now locked (it is shared across request threads and its eviction path could have raced), the two session-duration settings are re-clamped when read from `settings.json` (a hand-edited `null` there would have crashed the login page), and the login page resolves the light/dark theme like every other page instead of always rendering dark.
- **The weekly report could mark itself sent without sending.** If the Telegram bot had a token but its channel was switched off, and no other channel was on, the report counted as delivered and the week was silently skipped. The channel switch is checked now, and the Telegram copy ignores quiet hours to match the other channels — the report goes at the hour you set it, whichever channel carries it.
- **A plain `tcp://` host is unencrypted, and nothing said so (#60, @NotRetarded).** He accepted leaving TLS to a reverse proxy and added that he had been thinking of encryption between multi-hosts, "but the reverse proxy solves that issue". It does not, and that was our fault rather than his: the proxy sits in front of the Web UI and protects people coming *in*, while the multi-host connection runs the other way, out to the remote daemon, and never passes through it. On a plain `tcp://` that link has no TLS and no authentication at all, so reaching the port is enough to start a container that mounts the host filesystem. Checked before writing any of it: "unencrypted" and its neighbours appeared nowhere in the README or docs, `docs/security.md` did not mention multi-host once, and the README's own example put `tcp://pve1:2375` beside `ssh://root@nas` with nothing to tell them apart. There is now a startup warning naming the host, a section in the security docs comparing the three transports, and an example that no longer teaches the wrong thing.
- **The weekly report went to channels that were switched off, and skipped most of the ones that were on (#59, @NotRetarded).** He got it twice and reported the duplicate; the more interesting half is that the "both Discord paths are on, so everything arrives twice" warning was *not* showing on his page. Same cause. The report wrote its own list of recipients and asked whether a webhook URL was *set*, not whether the channel was *on* — so a switched-off Discord webhook still received it, while the warning card, which is about the switches, correctly stayed hidden. One configuration, both symptoms. And the list had never been extended: e-mail, ntfy, Gotify, Matrix and Apprise had never received a weekly report at all, which nobody reported because a report you have never seen looks like a report that was not due. Recipients now come from the notifier facade, and each channel renders it in the shape it already used elsewhere.
- **"Guild" and "Server" were used interchangeably (#57, @NotRetarded).** Discord's interface says Server, its API says Guild, and we had the variable saying `DISCORD_GUILD_ID`, the field saying "Server ID" and the log saying "guild". The variable keeps its name — renaming it would break every existing setup for a word — but `DISCORD_SERVER_ID` is accepted as well, the log lines say "server", and the reference explains the two names once.

## [2.7.1] - 2026-08-10

### Added
- **Discord suggests host and container names while you type (#57, @NotRetarded).** Nothing in Discord's interface tells you what a free-text option will accept, so working out that the local host is called `local` was a puzzle — reasonable to solve with one host, guesswork with five. Both fields now offer the real names, and the container list follows whichever host you already picked. The information was never secret: `/hosts` has listed it all along, just not where the typing happens. Suggestions, not fixed choices — a name you type yourself still works, which matters while a container is being renamed. Set in one place for all 26 options rather than per command, so the next command to grow a `host` cannot be added without it.

### Fixed
- **The Discord bot channel dropped the release link (#57, @NotRetarded).** The webhook renders a container name as a clickable link to its release page; the bot posted the name as plain code. Not a formatting quirk — the method accepted `source_url` and used it for nothing, and the same omission cost the "Updates available" list its `Source ↗` line and its version badge. Fixed by deleting the second rendering rather than patching it: the bot channel now *inherits* the webhook's embeds and overrides nothing but the transport, so the two are the same message by construction and a change to one is a change to both. Two hand-written renderings of a single notification was one too many to keep in step, which is exactly how this drifted.
- **The weekly report never reached the bot channel** — or SMTP, ntfy, Gotify, Matrix or Apprise. It writes its own list of recipients instead of going through the notifier facade, so every channel added since has simply been absent from it. The bot channel is wired in; the rest of that list is a wider repair and is not in this release.
- **The changelog's 2.7.0 section listed `context://` and the unreachable-host reason twice**, in two different lengths, from being written in two passes.

## [2.7.0] - 2026-08-09

### Added
- **The Discord bot can speak, not only answer (#57, @NotRetarded).** Every slash-command reply is *ephemeral* — visible only on the device that sent it, and it deletes itself — which is right for an answer naming your internal services and useless for "bot started" or a crash alert. Give the bot a **channel ID** and it posts there of its own accord: permanent, visible to everyone in that channel. It is a notification channel like any other, with its own card, switch and test button, so quiet hours and the rest apply without a special case. The mechanism is not new — `create_message` has been running as the fallback for answers that overran Discord's 15-minute window; it simply was not wired to anything else.
- **Command answers can be made visible to everyone**, per his argument rather than mine: an ephemeral answer also tidies itself away, and whether you want that depends on whose channel it is. Off by default, because the default should be the one that cannot embarrass anyone. The flag is set on the *acknowledgement*, where Discord fixes visibility — setting it only on the immediate path would have left every deferred command private whatever the switch said, which is most of them.
- **A notice when both Discord paths are configured.** Webhook and bot channel both on means every notification arrives twice. He asked for a restriction forbidding both; it says so instead — somebody may want the webhook public and the bot private, and a hard block takes that away.
- **A `DOCKER_HOSTS` endpoint can name a stored connection: `nas:context://nas`.** It becomes `podman --connection nas …` / `docker --context nas …`. This exists because of what `podman --url ssh://…` does with keys, which I only found by sitting down with a real sshd and measuring it: Podman's Go ssh client ignores `~/.ssh/config` entirely — in the default `golang` mode *and* under `--ssh native` — and then borrows the identity of whichever stored connection happens to be the **default** one, whatever host that connection points at. Not matched by URL, just the default. With two connections on the same URI, the default holding an unauthorised key and a second holding the right one, `--url` failed with `ssh: unable to authenticate` while `--connection <the second>` listed containers. So on any machine with more than one connection stored there was no way to give a remote Podman host its own key. Docker is the opposite and always was — it shells out to the real `ssh`, so an `IdentityFile` in `~/.ssh/config` is picked up (measured both ways, with the block and without). An unknown name fails loudly rather than falling back to the local socket, which would report this machine's containers under a remote host's name.
- **A Podman guide, `docs/podman.md`.** What `CONTAINER_CLI=auto` actually resolves to, what socket activation buys you and what it doesn't, the SSH key business above, pods, and the `io.containers.autoupdate` label. The socket-activation part is not the usual copy of the docs: the shipped `podman.service` passes no `--time`, so the 5-second default applies and an idle machine really does run no API process — but with `MONITOR` on and an event stream held open, Docksentry itself is what keeps it alive around the clock. Measured from the Podman journal over two hours rather than assumed, and the page says both halves.
- **A new logo, and the wordmark is text.** The mark is a tile; `DOCK`/`SENTRY` beside it is real HTML, so it stays sharp at any pixel density and takes the theme colour with it. The favicon is the same tile — the old one was a shield with an eye and shared nothing with the header. `docs/images/logo.png` drops from 1.4 MB to 92 KB, and the README screenshots are reshot.

### Fixed
- **The "Docksentry started" message only ever went to Telegram.** The hard-kill note and the what's-new note beside it have always gone to every channel; this one had a single recipient, so a Discord, e-mail or ntfy user has never seen it. Found by @NotRetarded (#57) while testing the bot's own channel: the start announcement arrived and nothing else did, from which he reasonably concluded the rest of the notifications were broken too. They were not — this one message simply never had a second recipient. Measured before and after against a real webhook receiver.
- **Everything Discord is one card now.** Webhook, command bot and bot channel were three cards in a list sorted by state, which put them anywhere — "they're all over the place", as he put it. Still three separate channels with their own state lines and switches; the card is only the grouping.
- **"nas: unreachable" was the only thing the Status page ever said, whatever had gone wrong.** The CLI's own words were caught and thrown away — Telegram and Discord have quoted them all along. Four quite different failures came out as the same word, and I measured what each one looks like before deciding it mattered: a refused SSH key, a closed port, a DNS miss and a wrong socket path in the URL. Only one of those is "your key is wrong", and it is the one nobody guesses — you go and check cables and firewalls instead. The row now carries the CLI's last line. Deliberately the *last* one and no attempt to classify it: Podman writes a two-line block whose first line sends you to `podman machine init`, which has nothing to do with a remote host over ssh, and a pattern I guessed at today is a pattern a future Podman wording falls through.
- **The unreachable-host hint never worked on Podman, and would have printed an error as an endpoint on Docker.** It read the CLI's context list with Docker's field name. Podman's is `.URI`, and it rejects `.DockerEndpoint` with exit 125 — so the hint silently produced nothing there. Worse the other way: Docker rejects `.URI` with exit **0** and the error only in its output, so a version that went by exit code alone would have shown `template parsing error: …` where an endpoint belonged. Both field names are tried now, and a template the CLI could not execute is recognised by what it said rather than by whether it bothered to set an exit code. Found within hours of shipping it, from a Podman remote-management tutorial that mentioned `podman system connection` — which `context ls` is an alias for.

## [2.6.0] - 2026-08-09

### Added
- **Telegram can be switched off too, and every card says what its channel can actually do.** The seven notifier channels got a switch in 2.5.0 and the one most people use did not, which read as a bug and was one. `CHANNEL_TELEGRAM_ENABLED` is a saved setting with a switch on its card. Off means off — no notifications **and** no answers to commands, because a channel that is off and still replies to `/status` is the confusing state, not the useful one. Distinct from `TELEGRAM_POLLING`, which keeps notifications on and only stops command polling so another app can share the token.
- **A short note on every card saying what that way can do** — *sends only*, *sends and takes commands*, *takes commands*. "Discord" appears on two cards, the webhook and the bot, and nothing on the page previously said they were different things.
- **Turning off the last channel now says so first.** Not a block — it is your machine — but Docksentry would keep checking and updating with no way to tell you about it, and you would find that out by opening this page. The startup check knows about the switch as well: `CHANNEL_TELEGRAM_ENABLED=false` alongside `WEB_UI=false` used to boot an instance with no way to report anything and no interface to turn the switch back on. It refuses now.

- **The advisory covers two-component tags now, and stays inside the major you pinned.** `postgres:16.3` used to get nothing at all — SemVer wants three numbers — and that gap swallowed exactly the images people pin most: `postgres` publishes 32 two-component tags on Docker Hub and not one three-component tag (measured). It is compared against tags of the same shape, because somebody on `7.2` means that line and the equivalent step is `7.4`, not `7.4.1` — `redis` carries 9 of one and 53 of the other, so mixing them would be the ordinary case. And the advisory now stops at the major it was pinned to: `16.3` hears about `16.4`, never `17.0`. A Postgres major is not a tag change, it is `pg_upgrade`, and a container that swapped the tag alone would not open its old data directory. **This changes what three-component tags advise too** — at the top of your own line you now see nothing, which is the honest answer. Major-version *confirmation* is untouched and still looks across majors; capping it there would have quietly disabled it for everyone.
- **A container claimed by `podman auto-update` says so.** The `io.containers.autoupdate` label hands it to Podman's own updater on a systemd timer, so both it and Docksentry have an opinion about that container and whichever fires first wins. Badged on the Status row, same as a quadlet (#55) — reported, never acted on.
- **The auto-update report leads with the outcome.** `⚡ Auto-update complete: 3 updated · 1 failed`. Every container is named on its own line below as before, but a long report is split across several Telegram messages and the answer was spread over all of them.
- **An unreachable host now lists the endpoints Docker itself knows about.** `DOCKER_HOSTS` is typed by hand and a typo looks exactly like a machine that is down, so at the one moment somebody is staring at "cannot reach nas", `docker context ls` may answer it outright. Only on failure, and never as a second source of hosts.

### Fixed
- **`<name>_old` containers piled up forever, and nothing ever removed them.** Every update renames the running container to `<name>_old` before creating its replacement and drops that backup once the new one is healthy — but a run whose process died in between left one behind for good. `cleanup_images` prunes images; `_prune_old_backups`, despite the name, deletes backup *directories* on disk; and the recovery path walked past them on the stated grounds that the backup "belongs to the cleanup grace period", which is not true of anything. @LeeNX found three and reasonably concluded his containers were not updating (#56). They were updating fine — he was looking at the debris. Recovery now removes the backup once the live container proves the swap finished, and only the name from its own journal: a `*_old` container somebody else named that way is theirs.
- **The successful-update path dropped its backup without `force`.** Every other call site forces; this one did not, and its exit code is not read. Measured: `docker rm` on a running container exits 1 with "container is running: stop the container before removing or force remove". In the ordinary case the backup is stopped and it worked — but a silent `rm` whose failure nobody notices is exactly how debris accumulates, and `_rollback_to_old` had the identical bug and was fixed long ago.
- **The leftovers already lying around are named on the Status page**, with the command to remove them. They were visible in the container table with nothing saying what they were. Not a button: removing a container this process did not create is the operator's call. It costs one `ps -a` per host per render — 42 ms measured — because a leftover backup is stopped and the table lists only what is running, which the first version of this check overlooked and could therefore never have found one.
- **The boot line said `Telegram: ON` for a Telegram that was switched off.** It meant "BOT_TOKEN and CHAT_ID are set", which stopped being the same thing.

## [2.5.1] - 2026-08-08

### Fixed
- **The channel summary said "0 active" on an instance whose Telegram notifications were working.** It counted the seven notifier plugins and not Telegram, which sits on the same page and is a notification channel to anyone reading it — it is just the bot, configured by `BOT_TOKEN` and `CHAT_ID` rather than by a plugin. Found immediately after releasing 2.5.0, on a real instance: `0 aktiv · 0 aus · 7 nicht eingerichtet`, with Telegram delivering perfectly well. A confidently wrong answer is exactly what the state lines were added to stop, so it counts now, and its card carries a state line of its own so the count and the cards agree.

## [2.5.0] - 2026-08-08

### Added
- **Every channel says whether it is actually going to send anything.** Each card on the Connections page now carries one of three states, because they need three different things done about them: *Active*, *Switched off*, or *Not active — missing: recipients*. Until now the only signal was silence, and "why is nothing arriving?" had no answer anywhere in the interface. What is missing comes from the channel itself, so it stays right when a channel's requirements change.
- **A switch per channel.** Turning a working channel off used to mean clearing its fields — and for the five channels whose credentials are write-only in the interface, that means fetching a token again to turn it back on. Default on, so an upgrade changes nothing. The switch only appears once a channel is complete: offering to turn on something that would then do nothing is exactly how you get "I enabled it and nothing happened". A switched-off channel is still reported as complete, just off — `active()` is "on and complete" and is kept deliberately apart from `configured()`, which is "has what it needs".
- **A test button on every channel**, not just the two webhooks. It sends through the saved settings with every other channel blanked in a copy of the config, so the answer is about the channel you pressed it on and nothing else. Quiet hours cannot swallow it, and a channel you have just switched off can still be tested — that is a question about whether it works, not whether it is on.

## [2.4.0] - 2026-08-08

### Added
- **E-mail is configured on the Connections page.** Server, port, encryption, sender, recipients, user and password, all of which were environment-only. The password is masked in the field, never rendered back into the page, kept off the loggable allow-list and redacted in the audit trail; removing it is a separate checkbox, same as the Discord bot token.
- **Certificate verification is visible now, and cannot be turned off by accident.** `SMTP_TLS_VERIFY` has existed since the unverified-context defect was fixed, but only as a variable — you had to know it was there. It is a checkbox on the E-mail card, with the reason spelled out: off, the password goes to whatever answers on that address, with any certificate at all. An unchecked box submits nothing, so "absent" normally reads as "off" — for this one flag that is not good enough, and it is only read that way when the submission carries the hidden marker proving it came from this form. A POST to the same path that simply omits it leaves verification alone.
- **ntfy, Gotify, Matrix and Apprise are on the Connections page too.** That completes it: every notification channel Docksentry has is configurable in the interface. These four read `os.environ` directly until now — deliberately, so that a whole channel stayed one file, with a note in `ntfy.py` that the reader could move "when the Web UI gains an ntfy field later". This is later. They read `config` now, which is where the variable and the saved value already meet for everything else; an unset attribute still falls back to the environment, so nothing that worked before stops working. The five credentials among them — the ntfy token and password, the Gotify and Matrix tokens, and the Apprise target URLs, which carry credentials in the URL itself — are masked, never rendered back into the page, and each has its own checkbox to remove it.
- **The cards are ordered by what they are doing, and counted at the top.** Active first, then switched off, then the ones that are not set up, with a `2 active · 1 off · 5 not set up` line above them. Eight cards is a lot to scroll, and burying the two that work under five that are not configured defeats the point of having said which is which. Not tabs, for the same reason — a tab shows one channel at a time, which is exactly the question the state lines answer without clicking seven times. The Discord webhook and the generic webhook have their own cards now, so every card is one channel and the state line no longer has to name which of two it belongs to.

### Fixed
- **Recreating a container on Podman never worked — three separate reasons, stacked.** Each one fatal on its own, so fixing one only revealed the next. `Config.StopSignal` is the *number* `15` on Podman 4.x where Docker gives `"SIGTERM"`, and that int went into the argument list, so `subprocess` raised `TypeError: expected str, bytes or os.PathLike object, not int` before the CLI was ever executed. (Corrected 2026-08-10: Podman 5.0.0 changed this to the signal name as a documented breaking change. It survives in a narrower form — the Docker-compat endpoint still returns a numeric *string* as of v6.0.2, and a client asking with an older API version still gets an int — so coercing the whole argument list to strings remains the right fix, but the sentence above only ever held on 4.x.) `HostConfig.Runtime` is `oci` — not a runtime name but Podman's generic label — and echoing it back gets `Error: default OCI runtime "oci" not found`. And a container inside a **pod** reports `NetworkMode: container:<infra-id>`, identical in shape to a Gluetun-style sidecar, so it was rebuilt with `--network container:…`, which Podman refuses: `container dependency … is part of a pod, but container is not`. The first two hit every container on Podman, not only pod members. All three measured against podman 4.9.3 and re-measured green afterwards, on a pod member and an ordinary container; the pod member is still in its pod after the recreate. Nothing was ever destroyed by this — the rollback restores the renamed original, also measured — but no update could succeed either.
- **And the tests could not have told anyone.** `test_podman_live.py` drove a real Podman and checked the plumbing — `ps`, the `--url` flag — and never once built a run command. It performs a real recreate in a real pod now. The three inspect shapes are pinned separately, without needing a runtime.
- **A manual check looked at the local machine only.** The Web UI's "Check Updates" button and Telegram's `/check --dry-run` each called `check_all()` on the single local checker, with no host loop anywhere near them — so on a multi-host install they checked the machine Docksentry runs on and silently ignored every other one. Measured on a two-host demo with four containers on moving tags, two of them remote: the button reported "Checking 2 containers for updates" and never mentioned the second host. This is the bad kind of wrong — it does not fail, it answers, and the answer is "checked". The scheduled check had always looped; the loop lived in `Scheduler._checkers` and nowhere else, so it is `hosts.host_checkers` now and all three share it. One unreachable host still costs only that host its check. Discord's `/check` was never affected — it has walked its targets from the start.
- **The env-override warning pointed at a tab that no longer exists.** After the channels moved, a set `DISCORD_WEBHOOK` overruled by a saved value still said "change it under Settings › Channels". It now says the Connections page.

## [2.3.0] - 2026-08-07

### Added
- **A Connections page, and the Discord bot is set up there rather than in compose.** @NotRetarded wrote a screenshot-by-screenshot guide for getting the bot running from environment variables (#57) and then asked whether the settings could just live in the Web UI. They can — and once they did, the Settings page's Channels tab was the longest thing on it, with SMTP, ntfy, Matrix, Apprise and Gotify still environment-only and still to come. So the channels moved to a page of their own: **Connections**, one card per channel, each readable on its own. It also matches how people think about it — the rest of Settings is about *when* Docksentry acts, this is about *where it talks*. Bot token, application ID, server ID and the allowed-user list live in the Discord bot card there. Saving restarts the bot and says what happened — connected, token rejected (with what Discord actually replied), server ID missing, or slash-command registration failed. That last part is the point of the exercise: `start()` always logged its reasons, which was fine while the only way to change the credentials was editing compose and recreating the container, and useless the moment you can type a token into a form and the console is somewhere else. The token is masked in the field, never rendered back into the HTML, kept off the loggable allow-list and redacted in the audit trail; removing it is a separate checkbox, because an empty password field has always meant "unchanged" here and cannot also mean "delete". These were env-only on the stated grounds that a credential has no business in settings.json — which does not survive looking at the file, since it already holds the Web password in plaintext and webhook URLs whose path *is* the credential, and is written 0600 for exactly that reason.
- **The same emptying bug on the new page, prevented rather than repeated.** `test_form_nesting.py` now checks both forms instead of only the settings one — that a page's form is empty and carries its id, that every control associates with it, and that what the handler reads is exactly what the page can send. It earned its keep immediately: it caught a field the settings handler still named after its input had moved away, and then it caught itself, because the first version of the loop found the *GET* router's `elif path == "/connections":` instead of the POST handler's and was passing vacuously.

### Fixed
- **No text field on the Settings page could be emptied.** Setting a Discord webhook and then clearing the field left the old URL in place: the page said "saved", the field came back filled, and nothing said why. `parse_qs` drops `name=` with an empty value unless asked not to, and every branch in the save handler is guarded by `if "field" in params` — so the one submission that means "clear this" looked exactly like the field had never been sent. The same for the generic webhook URL, the Telegram topic ID, both allowed-user lists, the bot label and the quiet hours. Found while adding the Discord fields, because a server ID that cannot be cleared is a bot that cannot be pointed at a different server.
- **A browser hanging up no longer looks like a crash in the log.** @NotRetarded found thirteen lines of Python internals in his container log and filed them, reasonably, as a bug (#58) — `ConnectionResetError: [Errno 104] Connection reset by peer`, with frames inside `http/server.py` and nothing anywhere saying "a client on your LAN closed a socket". The reset is ordinary and unavoidable: a browser abandons a request when you navigate away mid-load, closes a tab while a response is being written, or opens a speculative connection it then drops. There is nothing to fix about the reset; the traceback was the defect, because a log line that sends someone to open an issue about a healthy system has cost them time and told them nothing. Those now print one line, and only under `DEBUG`. Everything else escaping a request thread keeps its full traceback — that is a real bug and hiding it would be worse than the noise.
- **`VERIFICATION.md` said "How Docksentry v? is checked" for any pre-release.** Its version reader matched digits and dots only, so `2.7.0-rc.2` fell through — harmless until release candidates became part of how this ships, at which point every RC produced a report that could not name what it had verified.
- **A settings save could answer with a closed connection.** `quote` was imported inside the save handler, which made it a local name for the whole function, so a second use further down raised `UnboundLocalError` unless the first branch happened to run. Two more local imports of the same shape are gone with it.

## [2.2.1] - 2026-08-07

### Fixed
- **The log page answered with nothing at all for a container that had never written a line.** Not an error page, not an empty one — the connection closed with no body. v1.73.0 merged a container's two output streams at the pipe so `/logs` stopped showing only half of them (#2, @NotRetarded); what that missed is that `subprocess` leaves `.stderr` as `None` when it has been redirected into stdout, because there is no second pipe to capture. Every caller reads `result.stdout or result.stderr`, which was right for years and now evaluated to `None` whenever stdout was empty, and the `.strip()` on the next line raised in the middle of the response. Measured against a `sleep`-only container: curl exit 52, zero bytes, `AttributeError: 'NoneType' object has no attribute 'strip'` in the log. Containers that had written something were never affected, which is why this survived the release.
- **Telegram's `/logs`, Discord's `/logs` and the crash diagnostics never got the v1.73.0 stream merge at all.** Each built its own `["logs", …]` command line instead of calling the method that does the merging, so all three still showed one stream and silently dropped the other — and for a container that writes its errors to stderr, the dropped half is the half you opened the logs to read. The crash diagnostics attached to a health warning were worse than that: they concatenated the two captured streams, which is not interleaving but all of one followed by all of the other, so the lines arrived out of the order they happened in. All three now go through the same seam as the Web UI.

## [2.2.0] - 2026-08-07

### Added
- **A pinned version tag now says when a newer one exists.** A tag like `nginx:1.25.3` is immutable, so its digest never moves and the check reported "up to date" forever — including long after 1.26 shipped. True, and misleading in the same breath. @LeeNX raised exactly this in #33 and asked for it to be explained; the issue was closed answering a different question, and his actual request was never acted on. The Status page now carries a `↑ 1.26.2` badge on such containers. It is **advisory only**: Docksentry will not switch, will not offer a button, and does not count it among pending updates — pinning a version is a statement of intent, and overriding it unasked could destroy a database. Advisories live in their own file so nothing that reads pending updates can mistake one for something to apply. Costs nothing against Docker Hub's pull budget: measured, `/tags/list` returns no rate-limit headers and leaves the manifest budget untouched at 100/hour.

### Fixed
- **The tag matcher was comparing against the wrong tags, and it was already live.** `get_highest_semver_tag` filtered candidates with `if prefix and not ts.startswith(prefix)`, so a current tag beginning with a digit — the common case — produced an empty prefix and skipped the check entirely. Everything the SemVer pattern would swallow then qualified, and that pattern allows a leading `something-`. Measured against the real registry: `linuxserver/qbittorrent:4.6.5` matched `arm64v8-20.04.1` — an Ubuntu version, on the wrong architecture. This is not only used for the new advisory. `_is_major_bump` calls it, so **anyone running linuxserver images with major-confirmation enabled was being asked to confirm every ordinary patch update as a major bump.** Prefixes must now be equal, and an empty prefix is a prefix rather than the absence of one.
- **The same defect at the other end of the tag.** Candidates carrying a suffix were dropped outright, so `nextcloud:29.0.4-apache` was compared against the plain `32.0.13` — a different image variant. Suffixes must match too: an `-apache` tag matches only `-apache` tags, and a bare tag only bare ones, which is what kept pre-releases out to begin with.
- **A repository with two numbering schemes no longer confuses either feature.** `linuxserver/qbittorrent` tags both the application (`4.6.5`) and its Ubuntu base (`20.04.1`), and nothing in the tag text separates them. Candidates more than three majors ahead are read as a different scheme. That is a heuristic and is documented as one — it can be wrong in both directions, so it only ever suppresses an advisory or a confirmation prompt and never causes an update to be applied. A genuine major jump (radarr 5 → 6) still registers.

### Documentation
- **`docs/updates.md` now explains what "up to date" means** — the three tag cases in one table, why a pinned tag still receives security rebuilds but never a version jump, and both known limits: two-component tags like `postgres:16.3` are not covered, and the two-scheme heuristic above.

## [2.1.0] - 2026-08-05

@NotRetarded's Docksentry exited 137 during an update, and he learned of it from a third-party monitor rather than from us (#2). Two separate holes behind that, and the first one could cost a service its uptime.

### Fixed
- **An update interrupted mid-swap left the container down, with nobody looking for it.** A recreate goes stop → rename to `<name>_old` → build the run arguments → run. The rollback that guards every other failure lives in an `except` handler, and a SIGKILL raises nothing — the process is simply gone. The container stayed stopped under a backup name indefinitely, with no notification and no recovery. The swap is now journalled *before* the rename and finished on the next start: renamed back, started, and reported. Deliberately driven by that journal rather than by the `_old` suffix — someone may legitimately run a container called `foo_old`, and renaming theirs would be a worse bug than the one being fixed. A journal older than a day is reported and not acted on, because by then the operator has had time to intervene and a stale note describes a world that has moved on.
- **A hard kill was never reported at all.** The exit marker is written only on SIGTERM/SIGINT, so a SIGKILL left nothing behind and the next boot said nothing. The old code read an absent marker as "first boot or unclean kill — we can't prove which" and stayed silent. That was true when it was written and stopped being true in v2.0.0, when every successful start began recording its version: a state file with no exit marker beside it is a hard kill, provably. The message names exit 137's usual causes rather than just noting the fact, since that is the next question anyone asks.

## [2.0.3] - 2026-08-05

### Fixed
- **A long update report never arrived at all.** Three of @LeeNX's Cloudflare tunnels updated, all three failed their healthcheck, all three rolled back cleanly — and he was told none of it (#56). Over Telegram's 4096-character limit the whole message is rejected; the code then retried once *without* Markdown, which does nothing about length, and handed the failed result back to a caller that does not look at it. Silent loss, and the worst possible one: the report you only need when something went wrong is also the longest. He worked out the cause himself. Splitting now lives in `send_message` rather than at the call sites — there was already one hand-rolled split inline in `/status`, and the path producing the longest messages did not have it. Chunks break on line boundaries and carry an open ``` fence across the break, because a chunk ending mid-fence renders as literal backticks in one message and swallows the next as code; that would turn a truncation bug into a corruption one. Buttons ride on the last chunk, since they act on the whole report.

## [2.0.2] - 2026-08-05

Three from @NotRetarded in #2, reading his own crash alert back to me.

### Fixed
- **Event times were printed in UTC.** Docker stamps `StartedAt` with a trailing `Z`, and the alert sliced the time of day out and printed it unchanged. His crash at 23:29 local arrived as `03:29:20` — exactly his UTC-4 offset. Worse than the offset: the same message carried two clocks in two zones, because the event log's own timestamp comes from `datetime.now()` and is local. Converted now, so every time Docksentry prints is the wall clock of the machine it runs on. Set `TZ` on the container and the two agree.
- **The container that died was missing from its own alert.** The top-N list frequently does not contain it — it releases everything on the way out. His Unifi normally sits at 1.5–1.7 GB, is the largest thing on that box, and appeared nowhere; reading that, there is no way to tell whether it had grown or had already gone. Its own line now sits above the list, and is absent rather than zero when the container was gone before `docker stats` ran.
- **Exit 137 did not say what killed it.** With the kernel's OOM flag it was memory; without it, something else — the single most useful bit in the message, and we had it in the snapshot and never printed it. Deliberately three-valued: a bare "no" that actually meant "we never looked" would send someone hunting in the wrong direction, so it is only stated when the event stream was there to see it. `inspect` is not consulted for this — it reports the *current* run of a restarted container, and it was measured false on rootless Podman for a container the kernel really did kill.


## [2.0.1] - 2026-08-04

Both from @NotRetarded in #2, who caught another Unifi-OS-Server crash and
sent the alert it produced.

### Fixed
- **Crash alerts never reported host CPU load.** His container died with exit 137 during a `docker compose install` — memory nowhere near the problem at 4.4 of 7.6 GB with the largest container at 253 MiB — and the alert carried no CPU line at all. Not a threshold set too high: `docker stats` reports *containers*, and what was burning the processor was the daemon unpacking layers. An image pull, a compose build, a backup job, any host process — all invisible to us, so the line stayed silent in exactly the situation it exists for. Memory has had the right shape since v1.65.0 (the host reading first, answering "was the machine under pressure at all", then the per-container list); CPU never got its half. `/proc/loadavg` inside a container reports the *host's* run queue, the same way `/proc/meminfo` reports host memory — measured, both identical to the values outside. Deliberately not gated on a threshold: a low number answers the question as well as a high one, and gating is what made the container-level line useless here. Same locality guard as the memory line, so a monitor bound to a remote endpoint stays silent rather than reporting the wrong machine.
- **The Stop button looked latched.** The container row used one visual language for two meanings: Stop wore a filled red button, while the auto and major-confirm toggles use colour to mean "this switch is ON". Stop is momentary, so it read as a switch someone had left on. It has the neutral border the other momentary buttons carry now — 🛑 says what it is without help. The same red-on-red that came off every other button in v1.73.0; this one was missed.

## [2.0.0] - 2026-08-04

The version number catching up with what shipped. Everything below already
works in a 1.x release — 2.0 is the line under it, drawn once the last
thing I could verify myself was verified.

### The big pieces

- **Multi-host.** One instance, many machines. `DOCKER_HOSTS=pve1:tcp://pve1:2375, nas:ssh://root@nas` — the local box is always managed and is not listed. Per-host state, per-host checks, per-host monitoring, a host column in the Web UI and a `@host` token in the bots. An unreachable host is reported and skipped rather than taking the run down. Self-update stays local: Docksentry updates the instance it runs in, not the ones on your other boxes.

- **Everything is a front end now.** The Web UI, an interactive Telegram bot, an interactive Discord bot with 27 slash commands, `/metrics` and a read-only JSON API behind named tokens. All four drive the same update engine behind the same single lock, so none of them can disagree about what happened.

- **Notifications beyond Telegram.** Discord, ntfy, Gotify, Matrix, generic webhooks, SMTP, and Apprise — which covers around a hundred services without a line of our code.

- **It watches, not just updates.** Unhealthy, recovered, exited, crash-restart and OOM, with the exit code taken from the runtime's live event stream rather than from `inspect`, which reports 0 for a container the restart policy already brought back. The memory and CPU picture is captured 0.08 s after the death instead of up to a minute later, and it names the neighbour that squeezed the container out — not just the container that died.

- **An audit trail.** Who did what, through which front end, kept across restarts. Recorded at one seam per front end rather than per endpoint, so the next endpoint cannot be added without it. Secrets are redacted centrally and never reach the file.

- **A Web UI worth using on a phone.** Container cards below 700px, emoji actions with a legend, and a layout measured at every width from 1024 to 4K on every page, every release.

- **Registries it can actually reach.** Pull-through mirrors, plain-HTTP hosts you name, private CAs via `SSL_CERT_FILE`, `Link`-header tag pagination, and Basic-auth registries. Credentials go to the registry they belong to and no other — a substring match used to hand `eu.gcr.io`'s secret to `gcr.io`.

- **Safety rails.** Update policies per container, major-version confirmation, update windows, protected containers, `MIN_IMAGE_AGE_DAYS` so you are not the first to pull a compromised image, and a rollback that will not promote a stale backup over a healthy container.

### Added

- **`ssh://` endpoints actually work.** The image had no `ssh` binary, so every SSH host failed with `exec: "ssh": executable file not found` while the README said it worked. Found by driving the transport for the first time rather than asserting its argv.
- **A note on the first start after an update.** `docker pull` + `up -d` used to be silent. Now the first boot under a new version says what changed, from the same CHANGELOG you are reading, with a link to the release. Silent on a fresh install, where "updated" would be untrue.

### What is verified, and what is not

Multi-host now runs over real `tcp://` and `ssh://` endpoints, not the local socket wearing an `-H` flag: measured against a `docker:dind` over loopback and a real sshd with the socket mounted, both driving the whole chain. Two runtimes, Docker and Podman, are exercised on every change.

What has *not* happened is anyone but me running it. @LeeNX has offered to test multi-host and expects to get to it in a couple of weeks. If you are running Docksentry across several machines, #7 is the place to say how it went.

## [1.74.0] - 2026-08-03

### Fixed
- **The settings cards were on every tab.** Backup, Info, the update window and both maintenance cards sat outside all five panes, so switching tabs left them where they were and they read as repeated on each one (#2, @NotRetarded). They are now in the tab they belong to: Backup and Info under General, the update window under Updates, maintenance mode next to the cleanup it pauses.

  Moving them meant restructuring the form, which is the part worth knowing about. Those cards carry their own POST forms, and putting them inside a pane put them inside the settings form — nested forms are invalid HTML, and the parser does not just tolerate it: it drops the inner start tag and lets the inner `</form>` close the outer one. Measured before it went anywhere: the settings form ended after the Updates tab, and 23 fields from Cleanup, Notifications and Channels fell outside it. A checkbox that is not submitted reads as "off", so one press of Save would have turned off auto-cleanup, monitoring and the weekly report and blanked both webhooks. None of it was visible — `ast.parse` passed, the structure check passed, screenshots of all five tabs looked right. So the settings form is empty now and its 26 fields attach to it by id, which means no card can nest a form wherever it ends up.

### Removed
- **The Groups card on the settings page.** It was a signpost left behind in v1.21.1 when groups moved to their own page. "Gruppen" has been in the navigation on every page for the 52 versions since.

## [1.73.0] - 2026-08-03

From @NotRetarded's interface review in #2, written on a phone while checking the previous release's mobile changes.

### Changed
- **Action buttons are emoji.** 🔎 check · 🔃 update · 📌 pin · 🛑 stop · 🔁 auto · ⚠️ major-confirm. The argument that decided it is one we made ourselves: the legend under the container table exists *because* the previous line-art set needed explaining. A 📌 does not. The trade accepted with it is that emoji render differently per platform and cannot be recoloured, so the table is louder than it was. One departure from the request: 🔊 was suggested for major-confirm, which reads as "sound"; ⚠️ says what it means. The filled button backgrounds are gone where a glyph carries the state — they existed so a monochrome icon could signal "dangerous" or "on", and 🛑 on a red button is red on red.
- **The logs page opens on Docksentry, with 100 lines.** It used to open on nothing, rendering an empty frame until you picked a container — and the one people want first when an update failed is this one. Fifteen containers already produce more than 50 lines of its own output, so the old default cut off the part worth reading.
- **The cron preview labels its first entry.** "18:23 · today 21:23 · tomorrow" gave no way to tell which of the three had already happened.

### Fixed
- **Half of every container's log output was silently discarded.** Every caller read logs as `result.stdout or result.stderr`, which takes stdout whenever it is non-empty and drops the other stream. Measured on a container writing 30 lines to each: Docker has 60, the Web UI showed 30 — and the discarded half was the **error** output, the half you open a log page to read. It also made `--tail 50` look like it returned fewer than 50, which is how it was reported. Merged in `backend.logs()` rather than at the four call sites that shared the bug, and in the order the container wrote them.

## [1.72.0] - 2026-08-03

Three of the most-requested things across the tools surveyed, built together because they only make sense together.

### Added
- **Container cards on narrow screens.** Below 700px a five-column table is a compromise even inside a scroll container: you swipe sideways to find the buttons, and the columns that matter are the ones off-screen. Each container is a card instead, with everything on one screen and finger-sized actions. Built from the same locals as the table row in the same function — a row and a card that drift apart is the failure this project has already had twice.
- **`MIN_IMAGE_AGE_DAYS` — don't be the guinea pig.** Hold auto-updates back until the image has been public for N days, per container with `docksentry.min-age`. Two independent reasons people ask for it, and the second is what makes it more than a preference: risk deferral, and supply chain — a compromised image is usually noticed within days, so not being first to pull it is a real defence. It was the one gap in an otherwise complete safety chain. The auto path only; pressing the button always works, and the update stays pending so it applies by itself once the image has aged. Fails open when the registry exposes no build date, because a gate that cannot see cannot judge. Four of five projects surveyed had the request.
- **`/metrics` and `GET /api/status`, behind API tokens.** The highest-reacted issue in two of the five corpora — around 120 reactions on the one idea in diun alone, with four community PRs over four years, none merged. The motive that generalises best: people who will not allow unattended updates run the tool in report-only mode, and for them the metric *is* the product. So it reports what is pending, per host and per container, not just that the process is alive. Output validates clean under `promtool`.

  Tokens come with it rather than after it, because the endpoint is unusable without them: a scraper cannot log in, and the shared Web UI password would let a monitoring job stop containers. `API_TOKENS=prom:xxx,grafana:yyy` — named so one can be revoked alone, compared in constant time, and consulted for exactly two GET paths so a token cannot reach anything that changes state. A token that is presented and *wrong* is rejected rather than falling through to the password check: on an instance with no `WEB_PASSWORD` that check answers 200, so a revoked token would have appeared to keep working.

## [1.71.0] - 2026-08-02

### Added
- **`REGISTRY_MIRRORS` — check against a mirror.** Update checks go out over HTTPS straight to the registry named in the image reference, which means they ignore the daemon's own `registry-mirrors` completely. On a network where only the mirror is reachable — air-gapped, or behind a proxy that allows one host — `docker pull` worked while Docksentry reported "unreachable" on every cycle, which is the same confusing pair of facts as the HTTP-only registries fixed in v1.70.0. `REGISTRY_MIRRORS=docker.io=mirror.internal` sends lookups there instead. Verified against a real pull-through cache. Requested by @LeeNX in #34.

  Lookups **only**, deliberately. Pulling still hands the container's own image reference to the daemon: pulling from somewhere else would rewrite that reference — `nginx:1.25` becoming `mirror.internal/nginx:1.25` — and then the container no longer matches its own compose file, and the next check compares against something different again. Docker's `registry-mirrors` in `daemon.json` is the right place for the pull side, and covers every pull on the host rather than only ours.

### Fixed
- **Unlimited swap did not survive a recreate.** `MemorySwap: -1` was skipped alongside 0 on the assumption that omitting the flag was a no-op — the comment said as much, and it was wrong. Measured: `--memory=256m --memory-swap=-1` inspects as -1, while `--memory=256m` alone inspects as 536870912, Docker's 2× default. So a container explicitly given unlimited swap quietly acquired a swap cap on its first update and could be OOM-killed afterwards where it was not before.
- **One slow `docker` call discarded the whole sweep.** `_get_image_created` calls the backend unguarded and the backend raises on timeout; nothing wrapped the container loop, so a single slow `docker image inspect` took every result with it — including containers already checked — and the pending file was never written. Each container is its own try now, and a failure is treated like a failed registry check: the container keeps whatever pending entry it had, because being unable to look is not evidence that nothing is pending. (wud#490, wud#422, wud#551, wud#658)

## [1.70.0] - 2026-08-02

The last of the findings from mining comparable tools. Three of these made a whole class of setup silently unusable while `docker pull` kept working — which is a confusing pair of facts to be handed.

### Added
- **`INSECURE_REGISTRIES`** — registries to reach over plain HTTP, comma-separated, wildcards allowed. `https://` was hardcoded in every registry URL, so a local or internal HTTP-only registry reported "unreachable / unauthorized" on every cycle and there was no setting to change it. Only hosts named here, never guessed and never a fallback when TLS fails: a tool that quietly retries over HTTP hands credentials to whoever answers. (watchtower#277/#497/#767)
- **Basic-auth registries work.** The stock `registry:2` behind htpasswd answers `WWW-Authenticate: Basic`, and the parser returned nothing for any non-Bearer challenge — so the credentials already sitting in `config.json` were never sent. Verified end to end against a real registry:2 with htpasswd over HTTP: digest fetched, tags listed. Both this and `INSECURE_REGISTRIES` were needed to reach that setup at all, which is why they land together. (diun#357, diun#5, wud#797)
- **`NTFY_TOKEN`, or `NTFY_USER` + `NTFY_PASSWORD`.** No `Authorization` was ever sent, so a self-hosted ntfy with `auth-default-access: deny` — or any reserved topic on ntfy.sh — rejected every push with one log line and nothing to reach for. Measured against a real protected server: 403 without, 200 with. (wud#951)

### Fixed
- **Discord dropped the notification entirely above 25 updates.** Discord rejects an embed with more than 25 fields, with a 400, so the message was lost rather than truncated — someone back from a holiday to 30 pending updates got silence. Split into messages of 25 now, numbered when there is more than one. (dc#255, dc#185)
- **A rate-limited notification was thrown away.** Every channel treated HTTP 429 as terminal. It is the one status that is explicitly transient and carries `Retry-After` — so the notification was being discarded at exactly the moment the service was busiest, which is when a "3 containers failed to update" message matters most.
- **The daemon's cgroup version was cached once per process, not per host.** It feeds the arguments used when recreating a container, so on a mixed fleet whichever host was asked first decided for all of them — and knobs like `memory.swappiness` are cgroup-v1-only, so getting it wrong either drops a setting the container had or emits one the daemon rejects. Measured on a live two-host install: local reports cgroup 2, the Podman host reports 1. Third time a per-process cache has been a multi-host trap here.

## [1.69.0] - 2026-08-02

Three more from the same sweep, each reproduced before being fixed. All three are the same shape: the tool was quietly not doing what you thought.

### Fixed
- **An emoji in `BOT_LABEL` silently dropped every ntfy notification.** Python encodes HTTP header values as latin-1, so one emoji in the title raises `UnicodeEncodeError` inside `urlopen` — swallowed by the notifier's broad handler, one log line, push gone. The README recommends `BOT_LABEL=🖥 pve1`, so following our own documentation and using ntfy was enough to receive nothing and be told nothing. Titles are RFC 2047 encoded when they need it, verified against a real ntfy server. Note the condition is "not ASCII", not "not latin-1": an umlaut passes the latin-1 test but ntfy reads the header as UTF-8, and `Grün Größe` sent raw arrived as `Gr<?>n Gr<?>e` — quietly mangled rather than dropped, which is harder to notice. (dc#120)
- **Only the first page of a registry's tag list was read.** Registries hand out the oldest tags first, so this truncated at exactly the end that matters. Measured on `ghcr.io/home-assistant/home-assistant`: 100 tags returned, all from 2021, highest parseable version 2021.7.1 — while the project is on 2026.7.4. Every major-bump decision there was made against a four-year-old view, so the confirmation gate you opted into never fired. Docker Hub answers with everything, which is why "the first hundred are enough in practice" held there and was silently false elsewhere. The full crawl now reaches the current version (44 pages, 4379 tags, ~15s) and is cached per run, so a compose stack with five containers from one image walks it once. Only runs for a container that actually has an update. (diun#43, diun#518, diun#653)
- **Every registry failure said the same thing.** "Check FAILED (registry unreachable / unauthorized)" covered a rate limit, a deleted tag, a 500, a TLS failure and a DNS miss alike, which sends people hunting for an auth problem they do not have. The detail existed but went only to the debug log. The reason now reaches the visible line — `rate limited by the registry (HTTP 429)`, `tag or repository not found (HTTP 404)`, `TLS error (…)` — and is reset per container so one failure cannot inherit another's explanation. (diun#94, diun#245, wud#419)

## [1.68.0] - 2026-08-02

Findings from mining the bug histories of comparable tools, each reproduced here before being fixed — plus a feature that came out of the same conversation.

### Added
- **`MONITOR_ONLY_CONTAINERS` — watch and report, never update.** @LeeNX runs podman quadlets: systemd owns those containers, and recreating one behind its back leaves two things with an opinion about what should be running. Every existing exit meant "stop looking" — `pin`, `enable=false` and `exclude` drop the container from the scan entirely, so he lost exactly the version and update information he wanted to keep. Worse, with no middle setting he could not enable auto-update *for the whole host*. Takes wildcard patterns (`systemd-*,gitea-*`) for anyone who cannot easily add labels, plus a `docksentry.monitor-only` label for anyone who can. The refusal sits beside the existing self-kill backstop, so it covers the scheduler, the Web UI, both bots and anything added later — never automatically **and** never by hand, because the update is wrong no matter who asks. In the UI the row keeps its check button and loses every other action, with the disabled tooltip saying why. Not a niche: on the developer's own host 8 of 20 running containers are Portainer-managed, where a recreate fights the tool that deployed them. (#55)
- `EXCLUDE_CONTAINERS` accepts the same wildcards. A pattern without one behaves exactly as before, so existing values are unaffected.

### Fixed
- **A stale backup could leave Docksentry dead after a self-update.** The helper script was `stop && rename && (run || rollback) && rm _old`. `rename` fails when `<name>_old` survives an interrupted run — and the chain then stops *before* the `run`, while the rollback hangs off the run and never executes. The container has already been stopped. Docksentry is down with nothing to bring it back, and since it is the thing that would have told you, nobody finds out. Proved both ways against throwaway containers: the old script left `Exited (137)`, the new one comes back as the new generation. The stale backup is cleared first, and a rename that fails anyway restarts what was just stopped.
- **A label containing a newline or a comma corrupted every `docksentry.*` flag on that container.** Labels were parsed out of `docker ps --format '{{.Labels}}'`, a flat comma-separated string. A newline in any label value truncates the *line*, so a container carrying `docksentry.pin=true` alongside a two-line description parsed to just the description — the explicit pin silently ignored, the container left watched and updatable. And a value containing `, key=value` parses as two labels, so a `LABEL` baked into somebody else's image could set a docksentry flag on your behalf. Labels now come from `docker inspect`, which takes many refs at once and answers in JSON: one extra call per sweep rather than one per container. (wud#1113, wud#921)
- **An unreachable daemon reported "everything up to date".** `get_running_containers` never checked the return code, so a failing `docker ps` produced empty output, an empty list, zero updates, that host's pending slice wiped and an all-clear sent. It raises now, which the Discord bot already expected — and because it raises at the top of the check, the pending file is never touched. (wud#570, wud#711)
- **A failed registry check deleted an update we already knew about.** A full scan rewrites this host's pending entries from the updates it found; a container whose check failed is not among them, so its badge and update button vanished from the Web UI and the next report said everything was current. One 429 from Docker Hub was enough. Failed checks are remembered and their entries carried over. (wud#116, wud#419, wud#945)
- **The action buttons in the container table wrapped, making rows ragged.** Seven buttons need 248px and the column was 179px, so a row with six stood 89px tall next to one with four at 66px — which is why @LeeNX's docker host looked untidy while his podman host, with a uniform action set, did not. And the underlying constraint was a 900px cap on the whole content area: on a 2560px monitor the table scrolled by 48px with 1700px of screen sitting empty. The status page gets a wider measure and the buttons no longer wrap. Every row is now the same height. (#46)

### Changed
- **`UPDATE_POLICY` says when it cannot actually hold anything back.** It judges bump size from a version string, which comes from the OCI `image.version` label or a full `x.y.z` tag — and almost nobody runs `:1.2.3`. When nothing parses, the update goes through. That fail-open stays (switching it would stop every automatic update overnight for anyone on `:latest`), but the silence does not: someone who sets "patch only" to prevent a `postgres:16` → `17` jump was never told the setting does not reach that container. Startup now reports the real coverage — on the developer's host, 3 of 18 — and names the settings that do work. (wud#881)

## [1.67.0] - 2026-08-02

A pass through the web interface page by page, looking at it rather than reading its code. Every finding below was measured in a browser before it was touched.

### Fixed
- **On a phone, the whole page scrolled sideways instead of the table.** The container table is wider than a phone and had no scroll container, so the page itself moved and the header went with it — measured overflow of 286px at 360px wide, 256px at 390px, and still 46px at 768px. Every table now sits in its own horizontal scroll container: the table scrolls, the page stays put. Verified afterwards at 360 / 390 / 768 / 1024 across all pages — no horizontal page overflow anywhere.
- **The container page and the status table disagreed about the same container.** Measured on a live container carrying `docksentry.auto=true`: the status row showed "auto 🏷" while the container page showed the checkbox **unchecked and still clickable**. Clicking it wrote to storage, changed nothing because the label wins, and left the two pages contradicting each other — the same "a control showing a state it does not have" defect @LeeNX reported in #51, one page over. The status table has read labels since #42; this page never did. It now reads `docksentry.auto`, `.ask-major` and `.trust-running`, marks them 🏷 and disables the control. Disabled rather than merely marked, because a click that silently does nothing is what this whole class of bug is made of.
- **The action buttons in a table row were four pixels out of line.** @LeeNX in #46: "the force recheck buttons always seems a little higher than the rest." The check button sits bare in the row while every other action is wrapped in a `<form>`, and forms carry a browser default top margin — so inside that flex row the forms' margin box was 40px against the bare button's 32px, and centring put them at different heights. Measured at 572.5 versus 576.5 before; one shared position after.

### Changed
- **The cron preview spoke English inside a German interface** — "today 18:00 · tomorrow 18:00 · Tue 18:00". Two hardcoded words, plus a weekday from `strftime("%a")`, which reads the process locale, and in the container that is C. Both go through translation keys now, in all 16 languages.
- **The weekday picker for update windows was hardcoded English** too, in two places. Same fix.
- **Nine German strings were word-for-word transliterations of the English**, keeping every compound noun: "Drag-and-Drop-Sortierung", "Edit-in-place", "Head-Container-Visualisierung", "persistierte Settings", "forward-compatible", "Defense-in-Depth". One was wrong as well as ugly — "Container-Gruppen *wohnen* jetzt auf einer eigenen Seite", a literal rendering of "now live" that in German is what people do. Rewritten as German.
- **Five stat cards in a two-column grid left the fifth alone in a half-width row**, which reads as a rendering fault. It spans the row now, below 600px only — above that the grid finds room for more columns and there is no orphan.

### Known, not fixed
- Rows in the container table stand at different heights depending on how many actions a container has — 66px with four buttons, 89px with six, because six 32px buttons need 212px and the column is 179px. That raggedness is the second half of @LeeNX's #46. Widening the column to 212px did **not** help: `min-width` loses to the table layout algorithm, the buttons still wrapped, and one row grew from 91px to 108px. Backed out rather than shipped. Still open.

## [1.66.0] - 2026-08-02

More findings from mining comparable tools' bug histories — this round from a complete sweep of all 684 issues in `getwud/wud`, labels ignored. Both fixes below were measured here before being written.

### Fixed
- **The SMTP password was sent over an unverified TLS connection.** `smtplib` without an explicit SSL context falls back to `ssl._create_stdlib_context()`, which on current Python *is* `_create_unverified_context` — measured: `check_hostname=False`, `verify_mode=0`. So `SMTP_USER` and `SMTP_PASSWORD` went to whatever answered on `SMTP_HOST:SMTP_PORT`, presenting any certificate at all, with no compromise of the real mail server needed. Demonstrated both ways against a local server holding a self-signed certificate for a different hostname: before, it captured `AUTH PLAIN` with the credentials in it; now it gets nothing. Certificates are verified on both transports. If you run an internal mail server with a self-signed certificate you will now see a failure — `SMTP_TLS_VERIFY=false` restores the old behaviour, the error message says so, and using it logs a line saying what it costs. (wud#352)
- **Every day-of-week schedule fired one day late, and Sunday written as `7` never fired at all.** Cron counts weekdays from Sunday (`0`=Sun … `6`=Sat, with `7` a second spelling of Sunday); Python's `weekday()` counts from Monday. The matcher fed one straight into the other. Measured across a full week: `0 9 * * 1` ("Mondays") fired on **Tuesdays**, `0 3 * * 0` ("Sundays") on Mondays, and `0 3 * * 7` fired **never**. The Web UI's own shipped preset "Weekly (Mondays 9 AM)" is that first expression, so anyone who picked it from the dropdown has been getting Tuesdays. The "next 3 ticks" preview used the same function, so it confirmed the wrong day rather than catching it. Ranges and lists are covered too — `1-7` now means Monday through Sunday inclusive. (wud#410)

### Changed
- **A `docksentry.auto`, `.enable` or `.exclude` label on Docksentry's own container now says it does nothing.** It genuinely has no effect there — that label tells *another* instance how to treat this container, and no other instance is watching us — but silence was the wrong way to handle it. @LeeNX in #51: "I am not a fan of things that get ignored, it's a pattern that you don't know you might be doing something wrong and things look like they're just breaking." The ignore was introduced by the fix for that very issue, which makes it his point twice over. Startup now names the label and points at `AUTO_SELFUPDATE`.
- **Bind-mount propagation survives a recreate.** A `:rslave` or `:rshared` bind came back as `rprivate`, so the container silently stopped seeing host mounts that appeared later — the classic symptom being a media stack that no longer notices a disk mounted after it started. Found independently by two separate sweeps. (watchtower#221, ouroboros#1-#5)

## [1.65.1] - 2026-08-02

Found by mining the bug histories of comparable tools — Watchtower, Diun, WUD, dockcheck and Ouroboros — and reproducing each finding here before fixing it. Their backlogs are our backlog in waiting: same Docker API, same registry protocols.

### Fixed
- **A private registry's credentials could be sent to a different registry.** The `config.json` lookup matched with `registry in key or key in registry`, so a container pulled from `gcr.io` was handed the Basic-Auth header stored for `eu.gcr.io`, and anything under `example.com` got the credentials for `myregistry.example.com`. Measured, not theorised. Hostname matching is now structural: scheme and path are stripped, the comparison is exact, and Docker Hub's four aliases fold together. That last part also fixes the case the substring hack was written for and never handled — `registry-1.docker.io` never matched `https://index.docker.io/v1/`, because neither string contains the other, so the one credential nearly everyone has was silently ignored. (watchtower#376)
- **A leftover backup container could get promoted over your healthy one.** When `<name>_old` survived an interrupted run, the next update's rename failed, the recreate then failed on the name conflict, and the rollback — which trusts `<name>_old` to be *this* run's backup — force-removed the healthy container and started the stale one in its place. You end up running a previous generation of your container with nothing saying so. The guard already existed on the dependent-recreate path; the main path never had it. Two independent sweeps reproduced the same end state. A rename that fails now aborts before anything is destroyed. (watchtower#1101/#235, ouroboros#19/#20)
- **Compose stacks with an override file silently stopped using compose.** Docker joins multiple compose files into one comma-separated label, so `docker-compose.yml,docker-compose.override.yml` failed the file check and the stack fell through to the standalone `docker run` recreate — losing compose semantics on exactly the setup the Compose docs recommend. Confirmed on four containers here. Each file now gets its own `-f`, in Docker's recorded order, since compose applies overrides left to right. A path containing a comma is left alone: an unresolvable split would deploy from the wrong file, which is worse than the fallback it replaces. (dockcheck#27)

### Changed
- **The README's roadmap was two releases out of date.** It still listed multi-host management and the interactive Discord bot as "v2.0, ahead" and asked people to star the repo to signal demand for them — both shipped on 1 August. Anyone comparing Docksentry against another tool read that section and concluded it couldn't do what it had been doing for a day.
- `/favicon.ico` is served. The page has always declared its icon inline, so browser tabs showed it and the request looked satisfied — but bookmark managers, feed readers and link previewers ask for that path and all got a 404.

## [1.65.0] - 2026-08-02

### Fixed
- **Crash-restart alerts reported the wrong exit code — always 0.** The number that is supposed to tell "this crashed on its own" from "that was my own `docker stop`" carried no information at all. `docker inspect` cannot supply it: a container the restart policy has already brought back is *running* again by the time the monitor looks, and a running container reports `ExitCode: 0`. Measured, and true of the previous sweep too, since it was running then as well. The live event stream carries the real code, so alerts now say `exit 137` where they used to say `exit 0`. Introduced in v1.63.0 with the alert itself; reported in spirit by @famewolf in #2, who needed exactly this number to tell his own shutdown from a real crash.
- **Only the local host was being monitored for crashes and health.** Update checking has been multi-host since v1.62.0, but the container monitor was written before hosts existed and quietly kept its single local backend. On a live two-host install a Podman container died with exit 42 and nothing was reported, while the same instance logged "Managing 2 hosts" at startup. Anyone running a second host has been watching half their fleet. Alerts now carry the host — `nas/nginx` — because a fleet running the same stack on two machines otherwise produces two identical messages and no way to tell which box is on fire (#7).

### Added
- **A live watcher on the container event stream**, so the memory picture in a death alert is taken at the moment of death rather than up to a minute later. The monitor polls every 60 seconds; by the time it noticed, the container that ate the RAM may have released it, been restarted, or be the one you killed in a panic. Measured: the `die` event arrives 0 ms after the daemon stamps it, and the snapshot now lands 0.08s after death. The watcher never sends anything itself — alerting stays with the poller, which owns debouncing and cooldowns — it only records evidence the poller picks up. Turn it off with `MONITOR_EVENTS=false`; if it cannot start, Docksentry falls back to polling exactly as before. Asked for by @NotRetarded in #2.
- **CPU alongside memory when a container dies.** @NotRetarded came back with the thing that should have made me doubt the whole memory line of work: no memory limit on the container that died, no visible memory pressure on the host, but CPU spikes. So I measured whether CPU starvation can kill a container. Directly, no — one pinned to 1% of a core ran happily. But the indirect route is real, and produces a death that cannot be told apart from an out-of-memory kill:

  ```
  same container, same shutdown handler, same `docker stop -t 5`
  --cpus=1.0    → exit 0, clean
  --cpus=0.005  → exit 137, OOMKilled false
  ```

  Starved of CPU it could not answer SIGTERM inside its grace period, so Docker escalated to SIGKILL — exit 137 with `OOMKilled` false, byte-identical to what the kernel's OOM killer leaves behind. An alert reporting only memory therefore points at the wrong resource in exactly that case. CPU comes from the same `docker stats` call, no extra process, and is reported only when something is actually holding the processor, because contention is the point and a CPU line on an idle host would bury the signal it exists to carry.
- **Host memory on the status page**, next to disk. Reads the same `/proc/meminfo` the alerts use, so the page and the message can never disagree about how full the machine is. Also @NotRetarded's suggestion — he could see disk at a glance but had to leave Docksentry to find out whether the machine was under memory pressure at all, which is the question that comes before "which container did it".

## [1.64.0] - 2026-08-01

### Changed
- **Container deaths now come with the memory picture, not just the exit code.** The snapshot naming the biggest memory consumers used to fire only when Docker flagged an out-of-memory kill — which misses the commonest shape of the problem. A container squeezed out by a greedy neighbour frequently dies *without* that flag: it comes from the container's own cgroup, so a kill made under host-wide pressure need not land there. Exactly the case @NotRetarded hit in #2 — his Unifi container was taken down by an app he'd just installed, and the alert arrived with nothing about memory in it. Crash-restarts and non-zero exits now carry it too, with the host's own state ahead of the list:

  ```
  🔁 Unifi-OS-Server crashed (exit 137) and was restarted by its restart policy at 16:14:47 (restart #1).
  Host memory (used/total): 14.8/15.6 GB · Swap 3.9/4.0 GB
  Top memory at event time: some-new-app 9.1GiB · unifi-os 2.2GiB · postgres 890MiB
  ```

  The host line goes first on purpose. A top-consumers list on its own invites you to blame whoever is at the top, and if there were 8 GB free that's just your biggest container minding its own business — the line above it tells you whether memory was the story at all. Nothing polls in the background for this: it costs one `docker stats` at the moment of the event, shared across every event in the same sweep, plus a read of `/proc/meminfo`. Monitors watching a *remote* host print no host line, since that file describes the machine Docksentry runs on and wrong numbers are worse than none.

## [1.63.1] - 2026-08-01

### Fixed
- **On a mixed-architecture fleet, every host after the first was checked against the wrong platform.** The daemon's architecture was cached once per process rather than per host, so an arm64 box sitting next to an x86 one — an ordinary homelab — had its multi-arch images compared against amd64 digests and got the wrong answer every time. Only affects multi-host setups. Raised by @LeeNX in #7 before anyone had actually run one that way. While fixing it: a platform string with more than one slash used to fall through to the amd64 default instead of being parsed, which is worse than saying nothing.
- **The Web UI linked to a project's front page where the bot linked to its releases page.** The Web UI keeps its own copy of the link priority chain so it doesn't have to inspect every container just to draw the table, and that copy was missing the rewrite. Same container, two different links depending on where you looked. Spotted by @LeeNX in #52, on Docksentry's own row.
- **Gitea and Forgejo repo links now point at the release too.** They mirror GitHub's layout closely enough that `/releases/latest` resolves — @LeeNX proved it with the redirect. Covers gitea.com, Codeberg and self-hosted instances; a manual `docksentry.link` or `/setlink` is still never rewritten.

## [1.63.0] - 2026-08-01

### Added
- **An interactive Discord bot** (experimental). Set `DISCORD_BOT_TOKEN`, `DISCORD_APP_ID` and `DISCORD_GUILD_ID` and you get slash commands for the things you'd otherwise open the Web UI for: `/status`, `/check`, `/update`, `/restart`, `/logs`, `/pin`, `/history` and a dozen more, across every managed host. This is a second front-end onto the *same* update engine the Telegram bot and the Web UI use — one update lock, one set of rules, so the three can't drift apart. Destructive commands (`/stop`, `/updateall`) ask with a button first, replies are visible only to the person who ran the command, and `DISCORD_GUILD_ID` is required: it restricts the bot to your server, and without it the bot refuses to start. `DISCORD_ALLOWED_USERS` narrows it further. Note this needs a bot *token*, which is a different thing from the `DISCORD_WEBHOOK` used for notifications.
- **Three more notification channels**, each one file: **Gotify** (self-hosted push; failed updates go out at high priority so they cut through quiet hours), **Matrix** (plain text *and* HTML, so every client renders something sensible), and **Apprise** — which is the interesting one, because a single Apprise container fans out to Pushover, Signal, Rocket.Chat, Mattermost and around a hundred other services that Docksentry has no code for.
- **The Web UI can act on any host**, not just the local one. Every button a local row has, a remote row now has too, and a dropdown filters the table to one host.

### Changed
- **Crash-restart alerts now say what exited and when.** They used to read "🔁 name crashed and was restarted (restart #1)" — no exit code, no time — which is indistinguishable from your own `docker stop` if you happen to be shutting things down at the time. It now reads "crashed (exit 1) and was restarted by its restart policy at 16:19:25", so you can tell at a glance whether it's describing something you did. Reported by @famewolf in #2.
- **A pre-release will never move the `latest` tag.** Previously any `v*` tag published `:latest`, which means an `rc` or `beta` would have been pulled by everyone running `docksentry:latest` with auto-self-update — overnight, without being asked. Trying a pre-release is now a deliberate act: pin the version tag.

## [1.62.0] - 2026-08-01

### Added
- **Multi-host: one Docksentry, several Docker hosts** (#7). Point it at your other machines with `DOCKER_HOSTS=pve1:tcp://pve1:2375, nas:ssh://root@nas` and it checks, updates, recreates and reports across all of them. The endpoint is whatever the container CLI takes for `-H`, so a TCP socket / socket proxy works as well as SSH — a socket proxy is the simpler route if you'd rather not maintain keys. Aim a command at one box with `@host` (`/check @pve1`, `/update sonarr @nas`) or at everything with `@all`. Looking around (`/check`, `/status`, `/updates`) covers every host by default; anything that *changes* something stays on the local host unless you say otherwise, and says so in its reply. The Web UI gains a Host column listing every host's containers. Marked experimental: it hasn't run on real multi-host hardware yet.
- **Podman is now actually tested**, not just supported on paper: the suite drives a real `podman` and a remote Podman service over TCP. That immediately paid for itself — Podman documents `--url` for remote endpoints and *not* `-H`, which is what Docksentry was about to send. It accepts `-H` as an undocumented alias today, so this would have worked until some future Podman quietly dropped it.
- Leave `DOCKER_HOSTS` unset and **nothing changes** — no host column, no `@` anywhere, same state files, same messages, byte for byte.

### Fixed
- **Container groups worked only on Docker.** The restart-dependents cascade shelled out to `docker` by name instead of going through the configured CLI, so with `CONTAINER_CLI=podman` it called a binary that may not exist. Affects anyone on Podman using a container group with `restart_dependents`.
- **Every container CLI call is now time-bounded.** Calls without an explicit timeout inherited "wait forever", which a dead-but-established remote connection can trigger — and since Telegram commands are dispatched inline, one such call could stop the bot answering anything at all.
- The Podman documentation still claimed Docksentry had no Podman-specific code, which stopped being true in v1.61.0.
- Replaced the Web UI screenshots, which were several versions out of date and showed the old name and theme.

## [1.61.0] - 2026-07-31

### Added
- **Native Podman support.** Set `CONTAINER_CLI=podman` and Docksentry calls `podman` directly — checks, updates, recreates, rollback, start/restart, compose (`podman compose`), image cleanup, the lot. No more aliasing `docker` to `podman` to make it work. The default is `auto`, which behaves exactly as before: it uses `docker` whenever that command exists (so every existing setup, alias ones included, is untouched) and only reaches for `podman` when `docker` genuinely isn't there. One honest caveat: **self-update** still shells out to `docker` and launches a `docker:cli` helper container — it can't run inside the container it's replacing — so on Podman that single path still needs `docker` to resolve. Everything else goes through the CLI you picked.

### Changed
- **Every container command now goes through one seam.** The `docker` binary name used to be hardcoded in about ninety places across the update core; it's now behind a single backend, which is what made the Podman support above a class swap instead of ninety edits. Purely internal — the commands sent are byte-for-byte identical to before, verified command by command, and it's the groundwork for managing remote hosts later.

## [1.60.5] - 2026-07-31

### Fixed
- **Web UI "Update" now runs through the exact same update engine as the Telegram "Update all".** Web-triggered updates used to take a shortcut — a plain per-container loop that skipped container-group ordering, the network-namespace sidecar snapshot, the restart-dependents cascade and the per-container cooldown; on top of that the bulk-update path didn't take the shared update lock, so a Web "Update selected" could overlap a running scheduled update or a self-update swap. Both the single-row Update button and the bulk "Update selected" now go through the shared engine under that lock, so groups, sidecars, dependents and cooldowns are all honoured no matter where you start the update from, and two updates can no longer run at once. Notifications and the Telegram summary now also match the bot path exactly.

## [1.60.4] - 2026-07-31

### Changed
- **Internal groundwork (v2):** the shared per-container update loop — the single piece of code both the scheduled auto-update pass and the manual "Update all" run through (group ordering, the netns snapshot, the restart-dependents cascade, per-container notifier results and cooldown) — moved from the Telegram bot onto the `UpdateEngine`. The bot keeps a thin passthrough, so both paths and every test behave exactly as before; the code is a verbatim move, byte-for-byte. This is the heart of the update orchestration finally living outside the chat adapter, which is what lets a second interface share it later.

## [1.60.3] - 2026-07-31

### Changed
- **Internal groundwork (v2):** the neutral, Telegram-agnostic helpers behind update orchestration — major-bump detection, the per-container update-policy resolution, the cooldown wait, the group-dependents restart, and the container-name/version formatting — moved from the Telegram bot onto the `UpdateEngine`. The bot keeps a thin passthrough for each, so every existing caller and every test still works exactly as before, and the message wording is byte-for-byte unchanged. Same continued, deliberate step-by-step move of the update logic off the bot so a second interface can share it later.

## [1.60.2] - 2026-07-31

### Changed
- **Internal groundwork (v2):** the update lock — the single mutex that coordinates the scheduler, the Web UI and the bot so two updates never run at once — now lives on a new `UpdateEngine` instead of the Telegram bot, mirrored back onto the bot by property so every existing caller still sees the exact same lock object. Pure aliasing, no behaviour change; it's the first, deliberately smallest step of moving the update orchestration out of the Telegram bot so a second interface (a Discord bot, later) can share it. The riskier pieces follow one at a time.

## [1.60.1] - 2026-07-31

### Changed
- **Internal groundwork:** the container CLI is starting to move behind a small `ContainerBackend` seam instead of `docker …` calls scattered inline. This release routes the read-only commands in the monitor and the Web UI (ps, inspect, image inspect, logs, stats) through it; the update engine is untouched. No behaviour change — the commands sent are byte-identical — it's the groundwork that makes a real podman backend, and eventually remote hosts, a contained change rather than edits in ninety places.

## [1.60.0] - 2026-07-31

### Added
- **ntfy notifications**, and a notifier layer that makes adding a channel a one-file job. Set `NTFY_URL` (or `NTFY_SERVER` + `NTFY_TOPIC`) and update results and available-update alerts land on your ntfy topic, with the failure ones raised to high priority. Under the hood the notification channels — Discord, generic webhook, e-mail, and now ntfy — are each a self-contained plugin that the notifier discovers automatically; a channel that errors is skipped without taking the others down. Discord/webhook/e-mail send exactly what they did before: same embeds, same payload fields, same retry, same `BOT_LABEL` framing.

### Changed
- **Internal groundwork:** container link resolution moved out of the Telegram bot into its own `link_resolver` module, so the Web UI no longer reaches into private bot methods to build a link. No behaviour change — same resolution order, same release-notes rewriting — it just lives where it belongs now, which clears the way for sharing it with future interfaces.

## [1.59.0] - 2026-07-31

### Added
- **Five settings that could only be set by env var are now editable in the Web UI.** They live in `PERSISTENT_KEYS`, so once anything was saved in Settings they froze at their then-current value with no way back except hand-editing `settings.json`. They each have a field now: the Web UI password (Settings › General, as a change field — blank leaves it as-is, the stored value is never shown), the healthcheck and stop timeouts (Settings › Updates), and container-state monitoring on/off plus its poll interval (Settings › Notifications, under a new "Monitoring" heading). Each field carries the same `env` marker as the rest, so you can see when an environment variable is overriding the saved value.

### Fixed
- **A Web UI password change now takes effect immediately, without a restart.** The password was hashed once at startup and that hash was compared on every request, so changing it — previously only possible by env var and restart, now via the new field — wouldn't apply until the process restarted. Auth now hashes the current password fresh per request (still constant-time via `hmac.compare_digest`), so a change in Settings applies on the next request.

## [1.58.1] - 2026-07-31

### Changed
- **`/check <name>` and `/update <name>` only check the containers you named.** They used to check *everything* and then filter the output down to the match — so `/check nginx` still hit the registry for every container on the host. They now scope the check to the matched containers, which is fewer registry requests and easier on Docker Hub's anonymous rate limit (the thing behind much of #53). One deliberate consequence: a scoped check no longer refreshes the pending state of the *other* containers as a side effect — that's intended (targeted means it only touches what you named), and the scheduled full check keeps the rest current. The global `/check` and `/update` with no name still check everything.

## [1.58.0] - 2026-07-31

### Added
- **Auto-detected GitHub/GitLab links point at the release notes, not the homepage** ([#52](../../issues/52), @LeeNX). When a container's link comes from its `org.opencontainers.image.source` / `.url` label and that's a bare GitHub or GitLab repo, Docksentry now links to the releases page (`…/releases/latest`, GitLab `…/-/releases`) rather than the project's front page — which is what you actually want mid-upgrade, without clicking through. Only a bare `host/owner/repo` is rewritten; anything deeper (a URL that already points at a release, a subpath, a file) and any other host are left exactly as they were. A link you set by hand — the `docksentry.link` label or `/setlink` — is never touched, so your explicit choice always wins, which also covers the repos that don't publish releases.

## [1.57.3] - 2026-07-31

### Fixed
- **Error toasts in the Web UI didn't look like errors.** Eight of them passed `'error'` to the toast helper, but the stylesheet only defines `is-success` / `is-warn` / `is-danger` — so `is-error` matched no rule and the toast came up in the neutral accent colour instead of red. A failed import or a network error read like a normal notice. They pass `'danger'` now, which is the class the stylesheet actually has. (A second, dead copy of the toast function — the one that made `'error'` look plausible in the first place, unreachable since the shared script loads — is removed.)
- **The container detail page could show the wrong image's size.** It inspected the tag reference for `{{.Size}}`, so if the tag had moved forward but the container wasn't recreated (the same #53 drift), it reported the *new* image's size rather than the one the container runs. It now inspects the running image ID, matching what the status table (v1.47.0) and the update check (v1.57.x) already do.

## [1.57.2] - 2026-07-30

### Fixed
- **The v1.57.1 fix missed the case it was written for** ([#53](../../issues/53), @LeeNX). v1.57.1 compares the running image's digest against the registry — but when a tag gets pulled forward and the container isn't recreated, the *old* image the container keeps running loses its repo digest entirely (it's dangling). v1.57.1 had a deliberate fallback for a running image with no digest — compare the tag instead, rather than risk a false positive — and that fallback landed @LeeNX right back on the original bug: his gitea-runner (2.0.1, no digest) fell back to the tag (2.3.0), which matched the registry, so it read "up to date" again. His v1.57.1 log said it out loud: `Running image … has no repo digest — falling back to the tag`.

  Now, before that fallback, the check compares image *IDs*. If the running image has no digest but the tag is a real registry image (it has a digest, so a newer image genuinely is pullable) and the container's image ID differs from the one the tag now resolves to, the container is simply behind the tag — the newer image is already local, only a recreate is missing — and that's reported as an update. A locally-built image the user rebuilds but never recreates still doesn't false-positive: the tag has no repo digest there, so nothing is claimed to be pullable. New tests in `scripts/test_running_image_check.py` cover @LeeNX's exact shape (running 2.0.1 with no digest, tag on 2.3.0) and the local-build guard.

## [1.57.1] - 2026-07-29

### Fixed
- **The update check compared the tag, not the running container — and could miss real updates** ([#53](../../issues/53), @LeeNX). Surfaced by the v1.56.0 diagnostics on the first run: his Web UI showed gitea-runner as 2.0.1 while the check said "up to date" and read the registry as 2.3.0. All three were honest. His container was running image `b1addbb…` (2.0.1); his `:latest` tag pointed at a *different* image, `15cc00a…` (2.3.0). Once a tag gets pulled forward without recreating the container — a manual `pull`, another tool, anything — the tag and the running image drift apart, and the check was comparing the *tag's* digest against the registry. Tag and registry both said 2.3.0, so it reported "up to date" while never looking at what the container actually runs. Docksentry went blind to a genuine update.

  The check now compares the image the container is **running** against the registry, which is the same running-image-ID basis the Web UI has used since [#46](../../issues/46). Behaviour is unchanged for the normal case — a container created from its current tag runs the tag's image, so the two digests are identical and every verdict, log line and notification field comes out exactly as before. It only differs where the tag and the running image have drifted, where "up to date" was wrong. A running image with no repo digest (built locally, or a tag since moved) falls back to the old tag comparison rather than risk a false "update available", and with `DEBUG` on the log spells the drift out: `Container runs 2.0.1, but tag … points at 2.3.0 — container was not recreated after the tag moved`.

## [1.57.0] - 2026-07-29

### Fixed
- **An environment variable that stops working, and never says so** ([#53](../../issues/53), @LeeNX). He set `DEBUG=true` in his compose file, restarted, and got `debug OFF`. The cause is worse than it looks: settings you can edit in the Web UI are stored in `settings.json` and take precedence on load — that part is intended — but saving *anything* in the Web UI writes **all** of them at once. So changing your language, or a bot label, or a monitor setting, quietly froze the then-current value of every one of those variables, and your compose file stopped meaning anything for them from that moment on. He never turned debug off. A save he made for an unrelated reason did it for him.

  The precedence stays as it is — flipping it would reset everyone who set something in the environment once and later changed it in the Web UI. What was missing is that nothing ever *said* so. Startup now names each explicitly-set variable that a saved value overrules, and where to change it:

  ```
  Env override: DEBUG=true is set in the environment, but the saved setting debug=false wins
    — change it under Settings › General, or remove "debug" from /data/settings.json.
  ```

  Presence in the environment is the test, not "differs from the default": setting `DEBUG=false` is a statement, leaving it out is not, and comparing against defaults can't tell those apart. Secrets are named but never printed — `WEB_PASSWORD`, the webhook URLs and the Telegram fields say "values hidden". That's an allow-list, so a persistent setting added later is treated as secret until someone decides otherwise.

  The same note appears next to the affected field in Settings, so it's visible where you'd actually go looking.
- **Action buttons stacked one per line and made table rows enormous** ([#46](../../issues/46), @LeeNX). Nothing reserved any width for the Actions column, so the widest image reference anywhere in the table decided how much was left for it. On a host full of long `ghcr.io/owner/name:tag` references the buttons collapsed to a single column — four buttons tall, per row — while the same version on another host showed two across. Same code, different containers, different layout, which is why it looked like something he'd broken himself. The column now keeps room for four buttons across, which caps the seven at two rows; the image cell yields instead, since it can wrap and buttons can't.
- **Debug could not be switched on at all in Simple mode** ([#53](../../issues/53)). The only checkbox for it was marked advanced-only, so Simple mode hid it with `display: none` — which also means the browser's own find-on-page can't see it, which is exactly how @LeeNX ended up looking for a switch that was there all along. Combined with the bug above, there was genuinely no way left to turn it on. It's visible in both modes now.

## [1.56.0] - 2026-07-29

### Added
- **The update check can now explain itself** ([#53](../../issues/53), @LeeNX). "Up to date" reads like a claim you have to take on faith when the only evidence is two truncated hashes. With `DEBUG` on, the check now prints the URL it actually requested, the HTTP status and content type it got back (which also tells you whether a multi-arch index came down), whether authentication was anonymous or a token, and the final URL if the registry redirected. Plus one line per run naming the host platform, the daemon's configured registry mirrors, the daemon's proxy settings and any proxy set inside the Docksentry container itself — that last one matters because Python's URL opener quietly picks up `http_proxy`/`https_proxy` from the environment, and until now nothing said so.
- **Digests are shown in full, with their repository.** The log truncated them to 30 characters, which is plenty to compare two of them but useless if you want to check one yourself against `docker manifest inspect`. And a container can carry several repo digests, which showed up as unexplained hashes side by side — they now say which repository each belongs to.
- **"Up to date" can name the version behind the digest.** This is the actual answer to #53: seeing `66d8096…` tells you nothing, seeing that it *is* `2.3.0` settles the question. It runs for an explicit per-container check (the 🔍 button) or with `DEBUG` on, and prints either way in the first case — never during a scheduled sweep, for a reason worth stating: resolving a version needs `GET`s on manifests, and unlike the `HEAD` the digest check uses, those count against Docker Hub's anonymous budget of 100 an hour. Thirty containers on a quarter-hourly schedule would be about 240 requests an hour. Docksentry would rate-limit itself into `429`s and start genuinely missing updates — the exact failure this feature exists to explain.

### Changed
- **Registry tokens are reused within a check.** Every registry call negotiated a fresh one, so each lookup cost three round trips instead of one. They're now cached per repository for the run, honouring the expiry the registry gives.

### Docs
- `docs/updates.md` has a "Why didn't it see my new release?" section with an annotated log, and a note on the `HEAD` versus `GET` distinction behind Docker Hub's rate limit.

## [1.55.0] - 2026-07-29

### Added
- **`docksentry.link` label for the repo / changelog URL** ([#52](../../issues/52), @LeeNX). The link that shows up next to a container in update notifications can now be set with a label, which makes it part of your compose file instead of something you configure once and forget where. It takes precedence over a link set with `/setlink` or in the Web UI, in keeping with every other `docksentry.*` label — and where it wins, the Web UI form is disabled and marked 🏷, so the field can't quietly hold a value that nothing uses.
- **The link is now visible in the Web UI at all** ([#52](../../issues/52), @LeeNX). It has existed since v1.31.0, but only ever in Telegram, Discord and the webhook payload — in the Web UI it sat in a text field on the container page and appeared nowhere else. There's a 🔗 next to each container name now, and a row on the detail page, both saying where the URL came from: your label, your own entry, the image's OCI metadata, or a guess from the registry. Resolving it costs nothing extra — the labels the Web UI already reads carry everything the chain needs.
- **The Docksentry row finally shows its own update setting** ([#51](../../issues/51), @LeeNX). With `AUTO_SELFUPDATE=true` the status table showed a dash in the Auto column, exactly as if you'd switched it off. The column was reading the per-container opt-in list, and Docksentry deliberately isn't in it — self-update is a different mechanism with a different switch. It now reads `AUTO_SELFUPDATE` for its own row and marks it ⚙ in both states, because a dash there means "self-update is manual", not "nobody enabled this yet".

### Fixed
- **The Auto toggle on the Docksentry row did nothing, twice over** ([#51](../../issues/51)). Clicking it wrote our own name into the per-container opt-in list, the update flow skipped it because it skips us by definition, and the next restart quietly stripped it back out. So the button showed a state that had no effect and then forgot it. It's replaced by a link to Settings › Updates, where the switch that actually governs this lives — and the tab picker learned to follow `#hash`, so that link lands where it promises. The same bug was in the bulk action and is guarded there too.
- **`/changelog <name>` ignored a link you'd set yourself** ([#52](../../issues/52)). It went straight to the image's OCI metadata and skipped the stored value, even though `/setlink`'s own help text promised the opposite. Both now go through the same resolver.
- **Emoji leaking into Web UI tooltips and the stop dialog** ([#46](../../issues/46), @LeeNX). v1.47.4 stripped them from the legend but not from the `title` attributes or the confirmation modal, so `🔁 Restart` and `🟥 Stop` still turned up where the styled key already is the icon. They stay in the translations — Telegram's buttons want them.
- **The "Check Updates" button had two magnifying glasses** ([#46](../../issues/46)). The styled icon plus the 🔍 that lives inside the translated label, in all 16 languages, top right of the main page — the same doubling as above, sitting in the most visible spot in the app and somehow surviving every review since v1.31.0. There's now a check in `pre-commit-check.py` that fails the build if any label rendered beside a styled icon starts with an emoji, so this particular mistake can't come back.
- **Telegram markdown rendered literally in the Web UI.** The groups page described the first container as the `*head*` — asterisks and all — and a rolled-back update's log turned up on the history page wrapped in ``` fences. Every string in this project was written for Telegram first, and the Web UI was handing them straight to a browser. The Web UI now strips the markers on its way out, at the one place it builds its translator, so a future string with a backtick in it can't reintroduce this. Markers are removed rather than turned into `<strong>`/`<code>`: history details carry raw container logs, and converting arbitrary upstream output into markup is how injection holes start.
- **Two misaligned controls** ([#46](../../issues/46), @LeeNX). The Simple/Advanced switch sat 4px below the theme button, and "Show Logs" 12px below its own filter fields. Both came from global form margins that a flex container aligns along with everything else — and the 4px one was introduced by the v1.47.3 fix for the *previous* alignment bug at that spot. Both now use their own classes instead of inline styles, and the logs filter form on the container page had the same fault and gets the same fix.

### Changed
- **Button tooltips say what the button does** ([#46](../../issues/46), @LeeNX). Update, Pin, Restart and Stop had tooltips that repeated the button's own word. They now explain the action and what survives it.
- **The legend reads in one language** ([#46](../../issues/46)). `major-confirm` and `label` were English string literals, so a German legend read "Prüfen · Updaten · Neustart · Pinnen · auto · major-confirm · Stop · label". Both are translated now, and the entries are capitalised consistently.
- **An over-long link is rejected rather than truncated.** `set_link` used to silently cut at 500 characters, which produces a link that looks saved and doesn't work.

### Security
- **Backup import no longer trusts the links it restores.** Container links went into storage raw, bypassing the validation the normal save path applies, so a hand-edited backup could plant a `javascript:` URL. That was inert while nothing rendered a link — this release renders links, which would have made it live. Imported links are now validated like any other, rejects are counted and reported rather than silently dropped, and every link is validated again on its way into an `href`. URL validation itself now decides the scheme with a parser instead of a prefix check, which a capitalised `JavaScript:` or a leading space defeats.

Tests: new `scripts/test_link_safety.py`, `scripts/test_link_render.py` and `scripts/test_web_selfupdate_row.py` — the first Web UI rendering tests in the project.

## [1.54.0] - 2026-07-29

### Added
- **Per-container update check in the Web UI** ([#50](../../issues/50), @LeeNX). Every row in the status table gets a 🔍 button that checks that one container right now and tells you the answer on the spot — update available, already up to date, or what went wrong. It waits for the result instead of firing and forgetting, because a single container check is one registry HEAD request and there's no reason to make you guess.

  This matters most on the installs it was asked for. The existing "Check Updates" button starts a background thread and bounces you straight back to a page still showing the old numbers; the only feedback was the notification that follows, and on a host with no Telegram, no Discord and no webhook that notification goes nowhere at all. So you pressed a button and got, quite literally, nothing. Now you get an answer whether or not you have a channel configured.

  Two things behind the scenes worth knowing. A scoped check *merges* its result into `pending_updates.json` rather than overwriting the file — otherwise checking one container would have quietly wiped every other container's update badge and update button. And the Docksentry row is a special case: we filter our own container out of the normal check, so that button asks the registry about the running image directly instead of coming back empty and looking broken.

  On the "force refresh" idea that came with the request: there's nothing to force. Docksentry keeps no digest cache — every check goes to the registry fresh, and even the auth token is re-negotiated each time. What looks like a stale answer is just the check cadence (the default is once a day at 18:00) plus the fact that the table renders the last saved result, so reloading the page checks nothing. This button *is* the refresh.

### Fixed
- **The Web UI's check could run alongside an update and report a phantom one** ([#50](../../issues/50)). The Telegram `/check` path has skipped checks while an update is in flight since #26 — mid-recreate the container still reports its pre-pull digest, so a check catching that moment announces an update that was just applied seconds ago. The Web UI path never got that guard; it has it now.
- **Two clicks on "Check Updates" started two checks.** Both wrote `pending_updates.json`, so whichever finished second silently overwrote the other's result. A check now takes a lock that a second one won't queue behind — it's simply refused and says so.

### Docs
- The Web UI's update checking is described in the README for the first time; it was only ever documented for Telegram.

Tests: new `scripts/test_check_scoped.py` covers the scoped run and, above all, that an unrelated container's pending entry survives it.

## [1.53.3] - 2026-07-28

### Fixed
- **Podman recreate on cgroup-v2 hosts failed with `cannot set memory swappiness with cgroupv2`** ([#50](../../issues/50), @LeeNX). When replicating a container's resource limits on recreate we passed through `--memory-swappiness`, but `memory.swappiness` is a cgroup-v1-only control — it doesn't exist under cgroup v2, so crun/podman rejected the flag outright and the recreate died (the recovery net then rolled it back safely, so nothing was lost). Same class of bug as the earlier ulimit and namespace-mode podman fixes. We now check the daemon's cgroup version via `docker info` and skip `--memory-swappiness` on cgroup-v2 daemons, while cgroup-v1 docker hosts — where the flag is valid — still get it. New tests in `scripts/test_image_inherit.py`.

## [1.53.2] - 2026-07-25

### Fixed
- **Bogus "turned UNHEALTHY (health was: unhealthy)" alert** ([#2](../../issues/2), @famewolf). The monitor could fire a nonsensical unhealthy alert claiming the previous health was already "unhealthy". Two things caused it. First, a container that was already unhealthy when Docksentry built its baseline (rather than one we actually watched flip from healthy) got confirmed on the second pass and alerted — the confirm keyed off "was unhealthy last pass" instead of "we saw it flip". That contradicts the silent-baseline-on-restart principle, so now only a genuinely observed `healthy → unhealthy` transition alerts, and "health was:" can never say "unhealthy". Second, the container that triggered this was *stopped* — its logs were months old — and a stopped container's health field is just a frozen leftover from when it last ran, not a live signal. Health is now evaluated for running containers only; a stopped or exited container never produces a health event (its exit is already reported separately), and merely stopping isn't treated as a recovery. New tests in `scripts/test_monitor.py`.

## [1.53.1] - 2026-07-25

### Fixed
- **Crash loops were missed for containers that crash instantly** ([#2](../../issues/2), @NotRetarded's VPN-on-unsupported-kernel case). A container that dies the moment it starts spends almost all its time in restart-backoff — Docker reports it as `restarting`, Podman as `exited` between attempts — so a 60-second monitor sample rarely catches it `running`. The crash-restart detector required `status == "running"` at sample time, so it never saw the loop and nothing alerted. Detection now keys off the RestartCount increase alone, regardless of the sampled state, so a crash loop fires whether we catch it running, restarting or briefly exited, on both Docker and Podman. RestartCount only climbs when the restart *policy* kicks in (a manual `docker restart` doesn't bump it), so a rising count is always a real loop — and this needs **no healthcheck**, the count itself is the signal. A one-shot container that just exits once (count unchanged) still fires the plain "exited" as before. New tests in `scripts/test_monitor.py`.

## [1.53.0] - 2026-07-25

### Added
- **Per-container update policy** ([#2](../../issues/2), voted for by @NotRetarded and @famewolf). You can now cap how far an *auto-update* is allowed to jump on the semver ladder: `all` (every bump, the default and current behaviour), `minor` (apply minor and patch, but hold back majors) or `patch` (patch releases only). Set it per container with a `docksentry.policy=minor` label, or globally with `UPDATE_POLICY=minor` — the label wins where both are set. When an auto-update is held back you still get told a newer version is out (it stays in the "Updates Available" list, plus a one-line "⏸ held back by update policy" note), and `/update <name>` or the Bulk update button applies it immediately — an explicit action always overrides the policy. The bump level is worked out from the version info Docksentry already reads (the OCI `org.opencontainers.image.version` labels, falling back to a semver tag); anything it can't classify is allowed rather than silently skipped.

  To be clear about what this is *not*: it's a gate on the existing digest-based auto-update path, the same shape as the ask-before-major confirmation. It does **not** follow or switch semver tags — Docksentry still never rewrites `:1.2.3` to `:1.2.4`. Tag-following is a separate thing for another day.

### Fixed
- **Podman self-update/recreate failed with `invalid PID mode`** ([#49](../../issues/49), @LeeNX). Podman reports a container's default PID and UTS namespace mode as `"private"` where Docker reports an empty string, and `--pid private` / `--uts private` aren't valid `docker run` values — so the recreate crashed (and rolled back) on podman. We already skipped `"private"` for `--ipc`; PID and UTS had the same gap and now skip it too. Real values like `host` or `container:<id>` still carry over.

## [1.52.0] - 2026-07-25

### Fixed
- **A container that recovers by restarting no longer silences its monitoring for good** ([#2](../../issues/2)). The v1.51.0 debounce only ended an unhealthy episode on a direct `unhealthy → healthy` flip. But a container that recovers by restarting goes `unhealthy → starting → healthy` and never touches `healthy` coming straight off `unhealthy`, so the recovery alert never fired *and* the name got stuck in the "already alerted" set forever — which meant every future unhealthy alert for that container was suppressed silently. Now an episode ends the moment a container *leaves* the unhealthy state, wherever it lands, and that's the recovery signal. I also prune the pending/alerted state each pass so a container that vanishes and comes back starts with a clean slate instead of inheriting a stuck flag. New tests in `scripts/test_monitor.py` cover restart-recovery, the `unhealthy → starting → healthy` path, and the vanish-then-reappear case.
- **Empty remote digest no longer reads as a phantom update.** When a registry returns a 200 manifest with no `Docker-Content-Digest` header, the digest lookup hands back an empty string rather than `None`. The update check guarded only against `None`, so the empty string slipped through and looked like a digest that didn't match the local one — a false "update available". The check now treats any falsy digest (empty *or* missing) as a failed lookup, same as the self-update path already did. Test in `scripts/test_remote_version.py`.

### Changed
- **`DEBUG=true` is finally honored as an env var.** The README documented it and I'd told people to set it, but nothing actually read it — debug mode could only be turned on via `/debug` or the Web UI. It now seeds the initial debug state from the environment like every other persistent setting, and a later `/debug`/Web UI toggle still overrides and persists across restarts.

### Docs
- Corrected the Telegram command count (it was stuck at "14") and filled in the nine commands missing from the table, clarified the `AUTO_SELFUPDATE` vs `AUTO_UPDATE_ALL` / `docksentry.auto` distinction (self-update vs updating your other containers), and narrowed the Web-UI-persistence claim to the settings that are actually editable there.

## [1.51.0] - 2026-07-25

### Fixed
- **Self-update no longer bricks containers whose labels or env contain backticks or `$`** ([#49](../../issues/49), thanks @LeeNX). The helper that recreates Docksentry builds a `docker run` line and hands it to `sh -c`. I was wrapping args in double quotes, which don't protect backticks or `$` from the shell — so a Traefik label like `traefik.http.routers.x.rule=Host(`host.example.com`)` made `sh` try to run the hostname (`sh: host.example.com: not found`), the recreate failed, and the update rolled back and left the container dead. Every arg is now quoted with `shlex.quote` (single quotes, which are safe for backticks, `$`, spaces, the lot), so the label reaches Docker verbatim. This hit the current version too — worth updating for anyone running behind Traefik. (The `RLIMIT_NOFILE` line in Lee's log was a separate thing, [#48](../../issues/48), already fixed back in v1.47.4.)

### Changed
- **Flappy healthchecks don't spam anymore** ([#2](../../issues/2), @famewolf). A healthy→unhealthy flip no longer alerts on the spot — it waits one monitor pass. If the container is still unhealthy on the next pass, you get the "unhealthy" alert as before; if it's already back to healthy, you get nothing at all — no unhealthy, no recovery, complete silence. The motivating case is gluetun's ICMP-mismatch blip that clears itself within a minute. Exits, OOM kills and crash-restarts stay immediate — a death is a death. This isn't configurable, it's just the sensible default now. Tests in `scripts/test_monitor.py`.

## [1.50.0] - 2026-07-23

### Added
- **Send-only Telegram mode** (`TELEGRAM_POLLING=false`) for sharing one bot with another app ([#2](../../issues/2), @famewolf). If Docksentry and, say, Home Assistant both poll the same bot token, Telegram lets only one of them win `getUpdates` and the other logs `Conflict: terminated by other getUpdates request` on a loop. Send-only mode makes Docksentry stop polling entirely — no `getUpdates`, no startup flush, no `setMyCommands` (which is global per bot and would otherwise clobber the other app's command list) — while still sending notifications, since `sendMessage` never conflicts. Let the other app own the interactive side; drive Docksentry from the Web UI. Interactive Telegram commands are off in this mode by design. Env-only (it's a wiring decision, not a runtime toggle); default stays `true`, so nothing changes for existing setups. Test: `scripts/test_send_only.py`.

  This does **not** let two apps both run *interactive* commands on one token — that's a hard Telegram limit (one command-poller per token), not something any tool can work around.

## [1.49.0] - 2026-07-23

### Added
First round of monitoring follow-ups from the [#2](../../issues/2) discussion (thanks @NotRetarded):

- **Crash/exit/OOM/unhealthy notifications now carry the last 10 log lines.** The first question after "💥 X exited with code 137" is always *why* — the same log tail that update failures already attach is now on monitor events too. Recovery messages stay clean.
- **OOM notifications name the culprit, not just the victim.** @NotRetarded's case: on an 8 GB host, one container (Sencho) quietly ate the memory and a *different* one (UniFi OS Server) got OOM-killed for it — the alert named the victim, leaving the actual cause invisible. An OOM event now takes a single `docker stats --no-stream` snapshot *at event time* and lists the top memory consumers: `Top memory at event time: sencho 5.1GiB · unifi 1.9GiB · postgres 412MiB`. One stats call when it fires, zero idle polling.
- **`/events` Telegram command** — parity with the Web UI's Container Events section: the recent persisted events, newest first, rendered through the same message keys.

New i18n keys `monitor_top_memory`, `events_header`, `events_empty`, `help_events`, `help_detail_events` (16 languages). Tests: `scripts/test_monitor.py` (36 checks); log-tail attachment verified end-to-end.

## [1.48.1] - 2026-07-23

### Added
- **Persistent event history for the monitor** ([#2](../../issues/2) follow-up). v1.48.0's alerts had no memory: an OOM kill at 3 a.m. lived in Telegram scrollback, or — headless without notification channels — only in the container log. Monitor events (health flips, non-zero exits, OOM kills, crash-restarts) are now written to `/data/monitor_events.json` (atomic, capped at the latest 200) and shown in a **Container Events** section on the Web UI's History page, rendered through the same i18n keys as the live notifications so both channels tell the same story. A failed write never breaks monitoring. New i18n keys `web_events`, `web_events_empty`, `web_events_empty_hint` (16 languages). Tests: `scripts/test_monitor.py` extended to 26 checks; verified end-to-end against a live container.

## [1.48.0] - 2026-07-23

### Added
- **Container state monitoring** ([#2](../../issues/2), @NotRetarded: "why keep this app strictly as just an updating tool?"). Docksentry now watches for state *transitions* between checks and notifies on:
  - a healthcheck turning **unhealthy** — and the recovery back to healthy
  - a container **exiting with a non-zero code** (zero exits stay silent: one-shot jobs end normally all day)
  - an **OOM kill** (with a nudge to raise the memory limit)
  - a **crash + auto-restart** (RestartCount increased while the container kept running — the case a plain exit check misses on `restart: always` fleets)

  Guard rails, in order of importance: transitions only (an unhealthy container fires once, not every pass); the whole pass is skipped while any update flow runs (containers bounce legitimately during updates — the baseline is rebuilt afterwards, so recreates never read as crashes); per-(container, kind) cooldown of 30 minutes against flapping; first pass after boot is a silent baseline; quiet-hours and maintenance mode are honored like every other auto-notification.

  Config: `MONITOR` (default `true`), `MONITOR_INTERVAL` (default 60s, min 15). Per-container opt-out via the label family: `docksentry.monitor=false`. Notifications fan out to Telegram, Discord and webhooks like everything else; 5 new i18n keys in 16 languages. Runs on the scheduler's existing loop — no new thread, one `docker ps -a` + one batch inspect per pass. Disk-threshold monitoring (also on the wishlist) already existed: `DISK_WARN_PERCENT` / `DISK_WARN_AUTO_CLEANUP`. Memory-usage *thresholds* are deliberately deferred — they'd need `docker stats` polling; the OOM-kill notification covers the acute failure case without it. Test: `scripts/test_monitor.py` (21 checks), verified end-to-end against a live container.

## [1.47.4] - 2026-07-23

### Fixed
- **Podman standalone recreates failed on ulimits** ([#48](../../issues/48), @LeeNX — first bug caught end-to-end by the new debug log). Podman's inspect reports rlimit names in POSIX form (`RLIMIT_NOFILE`); the `--ulimit` flag only accepts the short form (`nofile`), so every podman standalone recreate of a container with ulimits failed with `invalid ulimit type: RLIMIT_NOFILE` — and rolled back (the recovery net held; the container kept running on the old image). Names are now normalized; Docker's already-short names pass through untouched.
- **Legend, corrected interpretation** ([#46](../../issues/46)): the "double red square" next to Stop was the `🟥` emoji *inside the translated label* sitting next to the styled key — same story for `🔁` next to Restart ("the old icon"). The legend now strips leading emoji from label words (the styled key already is the icon; actual text in all 16 languages survives untouched), and the Stop key is solid red again, matching the real button — v1.47.3's outlined variant was a misreading of the feedback.

## [1.47.3] - 2026-07-23

### Fixed
- **Theme button and UI-mode switcher are vertically aligned** ([#46](../../issues/46), @LeeNX's QA-friend round 😄). The mode switcher sat inside a `display:inline` form and participated in baseline layout, landing a few pixels below its flex-child sibling. Now `inline-flex`.

## [1.47.2] - 2026-07-23

### Changed
Web UI polish from @LeeNX's feedback round in [#46](../../issues/46):
- **Restart has its own icon** — a single circular arrow, drawn in-house in the same stroke style; Update keeps the two-arrow glyph. The two actions no longer share a symbol with colour as the only differentiator.
- **The legend's Stop key is outlined red instead of filled** — it mirrored the real button's solid red and looked heavy in a key row. The actual Stop button is unchanged (solid red is right for a destructive action).
- **Consistent help cursor** on every hover-explained element — the 🏷 marker had it, the protect/pinned/group badges didn't.

## [1.47.1] - 2026-07-23

### Fixed
- **The headless self-update path is no longer silent** ([#43](../../issues/43), @LeeNX). On installs without Telegram, the Web UI's self-update button runs the auto-selfupdate path — whose failure exits all returned a bare `False` with zero output. A failing image pull was therefore indistinguishable from "already up to date": the button did nothing and said nothing. Every exit now prints its reason to the container log (skipped — update running / can't inspect own container / pull failed with the actual ref and stderr / up to date with image ID / update found), and the Web UI logs the outcome of the button press. On headless installs the container log is the only feedback channel there is.

### Added
- **Startup log states version and debug state** (suggested by @LeeNX in #43): `Docksentry started. (v1.47.1, debug OFF)` — the container log is often the only thing a headless user can paste, and "which version even is this?" was the first question every time.

## [1.47.0] - 2026-07-22

### Fixed
Both reports from [#46](../../issues/46) plus the version-display mystery from [#43](../../issues/43) (@LeeNX):

- **Stale version numbers after self-updates — the "sticky label" bug.** Pre-v1.43.0 recreates pinned `org.opencontainers.image.version=<old>` explicitly onto containers. The v1.43.0 inherited-filter compares against the old image's own label — a stale value *differs* from it, so it masqueraded as a user override and stuck to the container through every future update: `/status docksentry` and the detail view kept reporting the old version forever. Two-part fix:
  - `org.opencontainers.*` labels are now **never** replicated on recreate — they're image metadata, the new image always supplies its own. (Root fix, prevents new stickiness.)
  - `/status` and the Web UI now read the version from the **running image's** label instead of the container's — the image can't lie about itself, the container can. This heals already-sticky containers immediately, no re-recreate needed. The Web UI table also keys by the running image **ID** rather than the tag, so a container still on an older image no longer borrows the tag's newer version.
- **`docksentry.protect` label was invisible to the Web UI.** Label-protected containers still showed the Stop button (the stop itself was refused — the backend has been label-aware since v1.32.0 — but the UI lied). Stop-button visibility, a new 🛡 badge (with 🏷 when label-controlled), and the detail-view protect checkbox (disabled under label control) now all use the effective state.
- **Icon legend fixes:** keys now carry the same styling as the real buttons (Update shows its active colour, Stop its red), a Restart key was added (same glyph as Update — colour is the differentiator, which the legend now actually shows), tooltips on every key, and a 🏷 key explaining the label marker.

Note: the "no Stop buttons at all" observation on the fresh Podman host is **not** a bug — Stop is an advanced-mode-only control (Settings → advanced UI); the other host had advanced mode enabled.

## [1.46.1] - 2026-07-22

### Added
- **Web UI shows when a `docksentry.*` label is in charge** ([#42](../../issues/42) follow-up, @LeeNX: "show the user that labels are the authoritative"). Wherever a label overrides the stored toggle, the status table now displays a 🏷 marker with the tooltip "Controlled by a docksentry.* label in the compose file — remove the label there to change this", and the corresponding Pin/Auto toggle buttons are **disabled** — a click couldn't override the label anyway, and an active-looking button that silently does nothing would be worse than an honest disabled one. The pinned badge also reflects label-pins now (previously it only knew the stored list). New i18n key `web_label_authoritative` (16 languages).

## [1.46.0] - 2026-07-20

### Added
- **Complete `docksentry.*` label family — GitOps-style per-container control from the compose file** ([#42](../../issues/42), @LeeNX). v1.32.0 added `enable`/`exclude`/`protect`; this completes the set so every per-container toggle has a declarative twin:
  - `docksentry.pin=true` — freeze a container (twin of `/pin`): never listed, never updated.
  - `docksentry.auto=true|false` — opt in to / out of auto-updates. `=false` keeps a container manual **even under `AUTO_UPDATE_ALL=true`** (it still shows up in the manual "updates available" notification); `=true` opts in without the Web-UI toggle.
  - `docksentry.ask-major=true|false` — require / skip the major-version confirmation gate for auto-updates.
  - `docksentry.trust-running=true` — accept "running" as healthy after updates (#9 behaviour).

  Precedence everywhere: label wins over the stored bot/Web-UI toggle; no label → toggle applies, exactly as before. The Web UI status table now shows the *effective* Pin/Auto state (label included) instead of just the stored toggle. README documents the full family. Tests: `scripts/test_labels.py` extended (34 checks).

## [1.45.0] - 2026-07-20

### Fixed
Systematic pass over every docker-mutating operation, prompted by @famewolf's "imagine /cleanup would also want to respect the queue — not sure if there are other fringe cases" ([#2](../../issues/2)). v1.44.x serialized the update flows; this closes the rest:

- **Cleanup can no longer delete an image an update just pulled.** `docker image prune -a` filters on image *creation* time, so an image built upstream days ago but pulled seconds ago was fair game — pruning during an update's pull→recreate window would have removed the image the update was about to run (recoverable via re-pull, but a real reliability hole; with a registry outage, a failed update). All four cleanup triggers — Telegram `/cleanup`, Web UI button, disk-warning auto-cleanup, post-auto-update cleanup — now take the shared update mutex via `cleanup_guarded()`: manual triggers report "⏳ Updates in progress — cleanup skipped", automatic ones skip silently and retry on their own cadence. Conversely, updates can't start mid-prune.
- **Stop/start/restart are refused while an update runs.** A user `/stop` landing during the post-update health wait read as "unhealthy" and triggered a bogus rollback of a perfectly good update; a restart could hit a container mid stop/rename/recreate. All lifecycle entry points (Telegram commands, bulk actions, Web UI buttons) funnel through `_lifecycle_action`, which now refuses with an honest message while any update flow holds the mutex. The update machinery itself bypasses this method, so its own stop/restart steps are never blocked.
- A self-update queued while cleanup holds the mutex runs right after cleanup finishes.

New i18n keys `cleanup_busy` / `lifecycle_busy` (16 languages). Test: `scripts/test_op_coordination.py`.

## [1.44.1] - 2026-07-19

### Fixed
- **A queued self-update is cancelled when the batch flow itself crashes** ([#2](../../issues/2) follow-up, @famewolf asked exactly this: "what happens if container updates FAIL with a queued selfupdate afterward?"). Two failure classes, two answers:
  - *Per-container failures* (rollback, "left in place") are normal batch results — reported first, and the queued self-update still runs afterwards; a Docksentry restart can't make an already-handled container worse.
  - *A flow-level crash* (exception in the batch machinery, state unknown, error not yet reported) now **cancels** the queued self-update with an honest message ("run /selfupdate again when ready") instead of restarting on top of an unreported error — the restart could have killed the process before the error message ever went out. New i18n key `selfupdate_queue_cancelled` (16 languages).

## [1.44.0] - 2026-07-19

### Fixed
- **`/selfupdate` no longer kills a running container-update batch** ([#2](../../issues/2), @famewolf). A self-update issued while updates were in progress restarted Docksentry mid-batch: the batch died, its updates were re-offered after the restart — and had the restart landed during a stop/rename/recreate, it would have left a renamed `_old` orphan (a #43-style brick). Self-updates now coordinate with every other flow through the shared update mutex:
  - `/selfupdate` during a batch is **queued** and announced ("self-update queued, it will start automatically once they finish"), then runs exactly once when the batch completes.
  - A running self-update **holds the lock through its own pull+swap**, so no batch can start in the final seconds before the restart.
  - `AUTO_SELFUPDATE` skips its cycle when any update flow holds the lock (next tick retries) instead of killing a manual batch.
  - **Web UI single-container updates now take the same mutex** — they used to run fully uncoordinated, so they could collide with a running batch or a self-update swap.

  New i18n keys `selfupdate_queued` / `selfupdate_dequeued` (16 languages). Test: `scripts/test_selfupdate_queue.py`.

## [1.43.2] - 2026-07-18

### Fixed
- **Web UI "Check updates" no longer pushes the debug log to Telegram** ([#35](../../issues/35) feedback, @NotRetarded). With `DEBUG=true`, a check run sends its full debug log to the requester — right for the Telegram `/check` command, where the requester is sitting in the chat, but a Web UI click sprayed the same wall of log chunks into Telegram too. Web-triggered checks now keep the log where the Web UI already shows it (the `/logs` page); "updates found" notifications are unaffected. Scheduled checks were never affected.

## [1.43.1] - 2026-07-18

### Fixed
Cross-tool audit (how do Watchtower / Diun / What's-up-Docker handle updates?) — three findings in the image-reference layer:

- **Digest-pinned images (`repo@sha256:...`) were parsed as garbage.** The parser split at the digest's colon, producing repository `library/nginx@sha256` with the hex digest as "tag" — the registry call failed every check cycle, so a deliberately pinned container looked like a permanently unreachable registry (plus one wasted network call per cycle). A digest pin is the user explicitly freezing an image; it now parses as "not update-checkable" and is skipped with an honest `pinned by digest` debug reason, matching how the established tools treat pins.
- **Bare image IDs (`sha256:...`) were queried on Docker Hub as `library/sha256`.** The guard for them sat *after* the tag split, which had already eaten the digest as a ":tag" — dead code since its introduction. Guard moved before the split.
- **Multi-arch version metadata was always read from the linux/amd64 manifest.** On ARM hosts (Raspberry Pi, ARM NAS) the "new version" shown in update notifications came from the amd64 image's config. Update *detection* was never affected (it compares the platform-independent index digest). The platform manifest is now chosen by the daemon's own os/arch (`docker version`, cached; asking the daemon rather than Python's `platform` matters for socket-proxy setups where the daemon lives elsewhere), falling back to linux/amd64.

Test: `scripts/test_image_ref.py`.

## [1.43.0] - 2026-07-18

### Fixed
Proactive audit following [#35](../../issues/35): v1.42.0 fixed `Env`, but the same trap applied to every other Dockerfile instruction that lands in a container's inspect `Config`. A container's Config is the image's defaults *merged with* the user's overrides, with no marker saying which is which — so replicating it wholesale pins the OLD image's values onto the NEW one. All of these are now filtered against the old image's own Config, and only genuine user overrides are replicated:

- **Labels.** Image `LABEL`s merged in indistinguishably from user/compose ones, so an updated container kept the old image's `org.opencontainers.image.version` — precisely what the container detail view reports as "what version is this really?" ([#36](../../issues/36)). An updated container therefore kept claiming its previous version. Compose and user labels are unaffected.
- **Healthcheck.** A code comment claimed image-default `HEALTHCHECK`s never reach `inspect.Config.Healthcheck`; that was wrong, and verified wrong against a live daemon. Pinning the old one is self-reinforcing: when a new image ships a *repaired* healthcheck, the stale one keeps failing, the post-update health gate reads that as a bad update and rolls back — so the release that fixes the check can never be installed.
- **User.** Images that re-harden across versions (root → non-root, or a changed uid) were forced back onto their predecessor's user, causing permission errors.
- **WorkingDir** and **StopSignal.** A relocated app directory or a changed stop signal (systemd-based images use `SIGRTMIN+3`) was overridden by the old image's value.
- **Self-update path.** `telegram_bot._do_selfupdate` built its run arguments without the v1.42.0 env filter, so Docksentry still pinned its own stale config when updating itself. It now uses the same filter as every other path.

Falls back to the previous replicate-everything behaviour whenever the old image can no longer be inspected. Test: `scripts/test_image_inherit.py` (renamed from `test_env_inherit.py`), which additionally verifies against a live Docker daemon that these fields really are image-inherited.

## [1.42.0] - 2026-07-18

### Fixed
- **Stale environment variables pinned onto the new image** ([#35](../../issues/35), @NotRetarded). A container's `Config.Env` is the image's own `ENV` *merged with* the user's `-e` overrides, and Docker records no distinction between the two. The standalone recreate replicated all of it, which handed the NEW image the OLD image's defaults on the command line. Images that carry configuration in their own `ENV` — unifi-os-server ships its version as `ENV APP_VERSION=5.1.21` — therefore kept reporting the old version after a *successful* update: the new image really was running (image IDs matched, which is why this looked so contradictory), it just received the stale value. Docksentry now reads the old image's own `ENV` and replicates only entries that differ from it, so genuine user overrides survive while inherited defaults come fresh from the new image. Falls back to the previous replicate-everything behaviour when the old image can't be inspected. This mirrors what v1.19.0 already did for `Cmd`/`Entrypoint`; `Env` was the remaining gap. Test: `scripts/test_env_inherit.py`.

### Changed
- **Post-update image verification now covers the standalone path too.** The "did the container actually pick up the pulled image?" check added in v1.23.7 only ran for Compose stacks; the standalone recreate reported `OK` on the strength of a passing health check alone. Extracted into a shared `_verify_running_image()` and applied to both paths — on the standalone path it runs *before* the old container is dropped, so a mismatch rolls back instead of being reported as success.

## [1.41.0] - 2026-07-07

### Added
- **Self-update failure diagnostics** ([#43](../../issues/43), @LeeNX). When a self-update recreate fails (seen repeatedly on rootless Podman) the reason used to vanish — the helper container runs `--rm`, so its stderr was gone before anyone could read it. The helper now also mounts Docksentry's `/data` host directory (resolved from our own inspect Mounts, same trick as the socket in v1.35.0) and redirects its whole swap-script output there. On the next boot, if the recreate rolled back, Docksentry reports the captured helper output so the actual `docker run` rejection is finally visible instead of a silent brick. On success the log is consumed silently. New i18n key `selfupdate_recreate_failed`. Test: `scripts/test_selfupdate_diag.py`. (This is the diagnostic that should finally reveal *why* the Podman recreate fails — the v1.38.2 recovery-net already made it survivable.)

## [1.40.1] - 2026-07-03

### Fixed
- **Compose recreates now pass `--project-directory`** when the stack was originally started from a different directory than the compose file's location (found via static-analysis audit: `_update_compose` accepted the `working_dir` from the `com.docker.compose.project.working_dir` label but never used it). Without it, compose resolves `.env` interpolation and `env_file:` paths against the compose file's directory instead of the original project directory — so a recreate could interpolate `${VARS}` differently than the original `up` did. Same recreate-fidelity class as [#27](../../issues/27)/[#29](../../issues/29).

### Changed
- **New permanent contract linter** (`scripts/lint_contracts.py`, wired into the pre-commit check): AST-based, flags functions whose return statements mix literal shapes (tuple vs scalar) and same-named methods across classes with conflicting return contracts — the exact class of the v1.37.1 `_wait_healthy` cascade crash. Verified it catches that historical bug. Plus a lint pass across the codebase: unused imports/variables removed, one `zip()` hardened with `strict=True`.

## [1.40.0] - 2026-07-03

### Added
- **`/checkimages`** ([#2](../../issues/2), @famewolf) — dry-run counterpart to `/cleanup`. Reports how much disk space `/cleanup` would reclaim right now (unused images + build cache, from `docker system df`) and calls out the AUTO_CLEANUP status. Useful when you're not on auto-cleanup and want to check on demand instead of waiting for the disk warning to fire.

### Fixed
- **`/check` no longer surfaced the Docksentry-update hint** ([#2](../../issues/2), @famewolf). Since v1.17.4 the hint was gated on Docksentry appearing in the results list — but `get_running_containers` explicitly filters ourselves out ("Skipped (self)"; a normal-flow container update on ourselves can't work — PID 1 can't replace its own container). The hint therefore never fired since the self-filter existed. `/check` now consults the checker directly (`has_selfupdate_available()` — a digest-only registry compare, no pull) so a newer Docksentry image on the registry surfaces the "run /selfupdate" hint again. Test: `scripts/test_checkimages.py`.

## [1.39.1] - 2026-07-03

### Fixed
- **Discord and generic webhook notifications could silently drop on a transient network blip** (proactive audit follow-up to the Telegram retry in v1.38.1). `_discord_post` and `_webhook_send` did a single `urlopen` in a `try/except` — exactly the same structural gap that ate NotRetarded's Telegram notification right after a self-update restart, just on the other channels. Extracted a shared `_post_json_with_retry`: 3 attempts with 2s/4s backoff on transient network failures (timeout / connection error), no retry on HTTP status codes (those are the server's word, not a transient blip). Test: `scripts/test_notifier_retry.py`. Note: for a **generic webhook** pointing at an automation (Home Assistant, ntfy, custom script), the tiny edge case where a delivered send timed out on read can yield a duplicate; prefer idempotent handlers — noted in the README.

## [1.39.0] - 2026-07-03

### Changed
- **Disk-space warning is now actionable** ([#2](../../issues/2), @famewolf). A bare `⚠️ Disk usage at 88% — 5.3 GB free.` used to get lost in the noise (famewolf's 215 GB-of-orphaned-images lesson: the warning did fire, he just glossed over it while wrestling with LLM disk pressure). The warning now tells you **how much space `/cleanup` could reclaim** (from `docker system df` — unused images / build cache) and, if `DISK_WARN_AUTO_CLEANUP` is OFF, a one-line nudge to turn it on. So the message becomes "🧹 21.3 GB reclaimable via `/cleanup` — auto-cleanup is OFF, set `DISK_WARN_AUTO_CLEANUP=true` to reclaim automatically next time" — hard to ignore. Test: `scripts/test_disk_reclaim.py`.

## [1.38.2] - 2026-07-02

### Fixed
- **A failed self-update recreate could leave Docksentry dead** ([#43](../../issues/43), @LeeNX). The helper ran `stop && rename→_old && run && rm _old`; if the `docker run` recreate failed (a flag the runtime rejects — seen on rootless Podman), the `&&` chain stopped with the container renamed to `_old` and stopped, and **no new container** — Docksentry was down with no recovery. The recreate is now guarded: on failure it removes any partial new container, renames `_old` back and starts it, so the bot survives on the previous version. `rm _old` runs only after a successful run, so a failed cleanup can't roll back a good update. Extracted into `_build_selfupdate_script`; test `scripts/test_selfupdate_recovery.py` runs the real shell against a fake `docker`. (Root cause of *why* the Podman recreate fails is still being gathered — the recovery net makes it non-fatal either way.)

## [1.38.1] - 2026-06-29

### Fixed
- **Transient Telegram API timeouts silently dropped notifications** ([#2](../../issues/2), @NotRetarded). Right after a self-update restart the network can still be settling; a single `urlopen timed out` on the Telegram API meant the update/self-update notification was lost (while Discord/webhook, hitting the network a moment later, got through). `api_call` now retries transient network failures (timeout / connection error) up to 3× with a short backoff (2s, 4s). The long-poll (`getUpdates`) is exempt — its timeouts are expected and it loops anyway — and HTTP 4xx bodies are still returned unretried. Trade-off: a read-timeout after Telegram already accepted a send could produce a duplicate, which is preferable to a dropped notification. Test: `scripts/test_api_retry.py`.

## [1.38.0] - 2026-06-29

### Added
- **Installable Web UI (PWA)**. Added a web app manifest + icon so Docksentry can be added to a phone's home screen and run standalone — handy for the touch-first usage the Web UI was just polished for ([#2](../../issues/2)). Served via the existing `/static` route; `theme-color` / apple-touch meta included. No new dependencies.
- **Version badge in Discord / webhook / e-mail "updates available"** ([#44](../../issues/44), @LeeNX). The `🔖 v_old → v_new` info added to the Telegram notification in v1.36.0 now also rides along on the other channels: Discord embeds show it per container, the generic webhook payload carries `old_version` / `new_version` fields, and the e-mail summary includes it. Empty when the image has no `org.opencontainers.image.version` label.

## [1.37.1] - 2026-06-29

### Fixed
- **Restart-dependents cascade crashed with "cannot unpack non-iterable bool object"** ([#2](../../issues/2), @famewolf). A group head (e.g. gluetun) updated fine, then kicking its dependents threw — the head showed a second, bogus ❌ line. Cause: a stray bot-local `_wait_healthy` that returned a **bool** shadowed the checker's canonical 3-tuple `(outcome, state, health)` version, so `outcome, _, _ = self._wait_healthy(...)` tried to unpack a bool. The cascade now calls the checker's 3-tuple `_wait_healthy`, and the duplicate bool method is removed. The regression test (`test_dependents_recreate.py`) was masking it by stubbing a 3-tuple onto the bot; it now stubs the checker, exercising the real path.

## [1.37.0] - 2026-06-29

### Added
- **Dedicated "Auto" column in the Web UI container table** ([#2](../../issues/2), @NotRetarded). The auto-update flag was a name-cell badge that wrapped under long container names; it now has its own column, so the name stays clean and the auto state is easy to scan.
- **Touch-friendly icon legend** under the container table ([#2](../../issues/2), @NotRetarded). The action buttons have hover tooltips, but those don't exist on touch devices — a small legend now spells out what each icon does (update / pin / auto / major-confirm / stop).

### Changed
- **Web UI CSS/JS moved out of Python string literals into real static files** (`app/static/app.css`, `app/static/app.js`), served via a `/static` route with cache-busting on version. No new dependencies — still pure stdlib. This kills the class of bug where a stray character in the embedded `_BASE_JS` string broke the whole UI (v1.22.0, [#40](../../issues/40)) and makes the ~1300 lines of front-end code lintable/editable as actual CSS/JS. Purely internal — no behaviour change.

## [1.36.0] - 2026-06-29

### Added
- **Version in the "Updates Available" notification** ([#44](../../issues/44), @LeeNX) — each container line now shows `🔖 v_old → v_new` (or the current version when only one side is known). The old version comes from the local image's `org.opencontainers.image.version` label (falling back to a SemVer tag); the new version is read from the **remote image's OCI config blob** before anything is pulled, via the registry's Bearer-auth manifest+blob flow (multi-arch indexes resolve to the linux/amd64 config). Best-effort: images without the label simply show no badge, and a registry hiccup never affects the check. The remote lookup only runs for the handful of containers that actually have a pending update, so it adds no per-check overhead. (Update *results* already showed this arrow since v1.18.x; this brings it to the pre-update notification too.)

## [1.35.0] - 2026-06-29

### Added
- **`AUTO_UPDATE_ALL`** ([#45](../../issues/45), @NotRetarded) — global, Watchtower-style auto-update of **every** checked container, not just the per-container opt-ins. Off by default; the per-container auto-update list still applies when it's off. Pinned / excluded / `docksentry.exclude` containers are skipped either way, and the auto-update window + ask-before-major gates still apply. For users who expected `AUTO_SELFUPDATE` to cover all containers — that flag only ever covered Docksentry itself.

### Fixed
- **Self-update on rootless Podman / custom socket paths didn't recreate the container** ([#43](../../issues/43), @LeeNX). The self-update helper hardcoded `-v /var/run/docker.sock:/var/run/docker.sock`, but on rootless Podman the real host socket lives elsewhere (e.g. `/run/user/1002/podman/podman.sock` mapped to `/var/run/docker.sock` inside the container). The helper got an empty socket, so its stop/rename/run swap silently no-op'd — the new image was pulled but never loaded. The helper now mounts the **same host socket Docksentry itself uses**, resolved from its own inspect Mounts (honouring `DOCKER_HOST`).

### Changed
- **Clearer date labels in self-update messages** ([#44](../../issues/44), @LeeNX). `🗓️ Current version: <date>` was a *date* mislabelled as a version, and `🗓️ New: … | Old: …` ran backwards versus every other line. Both now read `🗓️ Image date: …` with the consistent `old → new` direction. (The meaningful version line is the `🔖 v_old → v_new` arrow added in v1.34.0.)

## [1.34.1] - 2026-06-28

### Fixed
- **Recalled commands (↑ in Telegram) were silently ignored** ([#15](../../issues/15), @famewolf). Pressing the up-arrow in Telegram — especially Desktop — *edits* your last message instead of sending a new one, so a recalled `/command` arrives as an `edited_message`. Docksentry only subscribed to and processed `message` updates, so the edit never appeared (not even in the channel) and nothing happened; you had to retype the command. Docksentry now also handles `edited_message`, gated to *recent* edits (≤120s) so an old message edited for unrelated reasons can't silently re-run a command.

## [1.34.0] - 2026-06-28

### Added
- **Self-update messages now show the version** (`🔖 v1.33.1 → v1.34.0`). Previously the message only carried the build date — useless when several releases land the same day — and the opaque image hashes. The new line reads the target image's `org.opencontainers.image.version` label (stamped since v1.20.0) against the running version, on both the manual `/selfupdate` and the auto-self-update notifications. Omitted gracefully for pre-label images. New i18n key `selfupdate_versions` across all 16 languages.

## [1.33.1] - 2026-06-28

### Fixed
- **Self-detection failed where `$HOSTNAME` isn't an inspect-resolvable reference** ([#41](../../issues/41), @NotRetarded). On some hosts — confirmed on QNAP Container Station — `$HOSTNAME` is an ID-looking string that `docker inspect` reports as `no such object`. Every self-detection path resolved the running container by inspecting `$HOSTNAME` directly, so all of them silently failed, with two consequences: Docksentry checked (and could try to update) **itself** through the regular flow instead of filtering itself out, and the self-update paths couldn't identify their own container (so `AUTO_SELFUPDATE` never actually self-updated — users worked around it with external tools).

  Self-resolution is now centralised in `UpdateChecker.resolve_own_id()` / `inspect_self()`, which first inspects by `$HOSTNAME`/`/etc/hostname` (the normal fast path, unchanged on standard Docker) and, when that fails, **falls back to scanning running containers for one whose `Config.Hostname` matches `$HOSTNAME`**. All four call sites — `get_running_containers` self-filter, `check_selfupdate_auto`, manual `/selfupdate`, and the `/check` self-update badge — now route through it. Test: `scripts/test_self_detection.py`.

## [1.33.0] - 2026-06-28

### Added
- **Container labels for GitOps-style config** ([#42](../../issues/42), @LeeNX). Docksentry now reads a few `docksentry.*` labels straight off your containers, so you can keep the config in your compose file. A label, when present, **overrides** the equivalent bot/Web-UI toggle:
  - `docksentry.enable=false` / `docksentry.exclude=true` — take the container out of Docksentry's update scope (same effect as `/pin`).
  - `docksentry.protect=true` — refuse `/stop` for the container ([#38](../../issues/38)-style protection); `docksentry.protect=false` force-unprotects, overriding the toggle.

  Booleans accept `true`/`1`/`yes`/`on` (case-insensitive). Label lookups are best-effort — an inspect failure falls back to the stored toggle and never silently unprotects. More label-driven settings can follow. Test: `scripts/test_labels.py`.
- **`-?` per-command help alias** ([#15](../../issues/15), @LeeNX). Append `-?` to any command for its detailed help — `/protect -?` is exactly `/help protect`, routed through the same help code path.

## [1.32.1] - 2026-06-27

### Fixed
- **Self-update was always mislabelled as an "external stop signal (SIGTERM)" on the next boot** ([#2](../../issues/2)). The marker that tells the freshly-booted process "this restart was my own self-update" was never actually written: `_do_selfupdate` read the marker path off its local `config` variable — which is the **docker-inspect dict** (needed for the recreate), not the app `Config` object — so the write raised `AttributeError`, got swallowed by its best-effort `try/except`, and the error only ever printed inside the old container that was about to be deleted. Affected **both** the manual `/selfupdate` and the scheduled auto-self-update path, on every self-update since the marker was introduced in v1.26.2. The marker write now correctly uses `self.config.selfupdate_marker_file` (extracted into `_write_selfupdate_marker`), so a self-update no longer prints the misleading "restarted after an external stop — Docksentry did not restart itself" line. Regression test added in `scripts/test_selfupdate_marker.py` (the previous test only covered the boot-side *read* logic, never the write — which is why this slipped through).

## [1.32.0] - 2026-06-27

### Changed
- **Unified the manual and scheduled update paths onto a single engine** ([#2](../../issues/2), @famewolf). The per-container update loop — group-order sort, inter-member wait, the group-abort gate, the netns-owner-by-name snapshot, `update_container`, the restart-dependents cascade, notifier results and the per-container cooldown — now lives once in `_process_update_batch`. Both the manual path (`run_updates` / "Update all" / `/update`) and the scheduled-auto path (`handle_autoupdates`) call it; each keeps only its own scaffolding (candidate selection, mutex handling, message framing, pending-file bookkeeping). This removes the recurring class of bugs where the two paths drifted and one got a fix the other didn't.

  Two behaviours are now consistent across both paths as a result:
  - **Head-rollback dependents kick** — when a group head fails and rolls back, its dependents (whose network namespace was torn down) are re-attached. Previously auto-only ([#27](../../issues/27)); the manual path now does it too.
  - **No double-touch on the success cascade** — dependents that are themselves part of the same batch self-heal via their own update instead of also being kicked; only out-of-batch sidecars get the explicit restart. Previously the auto path kicked all of them.

  The one legitimately path-specific behaviour is preserved via a flag: the ask-before-major confirmation gate runs only on the auto path — tapping "Update all" / typing `/update` is itself the explicit go-ahead, majors included.

## [1.31.0] - 2026-06-26

### Added
- **Native e-mail / SMTP notifications** ([#2](../../issues/2)). A real e-mail channel alongside Telegram, Discord and generic webhooks — for "updates available", update results, and other notifications. Configure with `SMTP_HOST` + `SMTP_FROM` + `SMTP_TO` (plus optional `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS`). `SMTP_TLS` selects the transport: `starttls` (default, port 587), `ssl` (implicit TLS, 465), or `none` (plain/relay). `SMTP_TO` accepts a comma-separated list; the `BOT_LABEL` prefix is applied to the subject so multi-host setups are distinguishable. Built on the Python standard library (`smtplib`) — no new dependency — and respects quiet hours / maintenance like the other channels. Sending is best-effort (failures are logged, never crash an update). Test: `scripts/test_smtp.py`.

  This also makes the e-mail channel that earlier docs implied actually exist — it does now.

## [1.30.0] - 2026-06-26

### Added
- **`/check` and `/update` now take a name or glob** ([#40](../../issues/40), @LeeNX) — completing the glob support from v1.29.0:
  - **`/check <name|glob>`** — scope a check to selected containers, e.g. `/check gluetun-*`; shows the "Updates Available" prompt for matches, or "up to date" if none.
  - **`/update <name|glob>`** — new command: checks, then updates only the matching containers that actually have a pending update, e.g. `/update gluetun-*`. Runs through the same path as the Update-all button (group order, dependents cascade, health-check + rollback, mutex). Typing the pattern is the go-ahead; use `/check <glob>` first to preview. Bare `/update` shows usage.

  Plain `/check` (no arg) is unchanged (full scan). Shared `_select_containers` resolver (glob → all matches, plain → single). English + German translated; other languages fall back to English.

## [1.29.0] - 2026-06-26

### Added
- **Glob wildcard patterns for `<name>` commands** ([#40](../../issues/40), @LeeNX). When the argument contains `*`, `?` or `[...]`, Docksentry matches *all* containers (case-insensitively, running + stopped) instead of resolving one — handy for "address a bunch of similar containers at once". Supported now:
  - **`/status <glob>`** — compact one-line-per-match overview, e.g. `/status ctf-*-even` (read-only).
  - **`/start` / `/stop` / `/restart <glob>`** — act on every match, e.g. `/restart gluetun-*`, with aggregated per-container results. Each still goes through the same guards (self-kill protection, and protect-from-stop), so a glob can't stop a protected container or Docksentry itself.

  No glob characters → exact/partial single-name behaviour is unchanged. Glob support for `/check` and `/update` (which tie into the batch-update flow) is a planned follow-up. Test: `scripts/test_glob.py`. English + German translated; other languages fall back to English.

## [1.28.3] - 2026-06-26

### Fixed
- **Docksentry's own image now carries an `org.opencontainers.image.version` label**, so `/status docksentry` (and the Web UI detail) shows its version, not just the image hash ([#39](../../issues/39), @LeeNX). The image was missing the version label — the `/status` reader (added v1.26.1) was working fine, there was simply nothing to read. The Dockerfile now sets it (plus an `image.title`) from a `VERSION` build-arg that the publish workflow fills from the release tag. Takes effect from this image onward; third-party images that genuinely don't set the label still show only the hash (there's no version to invent).

## [1.28.2] - 2026-06-25

### Fixed
- **Manual "Update all" now applies the same group gates as scheduled auto-updates.** Prompted by @famewolf in [#2](../../issues/2), who noticed a recurring pattern: bugs that only hit the manual path because it had drifted from the auto path. Audited both; the manual path was missing two group behaviours: it now (1) processes containers in **Container-Group order** and (2) **skips the rest of a group once a member fails** (`group_aborted`) — so a failed Gluetun head no longer leaves Docksentry recreating its dependents against a broken namespace — plus honours the inter-member `wait_seconds`. (The maintenance-window filter and the ask-before-major confirmation gate remain intentionally auto-only: tapping "Update all" is itself the explicit "do it now, majors included".) Test: `scripts/test_manual_update_gates.py`.

  A deeper unification (manual + auto as thin triggers over one shared per-container routine, also @famewolf's suggestion) is tracked as a follow-up refactor.

## [1.28.1] - 2026-06-25

### Fixed
- **Gluetun-style dependents are now *recreated* after the VPN container updates, not just restarted** ([#8](../../issues/8), @famewolf). The `restart_dependents` cascade ran `docker restart` on each sidecar — but a container updated by Docksentry is **recreated** (new container ID), and a sidecar with `network_mode: container:<head>` can't rejoin by `docker restart`: it still references the head's dead old ID and the restart fails with `No such container`, leaving the sidecar **stopped**. The cascade now *recreates* netns-sharing dependents against the head's current name (reusing the v1.26.3 netns-by-name resolution, same image, no pull, with backup + rollback); non-netns group members are still just restarted. Two gaps closed at once: (1) the cascade now actually works after a head recreate, and (2) the cascade now also runs in the **manual "Update all"** path — previously only the scheduled auto-update path triggered it, so anyone who updates manually (and only the head has an update) was left with broken sidecars. Dependents already in the same batch are skipped (they self-heal via v1.26.3). End-to-end test: `scripts/test_dependents_recreate.py`.

## [1.28.0] - 2026-06-25

### Added
- **Per-container "protect from Stop" flag** ([#38](../../issues/38), @LeeNX). Mark a container and its Stop action is hidden and refused — in the Telegram `/status` buttons, the Web UI list, and via the new `/protect` command — so you can't accidentally take down something you depend on (LeeNX's example: stopping the VPN/tunnel that carries your remote access locks you out of the homelab). **Restart and updates stay allowed** — a brief restart is fine, a permanent stop is the dangerous one. Generalizes the existing self-stop guard to any container you choose. Set with `/protect <name>` (Telegram) or the toggle on the container detail page (Web UI); the callback/command is guarded server-side too, so a stale button can't slip a stop through. Test: `scripts/test_protect_stop.py`. English + German translated; other languages fall back to English.

## [1.27.0] - 2026-06-25

### Added
- **Web UI: sortable Name column** ([#37](../../issues/37), @LeeNX). With many containers it was hard to group similar names (e.g. several `cloudflare-*`). Clicking the **Name** header now cycles A→Z → Z→A → back to the default order. The default stays the deliberate Container-Group order (head first), so sorting is purely opt-in per view and a reload restores grouping.
- **Web UI: footer links to GitHub, Releases and the running version's release notes** ([#37](../../issues/37), @LeeNX). The footer now links the version number straight to its release tag, plus standalone GitHub and Releases links — so checking for a newer release no longer means hunting through the Sponsor link.

## [1.26.4] - 2026-06-25

### Added
- **Self-update notifications now link to the release notes** ([#2](../../issues/2), @famewolf / @NotRetarded) — a single clutter-free link to the GitHub releases page in the "new image found" / auto-self-update messages, matching the repo links container updates already carry.
- **`/changelog` shows the current version's notes when you're already up to date** ([#2](../../issues/2), @famewolf). Previously `/changelog` only listed releases *newer* than the running one, so after a `/selfupdate` there was no way to see what the version you just installed actually changed. Now, when nothing newer exists, it shows the running version's own changelog entry instead of a bare "up to date".

## [1.26.3] - 2026-06-25

### Fixed
- **Gluetun-style netns sidecars no longer fail to recreate when the VPN container is updated in the same batch.** Reported by @famewolf in [#2](../../issues/2): updating `gluetun` and its sidecars (`network_mode: container:gluetun`) together recreated gluetun first — giving it a new container ID — then the sidecars' recreate failed with `joining network namespace of container: No such container: <old-id>`, because their stored `NetworkMode=container:<id>` still pointed at the now-dead old gluetun. Docksentry now snapshots each updating container's netns-owner **by name** *before* anything is recreated, and rebuilds the sidecar against `container:<name>` (stable across the owner's recreate) instead of the volatile ID. Resolved per-container from the live owner, so it's correct even for non-head/chained netns sharing and needs no Container Group. Applies to both the manual "Update all" and scheduled auto-update paths. End-to-end test: `scripts/test_netns_recreate.py`.

## [1.26.2] - 2026-06-24

### Fixed
- **A manual `/selfupdate` no longer mislabels itself as an external restart.** Reported by @famewolf in [#2](../../issues/2): after `/selfupdate` recreated Docksentry, the next boot printed *"↻ Restart followed an external stop signal (SIGTERM) — Docksentry did not restart itself"* — which is exactly backwards. The v1.23.8 startup-reason logic only suppressed that line when the *auto*-self-update deferred-check marker was present, but manual `/selfupdate` never writes that marker, so its (self-inflicted) recreate SIGTERM was read as external. `_do_selfupdate` now writes a dedicated self-update marker before the recreate (covering both manual and auto paths); the next boot consumes it and skips the external-signal line — the version bump in the startup banner already tells the story. Stale markers (>1h) are ignored. Test: `scripts/test_selfupdate_marker.py`.

## [1.26.1] - 2026-06-23

### Added
- **`/status <name>` now shows the image version and short image ID** ([#36](../../issues/36), @LeeNX) — the same detail the Web UI exposes since #32. The `org.opencontainers.image.version` OCI label answers "what version is this really?" beyond a rolling `:latest` tag, and the 12-char image ID identifies the exact build. Both are pulled from the inspect data the status detail already fetches, so no extra Docker call. Version line is omitted when the image carries no version label.

## [1.26.0] - 2026-06-22

### Added
- **Per-container update cooldown** (advanced mode) — an opt-in pause after recreating a container, before the next one in a batch update. From @famewolf's GPU case in [#2](../../issues/2): when two memory-heavy containers update in the same run, the freshly-recreated one is still loading (its memory peak) when the next recreate begins, so they contend and one OOMs. The manual update-all path had *no* inter-container wait (only the scheduled auto-update path did, between Container Group members) — this fills that gap in **both** paths, without needing a group. Set `/cooldown <name> <seconds>` (Telegram) or the field on the container detail page (Web UI, advanced mode), 0–600s, default 0. Test: `scripts/test_cooldown.py`. English + German translated; other languages fall back to English.

  **Scope, honestly:** this spaces out *load-peak overlap* — it does not create memory. If your GPU/RAM genuinely can't hold all the recreated containers at once, ordering matters more than spacing: put them in a **Container Group** in the order that fits (heaviest first), which both update paths already honour. The crash-loop guard (v1.23.5) remains the backstop that rolls back an update that OOMs anyway.

## [1.25.1] - 2026-06-21

### Fixed
- **`/selfupdate latest` (and `stable`) now actually moves you back onto the rolling tag.** Closes the SET/UNSET asymmetry @famewolf raised in [#2](../../issues/2): `/selfupdate <version>` could *pin* a tag, but returning to `:latest` was a dead end — when the rolling tag's digest already equalled the pinned version's, the self-update short-circuited to "already up to date" and the container stayed on the pinned tag, so it would never track latest again. You had to edit compose on the host. Now, when you explicitly ask for `latest`/`stable` and the container is on a *different* tag, Docksentry re-tags and recreates even on an unchanged digest, so the image reference becomes the rolling tag. Asking for `latest` while already on `:latest` still does nothing (no pointless recreate). Logic test: `scripts/test_selfupdate_retag.py`.

## [1.25.0] - 2026-06-21

### Added
- **`/groups` Telegram command** — read-only Container Group view from chat, matching the Web UI Groups page. `/groups` lists every group with its member count and 👑 head; `/groups <name>` (matches the group name or slug, partial OK) shows each member with a 🟢 running / ⚪ stopped icon, the restart-dependents setting, and — for a `restart_dependents` group — a button to restart the dependents now (waits for the head to be healthy first). From the [#2](../../issues/2) roadmap. English + German translated; other languages fall back to English.

### Fixed
- **The "wait for head to be healthy before restarting dependents" check was a no-op.** In `_restart_group_dependents` the code did `healthy = self._wait_healthy(...)` then `if not healthy:` — but since v1.23.5 `_wait_healthy` returns a `(outcome, state, health)` tuple, which is always truthy, so the not-healthy branch never ran and the warning never logged. Dependents were still restarted (the intended fallback), but the outcome was silently ignored; now the tuple is unpacked and the real outcome is logged.

## [1.24.0] - 2026-06-21

### Added
- **`/check --dry-run` — preview an update before running it.** Runs the normal read-only update check, then instead of the actionable "Updates Available" notification it shows what *applying* each update would do — and changes nothing. For every pending update it reports: the recreate path (Compose `up -d <service>` vs. standalone recreate rebuilt from `docker inspect`, mirroring the real fallback rule), the dependents that would be restarted when the container is the head of a `restart_dependents` Container Group, and any major-version jump that would be held for confirmation. Aliases: `/check dry-run` and `/dryrun`. No buttons, no side effects. Logic test: `scripts/test_dry_run.py`. From the [#2](../../issues/2) roadmap. English + German translated; other languages fall back to English.

## [1.23.10] - 2026-06-21

### Changed
- **Auto-update toggles are now visible in simple UI mode too.** The per-container auto-update button (container list) and the bulk "auto-update on/off" buttons were marked `adv-only` and hidden in simple mode — yet the container *detail* page already showed the same toggle in both modes. That inconsistency hid a core feature from simple-mode users for no good reason. The toggles now appear in both modes, matching the detail page. The major-version-confirm toggle and the Stop button stay advanced-only (deliberate: Stop leaves a container offline until someone starts it).

## [1.23.9] - 2026-06-21

### Added
- **Per-container "trust running state over healthcheck" flag** ([#9](../../issues/9), @famewolf). Some containers report `health=unhealthy` even when they work fine — usually a brittle healthcheck command, not a broken app (classic case: a VPN-sidecar dependent whose probe hits the wrong network namespace). When this opt-in flag is set, after an update Docksentry accepts the container as healthy as long as `state=running`, instead of rolling back on the unhealthy verdict. **Safety preserved:** the relaxation applies *only* to the `health=unhealthy` rule — a climbing `RestartCount` (crash loop) and a container that isn't running are still treated as failed updates, so this can't mask a genuinely broken update. Default stays strict (trust the healthcheck). Toggle on the container detail page in the Web UI; stored in `/data/trust_running_containers.json`. Regression test: `scripts/test_trust_running.py`. English + German translated; other languages fall back to English.

## [1.23.8] - 2026-06-20

### Added
- **Startup notification now says *why* Docksentry restarted.** Reported by @famewolf in [#2](../../issues/2): three of his hosts reboot at midnight and each Docksentry came back with the generic "🚀 Docksentry started" banner, which made it look like Docksentry restarted *itself* — when in fact an external signal (host reboot / `docker restart` / Docker daemon restart) stopped it and the restart policy brought it back. The `SIGTERM`/`SIGINT` handler now records the exit cause, and the next boot appends a line to the startup message: *"Restart followed an external stop signal (SIGTERM) — Docksentry did not restart itself."* With `cron 0 18 * * *` + auto-selfupdate off there is no code path that restarts the process; the only emitter of "Shutting down…" is the signal handler, so this distinguishes external restarts from self-updates at a glance. Absent marker (first boot or an unclean SIGKILL/OOM/power loss) adds no suffix — we don't claim a cause we can't prove. Translated into all 17 languages.

## [1.23.7] - 2026-06-20

### Fixed
- **A compose update that pulls the new image but keeps running the old one is no longer reported as success.** Reported by @NotRetarded in [#35](../../issues/35): an auto-update pulled the new image (local digest matched remote) and reported `OK`, but the container kept running the previous image — the app stayed on its old version until a manual force-recreate. The compose path ran `docker compose up -d --no-deps`, which can leave the existing container in place if Compose judges the service "unchanged", so the freshly-pulled image never gets loaded. Two changes: the recreate now uses `--force-recreate` so the container is actually replaced, and after it comes up Docksentry compares the running container's image ID against the pulled image's ID — if they differ, it reports a failure with a clear message instead of a phantom success.

## [1.23.6] - 2026-06-19

### Fixed
- **Disk-usage monitoring no longer rides on the update-check schedule.** The disk check only ran inside a cron tick — so with a once-a-day schedule (`0 18 * * *`) the disk was inspected once per day, right after the update. A container that filled the disk *between* two daily ticks (e.g. a crash-looping service writing gigabytes of logs) crossed the warning threshold and ran the disk to 100% with no alert, because the next check wasn't due for ~24h. Disk monitoring now runs on its **own cadence**, independent of the update cron — every `disk_check_interval_seconds` (default 300s / 5 min), starting at boot. A disk can fill in minutes; it's now checked in minutes.

- **A failed state-file write no longer causes the same alert to repeat endlessly.** When the disk is full, the throttle/state files for the disk warning and the weekly report can't be written — so the "already sent" marker never persisted and both fired again on every pass (the disk warning would have, and the weekly report demonstrably did: several duplicate weekly messages during the outage). The scheduler now keeps an **in-memory guard** for each: once a disk warning or weekly report has gone out, it won't repeat for its throttle window even if the state file can't be persisted. The guard re-arms when the disk drops back below the threshold (or the process restarts). Verified with `scripts/test_disk_guard.py`.

## [1.23.5] - 2026-06-18

### Fixed
- **An update that leaves a container crash-looping is no longer reported as a success.** A container can pass back through `running`/`starting` between crashes — so an image whose new version exits on startup (e.g. a failed database migration) and gets revived over and over by its `restart: always` policy would, at the moment Docksentry happened to inspect it, look alive. The health-wait only checked the current `State`/`Health` snapshot, never the restart count, so it either reported `healthy` or timed out into the "slow start" branch — and in the standalone path that branch even **deleted the rollback backup** before recording success. The result: a service down in a tight restart loop, marked as a clean update, with the backup it needed for recovery already gone.

  The health-wait now records the container's `RestartCount` before it starts watching and re-checks it on every poll. If the count climbs while we wait, the update is classified as a **crash loop** — a hard failure: roll back to the backup (standalone), leave the old container in place (compose), and report `success: false` with a clear "crash-restart loop" message. The backup is never removed on this path.

  It also no longer declares success the instant a container first looks healthy: it confirms the container *stays* up, with no restarts, for `crashloop_stable_seconds` (default 30s) before reporting `healthy`. This catches a container that boots cleanly and then crashes a few seconds later — slower than a single health poll would see. Genuinely slow-but-stable services (no restarts) are unaffected and still resolve to the "starting" warning rather than a false failure.

  Together with the existing `restart: no` → `unhealthy` path, both crash shapes are now covered: a container that stays exited, and one that loops. Verified with a local five-case simulation against real crash-looping, healthy, slow-start, and boots-then-crashes containers (`scripts/sim_crashloop.py`).

## [1.23.4] - 2026-06-16

### Added
- **`/selfupdate latest` and `/selfupdate stable`** now work. Previously `/selfupdate` only accepted an exact `X.Y.Z` version or `previous`, so a container running a fixed version tag (e.g. `:1.19.0`) had no in-band way to rejoin the rolling `:latest` line. Reported by @famewolf in [#2](../../issues/2): his host was stuck on `:1.19.0` — and `/selfupdate latest` was rejected as an invalid version.

- **Plain `/selfupdate` now warns when you're on an outdated fixed version tag.** If the container's image is a specific version (e.g. `:1.19.0`), `/selfupdate` checks that tag — which is immutable, so it correctly reports "up to date" even when a much newer release exists. That was genuinely misleading (and dangerous: @famewolf's `:1.19.0` host kept losing its config to the pre-v1.22.0 non-atomic-write bug, with no obvious signal that it was stuck on a buggy version). Now, when the current tag is a fixed `X.Y.Z` and the upstream CHANGELOG advertises a newer release, `/selfupdate` says so and offers three concrete ways out: `/selfupdate <new>`, `/selfupdate latest`, or switching the compose image to `:latest`. Only triggers on a plain `/selfupdate` (an explicit `/selfupdate X.Y.Z` is never second-guessed), and falls back silently to the normal "up to date" message if the CHANGELOG can't be fetched.

### Changed
- `selfupdate_invalid_version` message updated to list all accepted targets (`X.Y.Z`, `latest`, `stable`, `previous`).

### Fixed
- **Auto-detect "Import selected" no longer imports stacks you didn't pick.** Reported by @famewolf in [#2](../../issues/2): he opened the auto-detect modal to browse, checked nothing, clicked "Import selected" — and a multi-container stack got imported anyway, because v1.21.2 pre-checked multi-container stacks by default. Now **nothing is checked by default**; "Import selected" imports only what the user explicitly ticks, and clicking it with nothing selected shows a "Nothing selected" toast instead of silently creating a group. The `single` badge still steers users away from single-container stacks, and already-imported stacks stay disabled.

## [1.23.3] - 2026-06-15

### Fixed
- **"Update all" now updates exactly the containers the notification showed, not whatever is currently pending.** Reported by @famewolf in [#2](../../issues/2): he tapped "Update all" on a notification that listed only `searxng`, but it updated all five containers a later check had since written to `pending_updates.json`. The button carried no reference to *which* notification it came from — it just re-read the global pending file at click time.

  Each "Updates Available" notification now snapshots its exact container set, keyed by a short token in the "Update all" button's `callback_data` (`update_all:<token>`). Clicking it updates that snapshot's containers and removes only their names from the pending file (leaving any others). Snapshots are capped FIFO (last 20) so the store can't grow unbounded. If a snapshot is gone (evicted, or the bot restarted), the user gets a clear "this notification is stale, run /check" message instead of silently updating the wrong set. Bare `update_all` (from notifications sent by older versions still in the chat) falls back to the previous read-pending behaviour.

  Verified end-to-end: with `[searxng]` snapshotted and the pending file later overwritten with five containers, "Update all" updates only `searxng` and removes only `searxng` from pending.

### Still open
- Slow SIGTERM response (bot blocks in the Telegram long-poll). This is a tradeoff between poll frequency and shutdown speed rather than a clear-cut bug — deferred pending a decision on the right balance.

## [1.23.2] - 2026-06-15

### Fixed
- **The entire Web UI JavaScript was broken in every browser from v1.22.0 to v1.23.1.** Reported by @famewolf in [#2](../../issues/2) via his browser console — the backup-restore button "did absolutely nothing" because `dsBackupImport` was `undefined`. Root cause: the `dsBackupImport` `confirm(...)` string in `_BASE_JS` was written with a `\n`. `_BASE_JS` is a regular (non-raw) Python triple-quoted string, so Python turned that `\n` into a **real newline character** in the rendered JavaScript — producing a string literal with an unescaped line break:
  ```js
  if (!confirm('Restore from "' + file.name + '"?
  This will overwrite...')) {   // ← SyntaxError: unescaped line break
  ```
  That is a hard `SyntaxError` that aborts the **whole `<script>` block**, so *every* function defined in `_BASE_JS` — tab switching, theme toggle, confirm dialogs, drag-and-drop reorder, auto-detect modal, webhook test, cron preview, toast, AND backup/restore — silently failed to define. Server-side features (favicon, Discord links, page rendering) were unaffected, which is why it wasn't obvious.

  Fixed by removing the newline from the confirm string. Verified with a comment- and regex-aware scanner that no raw control character remains inside any JS string literal across `/`, `/settings`, and `/groups`.

- **Regression guard added to `scripts/pre-commit-check.py`.** A new check parses `_BASE_JS` and fails the build if any raw newline/CR appears inside a single- or double-quoted JS string literal — the exact class of bug above. Verified it passes clean now AND catches a deliberately-injected break.

### Why our earlier testing missed it
The v1.22.0 backup feature was "tested" three ways: the backend endpoint with `curl` (worked), a check that the `dsBackupImport` function *text* was present in the served HTML (it was), and a browser-style multipart upload to the endpoint (worked). None of those execute the page's JavaScript, so none caught that the script block fails to *parse*. @famewolf's browser console caught in one line what our tests structurally could not. The pre-commit guard closes that gap going forward.

## [1.23.1] - 2026-06-14

Proactive audit pass: after the homarr deletion (v1.23.0), we swept the
codebase for the same *classes* of bug — destructive operations without
recovery, and concurrency on shared state — and fixed the two highest-
risk findings before anyone hit them.

### Fixed
- **Rollback could strand a container or destroy the user's only copy.** All three rollback paths in `_update_standalone` (run-failed, unhealthy, and the catch-all exception handler) used `docker rm <name>` (no `-f`) followed by `docker rename <old> <name>`. Two failure modes:
  1. If the broken new container wouldn't stop, the non-forced `docker rm` silently failed and the subsequent rename collided — leaving the user with the broken new container and the old one orphaned as `<name>_old`.
  2. The exception handler blindly renamed `<name>_old` back even when no such backup existed.

  New single `_rollback_to_old(name, old_name)` helper, used by all three sites. Safe ordering, "don't make it worse" first: **if no `<name>_old` backup exists it leaves `<name>` completely alone** (never destroys what might be the user's only container); otherwise it force-removes the broken new container (`-f` handles a wedged/running one) and restores the backup. Verified on a test host including the critical no-backup case.

- **Scheduler auto-update could run concurrently with a manual update.** The manual paths (`run_updates`, `_run_single_update`, `_confirm_major_update`) guarded on a plain `update_running` bool, but the scheduler's `handle_autoupdates` ignored it entirely — so a cron tick could recreate the very container a user was mid-updating from Telegram, two recreate flows racing on the same container. Replaced the bool with a single `threading.Lock`; all four entry points now claim it atomically (`acquire(blocking=False)`), and the scheduler skips its auto-update pass when a manual update holds the lock (retrying next tick). The `update_running` read is preserved as a property for the existing `/check` race-guard.

  Bonus: the lock is released in `try/finally` everywhere. The old `update_running = False` only ran at the end of `run_updates`, so an exception outside the inner loop would have left the flag stuck `True` and blocked every future update — that latent bug is gone too.

### Still open (confirmed, next)
- "Update all" stale-snapshot (updates current pending, not the notification's set).
- Slow SIGTERM response via long-poll block.

## [1.23.0] - 2026-06-14

### Fixed
- **`--rm` (AutoRemove) containers are no longer lost when an update stops a wedged container.** Reported by @famewolf in [#2](../../issues/2): his `homarr` container — which had `AutoRemove=true` — disappeared entirely during an update. We initially thought Docker's daemon garbage-collected it on its own. It did not: **our own stop sequence was the proximate cause**, and we reproduced the exact mechanism on a test host:

  1. A `--rm` container that's slow to stop (homarr was wedged — our stop ran into the 90s timeout)
  2. Our `docker stop` eventually reaps the process
  3. **Because `--rm` is set, Docker auto-removes the container the instant it stops**
  4. Our `docker kill` fallback then reports `cannot kill container: … No such container` — the exact error family @famewolf saw
  5. The old code hit `if not stop_ok: return False` and **walked away, leaving him with no container** — even though we had its full inspect config in memory the whole time

  We captured the container's config *before* stopping (we always have), so there was never any reason to leave the user stranded. Now: after the stop step we check whether the container still exists. If it vanished (the AutoRemove case), we **recreate it directly from the captured config** — the old container is already gone, so we skip the rename/rollback machinery and run the new one. `homarr` would have been updated correctly instead of deleted.

  Verified end-to-end on a test host: a wedged `--rm` container that vanishes on stop is now recreated with all labels and config preserved.

- New `_container_exists(name)` helper backs the recovery check.

### Honesty note
The first triage of this report concluded "probably not us — Docker daemon cleanup." That was wrong, and the working assumption should have been the opposite. When a user reports data loss during one of our operations, the burden is on us to *prove* we weren't involved, not to assume it. The empirical reproduction here came directly from re-investigating under that assumption.

### Still open (confirmed, shipping separately)
- "Update all" on a stale notification updates whatever is *currently* pending, not the set shown in that notification (global `pending_updates.json` is overwritten by each check). Confirmed from code; snapshot fix coming.
- Bot is slow to respond to SIGTERM because it blocks in the Telegram long-poll; contributes to slow `docker compose down`. Fix coming.

## [1.22.2] - 2026-06-13

### Fixed
- **Selfupdate history `(selfupdate vX → ?)` placeholder now always gets patched.** Reported by @famewolf in [#2](../../issues/2). The post-boot fixup in `main.py` (introduced in v1.17.6) that replaces the `?` placeholder with the new VERSION was gated on `post_selfupdate_restart`, which depends on the `deferred_check_file` marker. That marker is **only written by the auto-selfupdate path** (cron + `AUTO_SELFUPDATE=true`); manual `/selfupdate` doesn't create it. So users running manual selfupdates (which is the common case) saw the `?` placeholder stick around in their history forever, making downgrade discovery harder. Decoupled the fixup from the marker — the `endswith("→ ?)")` guard is itself the safety check, so running the fixup on every boot only ever touches the actual placeholder and is a no-op otherwise.

- **Selfupdate no longer reports "❌ Selfupdate failed: Unable to find image 'docker:cli' locally"** when it actually worked. Reported by @NotRetarded in [#2](../../issues/2). The helper-container launch (`docker run docker:cli ...`) was relying on Docker's implicit auto-pull when the image wasn't local — and the auto-pull writes progress to stderr ("Unable to find image 'docker:cli' locally" + layer download lines). If the auto-pull went sideways (slow registry, transient network blip, rate-limit hiccup) the helper-launch subprocess surfaced that stderr as the failure message even when the update completed successfully a few seconds later.

  Fixed: pre-pull `docker:cli` explicitly before launching the helper. Either the pull succeeds → helper launch is clean and silent → user sees no false failure; or the pull genuinely fails → user sees an honest error pointing at the helper image (not at our update logic).

## [1.22.1] - 2026-06-12

### Fixed
- **v1.22.0 only patched 2 of 13 non-atomic JSON writes — the other 11 are now fixed too.** Hours after v1.22.0 shipped, a careful re-audit found that the atomic-write fix had been applied to `container_store._save_dict` and `config.save_persistent` but missed eleven other sites that all have the same bug class:
  - `container_store._save` (list-format files: `pinned.json`, `autoupdate.json`, `ask_before_major.json`)
  - `maintenance._write` (`maintenance.json`)
  - `weekly_report._write_state`
  - `update_checker` x3 (history, pending, disk-warn state)
  - `telegram_bot` x4 (selfupdate-history, pending-after-single-update, pending-after-autoupdate-batch, deferred-check marker)
  - `web_ui` x2 (pending-after-Web UI single-update, pending-after-Web UI bulk-update)
  - `main.py` post-selfupdate history fixup

  Same root cause as the v1.22.0 fix: `open(path, "w")` truncates the target to 0 bytes immediately, then `json.dump` writes the new content. A kill between truncate and close leaves a partial file. v1.22.0 was an incomplete fix — for @famewolf's specific symptom (settings + groups gone) it sufficed, but his pinned containers, autoupdate flags, or update history could have been wiped silently by the same bug.

- **Refactor: shared `atomic_write_json(path, data, **dump_kwargs)` helper** at module level in `container_store.py`. All 13 write sites now route through it instead of inlining the tmp+fsync+rename pattern. Single point of fix if we ever need to change the strategy (retry on EBUSY, fdatasync vs fsync, etc.).

### Smoke-tested
- Helper write+read roundtrip ✓
- Helper cleans up `.tmp` after rename ✓
- Helper forwards `indent=2` kwarg ✓
- Helper overwrites existing file correctly ✓
- Live Docksentry container backup-export → backup-import roundtrip still works after the refactor ✓

### Lesson learned
v1.22.0 was rushed — the diagnosis was correct but the audit was shallow. Lesson: when fixing a bug class (vs a single bug), grep for the entire pattern across the codebase before declaring the fix complete. The 30-second `grep -rn 'json.dump' app/` would have caught all 11 missed sites yesterday.

## [1.22.0] - 2026-06-12

### Fixed
- **Persistent state survives mid-write kills (atomic writes).** Reported by @famewolf in [#2](../../issues/2): all three of his hosts simultaneously rebooted (likely `unattended-upgrades`) and **every Docksentry instance came back with empty config** — container groups gone, web setup wizard re-appearing. Root cause: both `container_store._save_dict` and `config.save_persistent` used `open(path, "w")` which truncates the file to 0 bytes immediately, before the new content is written. A kill between truncate and close (host reboot, Docker daemon restart, OOM, power loss) left a 0-byte or partial-JSON file, which the next boot failed to parse and fell back to empty defaults. Bug existed since v1.7.0.

  Fix: both write paths now write to `<path>.tmp`, `flush()` + `os.fsync()` to push bytes through the kernel page cache to disk, then `os.replace()` which is POSIX-atomic — either the new file is fully visible or the old one is still there, never a partial state. Applies to `settings.json`, `groups.json`, `notes.json`, `links.json`, `update_windows.json`, `pending_major.json`.

### Added
- **"No persisted settings" Telegram alert on boot.** When `BOT_TOKEN` is configured via env vars but `/data/settings.json` is missing on startup (and we're not in a post-selfupdate restart), surface a Telegram message warning of possible data loss. Means a user no longer needs to discover the wizard accidentally via the Web UI hours later — the bot tells them immediately. Implicit ask from @famewolf in the same [#2](../../issues/2) thread.

- **Backup & Restore via Web UI.** New "Backup & Restore" card on the Settings page with **Export backup** and **Restore backup…** buttons. Export downloads a single JSON file containing every persisted state — settings, pinned, autoupdate, ask-major flags, container groups, notes, links, update windows — with a `schema_version` sentinel for forward compatibility. Restore reads a previously-exported file and writes each section through the now-atomic save paths. Defense-in-depth against the kind of data loss that hit @famewolf — also useful for host migrations or just routine snapshots. Asked for by @famewolf in [#2](../../issues/2).

- **Container Groups ordering in update notifications, plus 👑 HEAD badge.** When a container is the first (head) member of a Container Group with ≥ 2 members, it now gets a 👑 badge in the "🔄 Updates Available" Telegram message. The same message also sorts updates by group position (head first, then dependents in order, orphans at the end) — mirrors the sort that `handle_autoupdates` already did during execution but extends it to the pre-update notification. Reported by @famewolf in [#2](../../issues/2): with a Gluetun+dependents stack, gluetun was showing up LAST in the notification, making cascade-debugging harder.

### Changed
- `_groups_html` removed. Dead code since v1.21.1 when the legacy Settings → Groups card was replaced by a redirect banner pointing at `/groups`. All Container Groups functionality lives in `_page_groups` now.

### API
- `/api/backup_export` (GET) — returns a `docksentry-backup-YYYYMMDD-HHMMSS.json` attachment with the full state bundle. Read-only.
- `/api/backup_import` (POST, multipart/form-data with `file` field) — accepts a backup bundle, restores each known section, returns `{ok, restored: [...], errors: [...], schema_version, from_version}`. Unknown / missing sections are silently skipped (forward-compatible).

### Notes
- Hard-reload the Web UI (`Ctrl+Shift+R`) once after pulling so the new Backup/Restore card's JS lands.
- Backup files contain webhook URLs and bot tokens — treat them like passwords.

## [1.21.2] - 2026-06-10

### Changed
- **Auto-detect modal default-checking is now smart.** Two UX issues from v1.21.1 first-touch feedback:
  - All stacks were auto-checked, including single-container Compose projects (6 of 9 stacks on a typical home setup). Making them Docksentry groups has no effect — `restart_dependents` needs ≥2 members to do anything. Now: multi-container stacks default checked, single-container stacks default unchecked (with a `single` warning badge). User can still manually check single-container stacks if they're planning to add members later.
  - The `restart_dependents` checkbox sat as a separate footer row visually adjacent to the member list — looked like another member row but was a totally different concept (group-level option). Moved it directly under the stack header as an inline labelled control. Disabled + grayed out for single-container stacks (no effect with 1 member).

- **`restart_dependents` recommendation when netns sharing is detected.** When at least one container in a stack runs with `NetworkMode=container:<head>` (the VPN-sidecar pattern: Sonarr / Radarr / qBittorrent on `network_mode: service:gluetun`), the `restart_dependents` checkbox is now **pre-checked** and labelled with a `netns recommended` hint badge. That's the case where restart_dependents matters most — the sidecar loses connectivity when the VPN head restarts, and our v1.17.0 cascade is the fix.

### Notes
- Storage layer and backend API unchanged — pure modal UX refinement.
- Hard-reload the Web UI (`Ctrl+Shift+R`) once after pulling so the updated JS lands.

## [1.21.1] - 2026-06-10

### Added
- **Auto-detect Compose / Portainer / Swarm stacks as Container Groups.** New "🔍 Auto-detect from Compose / Portainer" button on the `/groups` page. Backend scans every container's labels for `com.docker.compose.project` (Compose / Portainer / Dockge / podman-compose / anything that wraps Compose) and `com.docker.stack.namespace` (Swarm), groups containers by stack, and surfaces them in a modal where the user can:
  - **Check / uncheck individual stacks** to include in the import
  - **Drag-and-drop** members within each stack card to set the head order (first = head, gets `restart_dependents` semantics when enabled)
  - **Include / exclude individual containers** from a stack via per-row checkbox
  - **Toggle `restart_dependents` per stack** — conservative default (off); user must opt in
  - **See conflict / status badges**: `↻ <group>` when a container is already in another Docksentry group (will be moved on import), `netns` when a container shares a network namespace (VPN-sidecar hint), `already imported` when a same-named group exists
  - **Click "Import selected"** to bulk-create groups via the new `/api/groups_import_batch` POST endpoint

  Smoke-tested on the maintainer's own host: 9 stacks detected (Nextcloud's 3-container set, Paperless-NGX's 3-container set, an InfluxDB+NodeRed pair, plus 6 single-container Compose projects) — all rendered correctly with proper head detection.

### Changed
- **Legacy Settings → Groups card is now a thin redirect banner.** The card stays for users who bookmarked `/settings#groups` or follow status-page links pointing there, but the duplicated CRUD UI is gone — clicking the banner button takes you to the new `/groups` page where all the actual functionality lives. Removes code duplication; the v1.21.0 `_groups_html` helper is now dead code (kept as-is for one release to surface any forgotten callers, removal slated for v1.22.0).
- Modal markup on the `/groups` page now uses the existing `.modal-backdrop` / `.is-open` pattern shared with the confirm dialog, instead of inline `display:none`.

### API
- `/api/groups_detect` (GET) — scan + group containers by stack label, return JSON `{ok, stacks: [{name, source, containers, conflicts, exists}]}`. Read-only.
- `/api/groups_import_batch` (POST) — accepts repeated `stacks=<json>` form values, each carrying `{name, containers (ordered), restart_dependents, wait_seconds}`. Same-named existing groups update in place (storage layer's one-group-per-container invariant handles re-assignment automatically). Returns JSON `{ok, created}`.

### Notes
- No env vars or breaking changes. Pull and refresh the browser (hard reload once) so the new modal CSS + JS lands.
- Stacks without a `compose.project` or `stack.namespace` label (= manual `docker run` containers) can't be auto-detected — they remain manually-grouped via the existing "+ New group" form.

## [1.21.0] - 2026-06-10

### Added
- **Dedicated `/groups` Web UI page for Container Groups.** Promoted from a hidden tab under Settings (advanced-mode-only) to a first-class section in the main navigation. The legacy Settings → Groups tab still works (existing bookmarks survive) but the new page is the primary surface.

  The dedicated page brings four upgrades the legacy tab didn't have:
  - **Edit existing groups in place.** Each group card has an `✏️ Edit group` `<details>` block — rename, change member list, toggle `restart_dependents`, change `wait_seconds`, save. Previously the only way to "edit" a group was to delete and re-create it from scratch (and pray you'd remembered all the original settings).
  - **HEAD badge** on the first container of every group. The `restart_dependents` semantics depend on which container is the head — making it visually explicit removes the "which one was first again?" guesswork that the legacy table didn't address.
  - **Drag-and-drop reorder.** HTML5 native DnD on `.group-member` list items, persisted via a new `/api/group_reorder_batch` endpoint (atomically replaces the member list of the named group, defensively preserves any members not in the drag payload). The legacy ↑/↓ form buttons still work on the Settings tab for users who prefer them or have JS disabled.
  - **Stale-member warning** badge on members whose container name no longer matches any running or stopped container on the host. Surfaces the "I deleted that container but forgot to remove it from the group" case before the group's update flow hits a "container not found" error.

- **`/api/group_save` now updates in place when called with a `group_id`.** Previously it always generated a new slug from `name`, so renaming a group via the form would create a duplicate. The new path: form passes the existing `group_id`, save endpoint detects it, calls `store.save_group(existing_id, ...)`. Backward-compatible — calls without `group_id` continue to act as create-with-generated-slug.

### Changed
- `_BASE_CSS` gained `.group-members-list` + `.group-member.dragging` styles for the DnD UI.
- `_BASE_JS` gained two new helpers: `dsInitGroupDrag` (binds DnD handlers to every `.group-members-list` on page load) and `dsPersistGroupOrder` (POSTs the new order, refreshes the HEAD badge client-side, toasts the result). Both `dsToast`-based — uses the v1.19.3 toast helper.
- `_groups_html` (the legacy Settings tab renderer) is unchanged; the dedicated page has its own `_page_groups` builder so the two surfaces can diverge without coupling.

### Notes
- Storage layer (`container_store.save_group` / `delete_group` / `get_groups` / `reorder_group_container`) is unchanged — the one-group-per-container invariant and wait_seconds clamp [0, 600] are preserved.
- Pull is a no-op for users who don't use Container Groups. For users who do: the new page works immediately, no migration needed.

## [1.20.0] - 2026-06-07

### Added
- **`/help <command>` — per-command detailed help** ([#15](../../issues/15), @famewolf). The general `/help` lists all commands; the new variant returns a deeper block for one command — synopsis, parameter list, examples, and side effects. Driven by a new `detail_help_key` field on the `_BOT_COMMANDS` table (single source of truth). All 20 commands have a detail block; English and German translated, the other 14 languages fall back to English via the existing i18n layer. Examples:
  ```
  /help cleanup     → grace-hours behaviour, backup flag, side effects
  /help selfupdate  → :latest vs <version> vs previous, helper-container model
  /help setlink     → URL store, OCI label fallback, cross-message effect
  ```

- **`/audit <container>` — surface the v1.19.0 inspect-coverage auditor from chat.** The audit logger added in v1.19.0 only wrote to DEBUG logs, so users had to enable `DEBUG=true` and grep `docker logs docksentry` to find non-restored fields. The new command runs the same check on demand and reports findings directly in Telegram. Empty findings = `✅ clean` confirmation; non-empty findings come as a list of `HostConfig.<key>` / `Config.<key>` entries with an "open issue" link in the footer. `UpdateChecker._audit_inspect_coverage()` now returns its findings dict in addition to logging (callers that don't care can ignore the return).

- **Webhook test buttons in Web UI Settings** ([#2](../../issues/2)). Next to the `DISCORD_WEBHOOK` and `WEBHOOK_URL` inputs you now have a "Send test" button that POSTs to a one-off `/api/test_webhook` endpoint and sends a `🧪 Docksentry test message` via a temporary `Notifier` instance using whatever URL is currently in the input — so you can debug a new value *before* saving it. Result surfaces as a floating toast (success / failure with error text). Quiet-hours suppression is bypassed for the test so the user actually sees the message.

- **Per-container history filter** in Web UI. The `/history` page gained a dropdown listing every container that has at least one entry in `update_history.json`. Selecting one filters to just that container's events; the URL carries `?container=<name>` so the view is also deep-linkable from anywhere (Status-page link icons could wire into this in a later release). Empty-state for "no history for X yet" gives a "clear filter" link back to the full view, plus a "showing N of M entries" hint above the table when the filter is active.

- **Live cron preview in Settings page schedule editor**. As you type a CRON expression the field now shows `⏰ tomorrow 18:00 · Fri 18:00 · Sat 18:00` below it — the next 3 ticks the scheduler would actually fire at. Driven by a 300ms-debounced fetch to a new `/api/cron_preview` endpoint that runs the existing `Scheduler._matches_cron` logic (extracted to module-level `scheduler.cron_matches` / `cron_next_ticks` helpers) against minute-by-minute look-ahead, capped at 1 year. Malformed expressions surface as `⚠ expression needs exactly 5 fields` immediately — no more "save and pray it fires when you expect".

### Changed
- `_BOT_COMMANDS` grew a 4-th tuple element `detail_key` driving `/help <cmd>`. Backward-compatible only if all callers unpack the full 4-element tuple — the two existing call sites (`_register_commands_with_telegram` and the `/help` builder) were updated.

- `UpdateChecker._audit_inspect_coverage()` now returns `{"host_unknown": [...], "config_unknown": [...]}` instead of just logging — same DEBUG output as before, but `/audit` can render the same data in chat. Pre-v1.20.0 callers that ignored the return value continue to work unchanged.

- `Scheduler._matches_cron(now)` is now a one-line wrapper over the module-level `scheduler.cron_matches(expr, now)` helper. Behaviour identical; the extraction is purely so `/api/cron_preview` can preview a fresh-typed expression without instantiating a Scheduler.

### Notes
- 21 Telegram commands registered with `setMyCommands` now (was 20 — `/audit` is new).
- No new env vars or breaking config changes. Existing setups need no migration — pull and restart.

## [1.19.3] - 2026-06-07

### Changed
- **Discord update-result embed now wraps the container name as a clickable link** when a `source_url` is available — same parity fix as v1.19.2 did for Telegram. Surfaced by @NotRetarded in [#2](../../issues/2): the "Updates Available" Discord embed already linked container names since v1.18.4, but the "Update Successful" / "Update Failed" embeds emitted plain `**name**` bold without a link, so the link disappeared between the pre- and post-update messages. `Notifier.send_update_result()` now takes an optional `source_url` arg; all five callsites in `telegram_bot.py` pass the value resolved from `_enrich_with_source_url()` / `_resolve_container_link()`.

  Generic webhook payload (`/api/webhook` consumers) also gains a `source_url` field on the `update_result` event so downstream automations (Home Assistant, n8n, Ntfy templates) can render the link without re-resolving it.

### Added
- **WebUI favicon** — inline SVG data URL embedded in the HTML head ([#2](../../issues/2) request from @NotRetarded). Shows a small blue shield with a centred dot in browser tabs, bookmarks bar, and PWA installs. No file IO, no extra HTTP request, theme-independent.

## [1.19.2] - 2026-06-07

### Changed
- **Container names in post-update result messages now render as clickable links**, matching the pre-update "Updates Available" notification. Surfaced by @NotRetarded in [#2](../../issues/2): the "🔄 Updates Available" message used `[name](url)` markdown links since v1.18.4, but the "⚡ Auto-update complete" / "Update Result" follow-ups still emitted plain `` `name` `` code-formatting — so the same container would appear linked in one message and unlinked in the next. Inconsistency, not a bug, but worth fixing.

  New `_display_name(u)` helper centralises the choice (link when `source_url` is set, code otherwise). All six result-line builders in `handle_autoupdates()` and `run_updates()` now route through it, and both callsites enrich the update list via the existing `_enrich_with_source_url()` helper at the top of the flow so the URL is always available. Same `container_store.get_link()` → OCI label → registry fallback chain as the pre-update message — one source of truth.

## [1.19.1] - 2026-06-07

### Added
- **`/setlink <container> <url>` Telegram command** ([#2](../../issues/2)). Two-vote community feedback — @famewolf (CGNAT-hosted, prefers Telegram-first management) and @NotRetarded (uses Telegram for most operations) both asked for a Telegram-side affordance to set the per-container repo/changelog URL. The Web UI's Status-page link icon already does this; now you can do it from chat too: `/setlink homarr https://github.com/homarr-labs/homarr`. Omitting the URL clears the override and falls back to OCI labels. Saves to the same `container_store.get_link()` used by both `/changelog` and update-notification repo links.
- **Web UI: version / hash badge per container** ([#32](../../issues/32), @LeeNX). Container rows now show `org.opencontainers.image.version` (when the upstream image sets the OCI label — ~40% coverage in real-world stacks) as a small badge after the image ref. When the label is absent, falls back to the 12-char short image ID so you can tell two containers running `latest` apart at a glance. Implementation batches `docker image inspect` of all unique images per `_get_containers()` call — one extra subprocess regardless of container count.

### Fixed
- **Outer quotes in env values are now stripped** ([#30](../../issues/30), @LeeNX). Docker Compose passes env values literally, so writing `BOT_TOKEN="abc123"` in a compose file lands in the runtime env as the string `"abc123"` (quotes included). Downstream that broke the Telegram API call (wrong token), `int()` conversion on `WEB_PORT="8080"`, the `.lower() in ("true",...)` check on `AUTO_SELFUPDATE="false"`, etc. All `Config.from_env()` reads now go through a `_strip_quotes()` helper that strips matching `"…"` / `'…'` pairs. Mismatched or single quote chars are left alone so legitimately-quoted passwords/tokens are preserved.

### Docs
- **README: Healthcheck section** ([#31](../../issues/31), @LeeNX). Docksentry's image has shipped with a HEALTHCHECK since v1.16.1 (probes Web UI socket → Telegram `getMe` → webhook-only exit-0) but it wasn't documented anywhere, so users couldn't tell it existed. New section explains what each surface gets checked, how to verify on the host, and a Podman caveat: some Podman versions don't auto-run image-defined HEALTHCHECK and need explicit `--health-cmd` on the run command.
- **README: Quoting env values** — explicit callout in the Configuration section pointing at the quote-stripping behaviour and recommending users leave quotes off entirely.

### Verified
- **[#33](../../issues/33)** (@LeeNX not seeing v1.19.0 updates) — Docker Hub `:latest` digest was updated to v1.19.0 on 2026-06-06 15:53 UTC. No detection bug on our side. Most likely his local cron just hadn't fired between the push and his check; a manual `/check` would surface the update immediately. Followed up in the issue comment.

## [1.19.0] - 2026-06-05

### Fixed
- **Compose-stack containers no longer go into restart-loops after auto-update.** Internal report. When Docksentry's `_update_compose()` path can't see the host compose file (Docksentry's container without the host compose dirs mounted — the common deployment), it falls back to `_update_standalone()`. Until now the standalone path read only `HostConfig.NetworkMode` and emitted a single `--network <name>` — it did **not** restore `NetworkSettings.Networks[<net>].Aliases`. Compose-service hostnames (`db`, `redis`, `broker`, `app`, …) were silently dropped on recreate. After a Paperless-NGX or Nextcloud auto-update the recreated container could no longer resolve its companion services (`Error -5 connecting to broker:6379`, `db:5432 - no response`) and the stack entered a restart-loop until a manual `docker compose down && up -d` rebuilt the aliases.

  Standalone recreate now restores from `NetworkSettings.Networks[<primary>]`:
  - **Aliases** → `--network-alias <a>` for each (auto-id and container name filtered out — Docker re-adds them)
  - **Fixed IPs** → `--ip` / `--ip6` from `IPAMConfig`
  - **MAC address** → `--mac-address` (from `Config.MacAddress`, the user-set field)
  - **Legacy links** → `--link`

  Plus a new `_attach_extra_networks()` helper runs after `docker run` to `docker network connect` containers attached to **more than one network** (compose pattern: app on `frontend` + `backend`). The primary network is still set via `--network` on the run command; extras get their own connect call with aliases/IPs/links preserved.

### Added
- **Full HostConfig + Config coverage in standalone recreate.** Closing the same field-coverage gap class as v1.18.10 (#27, CapAdd/Devices) once and for all. New fields restored on recreate:
  - **Memory limits**: `Memory`, `MemorySwap`, `MemoryReservation`, `KernelMemory`, `KernelMemoryTCP`, `MemorySwappiness` (compose `mem_limit`, `memswap_limit`, `mem_reservation`).
  - **CPU limits**: `NanoCpus` → `--cpus`, `CpuShares`, `CpuPeriod`, `CpuQuota`, `CpuRtPeriod`, `CpuRtRuntime`, `CpusetCpus`, `CpusetMems` (compose `cpus:`, `cpu_*`).
  - **Process / OOM**: `PidsLimit` (compose `pids_limit`), `OomScoreAdj`, `OomKillDisable`.
  - **Block-IO**: `BlkioWeight`.
  - **Ulimits**: `Ulimits` array → `--ulimit name=soft:hard` (compose `ulimits:`).
  - **Groups**: `GroupAdd` → `--group-add` (compose `group_add:`).
  - **Lifecycle**: `AutoRemove` (only when no restart policy), `StopSignal` (compose `stop_signal:`), `StopTimeout` (compose `stop_grace_period:`).
  - **Process config**: `WorkingDir` (compose `working_dir:`), `Domainname`, `Tty` (compose `tty:`), `OpenStdin` (compose `stdin_open:`).
  - **Healthcheck override**: full `Config.Healthcheck` restored as `--health-cmd`, `--health-interval`, `--health-timeout`, `--health-start-period`, `--health-start-interval`, `--health-retries`, or `--no-healthcheck` (compose `healthcheck:` overrides the image's HEALTHCHECK).

- **Image-default-aware Cmd / Entrypoint restoration.** Previously the standalone recreate blindly restored `Container.Config.Cmd` on every update, which would **lock in the OLD image's CMD** when the new image release changed it. Now we read `docker image inspect` for the image's own Entrypoint+Cmd defaults and only restore the container-level value when it actually differs (i.e. user explicitly overrode). When the image inspect fails or the caller hasn't fetched defaults, we fall back to pre-v1.19.0 behaviour so nothing regresses.

- **Inspect-coverage audit logger.** Debug-only. Walks each container's inspect dict before recreate and logs `[audit] HostConfig.<key>` / `[audit] Config.<key>` for any non-default value in a field we *don't* restore *and* don't intentionally skip. Future Docker versions adding new keys will surface here instead of being silently dropped on recreate — turning the next "lost on recreate" bug from user-discovered into self-discovered. Enable with `DEBUG=true` in env.

### Changed
- `_build_run_args(config, image, name)` gained an optional `image_defaults={Entrypoint,Cmd}` parameter. Backward-compatible: passing `None` (the historical default) keeps the pre-v1.19.0 Cmd-restore behaviour. The standalone update path and the self-update helper in `telegram_bot.py` both now fetch + pass it.

### Why this is v1.19.0 not v1.18.14
- Multiple behavioural changes for compose-container recreate (network aliases now preserved, resource limits now preserved, healthcheck overrides now preserved). All changes are in the "restore more state than before" direction — no field that worked before is dropped — but the recreated container surface area is meaningfully larger, so a minor bump is more honest than a patch bump.
- No user-facing API or config changes. Existing setups need no migration. The next `docker pull` + `docker compose up -d` is enough.

## [1.18.13] - 2026-06-05

### Docs
- **README "Multi-bot setup" section now explicit that each host needs its own bot token.** Surfaced by @LeeNX in [#23](../../issues/23): he read the section as "I just need a unique `BOT_LABEL` per host" and ran two instances with the same `BOT_TOKEN`, which produced a Telegram 409 Conflict (one bot token = exactly one polling consumer). The YAML examples already showed distinct tokens, but the checklist only highlighted distinct `BOT_LABEL`. Now:
  - A new explanatory note up front explains *why* a separate token per host is required (`BOT_LABEL` is only a visual prefix; token is the identity).
  - The setup checklist now leads with "create one bot per host with `/newbot` in @BotFather" as step 1 and spells out distinct `BOT_TOKEN` + distinct `BOT_LABEL` + same `CHAT_ID` in the final configure step.

## [1.18.12] - 2026-06-05

### Fixed
- **Container health status no longer shows as just "running" under Podman.** Closes [#28](../../issues/28). Reported by @LeeNX. `/status` and the Web UI's container list both derived health (`🟢 healthy` / `🔴 unhealthy` / `🟡 starting`) by grepping `(healthy)` / `(unhealthy)` / `(health: starting)` substrings out of `docker ps`'s human-readable Status column. That worked on Docker because the CLI appends those markers cosmetically — but Podman's REST API returns the Status field *without* those suffixes (Docker CLI-only cosmetic, not part of the OCI/Docker REST API contract), so every healthy container under Podman fell through to the default ⚪ running icon.

  Both `/status` (Telegram) and `_get_containers()` (Web UI) now batch-inspect running containers and read `State.Health.Status` directly — consistently provided by both Docker and Podman APIs. Side benefit: uptime in `/status` is now computed from `State.StartedAt` instead of parsed from a string, so the format matches the per-container `/status <name>` detail view.

## [1.18.11] - 2026-06-05

### Docs
- **README: "Experimental Podman support" section** (#23, requested by @LeeNX). No code changes — Podman implements the Docker REST API, so mounting a Podman socket at the path Docksentry expects the Docker socket is enough for most operations. The new section documents:
  - Rootful setup (`/run/podman/podman.sock`)
  - Rootless setup (`/run/user/$UID/podman/podman.sock`)
  - What's expected to work (read-only inspection, pulls, lifecycle commands, container groups, the v1.18.10 17-field HostConfig recreate — all hit the Docker REST API endpoints Podman implements natively)
  - Known limitations: rootless UID-mapping edge cases for the [#16](../../issues/16) PID-1 self-protection, Quadlets out of scope, podman-compose label format variations, multi-arch availability
  - How to file targeted bug reports (Podman version + rootful/rootless + architecture + exact failure mode) so we can add specific fixes
- Auto-syncs to Docker Hub description via the existing GitHub Actions workflow.

## [1.18.10] - 2026-06-05

### Fixed
- **Standalone recreate silently dropped 17 HostConfig fields — broke Gluetun and every other VPN / firewall / capability-using container.** Closes [#27](../../issues/27). Reported by @famewolf. The reconstruct-from-inspect logic in `_update_standalone()` and `_do_selfupdate()` only emitted RestartPolicy, NetworkMode, PortBindings, SecurityOpt, Mounts, Env, Hostname and Labels. CapAdd, CapDrop, Devices, Privileged, Sysctls, Tmpfs, ExtraHosts, Dns/Search/Options, Init, ShmSize, ReadonlyRootfs, LogConfig, Runtime, Ipc/Pid/UTS modes and User were all silently discarded on every recreate.

  For Gluetun specifically: `cap_add: NET_ADMIN` + `devices: /dev/net/tun` were lost → iptables init failed inside the new container → health-check rolled the update back → dependent containers (sonarr, radarr, qbittorrent, sabnzbd, …) left without their network namespace until a manual `rebuild_all`.

  Single source of truth `UpdateChecker._build_run_args(config, image, name)` now covers **17 HostConfig fields**, used by both the standalone update path and the selfupdate helper. Empirically verified end-to-end: built run args from a Gluetun-flavoured test container, executed `docker run`, confirmed identical HostConfig on the recreated container.

- **`restart_dependents` cascade now fires on head-container update *failure* too.** Same #27. The v1.17.0 cascade only kicked dependents on success — but the failure-and-rollback path is exactly when dependents need a kick most: their network namespace is torn down when the head container is stopped, and the rollback brings the head back but doesn't tell the dependents to re-attach. They sit on a stale namespace until something restarts them. Now we kick them on failure too, with a `🔁 head rollback — dependents kicked` marker in the result message so the user can tell why the cascade fired.

### Docs
- **README: "Compose-managed containers" section** explains why "Compose file not found: … — falling back to standalone" appears in logs (Docksentry's own container can't see the host path docker inspect records) and how to mount compose dirs read-only into Docksentry to use the compose path instead.

## [1.18.9] - 2026-06-04

### Fixed
- **`/check` during an ongoing update no longer reports in-flight containers as "Updates Available".** Closes [#26](../../issues/26). Reported by @famewolf. While `run_updates()` was processing 3 containers, hitting `/check` again would re-run `check_all()` — which still saw the two not-yet-recreated containers as on their old digest, so they'd appear in a second "Updates Available" notification ~30s after the user already tapped "Update all". `run_updates()` was already single-instance protected (taps on the duplicate notification just returned "update already in progress" — no data harm), but the cosmetic confusion was real. `/check` now honours the same `update_running` flag and refuses with "Update already in progress…" instead. Reuses the existing `update_already_running` i18n key, no new translations.

## [1.18.8] - 2026-06-04

### Fixed
- **Partial-name resolver no longer hides stopped containers by default.** Closes [#25](../../issues/25). Confirmed by @famewolf: in his 3-bot setup most containers are stopped (not absent) on the duplicate hosts, so `/status jellyfin` was getting `not found` from those bots instead of the correct `stopped` state. `/logs <stopped>` was unusable for the same reason — and stopped is exactly when you want logs (to see *why* it died). The v1.18.7 call to keep running-only as default for non-lifecycle commands was the wrong instinct. Flipped: `_resolve_container()` now defaults to `include_stopped=True`. Filters out `_old`-suffix containers (our internal rollback leftovers from failed updates) so they don't pollute the picker. Callers that genuinely need running-only can opt out via `include_stopped=False`.

- **Multi-bot `@botname` targeting now actually targets.** Closes [#25](../../issues/25). v1.18.5's normalize block stripped `@<anything>` unconditionally, so in a 3-bot group `/status@dockmox-bot jellyfin` made *all three* bots respond — defeating the point of targeted addressing. Bot now queries `getMe` at startup to learn its own username and respects targeting: `@<own-username>` strips and handles, `@<other-bot>` silently ignores. Commands without `@` still broadcast to all bots (the common case for `/selfupdate` across all hosts).

### Docs
- **README "Multi-bot setup" section** now documents the broadcast vs. targeted-`@bot` behaviour with a worked example.

## [1.18.7] - 2026-06-04

### Fixed
- **`/start <stopped-container>` failed with "Container not found"** even on exact name match. Closes [#24](../../issues/24). Surfaced by @famewolf's question in #2. The partial-name resolver behind all lifecycle commands was hardcoded to `docker ps` (running only), so stopped containers were invisible — defeating the main use case of `/start`. Added `include_stopped=False` parameter to `_resolve_container()`; lifecycle commands (`/start`, `/stop`, `/restart`) pass `True` so they see `docker ps -a`. Everything else (`/pin`, `/logs`, `/unpin`, `/autoupdate`, `/notes`, etc.) keeps the running-only default so their pickers don't surface stopped containers where it would be confusing.

  Empirically verified before commit:
  - Default (`include_stopped=False`) on stopped container → still returns "not found" (existing behaviour preserved for the picker-style callsites)
  - `include_stopped=True` on stopped container → resolves correctly (exact + partial match)

## [1.18.6] - 2026-06-01

### Added
- **Version numbers in `/history`**. Closes [#22](../../issues/22). Requested by @famewolf. Two layers, both opt-in by data availability:

  **A. Docksentry self-update — precise.** `_save_selfupdate_history()` writes `(selfupdate v{OLD} → ?)` at the swap (we know our own version), and a post-boot fixup in `main.py` patches the `?` with the freshly-booted process's `VERSION` once the new container starts. Re-uses the v1.16.3 deferred-check marker we already have. Result:
    ```
    ✅ docksentry — 2026-06-01 14:11:10
        🗓️ 2026-05-31 → 2026-06-01 (selfupdate v1.18.5 → v1.18.6)
    ```

  **B. Other containers — best-effort from OCI labels.** Reads `org.opencontainers.image.version` before and after each pull; appends ` (v{old} → v{new})` to the detail line when both labels exist and differ. Empirical coverage on a 15-container real-world stack: ~40 % (n8n, mariadb, adguardhome, gitlab, paperless-ngx, open-webui carry it; nginx-proxy-manager, redis, postgres, nextcloud, portainer, influxdb don't). Label values are normalized — single leading `v` stripped, obviously-bogus values (12-char image IDs, branch names like `main`/`latest`) discarded. Containers without a usable label render the same as before (date + size only), so no regression for the 60 % that don't carry the label.

### Internal
- New `UpdateChecker._get_image_version_label()` + `_normalize_version_label()` + `_format_version_arrow()` static helpers. Unit-tested against 10 real-world label values incl. adguard's doubled `vv0.107.73`, gitlab's container-ID-as-label, branch-name-as-label, etc.

## [1.18.5] - 2026-06-01

### Fixed
- **Commands silently dropped in multi-bot groups.** Closes [#21](../../issues/21). Reported by @famewolf. A v1.18.2 regression: registering all commands via `setMyCommands` triggered Telegram's standard group-multi-bot-disambiguation, so tapping `/check` in a group with ≥ 2 bots actually sent `/check@dockmox-bot`. Our `_handle_message` matches by `text == "/check"` everywhere — the `@botname` form fell through and got silently dropped.

  Affected every BOT_LABEL multi-host setup on every command. 1:1 chats were unaffected.

  Fix: strip the `@<botname>` suffix from the first token at the top of `_handle_message`, then the existing per-command matchers route normally. All 19 commands inherit the fix automatically with no per-command changes. User mentions later in the message (e.g. `/notify @someone hello`) are preserved — the strip only touches the first token. 10-case unit test in commit `45...`.

## [1.18.4] - 2026-05-31

### Fixed
- **Self-update notifications now reach Discord and webhook channels.** Closes [#19](../../issues/19). Reported by @NotRetarded. All four selfupdate notification sites (manual `/selfupdate`, auto with `defer_check=True`, auto with `defer_check=False`, post-restart "checking your containers" message) previously called only `bot.send_message()` — which is Telegram-only — without a parallel `notifier.send_message()` call. Headless users (Web UI + Discord/webhook, no Telegram) got no visibility into self-updates at all. Same pattern as `main.py`'s startup-message handling.

### Added
- **Container names in update notifications are now clickable links.** Closes [#20](../../issues/20). Requested by @NotRetarded, à la WhatsUpDocker. When an update is reported, each container name is wrapped in a markdown link pointing at the source repo / changelog / registry page so you can preview release notes before deciding to apply.

  Resolution chain (first match wins):
  1. **Manual override** stored via Web UI (per-container "Repo / changelog link" text field on the Container Detail page)
  2. **`org.opencontainers.image.source`** OCI label
  3. **`org.opencontainers.image.url`** OCI label
  4. **Registry-overview heuristic** (Docker Hub `_/<image>`, ghcr.io / quay.io / lscr.io → fleet.linuxserver.io)

  Coverage across surfaces:
  - **Telegram:** `[name](url)` markdown link inline with the container row
  - **Discord:** appends a `[Source ↗](url)` line to each embed field's value (field names don't render links in Discord, but field values do)
  - **Generic webhook:** new `source_url` field per container in the JSON payload — downstream automations (Home Assistant, Ntfy, custom scripts) can route on it

  Auto-detection re-uses the v1.18.3 `/changelog <container>` helpers, so existing OCI-labelled images get the feature for free. Manual override on `/data/container_links.json` lets users fill in the gaps for popular labelless images (redis, postgres, nginx-proxy-manager, …).

### i18n
4 new keys (`web_link_title` + `_intro` + `_placeholder` + `_save`) × 16 language files. EN + DE translated; 14 others fall back to EN.

## [1.18.3] - 2026-05-30

### Added
- **`/changelog <container>`** — link-only. Closes [#14](../../issues/14). Looks up the container's `org.opencontainers.image.source` OCI label and sends the upstream repo URL. Three fallback tiers:
  1. **Source label present** → "here's the upstream source repo, releases at `/releases`, changelog at `/blob/main/CHANGELOG.md`"
  2. **Only `image.url` label** → "here's the product page, look for a Changelog section"
  3. **No label** → registry overview page heuristic (Docker Hub, ghcr.io, quay.io, lscr.io / fleet.linuxserver.io)

  **Deliberately no parsing.** Tried it empirically against 15 real containers: hit rate for fetchable + parseable upstream CHANGELOG was ~33 %. The remaining 67 % would produce "no changelog available" responses — a confusing UX worse than no feature. Honest link to the source repo is what we ship; users decide where to go from there.

### Internal
- **Single source of truth for commands** — module-level `_BOT_COMMANDS` table at the top of `telegram_bot.py` now drives both `setMyCommands` (the Telegram picker) AND the `/help` output. Adding a new command is one line; both consumers update in lockstep. Eliminates the previous three-place drift risk (handler + manual `/help` list + manual picker list).
- `/help` output is now derived: it iterates `_BOT_COMMANDS` and dedup's by shared i18n key (start/stop/restart all share `help_lifecycle`, so it shows once). The visible result is identical to v1.18.2, but the code is now ~30 lines shorter.

### i18n
4 new keys for the `/changelog <container>` response paths × 16 language files. EN + DE translated; 14 others fall back to EN.

## [1.18.2] - 2026-05-30

### Added
- **Telegram command picker via `setMyCommands`.** All 19 bot commands are now registered with Telegram on startup. Users get the native autocomplete popup when they type `/` in the chat — one-line description per command. The industry-standard Telegram bot UX that should have been here from day one. Idempotent on every boot, so new commands surface without any setup step on the user's side.
- **`/help` discovery hint.** A one-line tip at the top of `/help` points users at the `/` autocomplete picker, and a docs-link footer points at the README. Together they cover the spectrum from "I forgot the command name" (picker) to "I need details" (README) without adding 14 inline buttons or 16 × 19 i18n strings for per-command detailed help.

### Why no `/help <command>` (re #15)
While shipping this I realised the native command picker plus the README link covers the practical use cases for command discovery. `/help <command>` is left tracked in [#15](../../issues/15) but not built — if it turns out users actually want it after they have the picker, we'll revisit. Comment with your experience.

### i18n
2 new keys (`help_autocomplete_hint`, `help_docs_footer`) × 16 language files. EN + DE translated; 14 others fall back to EN.

## [1.18.1] - 2026-05-30

### Added
- **`/selfupdate <version>` — pin to a specific release or roll back.** Closes [#12](../../issues/12). Three forms:
  - **`/selfupdate`** — current behaviour, pulls whatever tag the container is on (usually `:latest`)
  - **`/selfupdate 1.17.4`** — pin to a specific semver tag. Useful when a release broke something and you want to stay on a known-good version, or when you need to test against an older build
  - **`/selfupdate previous`** — auto-detects the version older than the running one by reading the upstream CHANGELOG, no need to remember which version came before. Suggested by @famewolf

  Input validation refuses non-semver targets (`v1.17.4`, `latest`, `1.2`, etc.) with a clear example before triggering the helper container — saves the user from a mid-restart `docker pull` failure on a malformed tag.

- **Docker registry authentication** (`DOCKER_USERNAME` / `DOCKER_PASSWORD` / `DOCKER_AUTH_CONFIG`). Closes [#18](../../issues/18). Bypasses Docker Hub's anonymous 100-pull-per-6h-per-IP rate limit. Three input modes:
  1. **`DOCKER_AUTH_CONFIG`** — path to an existing Docker `config.json`. Best for users who already manage credentials outside Docksentry (mount your host's `~/.docker/config.json` read-only).
  2. **`DOCKER_USERNAME` + `DOCKER_PASSWORD`** — we run `docker login` once at startup. Simpler if you don't already have a config file. Set `DOCKER_REGISTRY` for non-Docker-Hub registries (ghcr.io, quay.io, internal Harbor, …).
  3. Neither set — anonymous pulls (existing default).
  
  Login failures are non-blocking: a clear warning is printed and the bot continues with anonymous pulls. Credentials are env-only — never persisted to `settings.json` so they don't end up on the data volume.

### Polish
- **README "What's different" section** between the hero screenshots and Features. Positions vs Watchtower (set-and-forget) and Diun (notify-only), then lists the six things that actually distinguish Docksentry: tap-to-update, container groups, lifecycle commands, auto-rollback, maintenance mode, multi-bot setup.
- **Docker Hub short-description** updated to match — now mentions Telegram + Web UI + Discord + webhooks + lifecycle commands (96 chars, Hub limit is 100). Auto-syncs on every README push via the existing GitHub Actions workflow.

### i18n
3 new keys × 16 language files. EN + DE translated; 14 others fall back to EN.

## [1.18.0] - 2026-05-30

### Added
- **Container lifecycle commands.** Closes [#17](../../issues/17). Three new Telegram commands plus per-container detail view, all with partial-name matching like `/pin`:
  - **`/start <name>`** — start a stopped container
  - **`/stop <name>`** — graceful stop, reuses the v1.17.5 timeout logic (respects each container's own `Config.StopTimeout`)
  - **`/restart <name>`** — stop + start in one shot with a 30s grace
  - **`/status <name>`** — per-container detail: state, health, uptime, image, host port mappings, volumes count, restart policy. Includes **inline action buttons** (`▶️ Start` / `🟥 Stop` / `🔁 Restart`) that adapt to the container's current state, so the common case is a single tap.

- **Web UI: Stop / Restart buttons** on the Status page rows. Restart is shown in both UI modes (reversible, low-risk); Stop is advanced-only with a confirm dialog because it leaves the container offline.

- **Catch-all for hung containers.** Requested by @famewolf (the homarr-stuck-after-timeout case from [#11](../../issues/11)): when a container is left in a weird state, you can now `/start`, `/stop`, or `/restart` it directly from Telegram without leaving the chat to open Portainer.

### Safety
- **Self-kill guard reused from v1.17.7.** `/stop docksentry`, `/restart docksentry`, the equivalent inline buttons, and the Web UI Stop/Restart buttons on Docksentry's own row are all refused with a clear message pointing to `/selfupdate` — same `_would_kill_self()` check that catches the regular update flow in #16. The lifecycle commands cannot kill the bot by accident.

### Scope
Deliberately **not** added: `/remove`, `/prune`, `/inspect`, `/exec`, `/stats`, bulk `/restart unhealthy`. The goal is "lifecycle control for containers that already exist", not "Portainer via chat" — those features are out of scope for this release and discussed individually in their own issues if interest emerges.

### i18n
14 new keys × 16 language files. EN + DE translated; 14 others fall back to EN.

### Internal
- New `TelegramBot._container_state()` helper builds the per-container detail dict from `docker inspect`.
- New `TelegramBot._lifecycle_action()` is the single entry point for start/stop/restart — used by both Telegram commands AND the inline-button callbacks AND the Web UI POST handler, so behaviour is identical across surfaces.
- New `/api/lifecycle` POST endpoint.

## [1.17.7] - 2026-05-30

### Fixed
- **Critical: self-kill when DockSentry is in its own auto-update list.** Closes [#16](../../issues/16). Reported by @NotRetarded. If users added DockSentry to their `/autoupdate` list (or hit "Update all" / Web UI "Update" on DockSentry's row), the regular update flow called `docker stop` on the running container — which kills PID 1 immediately, so the rename + `docker run` recreate steps never executed. The container ended up stopped on the new image and never came back up. DockSentry can only safely update itself via the dedicated helper-container path (`/selfupdate` or `AUTO_SELFUPDATE=true`).

  Defense in depth, three layers:
  1. **More robust self-detection.** The old `HOSTNAME` env var lookup silently missed in some compose / orchestrator setups (the storage-driver overlay path in `/proc/self/mountinfo` and the cgroups v2 unified hierarchy at `/proc/self/cgroup` aren't reliable here — they carry different identifiers). New `_own_container_id()` uses `HOSTNAME` then `/etc/hostname` to resolve via `docker inspect`, caches the full container ID, and uses *that ID* (not name) for self-comparison.
  2. **`update_container()` bottleneck.** Every code path that issues `docker stop` now goes through this method, and it refuses (with a clear message pointing to `/selfupdate`) when the target container ID matches our own. Even if a future feature or third-party caller bypasses the check_all filter, the bottleneck catches it.
  3. **Boot-time migration.** If a previous version saved DockSentry into `autoupdate_containers.json`, the entry is stripped on next start and a one-shot Telegram / Discord / webhook notification explains what happened and what to use instead (`/selfupdate` manually or `AUTO_SELFUPDATE=true` env var).

  Sneaky secondary bug found while testing the guard: the original implementation used `target_id.lstrip("sha256:")` to strip the prefix on image-style IDs — but `str.lstrip` strips any leading character in the set `{s, h, a, 2, 5, 6, :}`, so a hex ID starting with `2`, `5`, `6`, or `a` got silently corrupted (leading char chewed off). Now uses an explicit `startswith` + slice.

### i18n
1 new key (`migration_self_autoupdate_removed`) × 16 language files. EN + DE translated.

## [1.17.6] - 2026-05-30

### Fixed
- **`/history` and the Web UI history page now record Docksentry's own self-updates.** Closes [#13](../../issues/13). Previously only container updates appeared — `_save_history` was wired from `update_checker.update_container()` but never from the self-update path. New `TelegramBot._save_selfupdate_history()` helper writes an entry just before the helper container restarts the bot, so the event isn't lost in the process death. Detail uses the same date-arrow format as regular updates with a trailing `(selfupdate)` marker so the Web UI doesn't need special rendering. Reported by @famewolf in #2.

### Docs
- **README "Group / Topic setup" section** now starts with a prominent warning that **Group ≠ Channel** in Telegram — channels are broadcast-only and have no incoming messages for the bot to receive (the most common cause of "bot posts but doesn't respond" reports). Also added the `/setprivacy` cache caveat: toggling it in BotFather after the bot has joined the group doesn't apply retroactively — `docker compose down/up` of Docksentry won't help, the bot has to be kicked and re-added. Both based on @famewolf's first-time-setup feedback.

## [1.17.5] - 2026-05-30

### Fixed
- **`docker stop` 60s hardcoded — containers with longer StopTimeout failed.** Reported by @famewolf in [#11](../../issues/11). Updating a slow-stopping container (homarr was the example — DBs, log aggregators, anything with a tuned `--stop-timeout` similar story) would fail with `Command 'docker stop X' timed out after 60 seconds`. Worse: Docker kept stopping in the background and eventually finished, so the container ended up stopped on the new image but never got recreated — the user found it offline next morning.

  The stop logic now:
  1. **Reads `Config.StopTimeout` from `docker inspect`** (per-container)
  2. **Passes `--time N` to `docker stop`** so Docker's grace aligns with what we expect
  3. **Subprocess wait is `max(default, StopTimeout) + 30s`** for headroom around the SIGKILL phase
  4. **Falls back to `docker kill`** if even that's exceeded — so we never leave the recreate flow half-finished
  5. Same logic applied to the rollback `docker stop` path

### Added
- **`DOCKER_STOP_TIMEOUT` env var** (default `60`, also Web UI / persistent). Minimum subprocess timeout for `docker stop`; effective wait is `max(this, container.Config.StopTimeout)`. Raise globally for stacks with slow-shutdown apps.

## [1.17.4] - 2026-05-29

### Added
- **`/check` now flags Docksentry self-updates separately.** When a manual `/check` includes Docksentry itself among the updates, the bot follows up with `🚀 Docksentry update available — run /selfupdate to apply, or /changelog to preview what's new.` Previously the self-update was listed alongside container updates without any hint that it needs a different command, so users could miss bot releases entirely. Requested by @famewolf in #2.
- **`/changelog` command.** Fetches `CHANGELOG.md` from the GitHub raw URL and shows every version newer than yours — versions, dates, and the full release notes. Great for deciding whether to defer a `/selfupdate` after the new "Docksentry update available" hint surfaces one. Falls back gracefully when the network is unreachable.

### Fixed
- **Telegram API parse-error responses (HTTP 4xx) are now retried without Markdown** instead of being treated as network failures. Lets `send_message` recover from edge cases where a body legitimately can't be parsed as Markdown (long quoted bodies, stray brackets, mismatched asterisks). Previously the request silently dropped on the floor.

### Internal
- New helpers in `telegram_bot.py`: `_own_container_meta()` (caches the running Docksentry container's name + image, used for self-update detection), `_fetch_changelog()` + `_parse_changelog_entries()` (GitHub-raw fetch + version-block parsing), `_github_md_to_telegram()` (rewrites GitHub-flavoured Markdown to Telegram's classic variant so `**bold**` and `#` headings don't break the renderer).
- `/changelog` builds its message entry-by-entry and stops at the cap so truncation never lands mid-`*bold*` (which would leave an unpaired asterisk and force the Markdown-fallback retry path).

### i18n
7 new keys × 16 language files (EN + DE translated, others fall back to EN).

## [1.17.3] - 2026-05-27

### Added
- **`BOT_LABEL` — multi-bot-friendly notification prefix.** Optional env var / Web UI field (max 32 chars). When set, every outgoing notification gets the label prepended so multiple Docksentry instances sharing a chat/channel can be told apart:
  - **Telegram:** `🖥 pve1 · 🔄 Auto Self-Update / …`
  - **Discord:** label added to embed title + footer (`Docksentry · pve1`)
  - **Generic webhook:** new top-level `bot_label` field in the JSON payload so downstream automations can route per-host
  - Empty (default) keeps the previous single-host behaviour.
- **README: "Multi-bot setup (one group, multiple hosts)" section** — step-by-step for running one Docksentry per Docker host into a single Telegram group, with `BOT_LABEL` for identification. Includes a security checklist: lock down with `TELEGRAM_ALLOWED_USERS`, keep the group private, privacy-mode implications. Bridging pattern until v2.0 ships real multi-host support.
- New i18n keys `web_bot_label`, `_help`, `_placeholder` (EN + DE; 14 langs fallback to EN).

## [1.17.2] - 2026-05-27

### Fixed
- **Slow-startup apps (GitLab, Nextcloud, Mastodon, …) auto-rolled-back every cron tick.** The health-check timeout was 5 minutes, and a container still in `state=running, health=starting` after that was treated as a failure → automatic rollback. GitLab routinely needs 8–15 minutes for first-boot migrations / Rails warm-up. Result: the user got a "Health check failed — rolled back" notification every evening and GitLab never actually updated.

  Three changes:
  1. **Default wait raised from 300s → 600s** (`HEALTHCHECK_MAX_STARTING` env, configurable).
  2. **Respect the image's own `Healthcheck.StartPeriod`** — the effective wait is now `max(default, start_period × 1.5)`. An image declaring `start_period: 5m` no longer gets cut off after our 5-minute default.
  3. **`state=running AND health=starting` after the wait → no rollback.** The container is alive but slow; Docker's own healthcheck keeps running and will eventually flip the bit. Reported as a soft success ("⚠ updated but still 'starting' — left running") so the rest of the update batch / group continues.
  Active `unhealthy` or `not-running` still rolls back as before (standalone path) — only the previously over-eager "starting → failure" verdict is gone.

- **Compose-path "rolled back" messages were misleading** — for compose containers the "rollback" was effectively a no-op (the same compose file produces the same container). The message now says `container left in place (compose)` honestly instead of claiming a rollback that didn't happen.

### Added
- **Last 10 log lines attached to health-check warnings and failures.** When a container is rolled back or reports as "still starting", the notification now includes a code-fenced tail of its logs — so you can see in chat what was happening without SSH-ing to the host.
- **`HEALTHCHECK_MAX_STARTING` env var** (default 600s) for users with super-slow apps that need an even longer ceiling. Persisted via `settings.json` (advanced setting).

## [1.17.1] - 2026-05-27

### Fixed
- **Duplicate "Updates Available" notification after an auto-self-update.** When the scheduler restarted via the v1.16.3 self-update-first flow, both the deferred check (running on the freshly-booted process) AND the regular cron-tick of the same minute would fire `check_all()` — the user got two identical "Updates Available" messages, roughly a minute apart. The deferred-check resume now claims the current minute via `self._resumed_minute`, and the main scheduler loop initialises `last_check` from it, so the cron-tick for the already-handled minute is skipped.
- **Three near-identical restart messages.** A self-update produced `Starting self-update — your container update check will resume right after restart.` → generic `🚀 Docksentry started (v…)` → `✅ Restarted on the new version. Now checking your containers…` in quick succession. The generic startup message is now suppressed when a deferred-check marker is present at boot, leaving a cleaner sequence of three distinct steps (pre-restart notice → resume notice → updates report).
- The resume notice now includes the new version: `✅ Restarted on v1.17.1 — checking your containers...` (was: vague "on the new version").

Both reported from a real-world v1.17.0 deployment.

## [1.17.0] - 2026-05-26

### Fixed
- **Recreate crash on Gluetun-style stacks** — containers with `network_mode: "container:gluetun"` (or `"service:..."`) failed to update with `docker: Error response from daemon: conflicting options: hostname and the network mode`, then rolled back. Cause: Docksentry's recreate logic added `--hostname` and `-p` port flags from the inspect data, but Docker forbids those when a container inherits another's network namespace — they belong to the namespace owner, not the dependent. Fixed by detecting `NetworkMode` prefix `container:` / `service:` and skipping those flags. Reported by @famewolf in #2.

### Added
- **Container Groups: restart-dependents flag.** Extension of the v1.15 Container Groups feature: a group can now be flagged "restart dependents". When the FIRST container in the group (the "head", e.g. Gluetun) is updated, all other members are restarted after the head reports healthy. Covers the VPN-sidecar workflow where dependents share the head's network namespace and lose connectivity when the namespace owner restarts. New checkbox on the group edit form (Advanced UI mode), persisted as `restart_dependents: true` in `groups.json`. Includes a health-wait poll (up to `wait_seconds`) so we don't kick dependents while the VPN handshake is still in progress. If the head never reports healthy, dependents are restarted anyway with a log warning — a slightly-too-early restart is usually less bad than dependents stuck on a defunct namespace.

### i18n
3 new keys (`web_groups_restart_dependents`, `_hint`, `_badge`) × 16 language files. EN + DE translated; others get the EN fallback.

### Roadmap notes
- **Multi-host support is on the way** as a v2.0 item (one Docksentry instance managing several Docker hosts, with per-host pending/history and hostname-prefixed notifications). Big enough to need its own release window — happy to take any wishlist input via #2.

## [1.16.4] - 2026-05-20

### Fixed
- **Group / topic setup silently broken.** The bot accepted notifications into a group/topic but rejected every command and callback with "not authorised". Cause: the auth check compared `from.id` (the *clicker's* personal user ID) against `CHAT_ID` (which in a group is the group ID, often negative `-100…`). The two could never match outside a 1:1 chat, so every command in a group was silently dropped (messages) or refused (button clicks). Now compares `chat.id` (origin chat) instead — works for 1:1 chats, regular groups, and forum groups with topics. Reported by @jayjay3108 in #2.

### Added
- **`TELEGRAM_ALLOWED_USERS`** — optional comma-separated whitelist of Telegram user IDs allowed to control the bot. Empty (default) means "anyone in the configured chat" — the right behaviour for 1:1 chats. In a group, set this to lock down "Update all" / `/cleanup` / etc. to specific members. Env var + Web UI field (Channels tab, Advanced mode) + persisted in `settings.json`.
- **Debug-mode logging on auth failure.** With `DEBUG=true` (or the Web UI toggle), auth rejections now print the reason (mismatched `chat.id`, or user not in whitelist). Silent in non-debug so a shared group doesn't fill the log with drive-by-message noise.
- **README: Group / Topic setup section.** Step-by-step for running Docksentry in a Telegram group with topics, including the `@BotFather → /setprivacy → Disable` step that's easy to miss.

### i18n
3 new keys (`web_allowed_users`, `web_allowed_users_help`, `web_allowed_users_placeholder`) × 16 language files. EN + DE translated; others get the EN fallback.

## [1.16.3] - 2026-05-10

### Changed
- **Auto-self-update now runs *before* the container update check.** Previously a cron tick would (1) check all containers for updates and let the user click "Update all" on Telegram, (2) then auto-self-update mid-conversation — killing the bot while the user was still tapping buttons, and running the check itself on the *old* code. The new order is self-update first, restart, then check on the fresh image. The user gets one linear notification story ("self-updating, then checking your containers" → restart → "restarted, now checking…") instead of two unrelated ones with a process death in between. Only affects installs with `AUTO_SELFUPDATE=true`; default behaviour unchanged.

### Internal
- New `/data/deferred_check.json` marker — written by `check_selfupdate_auto(defer_check=True)` immediately before the helper container stops the process. The freshly-booted scheduler reads the marker on `start()`, runs `check_all()` on a background thread, and removes the marker. Markers older than 1 hour are discarded so a self-update that crashes the new image doesn't trigger a phantom check on the next manual restart.
- `check_selfupdate_auto()` now returns a bool (`True` = update applied, process about to die; `False` = no update or pull failed) so the scheduler tick can short-circuit cleanly.
- New i18n keys `selfupdate_restarting_then_check` and `selfupdate_resumed_check` (EN+DE; 14 other languages fall back to EN).

## [1.16.2] - 2026-05-10

### Fixed
- **Calendar emoji — revert v1.16.1 mistake.** v1.16.1 swapped the spiral-calendar `🗓️` for the basic-calendar `📅` based on a misread of @hypnosis4u2nv's original report. The basic glyph renders on Apple/iOS as a hard-coded "JUL 17" tile that looks like meaningful data — exactly the issue an earlier release had already fixed at his suggestion. All 5 code sites and 48 i18n strings reverted to `🗓️` (Telegram messages, Discord embeds, generic webhook detail strings, plus `selfupdate_current_version`, `selfupdate_dates` and `web_cron_schedule` across 16 language files). Reported again by @hypnosis4u2nv in #2 — apologies for the round-trip.
- **`/history` legacy entries.** `update_history.json` stores the full `detail` string (with emoji) at update time, so entries written between v1.16.1 and v1.16.2 still hold the wrong glyph on disk. The three rendering paths (`/history` Telegram command, container-detail Web UI history tab, global Web UI history page) now normalize `📅` → `🗓️` on display. The data file is left untouched (audit trail preserved); legacy entries simply render with the right glyph.

## [1.16.1] - 2026-05-07

### Fixed
- **Healthcheck for headless installs** — `app/healthcheck.py` previously hard-required `BOT_TOKEN` and called the Telegram API, so any container running Web-UI-only or webhook-only got marked `unhealthy` even when everything was working. The check now picks the right surface for the active config: Web-UI installs → TCP-probe `127.0.0.1:${WEB_PORT}`; Telegram installs → `getMe`; webhook-only headless → trust Docker's process supervision (exit 0). Reported by @hypnosis4u2nv in #2.
- **`/history` and update-result calendar glyph** — replaced the spiral-calendar emoji `🗓️` (Unicode 1F5D3) with the basic-calendar `📅` (1F4C5) everywhere it appeared in Telegram messages, Discord embeds, generic webhook payloads and the 16 language files. The spiral variant has spotty rendering across mobile clients (older Telegram on Android in particular fell back to a black `?` or `OBJ` box). The basic glyph has full Unicode 6.0 coverage. Spotted by @hypnosis4u2nv in #2.

## [1.16.0] - 2026-05-06

### Added

#### Maintenance Mode
- **Pause scheduled checks and notifications during host maintenance** — turn it on before pulling cables, rebooting the host, swapping disks, etc. Manual updates from Web UI / Telegram still work; only the cron-driven scheduler tick and auto-notifications (Telegram, Discord, generic webhook) are suppressed.
- **Web UI quick-buttons** on the Settings page: `1 hour`, `4 hours`, `1 day`, `Forever`. While active, every page shows a yellow **banner** at the top with the remaining time and a one-click "Disable" button so you never forget that maintenance is on.
- **Telegram `/maintenance` command** — toggle from chat with the same expressivity: `/maintenance` shows current state; `/maintenance 2h` (or `30m`, `1d`, `forever`) enables; `/maintenance off` disables. Listed in `/help`.
- State is persisted to `/data/maintenance.json` — survives restarts, expires automatically when the timer is up.

#### Container Notes
- Per-container **free-text memo** (max 2000 chars) — explain why this container is pinned, the reason it was excluded, what to check before updating, etc.
- Visible as a 📝 icon next to the container name in the Status table (full text in the tooltip on hover) and in a highlighted box on the container Detail page Overview tab.
- Edit on the container Detail Settings tab.
- Stored in `/data/container_notes.json`.

#### Simple / Advanced UI Mode
- New mode toggle in the Web UI header (👤 / 🛠 button next to the theme switcher).
- **Simple** hides the rarely-used controls — Debug toggle, Cleanup grace-hours / backup-days, Disk-warn percent, Weekly report, Telegram Topic ID, Container Groups, Update Windows, per-container Auto-update / Ask-before-major buttons. The basics (Update, Pin, Cleanup checkbox, Discord/Webhook, Quiet hours, Maintenance) stay visible.
- **Advanced** shows everything (the historical default).
- New installs default to *simple* via the wizard; existing installs keep *advanced* on upgrade.
- Persisted as `ui_mode` in `settings.json`.

### i18n
~30 new keys × 16 language files. EN + DE translated; other languages get the English fallback.

### Internal
- New module `app/maintenance.py` with `is_active`, `enable`, `disable`, `parse_duration`, `format_remaining` helpers. State file is the single source of truth, no in-memory cache.
- New persistent files: `/data/maintenance.json`, `/data/container_notes.json`.
- `notifier.Notifier._suppressed()` now checks maintenance in addition to quiet hours.
- `scheduler.Scheduler` skips the cron tick while maintenance is active.
- `telegram_bot.TelegramBot.send_message(auto=True)` honours maintenance for auto-notifications.
- `ContainerStore` gains `get_notes`, `get_note`, `set_note`.
- New POST endpoints: `/api/maintenance`, `/api/note`, `/api/ui_mode`.
- `Config.ui_mode` added to `PERSISTENT_KEYS`.

## [1.15.1] - 2026-05-06

### Changed
- **Group + Major-Confirm icons → SVG** — replaced the emoji glyphs (`📦` package, `⚠` alert) on the Status table badges, the Major-Update banner, the per-container detail Group row, and the Settings → Groups card headers. Emojis render differently across operating systems (Apple, Windows, Android) and sometimes show as `OBJ` boxes when the system font is missing the glyph. The new inline SVGs use `currentColor` and look identical everywhere. Reported by @hypnosis4u2nv in #2.
- New `_ICONS["package"]` plus an `_icon_label()` helper for badges that pair a small icon with text.

Notification text (Telegram messages, Discord embeds, generic webhook payloads) still uses emoji — those clients render emoji consistently across platforms via their own emoji sets, and plain-text channels can't embed SVG anyway.

## [1.15.0] - 2026-05-06

### Added

#### Container Groups (ordered update sequences)
- **Group containers** that need to update in a specific order — e.g. **database first, then app**, or **media stack: plex → sonarr → radarr**.
- Each group has a configurable **wait time** (default 30s) between containers, so the first one can fully come up before the next starts updating.
- **Failure aborts the group** — if container N in the group fails, the remaining members are skipped to avoid running a new app against an old (failed) database.
- A container can be in **at most one group** — saving the group automatically removes the listed containers from any other group (one-group-per-container invariant).
- New section on the Settings page: list of groups with `↑/↓` buttons to reorder containers within a group, plus an add-form below.
- Status table shows a `📦 GroupName` badge for grouped containers.
- Container-Detail Overview tab shows the group + position ("position 2 of 3").
- Group rules apply only to **auto-updates**. Manual single-container updates from the Web UI still work without triggering the group.

#### First-Run Wizard
- On the very first visit to the Web UI (when `web_setup_done` isn't set in `settings.json`), Docksentry **redirects you to `/setup`** for a 4-step onboarding:
  1. **Language** — pick from 16
  2. **Schedule** — quick presets (daily 6 PM / 6 AM / weekly / hourly) or custom cron
  3. **Channels** — Discord webhook / generic webhook URL plus a Telegram-status hint (env-only)
  4. **Auto-update mode** — *Notify only* / *Auto-update all* / *Pick later*
- Selecting "Auto-update all" turns on auto-update for every currently running container at once. *Pick later* / *Notify only* leaves the auto-update list empty and you toggle each container yourself afterwards.
- A discrete `Skip — I know what I'm doing` link sets the flag and lands you straight on the Status page (no settings touched).
- The flag is `web_setup_done` in `settings.json` — flip it back to `false` and restart to re-trigger the wizard for testing or onboarding a new admin.

### Internal
- ContainerStore exposes group CRUD: `get_groups`, `save_group`, `delete_group`, `reorder_group_container`, `get_group_for_container`.
- New persistent file: `/data/groups.json`.
- `handle_autoupdates()` sorts auto-update candidates by group + position, applies inter-container `wait_seconds`, and tracks an `aborted_groups` set on failure.
- New POST endpoints: `/api/group_save`, `/api/group_delete`, `/api/group_reorder`, `/api/wizard`, `/api/wizard_skip`.

### i18n
48 new keys × 16 language files. EN + DE translated; other languages get the English fallback.

## [1.14.0] - 2026-05-06

### Added

#### Container detail page
- The `/container/<name>` route is now a real page (was a v1.13.1 stub).
- **Overview tab** — image, size, created/started timestamps, status badge, compose project/service, configured update window, all per-container badges in one place.
- **History tab** — update-history filtered to this single container (last 50 entries).
- **Logs tab** — `docker logs` for this specific container with adjustable line count.
- **Settings tab** — toggle auto-update, ⚠ major-confirm, pin/unpin, plus a per-container update-window editor (HH:MM range + weekdays). Same window store as the global Update Windows section, just scoped.
- Container names on the Status page link directly to the detail view; tab state persists via localStorage.

#### Theme toggle (Light / Dark)
- New theme-toggle button in the header (sun/moon icon).
- Three states: `auto` (follows OS via `prefers-color-scheme`), `light`, `dark`. User choice persists via localStorage and is applied **before paint** to avoid the dark-to-light flash.
- Light theme uses GitHub-inspired tokens — every existing component picks it up because everything goes through CSS Custom Properties.

#### Weekly summary report
- `WEEKLY_REPORT_ENABLED=true` ships a once-a-week digest to all configured channels (Telegram, Discord embed, generic webhook).
- Configurable day of week (`WEEKLY_REPORT_WEEKDAY` 0=Mon..6=Sun) and hour (`WEEKLY_REPORT_HOUR`, default 9). Editable via Web UI under Settings → Notifications.
- Report contains: count of successful / failed / rolled-back updates over the last 7 days, current disk usage, top 5 most-updated containers.
- Idempotent: state file (`/data/weekly_report_state.json`) records the last send date; firing twice on the same day is impossible even if the scheduler restarts.

### Internal
- New module `weekly_report.py` (~150 lines) — pure aggregation + format functions, easy to test in isolation.
- Scheduler now runs a per-hour weekly-report check parallel to the cron-update tick.
- ContainerStore exposes `get_update_window(name)` and `is_ask_before_major(name)` for the detail page.

### i18n
40 new keys × 16 language files. EN + DE translated; other languages get the English fallback.

## [1.13.1] - 2026-05-05

### Web UI refresh

A structured polish pass that **also lays the foundation** for upcoming features (theme toggle, multi-host, per-container detail page).

#### Changed
- **CSS rewritten with Custom Properties** — all colors via `var(--*)` tokens. A future light-mode toggle is now a one-class swap on `<html>`, not a search-and-replace.
- **Settings page split into 5 tabs** — General / Updates / Cleanup / Notifications / Channels. Active tab persists via localStorage, so reloading the page keeps you where you were.
- **Status page action buttons → icon-only with tooltips** — `🔄 📌 ⚙ ⚠` instead of word buttons. Active state visualised via colored borders (active = accent, ⚠ enabled = warning).
- **Major-update banner uses the new `card-warn` style** — a subtle yellow accent gradient plus border, no inline styles.
- **Bulk-action bar polished** — selection count + visual divider + icon labels, contained in its own subtle panel.
- **Inline styles migrated to component classes** (`.btn-icon`, `.bulk-bar`, `.form-checkbox-row`, `.form-help`, `.section-divider`, `.empty`, `.help`, `.tabs`, `.toast`).

#### Added
- **Toast notifications** — saved/error feedback now slides in as a top-right toast and fades after 4s. Cross-page persistence via localStorage.
- **Confirm dialog** — destructive actions (Self-Update, Image Cleanup) now prompt for explicit confirmation via a modal. Wired via `data-confirm` attributes on forms — easy to add to more actions.
- **Help icons (`?`) with tooltips** — next to every non-obvious setting label. Hover reveals a multi-line explanation.
- **Empty states** — History page no longer shows an empty card with one line of text; gets a centered icon + title + hint pattern. Reusable for future pages.
- **Per-container detail route stub** — `/container/<name>` (linked from container names on Status). Page is a placeholder pointing at v1.14, but routing + URL is stable, so external links and bookmarks already work.

#### Foundation for upcoming features
- **Header has a `header-host-slot`** — empty for now, reserved for the multi-host selector planned for v2.0.
- **Tab system is reusable** — same JS component will power the per-container detail tabs in v1.14 (Overview / History / Logs / Settings).
- **Toast system is reusable** — first-run-wizard and long-running self-update notifications can hook into it.

### i18n
58 new keys added across all 16 language files. EN and DE are translated; other languages get the English fallback for the new keys (translation contributions welcome).

## [1.13.0] - 2026-05-05

### Added — Quality of Life Release

- **Update windows per container** — auto-updates can be restricted to a HH:MM time range on selected weekdays. Containers outside their window are skipped silently for that cron tick. Default: no restriction. Configurable via the new "Update Windows" section on the Settings page.
- **Major-update confirmation per container** — when the optional `⚠ on` toggle is set for a container, a SemVer major bump (e.g. `7.x → 8.0.0`) is held back from auto-updating and surfaces as a `⚠ Confirm` notification (Telegram inline buttons + Web UI banner). Patch and minor bumps go through as before. Detection uses the registry's `tags/list` endpoint and the same Bearer-token negotiation as the digest check.
- **Quiet hours** — `QUIET_HOURS_START` / `QUIET_HOURS_END` (HH:MM) suppress auto-notifications during the window. Manual command replies (Telegram /status, /check, …) always go through. Drops are silent — the user explicitly opted into "leave me alone during these hours". Wraps midnight automatically.
- **Disk space warning** — `DISK_WARN_PERCENT` (default 85, range 50–100). Once per day, Docksentry checks the data dir's filesystem and notifies via all configured channels if the threshold is exceeded. Optional `DISK_WARN_AUTO_CLEANUP` triggers an immediate cleanup pass when the warning fires.
- **Bulk actions in Web UI** — multi-select checkbox column on the Status table plus a bulk action bar: Update / Pin / Unpin / Auto-update on / Auto-update off across multiple containers in one click.

### Internal
- New helper modules `quiet_hours.py` and `update_window.py` keep the time-window logic decoupled and unit-testable.
- `ContainerStore` extended with update-window, ask-before-major and pending-major dictionaries (all stored under `/data/`).
- New persistent files: `update_windows.json`, `ask_before_major.json`, `major_confirmations.json`, `disk_warn_state.json`.

## [1.12.2] - 2026-05-05

### Added
- **`CLEANUP_GRACE_HOURS`** (default `24`) — image-age threshold below which images are protected from cleanup. Higher = safer; raise to `168` for a one-week buffer or `720` for a month. Editable via Web UI.
- **`CLEANUP_BACKUP_LOCAL_ONLY`** (default `false`) — before pruning, save unused **locally-built** images (those without a registry digest, i.e. not re-pullable) as tarballs in `/data/cleanup-backups/<timestamp>/`. Registry images are skipped because `docker pull` already covers them.
- **`CLEANUP_BACKUP_DAYS`** (default `7`) — retention window for backup tarballs. Older directories are removed on every cleanup run.
- **Cleanup result detail** — the post-cleanup notification now lists which images were removed (truncated to first 6) and notes how many local images were backed up, so you can spot something important disappearing.

### Changed
- **Calendar emoji** in update / self-update notifications: `📅` → `🗓️`. Apple/Discord/Slack rendered the old emoji with a hard-coded date number ("17"), which looked like meaningful data but was just cosmetic. The new spiral-calendar emoji has no fixed number. Suggested by @hypnosis4u2nv in #2.

## [1.12.1] - 2026-05-04

### Added
- **Automatic image cleanup** — new `AUTO_CLEANUP` env var / `Auto cleanup` toggle in the Web UI. When enabled, `docker image prune` runs after every successful auto-update to reclaim disk space. The 24h age filter (`until=24h`) keeps brand-new pulls intact, so rollbacks remain safe.
- Settings page: clearer description on the manual Cleanup / Self-Update buttons explaining when each runs automatically vs only on click.

### Changed
- **Cleanup logic centralised** — `UpdateChecker.cleanup_images()` is now the single entry-point used by Telegram `/cleanup`, the Web UI button, and the new auto-cleanup path.

## [1.12.0] - 2026-05-01

### Added
- **Headless mode** — Telegram is now optional. Docksentry runs fine with just the Web UI, just Discord, just a generic webhook, or any combination thereof. `BOT_TOKEN` / `CHAT_ID` are no longer required (but must still be set together if Telegram is wanted). Docksentry validates at startup that at least one notification/control channel is configured.
- **Web UI: Image Cleanup button** — runs `docker image prune` on demand from the Settings page. Was previously only reachable via Telegram `/cleanup`.
- **Web UI: Self-Update button** — triggers a self-update from the Settings page. Was previously only reachable via Telegram `/selfupdate`.
- **Settings page: Telegram status row** — shows whether Telegram is `enabled` or `disabled (headless)`.

### Changed
- **Pin / auto-update state extracted** into a new `container_store.py` module. The Web UI now reads/writes these lists directly instead of going through TelegramBot, so they keep working in headless mode.
- **Startup output** now reports `Telegram: ON / OFF` so you can immediately see which mode you're in.

### Migration
- Existing setups with `BOT_TOKEN` and `CHAT_ID` continue to work unchanged.
- To switch to headless mode: remove `BOT_TOKEN` and `CHAT_ID` from your environment, leave `WEB_UI=true` (or configure `DISCORD_WEBHOOK` / `WEBHOOK_URL`).

## [1.11.8] - 2026-05-01

### Fixed
- **Telegram long-poll timeout spam** — long-poll timeouts during `getUpdates` are an expected, normal occurrence on flaky networks (just means "no new messages within the long-poll window"). They were previously logged as `Telegram API error: The read operation timed out` and could spam logs. Real errors (HTTP 4xx/5xx, connection refused, JSON parse) are still logged.
- **Long-poll vs HTTP timeout balance** — long-poll window reduced from 30s → 25s, HTTP socket timeout adjusted to 40s (= 25 + 15s buffer). More headroom for slow TLS/DNS handshakes, faster reaction to genuinely dead connections.
- **`send_message` retry logic** — the no-Markdown retry only triggers when Telegram actively rejected the message (parse error, ok=false). Previously also retried on network failures, which couldn't help and just doubled the noise.

## [1.11.7] - 2026-04-30

### Fixed
- **Generic registry auth** — image checks now use the standard Docker Registry V2 Bearer-token negotiation (parse `WWW-Authenticate` on 401, fetch token from the advertised realm, retry). This makes update checks work for any spec-compliant registry without per-host hardcoding. Adds support for `lscr.io` (LinuxServer.io), `quay.io`, `gcr.io`, `registry.gitlab.com`, custom registries, etc. — Docker Hub & GHCR keep working unchanged.
- **Misleading "Up to date"** — when the registry was unreachable or returned an authorization error, Docksentry previously logged `→ Up to date`, suggesting the container was current. It now logs `→ Check FAILED (registry unreachable / unauthorized)` and skips the container instead of treating "unknown" as "ok".

## [1.11.6] - 2026-04-26

### Security
- **Webhook URL no longer logged in plaintext** — generic `WEBHOOK_URL` is now reported as `"configured"` on startup instead of printed in full. Prevents auth tokens (Ntfy, Gotify, Home Assistant) from leaking via `docker logs` or log aggregators.
- **Constant-time password comparison** — Web UI Basic Auth now uses `hmac.compare_digest` instead of `==` to eliminate the theoretical timing side-channel.
- **`settings.json` permissions tightened** — file mode is now `0600` (owner-only read/write), preventing other containers sharing the data volume from reading webhook URLs and Telegram topic IDs.
- **Cron schedule validation** — invalid cron expressions saved via the Web UI are now rejected with a clear error message instead of silently breaking the scheduler thread.

## [1.11.5] - 2026-04-26

### Security
- **XSS prevention** — all user-controllable values rendered into the Web UI (container/image names, history details, persisted settings, error messages) are now HTML-escaped, including in HTML attribute contexts
- **SSRF mitigation for webhook URLs** — Web UI now rejects webhook URLs targeting cloud metadata endpoints (`169.254.169.254`, `metadata.google.internal`, `fd00:ec2::254`), link-local addresses, and non-`http(s)` schemes. Discord webhooks are restricted to official Discord hosts. Private/LAN addresses remain allowed for selfhosted Ntfy/Gotify/Home Assistant setups.
- **CSRF protection** — every POST to the Web UI is verified via the `Origin` header (with `Referer` fallback). Forged cross-origin POSTs abusing cached Basic Auth credentials are rejected with HTTP 403.

## [1.11.4] - 2026-04-24

### Fixed
- **Cron range-with-step parsing** — schedules like `*/3 0-20/3 * * *` no longer crash the scheduler with `ValueError: invalid literal for int()` (regression affecting QNAP / advanced cron users)
- **IPv4-only by default** — Python preferred IPv6 in containers without IPv6 routing, causing `[Errno 101] Network unreachable` on registry/Telegram requests. Docksentry now forces IPv4 by default; set `DOCKSENTRY_IPV6=true` to re-enable IPv6 if your network supports it

## [1.11.3] - 2026-04-19

### Improved
- **Telegram message fallback** — automatically retries without Markdown formatting if Telegram rejects a message, ensuring notifications are always delivered

## [1.11.2] - 2026-04-19

### Fixed
- **`/status` Markdown error** — image names with underscores caused Telegram HTTP 400 (now wrapped in code formatting)

## [1.11.1] - 2026-04-19

### Fixed
- **`/status` crash** — split long status messages into chunks when exceeding Telegram's 4096 char limit (fixes HTTP 400 for users with many containers)
- **Synology / NAS compatibility** — documented `DOCKER_API_VERSION` workaround for Docker CLI version mismatch

## [1.11.0] - 2026-04-19

### Added
- **Telegram Topic ID support** — send messages to a specific topic/thread in Telegram groups with topics enabled (`TELEGRAM_TOPIC_ID` env var)
- Topic ID editable in Web UI settings and persisted across restarts

## [1.10.0] - 2026-04-19

### Fixed
- **Multi-digest comparison** — compare remote digest against all local RepoDigests, fixing false "update available" after updates (e.g. redis, postgres)
- **Image-ID resolution** — containers deployed via Portainer (showing image ID instead of tag) are now resolved via `docker inspect` and checked normally
- **Health check timeout** — increased from 30s to 5 minutes for slow-starting containers (GitLab, Nextcloud, databases); containers in `starting` state keep waiting instead of rolling back

## [1.9.0] - 2026-04-04

### Added
- **Startup notification** — all configured channels (Telegram, Discord, Webhook) receive a message on boot
- **`/logs <container>`** — view last 30 log lines directly in Telegram
- **Web UI: Update buttons** — update individual containers from the status page
- **Web UI: Pin/Unpin buttons** — pin or unpin containers directly in the browser
- **Web UI: Auto-update toggles** — toggle switches per container on the status page
- **Web UI: Logs page** — view container logs with configurable line count
- **Web UI: Full settings management** — cron schedule, exclude list, Discord webhook, webhook URL all editable
- **Persistent settings** — Web UI and Telegram changes saved to `/data/settings.json`, survive restarts
- **Logo** — new Docksentry logo in README and Web UI

### Changed
- README restructured: slim overview (~170 lines), detailed docs moved to `/docs/`
- Documentation split into 6 pages: updates, web-ui, notifications, compose, security, languages
- Sensitive values (Bot Token, Chat ID) masked in Web UI
- Auto Self-Update label clarified as "Bot only"
- New screenshots for Web UI and Discord

## [1.8.0] - 2026-04-04

### Added
- **Discord notifications** — rich embeds for update alerts, success/failure results
- **Generic webhook notifications** — JSON POST to Ntfy, Gotify, Home Assistant, or any HTTP endpoint
- Multi-channel architecture: Telegram (interactive), Discord (embeds), Webhook (JSON) — all run in parallel

### Fixed
- Discord webhook: add `User-Agent` header to avoid Cloudflare 403 block

## [1.7.0] - 2026-04-03

### Added
- **Docker Compose support** — automatically detects Compose-managed containers via labels and uses native `docker compose pull/up` for updates, preserving all Compose-specific configuration
- Compose containers marked with 🐳 icon in update notifications
- Automatic fallback to `docker run` recreation when compose file is not accessible

### Changed
- Health check logic extracted into reusable `_wait_healthy()` helper

## [1.6.2] - 2026-04-03

### Fixed
- Web UI: duplicate emojis in navigation tabs (Status, Settings)

### Changed
- Rewrite README with complete documentation and logical structure
- Add Telegram and Web UI screenshots to README

## [1.6.1] - 2026-04-03

### Fixed
- Health check crash on containers without Docker HEALTHCHECK defined (split into two separate inspect calls)

### Changed
- Centralize version management in `version.py`

## [1.6.0] - 2026-04-03

### Added
- **Per-container auto-update** — toggle with `/autoupdate`, updates run automatically without confirmation
- **Partial name matching** — type just the beginning of a container name (e.g. `/pin ngi` → `nginx`)

## [1.5.0] - 2026-04-03

### Added
- **Pin/Freeze containers** — `/pin` and `/unpin` commands to exclude containers from updates via Telegram
- **Health check after update** — verifies container is running (and healthy) after recreation, waits up to 30s
- **Auto-rollback** — failed updates or health checks automatically restore the previous container

## [1.4.0] - 2026-04-03

### Added
- **Update history** — persistent log of all updates, viewable via `/history` command and Web UI history page

## [1.3.0] - 2026-04-03

### Added
- **Optional Web UI** — dashboard with status overview, update history, and settings page
- **Multi-language support** — 16 languages included (EN, DE, FR, ES, IT, NL, PT, PL, TR, RU, UK, AR, HI, JA, KO, ZH)
- Switch language via `/lang`, Web UI, or `LANGUAGE` env var
- CI workflow for language sync and documentation checks
- Pre-commit hook for language file validation

## [1.2.0] - 2026-04-03

### Added
- **AUTO_SELFUPDATE** option — bot updates itself automatically on each scheduled check
- **Per-container update buttons** — update individual containers from the notification
- `/cleanup` command — remove old unused Docker images
- `/selfupdate` command — update the bot itself
- `/debug` toggle — detailed Telegram diagnostics
- Image size and creation date in update notifications
- Version number in `/help` output

### Fixed
- Self-update loop: flush old Telegram updates on startup
- Config check: use `isfile` instead of `exists` for Docker credentials

### Changed
- Container recreation with full config preservation (ports, volumes, env, labels, networks) instead of compose restart
- Increased Docker pull timeout to 30 minutes

## [1.0.0] - 2026-04-03

### Added
- Initial release
- Automatic update detection via Docker Registry HTTP API
- Telegram notifications with inline action buttons
- Cron-based scheduled checks
- Docker Hub authentication support
- Container exclusion via environment variable
