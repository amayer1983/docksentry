<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/logo.png" alt="Docksentry Logo" width="200">
</p>

<h1 align="center">Docksentry</h1>

<p align="center">
Your Docker container watchdog — monitors images for updates and lets you manage them via <b>Telegram</b>, <b>Discord</b>, <b>Web UI</b>, and <b>Webhooks</b>, with auto-rollback and 16 languages.
</p>

<p align="center">
  <img src="https://img.shields.io/docker/pulls/amayer1983/docksentry" alt="Docker Pulls">
  <img src="https://img.shields.io/docker/image-size/amayer1983/docksentry" alt="Docker Image Size">
  <img src="https://img.shields.io/github/license/amayer1983/docksentry" alt="License">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-update-notification.jpg" alt="Update Notification" width="350">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/telegram-update-result.jpg" alt="Update Result" width="350">
</p>

## Features

- **Automatic update detection** — compares image digests on a configurable cron schedule
- **Telegram bot** — full interactive control with inline buttons and 14 commands
- **Discord notifications** — rich embeds for updates, successes, and failures
- **Generic webhooks** — JSON POST to Ntfy, Gotify, Home Assistant, or any HTTP endpoint
- **Optional Web UI** — dashboard with status, logs, history, settings, pin/unpin, auto-update toggles
- **Per-container auto-update** — selected containers update without confirmation
- **Pin/Freeze containers** — exclude containers from updates
- **Auto-rollback** — failed updates automatically restore the previous container
- **Docker Compose support** — native `docker compose pull/up` for Compose stacks
- **Self-update** — the bot can update itself automatically
- **Persistent settings** — Web UI changes survive restarts
- **Multi-language** — 16 languages, switchable at runtime
- **Lightweight** — Python standard library only, zero external dependencies

## Quick Start

### 1. Create a Telegram Bot

Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.

### 2. Get your Chat ID

Send a message to your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and find your `chat.id`.

### 3. Run

```bash
docker run -d \
  --name docksentry \
  --restart unless-stopped \
  -e BOT_TOKEN=your-bot-token \
  -e CHAT_ID=your-chat-id \
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
    security_opt:
      - no-new-privileges:true

volumes:
  docksentry_data:
```

## Commands

| Command | Description |
|---------|-------------|
| `/status` | Container overview with health, uptime, images |
| `/check` | Manually trigger an update check |
| `/updates` | Show pending updates |
| `/logs <name>` | Show last 30 log lines of a container |
| `/pin <name>` | Pin container — excluded from updates |
| `/unpin <name>` | Unpin container |
| `/autoupdate <name>` | Toggle auto-update per container |
| `/history` | Show update history |
| `/cleanup` | Remove old unused images |
| `/selfupdate` | Update the bot itself |
| `/debug` | Toggle debug mode |
| `/lang <code>` | Switch language |
| `/settings` | Show current configuration |
| `/help` | Show all commands |

> Partial name matching: `/pin ngi` matches `nginx`.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | *required* | Telegram Bot API token |
| `CHAT_ID` | *required* | Your Telegram chat ID |
| `CRON_SCHEDULE` | `0 18 * * *` | Cron expression for scheduled checks |
| `EXCLUDE_CONTAINERS` | | Comma-separated names to exclude |
| `AUTO_SELFUPDATE` | `false` | Auto-update the bot on each check |
| `LANGUAGE` | `en` | Bot language ([16 available](docs/languages.md)) |
| `WEB_UI` | `false` | Enable web dashboard |
| `WEB_PORT` | `8080` | Web UI port |
| `WEB_PASSWORD` | | Web UI password (Basic Auth) |
| `DISCORD_WEBHOOK` | | Discord webhook URL |
| `WEBHOOK_URL` | | Generic webhook URL (JSON POST) |
| `TZ` | `Europe/Berlin` | Timezone |
| `DOCKER_HOST` | | Docker API endpoint (for [socket proxy](docs/security.md)) |

All settings except BOT_TOKEN and CHAT_ID can also be changed via the Web UI and persist across restarts.

## Web UI

Enable with `WEB_UI=true`. Provides status dashboard, container logs, update history, and full settings management — all in a dark-themed, mobile-responsive interface.

<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-status.png" alt="Web UI Status" width="700">
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-logs.png" alt="Web UI Logs" width="700">
</p>

See [Web UI Documentation](docs/web-ui.md) for details.

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
| Web UI | [docs/web-ui.md](docs/web-ui.md) |
| Notification Channels | [docs/notifications.md](docs/notifications.md) |
| Docker Compose Support | [docs/compose.md](docs/compose.md) |
| Security & Socket Proxy | [docs/security.md](docs/security.md) |
| Multi-Language | [docs/languages.md](docs/languages.md) |

## Contributing

- **Feature ideas?** Open an [Issue](https://github.com/amayer1983/docksentry/issues) with the label `enhancement`
- **Found a bug?** Open an [Issue](https://github.com/amayer1983/docksentry/issues) with steps to reproduce
- **Translations?** Submit a PR for `app/lang/*.json`
- **Vote on the roadmap:** [Community Roadmap (Issue #2)](https://github.com/amayer1983/docksentry/issues/2)

## License

MIT License - see [LICENSE](LICENSE)
