# Changelog

All notable changes to Docksentry (formerly Docker Telegram Updater) are documented here.

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
