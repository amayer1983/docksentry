<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/logo.png" alt="Docksentry Logo" width="128">
</p>

<h1 align="center">Docksentry</h1>

<p align="center">
Watches <b>Docker</b> and <b>Podman</b> containers for new images and updates them, rolling back automatically when the new container fails its healthcheck. One instance manages several hosts over ssh:// or tcp://; container groups keep a stack's update order, including containers that share a VPN sidecar's network; and update policy, major-version confirmation, update windows and pinning are set per container. When something dies you get the exit code from the live event stream, host memory and load, what each container was using at that moment, and whether the kernel OOM-killed it. <b>Web UI</b>, <b>Telegram bot</b>, <b>Discord bot</b>, /metrics and a read-only JSON API all drive the same update engine — 16 languages, and Telegram is optional: it runs fully headless.
</p>

<p align="center">
  <img src="https://img.shields.io/docker/pulls/amayer1983/docksentry" alt="Docker Pulls">
  <img src="https://img.shields.io/docker/image-size/amayer1983/docksentry" alt="Docker Image Size">
  <img src="https://img.shields.io/github/license/amayer1983/docksentry" alt="License">
  <a href="https://github.com/sponsors/amayer1983"><img src="https://img.shields.io/github/sponsors/amayer1983?label=Sponsor&logo=GitHub" alt="Sponsor"></a>
</p>

> ### ⚠️ Only these two sources are official
>
> **Code:** [github.com/amayer1983/docksentry](https://github.com/amayer1983/docksentry) · **Image:** [`amayer1983/docksentry`](https://hub.docker.com/r/amayer1983/docksentry) on Docker Hub, or `ghcr.io/amayer1983/docksentry`.
>
> Copies of this project exist on GitHub that carry my name and my MIT copyright notice, and whose README links to a **ZIP file containing a Windows executable**. Docksentry is Python running in a Linux container; it has never shipped a `.exe`, and it is not distributed as a ZIP download. One such file is listed by [Netskope Threat Labs](https://github.com/netskopeoss/NetskopeThreatLabsIOCs) as a command-and-control indicator and appears in the [URLhaus](https://urlhaus.abuse.ch/) malware feed.
>
> If you arrived here from a search result offering a download, you were somewhere else. Install with `docker pull` from the addresses above and nothing else.

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-updates-available.png" alt="Update notification in Telegram" width="400">
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

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-update-result.png" alt="Update result reported back in Telegram" width="520">
</p>

Telegram is optional — Web UI alone is plenty for a single-host setup. Discord and generic webhook channels work in parallel.

## Features

- **Docker and Podman** — set `CONTAINER_CLI=podman` and checks, updates, recreates, rollback, `podman compose` and image cleanup all go through Podman. Mounting the Podman socket where Docksentry expects the Docker one works too
- **Multi-host** — one instance managing several machines over `ssh://` or `tcp://`, with per-host checks and pending queues, a host column in the Web UI and `@host` / `@all` targeting in the bots
- **Automatic update detection** — compares image digests on a configurable cron schedule. A pinned version tag, whose digest never moves, gets an advisory badge when a newer version exists — advisory only, nothing is switched behind your back
- **Container groups** — ordered updates for a stack: database before app, or Gluetun before the containers sharing its network namespace. Those are recreated against the head's *name* rather than its dead container ID, so they come back instead of being left stopped, and a failure in the group aborts the rest of it
- **Update policies per container** — `all` / `minor` / `patch`, major-version confirmation, per-container update windows, and `MIN_IMAGE_AGE_DAYS` so you need not be the first to pull a new image
- **Web UI** — dashboard with status, logs, history, settings, pin/unpin, auto-update toggles, manual update triggers, image cleanup, self-update. Container cards instead of a table below 700px
- **Telegram bot** *(optional)* — full interactive control with inline buttons and 20+ commands
- **Discord bot** — 27 slash commands and the same control surface, driven by the same update engine
- **Discord notifications** — rich embeds for updates, successes, and failures
- **Generic webhooks** — JSON POST to Home Assistant or any HTTP endpoint
- **Native push channels** — ntfy, Gotify, Matrix, and Apprise (which fans out to ~100 further services)
- **`/metrics` and a read-only JSON API** — Prometheus format and `GET /api/status`, both behind named `API_TOKENS` so a scraper never needs the Web UI password
- **Headless mode** — run without Telegram; Web UI + Discord/Webhook is enough
- **Per-container auto-update** — selected containers update without confirmation
- **Pin/Freeze containers** — exclude containers from updates
- **Auto-rollback** — failed updates automatically restore the previous container
- **Container monitoring** — transition-based alerts for unhealthy containers, non-zero exits, OOM kills and crash-restarts; disk-space warnings with reclaim preview. A crash alert carries the exit code taken from the runtime's live event stream (not from `inspect`, which reports 0 for a container the restart policy already brought back), the host's memory and load, what each container was using at the moment of death, and whether the kernel OOM-killed it. Every event is kept in a persistent history you can browse on the Web UI History page or recall with `/events`
- **Audit trail** — who did what, through which front end, kept across restarts. Secrets are redacted before anything is written
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

**[The full Podman guide is in `docs/podman.md`](docs/podman.md)** — what `CONTAINER_CLI=auto` actually resolves to, what socket activation does and doesn't buy you, remote Podman hosts over SSH (and why the key handling differs from Docker's), pods, and the `io.containers.autoupdate` label. What follows here is the short version and the socket recipes.

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

> **On Discord** the same commands take named options rather than positional words: `/status container:nginx`, `/check host:nas`. Both fields suggest values while you type, so you don't have to know them in advance — the container list follows whichever host you picked, and the machine Docksentry itself runs on is called `local`. `/hosts` lists them all.

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

These are the ones worth knowing on the first day. **[Every variable, with its default and the full explanation, is in `docs/configuration.md`](docs/configuration.md)** — 58 of them, and you will not need most.

| Variable | Default | What it is for |
|----------|---------|----------------|
| `WEB_UI` | `false` | Turn the web interface on. Set this and you can do everything from a browser. |
| `WEB_PORT` | `8080` | Port it listens on. |
| `WEB_PASSWORD` | | Password for the web interface. Empty means no login — fine on a trusted LAN, not otherwise. |
| `TZ` | `UTC` | Your timezone. Worth setting: every time Docksentry prints is this clock. |
| `CRON_SCHEDULE` | `0 18 * * *` | When it checks for updates. |
| `LANGUAGE` | `en` | One of 16. |
| `EXCLUDE_CONTAINERS` | | Containers to leave alone entirely, comma-separated. |
| `AUTO_SELFUPDATE` | `false` | Whether Docksentry updates *itself* unattended. Other containers are opted in individually. |
| `BOT_TOKEN` | | Telegram bot token — optional, set together with `CHAT_ID`. |
| `CHAT_ID` | | Telegram chat ID — optional, set together with `BOT_TOKEN`. |
| `DISCORD_WEBHOOK` | | Discord webhook URL, if you would rather be told there. |
| `WEBHOOK_URL` | | Generic webhook, for anything else. |
| `DOCKER_HOST` | | Point at a socket proxy or another daemon instead of the local socket. |

> **Quoting env values in `docker-compose.yml`**: Docker Compose passes env values literally, so `BOT_TOKEN="abc123"` lands as the string `"abc123"` (quotes included) — which breaks Telegram API calls, `int()` parsing on `WEB_PORT`, and more. Docksentry strips matching outer quote pairs since v1.19.1, but the cleanest fix is to leave them off: `BOT_TOKEN=abc123`.

> **Synology / NAS users:** If Docksentry shows 0 containers, add `DOCKER_API_VERSION=1.43` to your environment variables.

> **Settings saved in the Web UI win over the environment.** Roughly half of these are stored in `/data/settings.json` once saved, and the saved file is applied on top of the environment on every start — so changing the compose file afterwards does nothing. Docksentry says so at startup when it happens. [The full list and both ways out are in the reference.](docs/configuration.md)

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

### Real multi-host

*New in v1.62.0. Since v2.0.0 the transports are driven for real rather than asserted — measured against a `docker:dind` over loopback and a real sshd with the socket mounted. What hasn't happened is anyone but me running it across several machines; [#7](https://github.com/amayer1983/docksentry/issues/7) is the place to say how it went. Leave `DOCKER_HOSTS` unset and nothing about your install changes.*

One Docksentry, several hosts. Point it at the others with `DOCKER_HOSTS`:

```yaml
environment:
  # ssh:// is encrypted and authenticated by your key. A plain tcp://
  # endpoint is neither, so only use one on a network Docksentry alone
  # can reach — see docs/security.md.
  - DOCKER_HOSTS=pve1:ssh://root@pve1, nas:ssh://root@nas
```

Each entry is `name:endpoint`, and the endpoint is whatever the container CLI takes for `-H`. A **TCP socket / [socket proxy](docs/security.md) is the simplest option** — the same pattern you'd use locally, no keys to manage. SSH endpoints work too and lean on the CLI's own SSH handling, so key-based login has to already succeed non-interactively for the user Docksentry runs as.

An endpoint may also be `context://<name>`, meaning "the endpoint this machine already has saved under that name" — `docker --context <name>` / `podman --connection <name>`. On Podman that is the *only* way to give each remote host its own SSH key, because `podman --url ssh://…` ignores `~/.ssh/config` and borrows whichever stored connection happens to be the default one. Measured, and written up in [docs/podman.md](docs/podman.md#ssh-endpoints-podmans-key-handling-is-not-dockers).

The machine Docksentry runs on is always managed and is **not** listed. Leave `DOCKER_HOSTS` unset and everything behaves exactly as a single-host install — no host column, no `@` anywhere.

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-multihost.png" alt="Update notification for a remote host, containers marked @nas" width="520">
</p>

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

Enable with `WEB_UI=true`. Provides status dashboard, container logs, update history, a Container Events history (the same crash/OOM/health-flip log you get from `/events`), an audit trail of who did what, and full settings management. The theme follows your system's light/dark preference and there's a toggle in the header; the screenshots below are the light one.

**Checking for updates.** The **Check Updates** button above the container table runs the same check as `/check` and the cron schedule: every container, results in the pending list. Each row also has its own 🔎 button that checks just that container and tells you the outcome right there — handy when you only care about one image and don't want to wait for a full sweep, and the only way to get feedback at all if you run without Telegram or a webhook. Both refuse to start while an update is in progress, and while a check is already running, so a double-click can't produce two competing results. With `DEBUG=true` the per-container check also writes its full registry log — request URLs, status codes, redirects, full digests, resolved versions — to the browser console.

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-status.png" alt="Web UI status page: two hosts, version advisories, pending updates" width="700">
</p>

The `↑` badges are version advisories on pinned tags — a newer version exists, and Docksentry is telling you rather than acting on it. The `Host` column and the host filter only appear once `DOCKER_HOSTS` is set.

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-history.png" alt="Web UI history page: update history, container events, audit trail" width="700">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-logs.png" alt="Web UI logs page" width="700">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-settings.png" alt="Web UI settings page" width="700">
</p>

Below 700px the container table becomes cards, so the actions are on screen instead of behind a sideways swipe:

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-mobile-cards.png" alt="Container cards on a narrow screen" width="300">
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
| **Discord** | rich embeds | rich embeds | 27 slash commands |
| **Webhook** | JSON | JSON | via Web UI |

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/discord.png" alt="Discord Notifications" width="400">
</p>

Updates are only half of what arrives. A container that dies gets an alert with the exit code, the host's memory and load, which container was consuming what at that moment, and — when the event stream saw it — whether the kernel was the one that killed it:

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-crash-alert.png" alt="Crash alert with host memory, host load, OOM verdict and top consumers" width="520">
</p>

Health flips are reported the same way, in both directions, and the unhealthy message quotes what the healthcheck itself said — usually the fastest way to the cause:

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-unhealthy-recovered.png" alt="Unhealthy alert quoting the healthcheck output, followed by the recovery message" width="520">
</p>

See [Notification Setup](docs/notifications.md) for Discord and Webhook configuration, and [Container Monitoring](docs/monitoring.md) for what triggers each alert.

## Documentation

| Topic | Link |
|-------|------|
| **Configuration reference** — every env var | [docs/configuration.md](docs/configuration.md) |
| Update Workflow & Rollback | [docs/updates.md](docs/updates.md) |
| Container Monitoring | [docs/monitoring.md](docs/monitoring.md) |
| Web UI | [docs/web-ui.md](docs/web-ui.md) |
| Notification Channels | [docs/notifications.md](docs/notifications.md) |
| Docker Compose Support | [docs/compose.md](docs/compose.md) |
| Podman — CLI, socket activation, remote hosts | [docs/podman.md](docs/podman.md) |
| Security & Socket Proxy | [docs/security.md](docs/security.md) |
| Multi-Language | [docs/languages.md](docs/languages.md) |

## Roadmap

Docksentry is actively developed — see the [CHANGELOG](CHANGELOG.md) for what shipped in each release.

**v1.x — ongoing.** Continued bug fixes and smaller features driven by user feedback in [#2](https://github.com/amayer1983/docksentry/issues/2). Recent: container groups, maintenance mode, container notes, group/topic auth, restart-dependents for VPN-sidecar stacks.

**Shipped since — both of the items that used to sit under "v2.0, ahead":**

- **Multi-host management** — one instance managing several Docker or Podman hosts (`DOCKER_HOSTS=name:endpoint`, TCP or SSH), with per-host pending queues, host-prefixed notifications, a host selector in the Web UI, and `@host` / `@all` command targeting. Landed in v1.62.0.
- **Interactive Discord bot** — 27 slash-commands, confirmation buttons and the same control surface the Telegram bot offers, driven by the same update engine so the three front-ends cannot drift apart. Landed in v1.63.0.

**Shipped since:** an audit-free read-only surface — `/metrics` in Prometheus format and `GET /api/status` as JSON, both behind `API_TOKENS` so a scraper never needs the Web UI password. Plus `MONITOR_ONLY_CONTAINERS` for containers another tool owns, `MIN_IMAGE_AGE_DAYS` so you need not be first to pull a new release, registry mirrors for lookups, and the audit trail of who did what across the front ends — that one landed in v2.0.0 and lives on the Web UI History page.

**Next.** Notification templates with per-channel routing — the one thing every comparable project's users ask for that Docksentry still builds in code. And per-container resource figures on the status page, loaded after the page renders rather than making every load wait on `docker stats`.

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
