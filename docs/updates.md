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

## Container Groups (ordered updates)

For stacks where update order matters (e.g. **database before app**, **plex before sonarr/radarr**), define a **container group** under Settings → Container Groups:

- Pick a name and the containers in update order (drag-style reorder via ↑/↓ after creation)
- Optional **wait time** between containers (default 30s) — gives the first one time to come up
- A container can only be in **one** group; saving the group moves listed containers from other groups automatically

Behaviour:
- **Auto-updates respect the order.** Container 1 updates → wait → container 2 updates → wait → container 3 …
- **Failure aborts the rest.** If container 2 fails its health check, container 3 is *not* updated. (Avoids an updated app talking to a still-old database that just got rolled back.)
- **Manual updates ignore groups** — clicking "Update" on container 2 alone updates only container 2.

The Status table shows a `📦 GroupName` badge for grouped containers; the per-container detail page shows the group + position.

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

## Why didn't it see my new release?

By default a check prints a verdict and the digests behind it:

```
Checking 2 containers for updates...
  Checking: gitea-runner (registry-1.docker.io/gitea/runner:latest)
  Local:  gitea/runner@sha256:66d80966792e621c9761c47919644198d35fd1c297e9a01e69ed3c1ae37db0c7
  Remote: sha256:66d80966792e621c9761c47919644198d35fd1c297e9a01e69ed3c1ae37db0c7
  → Up to date
```

The digests are printed in full and with the repository prefix on purpose — that's the exact string you can hand to `docker manifest inspect gitea/runner:latest` to confirm the verdict yourself.

When that isn't enough, set `DEBUG=true` (or `/debug`, or the Web UI toggle) and run the check again. Same two containers:

```
Environment: host linux/amd64, mirrors: none, daemon proxy: none, our proxy: none
Checking 2 containers for updates...
  Checking: gitea-runner (registry-1.docker.io/gitea/runner:latest)
    HEAD https://registry-1.docker.io/v2/gitea/runner/manifests/latest
      HTTP 200, auth anonymous, content-type application/vnd.oci.image.index.v1+json
  Local:  gitea/runner@sha256:66d80966792e621c9761c47919644198d35fd1c297e9a01e69ed3c1ae37db0c7
  Remote: sha256:66d80966792e621c9761c47919644198d35fd1c297e9a01e69ed3c1ae37db0c7
  → Up to date
    local image built 2026-07-11, size 141 MB
    GET https://registry-1.docker.io/v2/gitea/runner/manifests/latest
      HTTP 200, auth anonymous, content-type application/vnd.oci.image.index.v1+json
    GET https://registry-1.docker.io/v2/gitea/runner/manifests/sha256:bbb…
      HTTP 200, auth anonymous, content-type application/vnd.oci.image.manifest.v1+json
    GET https://registry-1.docker.io/v2/gitea/runner/blobs/sha256:ccc…
      HTTP 200, auth anonymous, content-type application/octet-stream
      redirected to https://production.cloudflare.docker.com/registry-v2/…
    remote :latest is version 2.3.0 (built 2026-07-11)
  Checking: vaultwarden (registry-1.docker.io/vaultwarden/server:latest)
    HEAD https://registry-1.docker.io/v2/vaultwarden/server/manifests/latest
      HTTP 200, auth anonymous, content-type application/vnd.oci.image.index.v1+json
  Local:  vaultwarden/server@sha256:968b93c034b6231be037b8abce159dedbf7eb16adbc79ee2b1555c0eea31a4d3
  Remote: sha256:66d80966792e621c9761c47919644198d35fd1c297e9a01e69ed3c1ae37db0c7
  → UPDATE AVAILABLE (current: 2026-03-02, size: 196 MB)
    …
    remote :latest is version 1.35.0 (built 2026-07-24), local is 1.34.1
Found 1 updates.
```

What each part answers:

| Line | Tells you |
|---|---|
| `Environment:` | The daemon's platform, its **registry mirrors**, its proxy settings, and the `http_proxy`/`https_proxy` variables set inside the Docksentry container. The last one matters: Python's HTTP client picks those up on its own, so a proxy nobody configured in Docksentry can still sit in the path. `unknown` means `docker info` was refused — normal behaviour behind a restrictive socket proxy. |
| `HEAD` / `GET <url>` | The exact request. Docksentry talks to the registry itself over HTTP — no docker CLI, no daemon — so this is the whole story. |
| `HTTP <code>, auth …, content-type …` | Status, how we authenticated (`anonymous`, `bearer`, or `credentials from config` — the token itself is never logged) and what came back. An `image.index` / `manifest.list` content type means a multi-arch index; anything else means a single-arch image. |
| `redirected to …` | A mirror, a proxy or a CDN answered instead of the host we asked. If you suspect stale caching, this is where it shows. |
| `remote :<tag> is version …` | **The digest, resolved to a human version.** `66d8096…` on its own tells you nothing; `66d8096… is 2.3.0` settles the question. |

Two things worth knowing about the version line:

- It's read from the image's `org.opencontainers.image.version` label. Images that don't set one report `carries no version label` — that's the image's choice, not a failure.
- It costs extra registry requests, so it doesn't run on every container of every scheduled sweep. You get it when you press the 🔍 button on a single container, or on any check while `DEBUG` is on. See the rate-limit note below for why.

Added for [#53](https://github.com/amayer1983/docksentry/issues/53) (@LeeNX).

## Docker Hub Rate Limits

| | Update checks | Image pulls |
|---|---|---|
| **Without login** | Unlimited (uses registry API) | 100 per 6 hours |
| **With login** | Unlimited | Unlimited |

Update checks use the registry API and do **not** count against pull limits. For most setups, the rate limit is not an issue.

The digest check is a `HEAD` on the manifest, and Docker Hub doesn't charge for those. Reading the *version* behind a digest needs `GET`s (two or three manifests plus the config blob, per container), and those do count — anonymously that's 100 an hour. Thirty containers on a 15-minute schedule would be roughly 240 requests an hour and Docksentry would rate-limit itself into `429`s, which is a far better way to miss an update than any it might explain. So version resolution is limited to a single-container check or a `DEBUG` run, and the scheduled sweep stays on `HEAD`.

To add Docker Hub login, mount your credentials read-only:

```yaml
volumes:
  - /root/.docker/config.json:/.docker/config.json:ro
```

## Other registries

The numbers above are Docker Hub's — it's the strict one, and the registry most people bump into. Others set their own rules, and they're generally more permissive: GitHub's `ghcr.io` and `quay.io`, for example, don't rate-limit anonymous manifest reads the way Hub does. Docksentry doesn't special-case any of them; it does the same lightweight `HEAD`-first check everywhere, which is exactly the shape that stays under whatever limit a given registry happens to enforce. If you do run into throttling on a registry, the same credential mount applies — Docksentry authenticates against whatever host `DOCKER_REGISTRY` points at, not just Hub — and the `DEBUG` log will show you which host actually answered, so you can tell a real limit from a mirror or proxy in the path.
