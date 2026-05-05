# Update Workflow & Rollback

## How It Works

1. On the configured schedule, Docksentry compares local image digests with remote registry digests via the Docker Registry HTTP API
2. Pinned containers and containers in `EXCLUDE_CONTAINERS` are skipped
3. Containers on the auto-update list are updated immediately
4. Remaining updates are sent as a Telegram notification with inline action buttons
5. When you press update, the bot uses **Docker Compose** (if detected) or **docker run** to recreate the container
6. A health check verifies the container is running (and healthy)
7. If recreation or health check fails, the old container is automatically restored (rollback)
8. All updates are logged to the update history

## Update Notification

When updates are found, you receive a Telegram message with image sizes, dates, and action buttons:

- **Individual buttons** — update a single container, button changes to checkmark when done
- **Update all** — pull and restart all containers at once
- **Manual** — dismiss and handle updates yourself

## What Happens During an Update

1. Pull the new image
2. Stop the old container and rename it as backup
3. Recreate the container with the same configuration (ports, volumes, environment, labels, networks)
4. Run a **health check** — wait up to 30 seconds, verify the container is running (and healthy if a Docker HEALTHCHECK is defined)
5. On success: remove the backup and log the update to history
6. On failure: **automatically roll back** to the previous container

## Auto-Update Mode

Containers set to auto-update (`/autoupdate nginx` or via Web UI toggle) are updated automatically during scheduled checks — no confirmation needed. The bot sends a summary after completion. All other containers still show the usual notification with buttons.

## Update Windows (per container)

You can restrict auto-updates for specific containers to a time-of-day range and selected weekdays via the **Update Windows** section on the Settings page. Format: `HH:MM`–`HH:MM` plus a list of weekdays (Mon–Sun).

- Containers without a window entry are unrestricted (default).
- Time windows can wrap midnight (e.g. `23:00`–`02:00` on Sunday continues into Monday morning).
- If the cron tick falls outside a container's window, that container is skipped silently and the update remains pending — it'll trigger the next time the cron and the window line up.

Use case: keep Plex from auto-restarting at 19:00 family time; let database containers only update during a 02:00–04:00 maintenance slot.

## Major-Update Confirmation

For containers using SemVer-pinned tags (e.g. `redis:7.0.5`), you can opt in via the `⚠ on` button on the Status table. Once enabled:

- **Patch / minor bumps** (e.g. `7.0.5` → `7.0.6` or `7.1.0`) update normally.
- **Major bumps** (e.g. `7.x` → `8.0.0`) are held back. You'll see:
  - Telegram: an inline message with **Confirm** / **Skip** buttons
  - Web UI: a yellow banner on the Status page with the same actions

Confirm runs the update; Skip drops the request and you'll be asked again next cron tick if the version is still available.

> **Note:** detection requires a SemVer-parseable tag (`1.2.3`, `v1.2.3`, `redis-7.0.5`, …). Containers using `:latest` or non-numeric tags fall back to the existing digest-based update flow without major-confirm.

## Pinned Containers

Pinned containers (`/pin nginx` or via Web UI) are completely excluded from update checks. Use this for containers you want to keep on a specific version.

## What Gets Skipped

- The bot's own container (use `/selfupdate` instead)
- Containers running with image IDs instead of tags (locally built images)
- Containers in the `EXCLUDE_CONTAINERS` list
- Pinned containers

## Docker Hub Rate Limits

| | Update checks | Image pulls |
|---|---|---|
| **Without login** | Unlimited (uses registry API) | 100 per 6 hours |
| **With login** | Unlimited | Unlimited |

Update checks use the registry API and do **not** count against pull limits. For most setups, the rate limit is not an issue.

To add Docker Hub login, mount your credentials read-only:

```yaml
volumes:
  - /root/.docker/config.json:/.docker/config.json:ro
```
