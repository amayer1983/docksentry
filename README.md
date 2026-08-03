<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/logo.png" alt="Docksentry Logo" width="200">
</p>

<h1 align="center">Docksentry</h1>

<p align="center">
Auto-update, monitor and manage Docker containers via interactive <b>Telegram bot</b>, <b>Web UI</b>, <b>Discord</b>, or <b>webhooks</b>. Auto-rollback on failed updates, alerts on crashes, OOM kills and failing healthchecks. 16 languages. Telegram is optional — runs fully headless.
</p>

<p align="center">
  <img src="https://img.shields.io/docker/pulls/amayer1983/docksentry" alt="Docker Pulls">
  <img src="https://img.shields.io/docker/image-size/amayer1983/docksentry" alt="Docker Image Size">
  <img src="https://img.shields.io/github/license/amayer1983/docksentry" alt="License">
  <a href="https://github.com/sponsors/amayer1983"><img src="https://img.shields.io/github/sponsors/amayer1983?label=Sponsor&logo=GitHub" alt="Sponsor"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-update-notification.jpg" alt="Update Notification" width="350">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-update-result.jpg" alt="Update Result" width="350">
</p>

## What's different

Most Docker auto-update tools either set-and-forget like Watchtower (no human in the loop, no veto) or notify-only like Diun (heads-up but you SSH in to apply it). Docksentry does **both, plus interactive control from your phone or browser**:

- **Tap "Update all"** in Telegram or "Bulk update" in the Web UI — updates apply, results stream back
- **Container groups** — update Gluetun first, restart the Sonarr / Radarr / qBittorrent stack after it's healthy
- **Lifecycle commands** — `/status nginx` shows state + inline `[🔁 Restart] [🟥 Stop]` buttons. One tap to fix a hung container without leaving the chat
- **Auto-rollback** if the new container fails its healthcheck (respecting the image's own `start_period`)
- **State monitoring** — a healthcheck flips to unhealthy, a container dies with a non-zero exit code, gets OOM-killed or crash-loops through its restart policy → you hear about it, with flap protection and quiet hours
- **Maintenance mode** to pause everything while you tinker with the host (`/maintenance 2h`)
- **Multi-bot setup** for several Docker hosts in one Telegram group, each labelled so you can tell them apart

Telegram is optional — Web UI alone is plenty for a single-host setup. Discord and generic webhook channels work in parallel.

## Features

- **Automatic update detection** — compares image digests on a configurable cron schedule
- **Web UI** — dashboard with status, logs, history, settings, pin/unpin, auto-update toggles, manual update triggers, image cleanup, self-update
- **Telegram bot** *(optional)* — full interactive control with inline buttons and 20+ commands
- **Discord notifications** — rich embeds for updates, successes, and failures
- **Generic webhooks** — JSON POST to Home Assistant or any HTTP endpoint
- **Native push channels** — ntfy, Gotify, Matrix, and Apprise (which fans out to ~100 further services)
- **Headless mode** — run without Telegram; Web UI + Discord/Webhook is enough
- **Per-container auto-update** — selected containers update without confirmation
- **Pin/Freeze containers** — exclude containers from updates
- **Auto-rollback** — failed updates automatically restore the previous container
- **Container monitoring** — transition-based alerts for unhealthy containers, non-zero exits, OOM kills and crash-restarts; disk-space warnings with reclaim preview. Every event is kept in a persistent history you can browse on the Web UI History page or recall with `/events`
- **Docker Compose support** — native `docker compose pull/up` for Compose stacks
- **Self-update** — the bot can update itself automatically
- **Persistent settings** — Web UI changes survive restarts
- **Multi-language** — 16 languages, switchable at runtime
- **Lightweight** — Python standard library only, zero external dependencies

## Quick Start

You need **at least one** of: Web UI, Telegram, Discord webhook, or generic webhook. The most popular setup is Web UI + Telegram.

### Option A — Web UI only (headless, no Telegram)

```bash
docker run -d \
  --name docksentry \
  --restart unless-stopped \
  -e WEB_UI=true \
  -e WEB_PORT=8080 \
  -p 8080:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  amayer1983/docksentry:latest
```

### Option B — Web UI + Telegram (full interactive)

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Send a message to your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and find your `chat.id`
3. Run:

```bash
docker run -d \
  --name docksentry \
  --restart unless-stopped \
  -e BOT_TOKEN=your-bot-token \
  -e CHAT_ID=your-chat-id \
  -e WEB_UI=true \
  -p 8080:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  amayer1983/docksentry:latest
```

### Docker Compose

```yaml
services:
  docksentry:
    image: amayer1983/docksentry:latest
    container_name: docksentry
    restart: unless-stopped
    environment:
      - BOT_TOKEN=your-bot-token
      - CHAT_ID=your-chat-id
      - CRON_SCHEDULE=0 18 * * *
      - TZ=Europe/Berlin
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - docksentry_data:/data
      # Optional: mount your compose project directories so Docksentry
      # can call `docker compose up` for compose-managed containers.
      # When not mounted (or path doesn't match), Docksentry falls back
      # to a standalone `docker run` recreate from the container's
      # inspect data — works for almost everything but loses some
      # compose-only metadata. See "Compose-managed containers" below.
      # - /opt/stacks:/opt/stacks:ro
      # - /mnt/dockerdata:/mnt/dockerdata:ro
    security_opt:
      - no-new-privileges:true

volumes:
  docksentry_data:
```

### Compose-managed containers

When a container was started by `docker compose`, its inspect data records the **host-side path** of the compose file (e.g. `/opt/stacks/myapp/docker-compose.yml`). Docksentry runs inside its own container and can't see that path unless you mount it.

| Mount setup | Update path |
|---|---|
| Compose dirs mounted at the same paths inside Docksentry | `docker compose pull` + `docker compose up -d --no-deps <service>` (preserves all compose semantics) |
| Compose dirs not mounted | Falls back to standalone `docker run` recreate from inspect data — preserves capabilities, devices, sysctls, mounts, env, ports, labels, network mode, network aliases, fixed IPs, MAC, resource limits, healthcheck overrides, etc. |

The standalone fallback is comprehensive. As of v1.19.0 it covers everything `_build_run_args()` knows to read from `docker inspect`:

- **Network state**: `--network` (primary), `--network-alias` (compose service hostnames like `db`, `redis`, `broker`), `--ip` / `--ip6` (fixed IPs), `--mac-address`, `--link`. Additional networks (containers attached to >1 network) get `docker network connect` after run, preserving aliases/IPs per network.
- **Capabilities / devices / sysctls / tmpfs / extra-hosts / DNS / security-opts** (Gluetun-style stacks).
- **Resource limits**: memory, CPU, pids, oom, blkio, ulimits, group-add.
- **Lifecycle**: stop-signal, stop-timeout, auto-remove (when no restart policy).
- **Process config**: working-dir, domainname, tty, stdin, healthcheck override.
- **Image-default-aware Cmd / Entrypoint** — only restores container-level Cmd/Entrypoint when they actually differ from the new image's defaults, so image updates that change CMD aren't locked to the old value.

If you have **compose-specific orchestration** (depends_on chains, profiles, multiple compose files merged via `-f`, project-level network options beyond defaults), mounting your compose dirs is still the cleanest path to keep those intact.

The log line `Compose file not found: <path> — falling back to standalone` is the marker that the fallback is being taken. Not an error per se, just informational. If you see it on every update and want the compose path instead, mount the relevant host directory read-only into Docksentry.

**Audit mode (debug):** on every update check Docksentry logs `[audit] HostConfig.<key>` / `[audit] Config.<key>` to the container log (`docker logs docksentry`) for any inspect field that's non-default but not restored on recreate. Set `DEBUG=true` to also fan the check's debug output out to Telegram (and flip it at runtime with `/debug` or the Web UI — that toggle persists). Future Docker versions adding new keys surface here — please report any sightings as an issue so we can extend coverage.

**Registry diagnostics (debug).** With `DEBUG=true` the update check also explains itself instead of just printing a verdict — see [Update Workflow → Why didn't it see my new release?](docs/updates.md#why-didnt-it-see-my-new-release) for the full annotated log. Short version: the URL it asked, the status and content type it got back, any redirect, the auth method (category only — never the token), full digests with their repository prefix, the version a digest resolves to, and one line naming the host platform, the daemon's registry mirrors and every proxy in the path. All of it is DEBUG-only: without it the log stays exactly as short as it was.

### Podman support

Since v1.61.0 Docksentry can drive `podman` directly — set `CONTAINER_CLI=podman` and checks, updates, recreates, rollback, start/restart, `podman compose` and image cleanup all go through it. No aliasing needed. Originally surfaced by [@LeeNX in #23](https://github.com/amayer1983/docksentry/issues/23), with the recreate-level fixes in [#43](https://github.com/amayer1983/docksentry/issues/43), [#48](https://github.com/amayer1983/docksentry/issues/48), [#49](https://github.com/amayer1983/docksentry/issues/49) and [#50](https://github.com/amayer1983/docksentry/issues/50).

Two caveats worth knowing up front:

- **Self-update still needs `docker` to resolve.** It launches a `docker:cli` helper container, because it can't run inside the container it's replacing. Everything else uses the CLI you picked.
- **There's now a Podman test bed**, added in v1.62.0 — `scripts/test_podman_live.py` runs the backend against a real `podman`, including a remote Podman service over TCP. Everything before that was fixed from bug reports rather than a machine to try things on, so if something still misbehaves please open an issue; that's genuinely how all of them got found. Podman isn't in CI yet, only in the local suite.

The older route still works too, and is what you want if you'd rather not set anything: Podman implements the Docker REST API, so mounting the Podman socket where Docksentry expects the Docker one is enough. No env var changes, no different image.

#### Rootful Podman

```bash
sudo systemctl enable --now podman.socket
# creates /run/podman/podman.sock
```

```yaml
services:
  docksentry:
    image: amayer1983/docksentry:latest
    volumes:
      - /run/podman/podman.sock:/var/run/docker.sock:ro
      - docksentry_data:/data
    environment:
      - WEB_UI=true
      # ... rest of your config
```

#### Rootless Podman

```bash
systemctl --user enable --now podman.socket
# creates /run/user/$UID/podman/podman.sock
```

```yaml
services:
  docksentry:
    image: amayer1983/docksentry:latest
    volumes:
      - /run/user/1000/podman/podman.sock:/var/run/docker.sock:ro
      - docksentry_data:/data
    environment:
      - WEB_UI=true
```

#### What's expected to work

- `/status`, `/check`, `/updates`, `/history` — read-only inspection via the Docker REST API
- `docker pull` of registry images, `docker stop`, `docker rm`, `docker rename`, `docker start`, `docker run`
- Container groups, the `restart_dependents` cascade
- The v1.18.10 17-field HostConfig recreate (Podman's inspect carries the same `HostConfig.CapAdd`, `Devices`, `Sysctls` structure — see [release notes](https://github.com/amayer1983/docksentry/releases/tag/v1.18.10))

#### Known limitations

- **Rootless Podman with complex UID mappings.** Docksentry's [#16](https://github.com/amayer1983/docksentry/issues/16) PID-1 self-protection reads container IDs from cgroup paths, which behave differently rootless. May misidentify the running container.
- **Quadlets / systemd-managed Podman containers.** Completely different paradigm — containers are managed by systemd, the update cycle is a `.container` file edit + `systemctl restart`, not `docker stop` + `docker run`. Out of scope for the v1.x line.
- **`podman-compose`-specific labels.** Podman Compose uses some compose-project labels with slightly different formats. Compose-detection might miss them and fall back to the standalone recreate (which is comprehensive after v1.18.10 but loses compose-project orchestration).
- **Multi-arch.** Docksentry's image is published as `amd64` and `arm64`. Raspberry Pi 4/5 and most other ARM SBCs work. Pi 3 (armv7) is not currently built.

#### Reporting issues

If you try this and something breaks, open a new issue with:

- Your Podman version (`podman --version`)
- Rootful or rootless
- Architecture (`uname -m`)
- The exact failure mode (Telegram message, log line, web UI screenshot)

Concrete failure modes let us add targeted Podman-specific fixes; vague "doesn't work" can't be acted on.

## Commands

| Command | Description |
|---------|-------------|
| `/status` | Container overview with health, uptime, images |
| `/status <name>` | Per-container detail with inline Stop/Restart/Start buttons |
| `/check` | Manually trigger an update check (add a name/glob to scope) |
| `/update <name\|*>` | Update a container or everything matching a glob |
| `/updates` | Show pending updates |
| `/start <name>` | Start a stopped container |
| `/stop <name>` | Stop a running container |
| `/restart <name>` | Restart a container |
| `/logs <name>` | Show last 30 log lines of a container |
| `/pin <name>` | Pin container — excluded from updates |
| `/unpin <name>` | Unpin container |
| `/autoupdate <name>` | Toggle auto-update per container |
| `/cooldown <name> <seconds>` | Per-container post-update cooldown before the next in a batch |
| `/protect <name>` | Protect a container from `/stop` |
| `/setlink <name> <url>` | Set a repo/changelog link for a container |
| `/groups` | Show container groups (or `/groups <name>`) |
| `/maintenance <2h\|off>` | Pause auto-updates for a window |
| `/history` | Show update history |
| `/events` | Recent container events (crashes, OOM, health flips) |
| `/audit <name>` | Audit container inspect coverage |
| `/cleanup` | Remove old unused images |
| `/checkimages` | How much space `/cleanup` would free (dry-run) |
| `/selfupdate` | Update the bot itself (latest) |
| `/selfupdate <version>` | Pin to a specific version (e.g. `/selfupdate 1.17.4`) |
| `/selfupdate previous` | Roll back to the previous release |
| `/changelog` | Show what's new in versions ahead of yours (fetched from GitHub) |
| `/debug` | Toggle debug mode |
| `/lang <code>` | Switch language |
| `/settings` | Show current configuration |
| `/help` | Show all commands |

> Partial name matching: `/pin ngi` matches `nginx`.

> Per-command help: append `-?` to any command for its detailed help — `/protect -?` is the same as `/help protect`.

## Container labels (GitOps)

For GitOps-style setups where you keep all container config in one place, Docksentry reads a few `docksentry.*` labels straight off your containers. A label, when present, **overrides** the equivalent bot/Web-UI toggle — so your compose file stays the source of truth.

```yaml
services:
  myapp:
    image: ghcr.io/me/myapp:latest
    labels:
      - "docksentry.auto=true"        # auto-update this container without touching the Web UI
      - "docksentry.protect=true"     # refuse /stop for this container (#38-style protection)
      - "docksentry.ask-major=true"   # pause auto-updates on major version bumps until confirmed
      - "docksentry.link=https://github.com/me/myapp/releases"  # repo / changelog link
```

| Label | Effect |
|-------|--------|
| `docksentry.enable=false` | Take the container out of Docksentry's scope entirely (not checked, not listed) |
| `docksentry.exclude=true` | Same as `docksentry.enable=false` |
| `docksentry.pin=true` | Freeze the container — never listed as an update, never updated (twin of `/pin`) |
| `docksentry.auto=true` / `=false` | Opt in to / out of auto-updates **(auto-updating your other containers, not Docksentry itself)**. `=false` keeps a container manual **even with `AUTO_UPDATE_ALL=true`**; `=true` opts it in without the per-container toggle |
| `docksentry.protect=true` | Protect from `/stop` (a `=false` label force-unprotects, overriding the toggle) |
| `docksentry.ask-major=true` / `=false` | Require / skip the major-version confirmation gate for auto-updates |
| `docksentry.policy=all` / `minor` / `patch` | Cap **auto-updates** by semver bump level: `all` applies every bump (default), `minor` applies minor+patch but holds back majors, `patch` applies patch only. Manual `/update` and the Bulk "Update all" button always apply regardless. An update whose version can't be classified is allowed. Overrides the global `UPDATE_POLICY`. |
| `docksentry.trust-running=true` | Accept "running" as healthy after updates, even if the healthcheck stays unhealthy (#9 behaviour) |
| `docksentry.monitor=false` | Exclude the container from state monitoring (health/exit/OOM notifications) |
| `docksentry.link=<url>` | Repo / changelog URL for the container — wrapped around its name in update notifications, shown as `🔗` in the Web UI status table and on the detail page, and used by `/changelog <name>`. Beats both `/setlink` and the Web UI field (which is disabled while the label is set). Must be a complete `http://` or `https://` URL; anything else is ignored and the next source in the chain applies. Resolution order: this label → `/setlink` value → `org.opencontainers.image.source` → `org.opencontainers.image.url` → registry overview page guessed from the image name |

Booleans accept `true`/`1`/`yes`/`on` (case-insensitive). Precedence everywhere: **label wins over the stored bot/Web-UI toggle; no label → toggle applies.** The Web UI status table shows the *effective* state (label included) for Pin and Auto — note that clicking a UI toggle cannot override a label; remove the label from your compose file instead.

## Configuration

At least one of `BOT_TOKEN`+`CHAT_ID`, `WEB_UI=true`, `DISCORD_WEBHOOK`, `WEBHOOK_URL`, or e-mail (`SMTP_HOST`+`SMTP_FROM`+`SMTP_TO`) must be configured — otherwise Docksentry has no way to notify or be controlled.

> **Quoting env values in `docker-compose.yml`**: Docker Compose passes env values literally, so `BOT_TOKEN="abc123"` lands as the string `"abc123"` (quotes included) in Docksentry — which breaks Telegram API calls, `int()` parsing on `WEB_PORT`, etc. Since v1.19.1 Docksentry strips matching outer `"…"` and `'…'` quote pairs automatically, but the cleanest fix is to leave the quotes off entirely: `BOT_TOKEN=abc123`.

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | | Telegram Bot API token (optional — set together with `CHAT_ID` to enable Telegram) |
| `CHAT_ID` | | Telegram chat ID (optional — set together with `BOT_TOKEN`) |
| `CRON_SCHEDULE` | `0 18 * * *` | Cron expression for scheduled checks |
| `EXCLUDE_CONTAINERS` | | Comma-separated names to exclude — wildcards allowed (`systemd-*`) |
| `MONITOR_ONLY_CONTAINERS` | | Watch and report these, never update them. Wildcards allowed. For containers something else owns — quadlets, Portainer stacks, anything deployed by Ansible or GitOps, where a recreate fights the tool that put them there. Unlike `EXCLUDE_CONTAINERS` they stay visible and still report updates. The label `docksentry.monitor-only=true` does the same per container |
| `API_TOKENS` | | `name:token` pairs, comma-separated (`prom:xxx,grafana:yyy`). Grants **read-only** access to `/metrics` and `GET /api/status` without the Web UI password — a scraper cannot log in, and the browser password would let a monitoring job stop containers. Named so one can be revoked without disturbing the other. Send as `Authorization: Bearer <token>`, or `?token=<token>` for scrapers that cannot set headers (note that a query string lands in access logs) |
| `MIN_IMAGE_AGE_DAYS` | `0` | Don't auto-update to an image younger than this many days. Two reasons people want it: let someone else find the broken release first, and give a compromised image time to be noticed before you pull it. **Auto path only** — pressing the button yourself always works, and the update stays pending so it applies by itself once the image has aged. Per container with `docksentry.min-age=7`. Off by default |
| `REGISTRY_MIRRORS` | | `origin=mirror` pairs, comma-separated (`docker.io=mirror.internal`). Applies to update **checks** only — those go straight to the registry over HTTPS and otherwise ignore the daemon's own `registry-mirrors`, so on a network where only the mirror is reachable Docksentry could not check at all. Pulling still goes through the daemon with the container's own image reference; use `registry-mirrors` in `daemon.json` for that side |
| `INSECURE_REGISTRIES` | | Registries to reach over plain HTTP instead of HTTPS, comma-separated, wildcards allowed. Only hosts named here — never guessed, and never a fallback when TLS fails |
| `NTFY_TOKEN` | | ntfy access token for a protected topic |
| `NTFY_USER` / `NTFY_PASSWORD` | | ntfy credentials, if you use basic auth rather than a token |
| `SMTP_TLS_VERIFY` | `true` | Verify the mail server's certificate. Set `false` only for an internal server with a self-signed certificate — it sends the password to whatever answers |
| `MONITOR_EVENTS` | `true` | Watch the runtime's live event stream, so a death alert's resource snapshot is taken at the moment it happens rather than at the next poll |
| `AUTO_SELFUPDATE` | `false` | Auto-update the bot itself on each check — **self-update / selfupdate: Docksentry updating itself** (via `/selfupdate` or the Web UI button), not your other containers |
| `AUTO_UPDATE_ALL` | `false` | Auto-update **every** checked container **(auto-updating your other containers, not Docksentry itself)** — Watchtower-style, not just per-container opt-ins. Pinned / excluded / `docksentry.exclude` containers are still skipped. |
| `UPDATE_POLICY` | `all` | Global default cap on which semver bump levels **auto-updates** apply: `all` (every bump), `minor` (minor+patch, hold back majors) or `patch` (patch only). The per-container `docksentry.policy` label overrides it. Manual `/update` and Bulk update always apply. An update whose version can't be classified is allowed. This caps *by bump level only* — it does **not** make Docksentry follow or switch semver tags (it never rewrites `:1.2.3` → `:1.2.4`); that's a separate future capability. Env-only. |
| `AUTO_CLEANUP` | `false` | Run image cleanup after every successful auto-update |
| `CLEANUP_GRACE_HOURS` | `24` | Cleanup only removes images unused for at least this long (1–8760h) |
| `CLEANUP_BACKUP_LOCAL_ONLY` | `false` | Before deletion, save unused locally-built images (no registry digest) to `/data/cleanup-backups/` |
| `CLEANUP_BACKUP_DAYS` | `7` | How long backup tarballs are kept (1–365 days) |
| `DISK_WARN_PERCENT` | `85` | Notify when disk usage exceeds this percentage (50–100) |
| `DISK_WARN_AUTO_CLEANUP` | `false` | Automatically run cleanup when disk warning fires |
| `MONITOR` | `true` | Container state monitoring: notify on health turning unhealthy (and recovering), non-zero exits, OOM kills, and crash-restarts. Transitions only — no repeated alarms, quiet during updates. |
| `MONITOR_INTERVAL` | `60` | Seconds between monitoring passes (min 15) |
| `QUIET_HOURS_START` | | Quiet-hours window start (HH:MM). Auto-notifications in this window are dropped. |
| `QUIET_HOURS_END` | | Quiet-hours window end (HH:MM). Manual command replies always go through. |
| `WEEKLY_REPORT_ENABLED` | `false` | Send a once-a-week summary report to all configured channels |
| `WEEKLY_REPORT_WEEKDAY` | `0` | Day of week for the report (0=Mon, 6=Sun) |
| `WEEKLY_REPORT_HOUR` | `9` | Hour of day for the report (0-23, local time) |
| `LANGUAGE` | `en` | Bot language ([16 available](docs/languages.md)) |
| `WEB_UI` | `false` | Enable web dashboard |
| `WEB_PORT` | `8080` | Web UI port |
| `WEB_PASSWORD` | | Web UI password (Basic Auth) |
| `TELEGRAM_TOPIC_ID` | | Telegram topic/thread ID (for groups with topics) |
| `TELEGRAM_ALLOWED_USERS` | | Optional whitelist — comma-separated Telegram user IDs allowed to control the bot. Empty = anyone in the configured chat. See [Group / Topic setup](#group--topic-setup) below. |
| `TELEGRAM_POLLING` | `true` | Set `false` for **send-only mode**: Docksentry sends notifications but doesn't poll for commands. Use this to share one bot token with another app (e.g. Home Assistant) — Telegram allows only one command-polling consumer per token, so let the other app own commands while Docksentry just posts. Control Docksentry via the Web UI in this mode. |
| `BOT_LABEL` | | Optional prefix prepended to every outgoing notification (Telegram, Discord, webhook). Useful when multiple Docksentry instances share a chat / channel so you can tell which host a message is from. See [Multi-bot setup](#multi-bot-setup-one-group-multiple-hosts) below. Max 32 chars. |
| `DISCORD_WEBHOOK` | | Discord webhook URL |
| `WEBHOOK_URL` | | Generic webhook URL (JSON POST). Transient network failures (timeout / connection error) are retried up to 3× with a short backoff so a blip right after a self-update restart doesn't drop a notification — same as Telegram and Discord. Note: if the endpoint triggers an automation (Home Assistant, ntfy, custom script), a rare edge case can produce a duplicate delivery — prefer idempotent handlers. |
| `NTFY_URL` | | [ntfy](https://ntfy.sh) topic URL (full), e.g. `https://ntfy.sh/my-topic`. Setting it enables ntfy push notifications — a plain HTTP POST with the message as body, the subject in the `Title` header and `Priority` set higher for failures. Use a private/self-hosted server or an unguessable topic; anyone who knows the topic URL can read your notifications. Alternatively set `NTFY_SERVER` + `NTFY_TOPIC`. |
| `NTFY_SERVER` | | ntfy server base URL, e.g. `https://ntfy.sh` — combined with `NTFY_TOPIC` when `NTFY_URL` isn't set |
| `NTFY_TOPIC` | | ntfy topic name, e.g. `my-topic` — used together with `NTFY_SERVER` |
| `SMTP_HOST` | | E-mail/SMTP server host. Setting this + `SMTP_FROM` + `SMTP_TO` enables e-mail notifications |
| `SMTP_PORT` | `587` | SMTP port (587 for STARTTLS, 465 for SSL, 25 for plain) |
| `SMTP_USER` | | SMTP username (omit for an unauthenticated relay) |
| `SMTP_PASSWORD` | | SMTP password |
| `SMTP_FROM` | | Sender address, e.g. `docksentry@example.com` |
| `SMTP_TO` | | Recipient(s), comma-separated |
| `SMTP_TLS` | `starttls` | `starttls`, `ssl`, or `none` |
| `TZ` | `Europe/Berlin` | Timezone |
| `DOCKER_HOSTS` | | **Multi-host (experimental).** Extra hosts this instance also manages, as `name:endpoint` pairs: `pve1:tcp://pve1:2375, nas:ssh://root@nas`. The endpoint is whatever the container CLI accepts for `-H`. **A TCP socket / [socket proxy](docs/security.md) is the simplest option** — same pattern as the local `DOCKER_HOST` setup, no keys and nothing in `~/.ssh` to maintain. SSH endpoints also work and rely on the CLI's own handling, so key-based login must already succeed non-interactively for the user Docksentry runs as. The machine Docksentry runs on is always managed and is *not* listed here — leave this unset and everything behaves exactly as a single-host install. A host that can't be reached is reported and skipped rather than taking the run down — every call to a host is time-bounded, so an unresponsive box costs a short wait on that host, not the others. Self-update stays local-only: Docksentry updates the instance it runs in, not the ones on your other boxes. Env-only. |
| `APPRISE_URL` | | **Apprise fan-out.** The notify endpoint of a self-hosted [Apprise API](https://github.com/caronc/apprise) container, e.g. `http://apprise:8000/notify/docksentry`. Apprise then forwards to whatever *it* is configured for — Pushover, Signal, Rocket.Chat, Mattermost, SMS gateways and ~100 more — so this one setting covers services Docksentry has no code for. Failures are sent with Apprise type `failure` so destinations that colour or prioritise by severity treat them differently. Optional `APPRISE_URLS` (comma-separated Apprise URLs) for the stateless endpoint, and `APPRISE_TAG` to route by tag. Env-only. |
| `GOTIFY_URL` / `GOTIFY_TOKEN` | | **Gotify push.** Base URL of your [Gotify](https://gotify.net) server plus an **application** token from its Apps tab (not a client token — they look alike and only the application one may post). Failed updates are sent at priority 8, which Gotify's app treats as loud and lets through quiet hours; everything else at 5. Env-only. |
| `MATRIX_HOMESERVER` / `MATRIX_TOKEN` / `MATRIX_ROOM` | | **Matrix room.** Homeserver base URL (`https://matrix.example.com`, *not* the server name), an access token for a dedicated sending account, and the internal room ID (`!abc:example.com`; a `#alias` also works and is resolved once). Messages carry both a plain-text and an HTML body, so formatting-capable clients render it and the rest still read fine. Env-only. |
| `DISCORD_BOT_TOKEN` | | **Interactive Discord bot (experimental).** A bot token from the [Discord developer portal](https://discord.com/developers/applications) — this is *not* the same thing as `DISCORD_WEBHOOK`, which only pushes notifications one way. With a token, Docksentry connects to Discord and answers slash commands (`/status`, `/check`, `/updates`, `/hosts`), the same way the Telegram bot does. Needs `DISCORD_APP_ID` as well. Replies are ephemeral — only the person who ran the command sees them — because container listings name internal services. Env-only. |
| `DISCORD_APP_ID` | | The application ID from the same portal page. Required alongside `DISCORD_BOT_TOKEN`; it's what the slash commands get registered against. Env-only. |
| `DISCORD_GUILD_ID` | | **Required** when `DISCORD_BOT_TOKEN` is set — the bot refuses to start without it. It is what restricts the bot to *your* server: it registers the slash commands there (which also makes them appear instantly rather than after up to an hour) and every incoming command is checked against it. Without that restriction the commands are global, and a "Public" application can be invited to a stranger's server and used to drive your containers. Discord → Settings → Advanced → Developer Mode, then right-click the server name → Copy Server ID. Env-only. |
| `DISCORD_ALLOWED_USERS` | | Optional, comma-separated Discord user IDs. Unset means anyone in that server can run the commands; set it when the server has members who shouldn't be able to stop a database or read `/logs`. On top of this, the commands are registered Administrator-only and disabled in DMs, so a server admin can also grant them per role in Discord itself. Env-only. |
| `CONTAINER_CLI` | `auto` | Which container CLI to drive: `auto`, `docker` or `podman`. `auto` uses `docker` whenever that command exists — including the usual `docker`→`podman` alias — and only falls back to `podman` when `docker` genuinely isn't there, so existing setups are unaffected. Set `podman` to call `podman` directly, no alias needed. One caveat: Docksentry's **self-update** still shells out to `docker` and launches a `docker:cli` helper container (it can't run inside the container it's replacing), so on Podman that one path still needs `docker` to resolve. Everything else — checks, updates, recreates, rollback, lifecycle, cleanup — goes through the selected CLI. Env-only. |
| `DOCKER_HOST` | | Docker API endpoint (for [socket proxy](docs/security.md)) |
| `DATA_DIR` | `/data` | Where Docksentry keeps its state — settings, pending updates, history, groups, the event log. Change it only if you mount the volume somewhere else; everything in it is what a backup would restore |
| `DOCKER_API_VERSION` | | Force Docker API version (e.g. `1.43` for Synology/older Docker) |
| `DOCKER_STOP_TIMEOUT` | `60` | Minimum seconds to allow `docker stop` to take before falling back to `docker kill`. The effective wait is `max(this, container.Config.StopTimeout)`. Raise for slow-shutdown apps (some DBs, log aggregators). |
| `DOCKER_USERNAME` / `DOCKER_PASSWORD` | | Docker Hub (or other registry) credentials. Bypasses the anonymous pull rate limit (100 / 6h / IP). We run `docker login` once at startup. |
| `DOCKER_AUTH_CONFIG` | | Path to an existing `config.json` with stored credentials (alternative to USERNAME/PASSWORD). Mount your host's `~/.docker/config.json` read-only and point at it. |
| `DOCKER_REGISTRY` | `docker.io` | Registry to log into. Set to `ghcr.io`, `quay.io`, an internal Harbor, etc. when using `DOCKER_USERNAME`/`PASSWORD`. |
| `HEALTHCHECK_MAX_STARTING` | `600` | Max seconds to wait for a freshly-updated container to leave `starting` health-state. Slow apps (GitLab, Nextcloud, Mastodon, large Postgres) may need more. We also respect the image's own `Healthcheck.StartPeriod` — the effective wait is `max(this, start_period × 1.5)`. If a container is still `starting` after the wait, Docksentry leaves it running (no rollback) and Docker's own healthcheck takes over. |
| `DOCKSENTRY_IPV6` | `false` | Enable IPv6 outbound connections (default: IPv4-only to avoid `Network unreachable` in containers without IPv6 routing) |
| `DEBUG` | `false` | Seed debug mode on at startup (verbose logging, the full registry diagnostics on every update check, and the check's debug output fanned out to Telegram). Also toggleable at runtime via `/debug` or the Web UI, which persists and overrides this on later restarts. |

> **For the persistent settings, the env var is only the *starting* value.** The settings listed in the next paragraph are stored in `/data/settings.json`, and on every start the saved file is applied on top of the environment — so once a value has been saved, changing the env var in your compose file does nothing. Note that saving *anything* in the Web UI writes all of these settings at once, so a value you never touched can end up saved too. Docksentry says so at startup when it happens, e.g. `Env override: DEBUG=true is set in the environment, but the saved setting debug=false wins — change it under Settings › General, or remove "debug" from /data/settings.json.`, and the affected field carries a small `env` marker in the Web UI. (This only triggers for a variable set to something other than its default — the image declares most of these itself, so a default value can't be told apart from you not setting it at all.) Two ways out: change the value in the Web UI (that's now the authoritative place), or delete the key from `settings.json` and restart so the env var takes over again.

Only the settings that live in the Web UI (roughly: schedule, exclude list, auto-selfupdate, cleanup options, disk-warning, quiet hours, weekly report, language, Web password, Discord/webhook URLs, debug, Telegram topic/allowed-users, bot label, stop timeout, monitoring toggle/interval) can be edited there and persist across restarts. Everything else is env-only — notably `SMTP_*`, `DOCKER_USERNAME`/`PASSWORD`/`AUTH_CONFIG`/`REGISTRY`, `AUTO_UPDATE_ALL`, `UPDATE_POLICY`, `CONTAINER_CLI`, `DOCKER_HOSTS`, `APPRISE_*`, `GOTIFY_*`, `MATRIX_*`, `DISCORD_BOT_TOKEN`/`APP_ID`/`GUILD_ID`/`ALLOWED_USERS`, `TELEGRAM_POLLING`, `WEB_UI`, `WEB_PORT`, plus `BOT_TOKEN`/`CHAT_ID` — several of them (credentials especially) intentionally never touch the data volume. Telegram is fully optional — if BOT_TOKEN/CHAT_ID are unset, Docksentry runs headless (Web UI + Discord/Webhook).

> **Synology / NAS users:** If Docksentry shows 0 containers, add `DOCKER_API_VERSION=1.43` to your environment variables.

### Group / Topic setup

If you want to use Docksentry in a Telegram **group** (so multiple people see the notifications) instead of a private chat:

> ⚠️ **Make sure it's a Group, not a Channel.** Telegram's "New Channel" creates a broadcast-only chat — admins post, members read, nobody can send `/commands`. The bot will happily post its startup message there but `getUpdates` always returns empty because there are no incoming messages. Use **New Group** in the Telegram app (not New Channel). A working group ID is negative — typically `-100…` for supergroups or shorter negatives (`-52…` etc.) for basic groups; both work.

1. **CHAT_ID is the group ID**, not your personal user ID. Find it by sending a message in the group and visiting `https://api.telegram.org/bot<TOKEN>/getUpdates`.
2. **Add the bot to the group** with permission to post and read messages. Disable group privacy in [@BotFather](https://t.me/BotFather) → `/setprivacy` → `Disable`, otherwise the bot only sees messages that mention it directly — so commands like `/status` won't trigger.
   > 💡 **`/setprivacy` is per-chat-membership cached.** If you toggle it in BotFather *after* the bot is already in the group, the new setting doesn't apply to that existing membership — `docker compose down/up` of Docksentry **does not** clear it. You have to **kick the bot from the group and add it again**. This trips most people up on first setup.
3. **Topics (Forum groups):** if the group has topics enabled, set `TELEGRAM_TOPIC_ID` to the topic where the bot should post. The ID is the integer after the last slash in a topic URL (right-click a topic → Copy link).
4. **Restrict who can control the bot** (optional but recommended for shared groups): set `TELEGRAM_ALLOWED_USERS` to a comma-separated list of personal user IDs. Without it, *any* group member can click "Update all". Find user IDs the same way as the chat ID — `from.id` in the `getUpdates` response.

```yaml
environment:
  - BOT_TOKEN=123456:abc...
  - CHAT_ID=-1001234567890           # the group ID
  - TELEGRAM_TOPIC_ID=42             # only needed for Forum groups
  - TELEGRAM_ALLOWED_USERS=11111111,22222222   # only these users can issue commands
```

### Multi-bot setup (one group, multiple hosts)

If you have several Docker hosts (different boxes, VMs, Proxmox LXCs, …), v2.0's real multi-host support is on the roadmap — but until then you can already control multiple instances from a **single Telegram group** by running one Docksentry per host, **each with its own bot token from [@BotFather](https://t.me/BotFather)**, labelling each instance with `BOT_LABEL`:

> **Why a separate token per host?** Telegram allows exactly **one polling consumer per bot token** — two instances sharing the same token fight over `getUpdates` and one of them gets evicted with a 409 Conflict every poll. `BOT_LABEL` is only a visual prefix in messages; it doesn't change the underlying bot identity (the token is the identity). Create one bot per host with `/newbot` in @BotFather and use a distinct token per instance.

```yaml
# Host pve1
environment:
  - BOT_TOKEN=...token-for-bot-1...
  - CHAT_ID=-1001234567890                  # shared group ID, same for all hosts
  - TELEGRAM_ALLOWED_USERS=11111111         # your own user ID — lock down control
  - BOT_LABEL=🖥 pve1                       # prefixes every notification
```

```yaml
# Host pve2
environment:
  - BOT_TOKEN=...token-for-bot-2...
  - CHAT_ID=-1001234567890                  # same group
  - TELEGRAM_ALLOWED_USERS=11111111
  - BOT_LABEL=🖥 pve2
```

Issue `/status` in the shared group and each bot replies with its label prefix:

```
🖥 pve1 · *Container Status:* …
🖥 pve2 · *Container Status:* …
🖥 pve3 · *Container Status:* …
```

The label also flows into Discord embeds (added to title + footer) and the generic webhook payload (`bot_label` field), so downstream automations can route per-host.

**Targeting one bot vs. broadcasting to all:**

In the same group you can choose whether a command hits *all* bots or just *one* by including or omitting the bot's Telegram `@username`:

| Form | Behaviour |
|---|---|
| `/check` | All bots respond (broadcast) |
| `/check@pve1-bot` | Only the bot whose Telegram username is `pve1-bot` responds; the others silently ignore |

Common pattern: broadcast `/selfupdate` so all hosts update together; target `/status@pve2-bot jellyfin` when you want a quick check on just one host without three bots' worth of "not found" noise. The bot's `@username` is what you set in BotFather when you created it (separate from `BOT_LABEL`, which is purely the visual prefix in messages).

**Setup checklist:**

1. In [@BotFather](https://t.me/BotFather), run `/newbot` **once per host** — each Docksentry instance needs its **own bot token**. `BOT_LABEL` alone is not enough; sharing one token across instances causes a Telegram 409 Conflict.
2. Create a private Telegram group, add yourself and **all bots** (one per host).
3. For **each** bot, in @BotFather → `/setprivacy` → **Disable**, so bots see `/commands` in groups (groups have privacy mode on by default, which restricts bots to messages that mention them directly).
4. Find the group ID (send a message in the group, visit `https://api.telegram.org/bot<TOKEN>/getUpdates`, look for `chat.id`).
5. Configure each Docksentry instance with: a **distinct** `BOT_TOKEN` (from step 1), the **same** `CHAT_ID` (the group ID), and a **distinct** `BOT_LABEL`.

**Security note — please read:**

- **Set `TELEGRAM_ALLOWED_USERS` to your own user ID.** Without it, any group member can trigger `/cleanup`, `/selfupdate`, "Update all", etc. against every host — accidentally adding a colleague to the group would hand them control over everything.
- **Keep the group private.** Disable invite links or rotate them, and audit membership occasionally. The group is now a single point of trust.
- **Be aware: privacy-mode off means each bot sees every human message in the group.** Don't use the same group for casual chat — keep it ops-only.
- Telegram's own Bot API filters out bot-to-bot communication, so bots can't accidentally trigger each other's commands.

This is a stepping stone, not a replacement for v2.0 multi-host: you still maintain N bot tokens, N Docksentry containers, N updates. But it makes "single chat, all hosts" usable today.

### Experimental: real multi-host

*New in v1.62.0, and marked experimental on purpose: it's had no run on real multi-host hardware yet. Leave `DOCKER_HOSTS` unset and nothing about your install changes.*

One Docksentry, several hosts. Point it at the others with `DOCKER_HOSTS`:

```yaml
environment:
  - DOCKER_HOSTS=pve1:tcp://pve1:2375, nas:ssh://root@nas
```

Each entry is `name:endpoint`, and the endpoint is whatever the container CLI takes for `-H`. A **TCP socket / [socket proxy](docs/security.md) is the simplest option** — the same pattern you'd use locally, no keys to manage. SSH endpoints work too and lean on the CLI's own SSH handling, so key-based login has to already succeed non-interactively for the user Docksentry runs as.

The machine Docksentry runs on is always managed and is **not** listed. Leave `DOCKER_HOSTS` unset and everything behaves exactly as a single-host install — no host column, no `@` anywhere.

**Aiming a command at a host** — append `@<host>`:

```
/check @pve1        check just that host
/update sonarr @nas update sonarr on nas
/update * @all      update everywhere, deliberately
```

The default when you *don't* say `@`:

| | without `@` | why |
|---|---|---|
| **Looking** — `/check`, `/status`, `/updates` | every host | you almost always want the whole picture |
| **Changing** — `/update`, `/start`, `/stop`, `/restart` | local host only | so a habit from single-host days can't restart something three boxes away |

A write command that stayed local says so in its reply and points at `@<host>` / `@all`, so the rule is visible without reading this.

**Limitations worth knowing:** Compose-managed containers on a remote host are recreated from their inspect data rather than through `docker compose` — the compose file lives on that host's filesystem and `docker compose -f` would parse a *local* path, so at best it wouldn't find the file and at worst it would deploy your local file's definition onto the other box. The standalone recreate preserves almost everything (see [Compose-managed containers](#compose-managed-containers)) but loses compose-only metadata. Beyond that: self-update is local-only by design — Docksentry updates the instance it runs in, not the ones on your other boxes. A host that can't be reached is reported and skipped rather than taking the run down — every call to a host is time-bounded, so an unresponsive box costs a short wait on that host, not the others.

## Web UI

Enable with `WEB_UI=true`. Provides status dashboard, container logs, update history, a Container Events history (the same crash/OOM/health-flip log you get from `/events`), and full settings management — all in a dark-themed, mobile-responsive interface.

**Checking for updates.** The **Check Updates** button above the container table runs the same check as `/check` and the cron schedule: every container, results in the pending list. Each row also has its own 🔍 button that checks just that container and tells you the outcome right there — handy when you only care about one image and don't want to wait for a full sweep, and the only way to get feedback at all if you run without Telegram or a webhook. Both refuse to start while an update is in progress, and while a check is already running, so a double-click can't produce two competing results. With `DEBUG=true` the per-container check also writes its full registry log — request URLs, status codes, redirects, full digests, resolved versions — to the browser console.

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-status.png" alt="Web UI Status" width="700">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-logs.png" alt="Web UI Logs" width="700">
</p>

See [Web UI Documentation](docs/web-ui.md) for details.

## Healthcheck

Docksentry ships with its own `HEALTHCHECK` baked into the image since v1.16.1. It runs every 60 seconds and checks **whichever surface(s) you have configured**, in priority order:

1. **Web UI socket** — when `WEB_UI=true`, a successful TCP connect to `127.0.0.1:<WEB_PORT>` is sufficient (the listener being up implies the scheduler thread is alive). This is the cheapest and most deterministic check.
2. **Telegram Bot API** — when `BOT_TOKEN` + `CHAT_ID` are set but Web UI is off, the healthcheck calls `getMe` against `api.telegram.org` to confirm the bot can still reach upstream.
3. **Webhook-only / headless** — when only `DISCORD_WEBHOOK` or `WEBHOOK_URL` is set, there's nothing local to probe; the healthcheck exits 0 (Docker's normal process supervision is the actual signal).
4. **Misconfigured** — no surface configured → exit 1 (matches `main.py`'s startup refusal).

Verify on your host:

```bash
docker inspect docksentry | jq '.[0].State.Health'
docker ps        # should show "(healthy)" after ~3 minutes of uptime
```

> **Podman caveat (#31):** some Podman versions don't auto-execute image-defined HEALTHCHECK directives. If `podman ps` doesn't show `(healthy)` for Docksentry but does for other containers, your Podman is one of them. Workaround: add `--health-cmd "python3 /app/healthcheck.py"` to your `podman run` / compose-equivalent. The script is bundled inside the image at that path.

## Notification Channels

| Channel | Updates | Results | Interactive |
|---------|:-:|:-:|:-:|
| **Telegram** | buttons | detailed | full control |
| **Discord** | rich embeds | rich embeds | via Web UI |
| **Webhook** | JSON | JSON | via Web UI |

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/discord.png" alt="Discord Notifications" width="400">
</p>

See [Notification Setup](docs/notifications.md) for Discord and Webhook configuration.

## Documentation

| Topic | Link |
|-------|------|
| Update Workflow & Rollback | [docs/updates.md](docs/updates.md) |
| Container Monitoring | [docs/monitoring.md](docs/monitoring.md) |
| Web UI | [docs/web-ui.md](docs/web-ui.md) |
| Notification Channels | [docs/notifications.md](docs/notifications.md) |
| Docker Compose Support | [docs/compose.md](docs/compose.md) |
| Security & Socket Proxy | [docs/security.md](docs/security.md) |
| Multi-Language | [docs/languages.md](docs/languages.md) |

## Roadmap

Docksentry is actively developed — see the [CHANGELOG](CHANGELOG.md) for what shipped in each release.

**v1.x — ongoing.** Continued bug fixes and smaller features driven by user feedback in [#2](https://github.com/amayer1983/docksentry/issues/2). Recent: container groups, maintenance mode, container notes, group/topic auth, restart-dependents for VPN-sidecar stacks.

**Shipped since — both of the items that used to sit under "v2.0, ahead":**

- **Multi-host management** — one instance managing several Docker or Podman hosts (`DOCKER_HOSTS=name:endpoint`, TCP or SSH), with per-host pending queues, host-prefixed notifications, a host selector in the Web UI, and `@host` / `@all` command targeting. Landed in v1.62.0.
- **Interactive Discord bot** — 27 slash-commands, confirmation buttons and the same control surface the Telegram bot offers, driven by the same update engine so the three front-ends cannot drift apart. Landed in v1.63.0.

**Shipped since:** an audit-free read-only surface — `/metrics` in Prometheus format and `GET /api/status` as JSON, both behind `API_TOKENS` so a scraper never needs the Web UI password. Plus `MONITOR_ONLY_CONTAINERS` for containers another tool owns, `MIN_IMAGE_AGE_DAYS` so you need not be first to pull a new release, and registry mirrors for lookups.

**Next.** Notification templates with per-channel routing — the one thing every comparable project's users ask for that Docksentry still builds in code. An audit trail of who did what across the three front-ends. And per-container resource figures on the status page, loaded after the page renders rather than making every load wait on `docker stats`.

Wishlist input and "+1"s welcome on [#2](https://github.com/amayer1983/docksentry/issues/2).

## Contributing

- **Feature ideas?** Open an [Issue](https://github.com/amayer1983/docksentry/issues) with the label `enhancement`
- **Found a bug?** Open an [Issue](https://github.com/amayer1983/docksentry/issues) with steps to reproduce
- **Translations?** Submit a PR for `app/lang/*.json`
- **Vote on the roadmap:** [Community Roadmap (Issue #2)](https://github.com/amayer1983/docksentry/issues/2)

## Support the project

Docksentry is free and open source. If it saves you time and you'd like to support continued development, you can sponsor the project on GitHub:

[![Sponsor](https://img.shields.io/github/sponsors/amayer1983?label=Sponsor%20on%20GitHub&logo=GitHub&style=for-the-badge)](https://github.com/sponsors/amayer1983)

Sponsorships fund: faster bug-fixes, more registry integrations, multi-host support, and keeping the lights on. No feature is paywalled — Docksentry stays free.

## License

MIT License - see [LICENSE](LICENSE)
