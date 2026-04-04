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

### Discord Notifications Include

- **Update available** — blue embed with container list, image sizes, and creation dates
- **Update successful** — green embed with container name and details
- **Update failed** — red embed with error details
- **Startup message** — notification when the bot starts

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
