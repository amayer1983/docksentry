# Notification Channels

Docksentry sends notifications via **Telegram** (primary, with interactive commands) and optionally via **Discord** and/or **generic webhooks**. All channels receive notifications in parallel.

![Discord Notifications](https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/discord.png)

## Channel Comparison

| Channel | Updates Available | Update Results | Interactive Commands |
|---------|:-:|:-:|:-:|
| **Telegram** | with buttons | detailed | full control |
| **Discord** | rich embeds | rich embeds | via Web UI |
| **Webhook** | JSON | JSON | via Web UI |

## Startup Notification

When Docksentry starts, it sends a startup message to all configured channels. This is useful to detect server reboots or container restarts.

## Quiet Hours

Set `QUIET_HOURS_START` and `QUIET_HOURS_END` (both `HH:MM`) to silence **auto-notifications** during a window — for example overnight:

```yaml
environment:
  - QUIET_HOURS_START=22:00
  - QUIET_HOURS_END=07:00
```

What's affected:

- **Suppressed during the window:** scheduled update notifications, auto-update results, cleanup results, disk warnings — across Telegram, Discord, and generic webhook
- **Always sent (regardless of the clock):** replies to manual commands you trigger yourself (`/status`, `/check`, Web UI button clicks). You're actively asking, you get an answer.

Notes:

- The window can wrap midnight — `22:00`–`07:00` works as expected
- Drops are silent — Docksentry doesn't queue and replay them later. The user explicitly opted into "leave me alone during these hours"
- Both empty = feature off

## When the network is down

A notification that fails on a network error is not lost. It is held and
tried again on the next scheduler pass — within 30 seconds of the connection
coming back.

**Not every channel has this.** Telegram, the Discord bot, the Discord
webhook and the generic webhook hold a failed message. ntfy, Gotify, Matrix,
Apprise and SMTP do not — if you only run one of those, a network blip while
an alert fires means that alert is gone.

Three limits keep the queue from becoming a second problem. Nothing is
delivered more than 15 minutes late, because a crash alert arriving two hours
on reads as something happening now. At most 20 messages are held at once,
and when it overflows the oldest goes first — it is the one closest to being
a lie anyway. Nothing is written to disk, so a restart clears the queue.

A late message says so, above whatever it was going to say:

```
⏳ Delayed 12m — no network when this fired. It happened at 15:35:03.
```

This is not quiet hours. A quiet-hours drop is a decision you made and stays
dropped; only delivery failures are replayed.

## Disk Space Warning

`DISK_WARN_PERCENT` (default `85`, range `50..100`) — when the data directory's filesystem usage exceeds this percentage, Docksentry sends a warning across all configured channels. Rate-limited to **one notification per 23-hour window** to prevent log floods.

```yaml
environment:
  - DISK_WARN_PERCENT=85
  - DISK_WARN_AUTO_CLEANUP=false   # set true to also trigger image cleanup
```

When `DISK_WARN_AUTO_CLEANUP=true`, crossing the threshold also runs `docker image prune` (using the configured `CLEANUP_GRACE_HOURS` and `CLEANUP_BACKUP_LOCAL_ONLY` settings). The cleanup result is sent as a follow-up notification.

> **Note:** the warning is based on the filesystem hosting the [data directory](configuration.md#where-the-data-lives). In typical setups this shares the same disk as `/var/lib/docker`. If you're running with a separate Docker storage driver mount, the percentage may not reflect Docker's actual disk usage.

## Discord

Add a webhook URL to receive notifications as rich embeds in a Discord channel:

1. In Discord: **Server Settings** -> **Integrations** -> **Webhooks** -> **New Webhook**
2. Copy the webhook URL
3. Add to your container:

```yaml
environment:
  - DISCORD_WEBHOOK=https://discord.com/api/webhooks/123456/abcdef...
```

You can also configure or change the Discord webhook URL via the Web UI settings page.

A webhook only pushes notifications one way. If you also want to *drive*
Docksentry from Discord — slash commands, and a channel the bot posts into
itself — that's the bot, and [docs/discord-bot.md](discord-bot.md) walks the
whole setup with screenshots.

### Discord Notifications Include

- **Update available** — blue embed with container list, image sizes, and creation dates
- **Update successful** — green embed with container name and details
- **Update failed** — red embed with error details
- **Startup message** — notification when the bot starts

## ntfy

Set `NTFY_URL` (a full topic URL) or `NTFY_SERVER` + `NTFY_TOPIC`.

For a protected topic — a self-hosted ntfy with `auth-default-access: deny`,
or a reserved topic on ntfy.sh:

```yaml
environment:
  - NTFY_TOKEN=tk_...            # an ntfy access token
  # or
  - NTFY_USER=me
  - NTFY_PASSWORD=...
```

Titles containing emoji or umlauts are encoded properly. If you use a
`BOT_LABEL` like `🖥 pve1` — which the README suggests — versions before
1.69.0 dropped every ntfy notification silently; upgrade if that sounds
familiar.

## Generic Webhook

For integration with Ntfy, Gotify, Home Assistant, or any service that accepts JSON POST requests:

```yaml
environment:
  - WEBHOOK_URL=https://your-service/webhook
```

### Payload Format

```json
{
  "event": "updates_available",
  "source": "docksentry",
  "count": 2,
  "containers": [
    {
      "name": "nginx",
      "image": "nginx:latest",
      "size": "141 MB",
      "created": "2026-03-15",
      "compose": false
    }
  ]
}
```

### Event Types

| Event | Description |
|-------|-------------|
| `updates_available` | New updates found during check |
| `update_result` | Single container update completed (success or failure) |
| `message` | General text message (startup, etc.) |
