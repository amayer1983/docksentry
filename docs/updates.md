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
4. Run a **health check** — wait up to `HEALTHCHECK_MAX_STARTING` (default **600 s**), or longer if the image declares a `start_period` (that value × 1.5), and verify the container is running (and healthy if a Docker HEALTHCHECK is defined)
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

### Containers behind a VPN sidecar (Gluetun and friends)

This is the case ordering alone does not solve, and the one most update
tools get wrong.

When a container routes its traffic through another one — Gluetun being
the common example — it does not have a network of its own. Its compose
entry says:

```yaml
services:
  gluetun:
    image: qmcgaw/gluetun
    # ports for the whole stack are published HERE, not on the apps
    ports:
      - 8080:8080     # qbittorrent
      - 8989:8989     # sonarr

  qbittorrent:
    image: linuxserver/qbittorrent
    network_mode: "container:gluetun"   # ← no network of its own
    depends_on: [gluetun]

  sonarr:
    image: linuxserver/sonarr
    network_mode: "container:gluetun"
```

`network_mode: container:gluetun` is stored by Docker as
`container:<id>` — the *ID* of the running Gluetun container, not its
name. That single detail is what breaks updates:

**Updating Gluetun destroys the old container and creates a new one with
a new ID.** Every container pointing at the old ID is now pointing at
something that no longer exists. They do not fail over, they do not
reconnect, and `docker restart` on them fails outright with a dead
reference. In practice you come back to a stack where the VPN updated
cleanly and everything behind it is stopped, with the apps still
reachable on no port at all because the ports belong to the sidecar.

**What Docksentry does.** Put Gluetun and everything behind it in one
group, with Gluetun first, and tick **"Restart dependents when the head
container updates"** on that group:

```
Group "vpn-stack":          ☑ restart dependents
  1. gluetun        ← the head
  2. qbittorrent
  3. sonarr
```

That tick is what switches the whole mechanism on. Without it the group
is an ordering group like any other: the containers update in sequence
and nothing repairs their network afterwards. Grouping alone is not
enough.

The head updates. Docksentry waits for it to become healthy, then, for
each container behind it, checks whether it shares the head's network
namespace. Those are **recreated** rather than restarted, rebuilt from
their own inspect output with the *same image* — no pull, no version
change — and attached to `container:<name>` instead of the ID. The name
survives the next update; the ID does not. Group members that are *not*
netns-sharing are simply restarted, which is cheaper and enough.

Points worth knowing:

- **The head is identified by position, not by configuration.** The first
  container in the group is the head. Put the sidecar anywhere else and
  the containers behind it will be handled before it updates, which
  achieves nothing.
- **An unhealthy head does not stop the repair.** Docksentry waits for the
  head using the group's own wait time (never less than 30 seconds), and
  if it still is not healthy it says so and fixes the dependents anyway —
  leaving them pointing at a dead ID would be the worse outcome of the two.
- **The old container is kept until the new one is up.** The dependent is
  renamed to `<name>_old` before the rebuild and restored if the rebuild
  fails, so a failed recreate leaves you where you started rather than
  with nothing.
- **`--rm` containers are handled.** A container started with `AutoRemove`
  vanishes the moment it is stopped, so its configuration is read *before*
  the stop rather than after.
- **This applies to any netns sharing**, not just Gluetun: the same is
  true for a Tailscale sidecar, a WireGuard container, or anything else
  another container is routed through.

Beyond the group, the order, and that one tick, there is nothing to
configure: which members share the head's namespace is detected per
update, not stored, so it stays right when you change the stack.

The Groups page in the Web UI marks such a group with 🔁, and `/groups`
in Telegram or Discord shows the head with 👑. There is also a **"Restart
dependents now"** button for the case where you updated the head yourself,
outside Docksentry, and need the stack put back together.

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

## Tags, versions, and what "up to date" means

This is the thing newcomers most often expect to work differently, so it
is worth being exact about (#33, @LeeNX, who asked for precisely this).

Docksentry compares the **digest of the tag you are running** against the
same tag on the registry. That single sentence explains every case:

| Your tag | What happens |
|---|---|
| `:latest`, `:stable`, `:main` | The publisher moves the tag, the digest changes, you get an update. |
| `:1.25.3` | The tag is immutable. Its digest never changes, so there is never an update — **even when 1.26 has shipped.** |
| `:1.25.3`, rebuilt upstream | If the publisher re-pushes the *same* tag, usually for a security fix, the digest does change and you do get an update. |

So a pinned version tag still receives security rebuilds. What it never
receives is a **version jump** — and that is deliberate. Writing
`nginx:1.25.3` in your compose file is a statement of intent; moving you to
1.26 without being asked would override it, and for a database it could
destroy data.

### But it tells you

Since v2.2.0, a container on a pinned SemVer tag that has a newer version
available carries a badge saying so:

```
nginx  1.25.3  ↑ 1.26.2
```

It is **advisory only**. Docksentry will not switch to it, will not offer a
button for it, and will not count it among your pending updates. To move,
change the tag in your compose file and redeploy — which is the same thing
you would have done anyway, now with the information you were missing.

What it compares against, and why:

- **The same number of components.** `postgres:16.3` is matched against
  `16.4`, not against `16.4.1`. Somebody pinned to a two-number tag means
  that line, and the equivalent step has two numbers too. This matters in
  practice: `redis` publishes both shapes (9 two-component tags and 53
  three-component ones, measured on Docker Hub), so mixing them would be
  the ordinary case rather than a corner.

  Two-component tags were not covered at all before v2.6.0, on the grounds
  that SemVer needs three numbers. That gap swallowed exactly the images
  people pin most: `postgres` publishes **32** two-component tags and not a
  single three-component one, so the advisory could never fire for it.

- **The same suffix.** `redis:7.2-alpine` is matched against
  `7.4-alpine`, never against the plain `7.4` — a different image variant
  is not a newer version of yours.

- **The same major version.** `postgres:16.3` hears about `16.4`, not
  about `17.0`. A Postgres major is not a tag change, it is `pg_upgrade`;
  a container that swapped only the tag would not open its old data
  directory. The badge says "there is a newer one", and pointing it at
  something you cannot reach by changing a tag would make it say the wrong
  thing. At the top of your own line you therefore see nothing, which is
  the honest answer: within what you pinned, you are current.

  This applies to the advisory only. Major-version **confirmation** —
  the `⚠ on` button above — still looks across majors, because spotting a
  major is its entire job.

- **A four-digit first number is treated as a date, not a version.** So a
  tag like `2024.11` produces no advisory. This one is precaution rather
  than a measured case: no such tag exists in `postgres`, `mariadb`,
  `redis` or `mysql` (300 tags each, checked). It is written down here so
  it stays a decision.

One limit worth knowing rather than discovering:

- **A repository with two numbering schemes can confuse it.**
  `linuxserver/qbittorrent` tags both the application (`4.6.5`) and its
  Ubuntu base (`20.04.1`), and nothing in the tag text separates them.
  Candidates more than three major versions ahead are ignored as a
  different scheme — a heuristic, and one that can be wrong in both
  directions. It only ever suppresses an advisory or a confirmation
  prompt; it never causes an update to be applied.

## Pinned Containers

Pinned containers (`/pin nginx` or via Web UI) are completely excluded from update checks. Use this for containers you want to keep on a specific version.

## What Gets Skipped

- The bot's own container (use `/selfupdate` instead)
- Containers running with image IDs instead of tags (locally built images)
- Containers in the `EXCLUDE_CONTAINERS` list
- Pinned containers

## Watch but never update

Some containers are not yours to recreate. A podman quadlet belongs to
systemd; a Portainer stack belongs to Portainer; anything deployed by
Ansible or a GitOps pipeline belongs to that. Recreating one behind its
owner's back leaves two things with an opinion about what should be
running.

The older ways out all mean *stop looking*: `docksentry.pin`,
`docksentry.enable=false` and `EXCLUDE_CONTAINERS` drop the container from
the scan entirely, so you lose the version and update information that was
the reason for watching it.

```yaml
environment:
  - MONITOR_ONLY_CONTAINERS=systemd-*,gitea-*
```

or per container:

```yaml
labels:
  - "docksentry.monitor-only=true"
```

Matched containers stay in the list, keep reporting updates, and are never
updated — not on the schedule and not by pressing the button, because the
update is wrong whoever asks for it. In the Web UI the row keeps its check
button and loses the rest.

`EXCLUDE_CONTAINERS` takes the same wildcards (`*`, `?`, `[abc]`). A
pattern without one behaves exactly as it always did.

## Don't be the guinea pig

```yaml
environment:
  - MIN_IMAGE_AGE_DAYS=7        # or docksentry.min-age=7 per container
```

Holds automatic updates back until the image has been public for that
long. Two reasons people want it: let someone else find the broken release
first, and give a compromised image time to be noticed before you pull it.

Auto path only — pressing the button always works. And it defers rather
than discards: the update stays in the pending list and applies by itself
on a later run once the image has aged.

If the registry exposes no build date, the update goes through. A gate
cannot judge what it cannot see, and blocking everything undateable would
quietly stop updates for a large share of images.

## Registries that need help being reached

Checks go out over HTTPS straight to the registry named in the image
reference. They do **not** use the daemon's `registry-mirrors`, so on a
network where only a mirror is reachable, `docker pull` works and
Docksentry reports "unreachable" forever.

```yaml
environment:
  - REGISTRY_MIRRORS=docker.io=mirror.internal
  - INSECURE_REGISTRIES=mirror.internal,10.0.0.*
```

`REGISTRY_MIRRORS` redirects **lookups** only. Pulling still hands the
container's own image reference to the daemon — pulling from elsewhere
would rewrite that reference (`nginx:1.25` becoming
`mirror.internal/nginx:1.25`) and your container would stop matching your
own compose file. Use `registry-mirrors` in `daemon.json` for the pull
side; it covers every pull on the host rather than only ours.

`INSECURE_REGISTRIES` allows plain HTTP for the hosts you name — never
guessed, and never a fallback when TLS fails, because a tool that quietly
retries over HTTP hands credentials to whoever answers.

### A registry behind your own CA

If your registry has a real certificate signed by a CA you run yourself,
`INSECURE_REGISTRIES` is **not** the setting you want. It drops TLS
entirely, which against a TLS-only port fails outright and against a port
that does answer HTTP sends your Basic credentials in clear text.

Point `SSL_CERT_FILE` at the CA bundle instead and TLS keeps working —
you get verification against *your* CA rather than none at all:

```yaml
environment:
  - SSL_CERT_FILE=/certs/my-ca.pem
volumes:
  - /path/to/ca.pem:/certs/my-ca.pem:ro
```

Measured against a self-signed `registry:2`: without it the check fails
with a certificate error, with it the digest and the tag list come back
normally. This is Python's own variable rather than something Docksentry
invents, so it also covers a self-signed *webhook* or SMTP endpoint on the
same bundle.

Your CA alone is enough — public registries keep working alongside it.
`SSL_CERT_FILE` replaces only the bundled CA *file*; the hashed directory
at `/etc/ssl/certs` is still consulted, and in the Docksentry image that
holds 119 public roots. Measured inside the running container: with
`SSL_CERT_FILE` pointing at a private CA and nothing else, Docker Hub
still verified. So there is no need to concatenate anything.

Registries answering `WWW-Authenticate: Basic` (the stock `registry:2`
behind htpasswd, Nexus and Artifactory in their simpler modes) work with
the credentials already in your `config.json`; nothing extra to set.

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
