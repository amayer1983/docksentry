# Changelog

All notable changes to Docksentry (formerly Docker Telegram Updater) are documented here.

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
