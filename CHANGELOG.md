# Changelog

All notable changes to Docksentry (formerly Docker Telegram Updater) are documented here.

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
