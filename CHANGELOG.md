# Changelog

All notable changes to Docksentry (formerly Docker Telegram Updater) are documented here.

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
