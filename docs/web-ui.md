# Web UI

Docksentry includes an optional, lightweight web dashboard. Enable it with `WEB_UI=true`.

![Web UI Status](https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-status.png)
![Web UI Logs](https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-logs.png)
![Web UI Settings](https://raw.githubusercontent.com/amayer1983/docksentry/main/docs/images/webui-settings.png)

## Setup

```yaml
environment:
  - WEB_UI=true
  - WEB_PASSWORD=your-secret    # optional, recommended
  - WEB_PORT=8080               # default
ports:
  - "9090:8080"
```

Access at `http://your-server:9090` with the configured password (Basic Auth).

## Pages

### Status

Live overview of all running containers with:
- Health badges (healthy / starting / running)
- Pending update badges
- Pinned container badges
- Auto-update toggle switches per container
- Pin/Unpin buttons per container
- Update buttons for containers with available updates
- "Check Updates" button to trigger a manual scan

### Logs

View container logs directly in the browser:
- Dropdown to select any running container
- Configurable number of lines (10-500)

### History

Full update log showing:
- Timestamp
- Container name
- Success/failure status
- Detail message

### Settings

All settings are editable and **persist across restarts** (saved to `/data/settings.json`):

| Setting | Editable in Web UI |
|---------|--------------------|
| Language | Yes |
| Cron Schedule | Yes |
| Debug Mode | Yes |
| Auto Self-Update (Bot) | Yes |
| Exclude Containers | Yes |
| Discord Webhook | Yes |
| Webhook URL | Yes |
| Bot Token | No (ENV only, masked) |
| Chat ID | No (ENV only, masked) |

## Security

- Password protection via Basic Auth (`WEB_PASSWORD`)
- Password hashed with SHA-256, never stored in plain text
- Sensitive values (Bot Token, Chat ID) are masked in the UI
- For HTTPS, use a reverse proxy — see [Security](security.md)
