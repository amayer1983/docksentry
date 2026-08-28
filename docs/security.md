# Security

## Overview

- Only the configured `CHAT_ID` can interact with the Telegram bot
- `no-new-privileges` security option recommended
- Zero external dependencies — Python standard library + Docker CLI only (no supply-chain risk)
- Docker credentials mounted read-only
- Web UI password set in the interface is hashed with **scrypt** (n=16384, r=8, p=1, per-password salt) before it reaches `settings.json`
- A password supplied as `WEB_PASSWORD` is **not** hashed — an environment variable is plain text by nature, and it is compared as such. Set it in the interface if you would rather it were not
- Sensitive values (Bot Token, Chat ID) masked in Web UI

## Docker Socket Proxy (recommended)

Direct access to the Docker socket (`/var/run/docker.sock`) grants root-equivalent permissions on the host. This applies to **all** container management tools (Portainer, Watchtower, etc.), not just Docksentry.

For production environments, use a **Docker Socket Proxy** to restrict API access:

```yaml
services:
  socket-proxy:
    image: ghcr.io/tecnativa/docker-socket-proxy:latest
    container_name: socket-proxy
    restart: unless-stopped
    privileged: true
    environment:
      POST: 1           # Required for pull, rename, remove
      CONTAINERS: 1     # List, inspect, stop, start, rename, remove
      IMAGES: 1         # Pull, inspect, prune
      ALLOW_START: 1    # Start containers
      ALLOW_STOP: 1     # Stop containers
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - docksentry-internal

  docksentry:
    image: amayer1983/docksentry:latest
    container_name: docksentry
    restart: unless-stopped
    environment:
      - BOT_TOKEN=your-bot-token
      - CHAT_ID=your-chat-id
      - DOCKER_HOST=tcp://socket-proxy:2375
      - TZ=Europe/Berlin
    depends_on:
      - socket-proxy
    networks:
      - docksentry-internal
    # No docker.sock mount needed!
    volumes:
      - docksentry_data:/data
    security_opt:
      - no-new-privileges:true

networks:
  docksentry-internal:
    driver: bridge

volumes:
  docksentry_data:
```

**What this blocks:** Exec into containers, volume/network management, Swarm/secrets access, image builds — only container lifecycle and image pull/inspect are allowed.

> **Alternative:** [linuxserver/socket-proxy](https://github.com/linuxserver/docker-socket-proxy) is a drop-in replacement with the same environment variables and rootless support.

## Multi-host: how Docksentry reaches the other machines

Two directions, and they need different things. A reverse proxy protects
people *coming in* to the Web UI. It does nothing for the connection going
*out* from Docksentry to a remote Docker daemon, which is a separate link
that never passes through it. Worth stating plainly, because it is an easy
and reasonable thing to assume (#60).

| Endpoint in `DOCKER_HOSTS` | Encrypted | Authenticated |
|---|---|---|
| `ssh://user@host` | yes | yes, by your SSH key |
| `context://name` | whatever that stored connection uses | same |
| `tcp://host:2375` | **no** | **no** |

The last row is the one to be careful with. Docker's API on a plain `tcp://`
port has no TLS and no login: anyone who can reach that port can start a
container that mounts the host filesystem, which is root on that machine.
It does not matter how well the Web UI in front of it is protected.

**Use `ssh://` unless you have a specific reason not to.** It needs no
daemon configuration on the remote host, it is encrypted, and it
authenticates with a key you already manage. On Podman, give each host its
own key with `context://` — see [podman.md](podman.md) for why `ssh://`
cannot do that there.

`tcp://` is fine in two situations:

- the endpoint is a **socket proxy** on a private Docker network that only
  Docksentry is attached to (the section above), so the port is not
  reachable from your LAN at all;
- the remote daemon is configured with **TLS client certificates**
  (`2376`), which Docker supports and which is a job for the daemon's own
  configuration, not for Docksentry.

If you have a `tcp://` host that is neither, Docksentry says so once at
startup, naming the host. That warning is deliberately not silenceable: it
is a port that hands out root.

## Web UI with HTTPS (Reverse Proxy)

The built-in Web UI uses HTTP. For secure remote access, put it behind a reverse proxy with TLS.

**Traefik example:**

```yaml
  docksentry:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.docksentry.rule=Host(`docksentry.yourdomain.com`)"
      - "traefik.http.routers.docksentry.entrypoints=websecure"
      - "traefik.http.routers.docksentry.tls.certresolver=letsencrypt"
      - "traefik.http.services.docksentry.loadbalancer.server.port=8080"
```

**Caddy:**

```
docksentry.yourdomain.com {
    reverse_proxy docksentry:8080
}
```

> When using a reverse proxy, don't expose port 8080 directly — remove the `-p` mapping and let the proxy handle external access.

## API tokens (read-only)

`/metrics` and `GET /api/status` can be reached with a token instead of the
Web UI password:

```yaml
environment:
  - API_TOKENS=prom:a-long-random-string,grafana:another-one
```

```
curl -H "Authorization: Bearer a-long-random-string" http://host:8080/metrics
curl "http://host:8080/api/status?token=a-long-random-string"
```

Why a separate credential rather than the Web UI password: a scraper cannot
log in, and the browser password would give a monitoring job the ability to
stop your containers. A token reaches exactly two GET endpoints and can do
nothing else — `POST /api/update` with a valid token answers 401.

Name them so one can be revoked without disturbing the others. A token that
is presented and rejected gets a 401 rather than falling through to the
password check, so a revoked token stops working immediately even on an
instance with no `WEB_PASSWORD` set.

`?token=` exists because several scrapers cannot set headers. It does put
the secret in access logs — prefer the header where you can.

**Without `API_TOKENS` set, these endpoints follow the same rule as every
other page**: open if you have not set `WEB_PASSWORD`, behind it if you
have. If your Web UI is reachable from anywhere untrusted, set a password;
the metrics carry your container names.

## Mail

`SMTP_TLS_VERIFY` defaults to `true` and should stay there. Set it to
`false` only for an internal mail server with a self-signed certificate,
and know what it costs: the SMTP password is then sent to whatever answers
on that address, with any certificate at all.

## Security Checklist

| Measure | Priority | How |
|---------|----------|-----|
| Docker Socket Proxy | High | See example above |
| HTTPS for Web UI | High | Reverse proxy with TLS |
| Strong Web UI password | Medium | `WEB_PASSWORD=...` (hashed internally) |
| `no-new-privileges` | Medium | `security_opt` in compose |
| Private network | Medium | Internal Docker network for proxy |
| Rotate Telegram bot token | Low | Revoke via @BotFather if compromised |
| Docker Hub login | Low | Avoids rate limits, credentials read-only |
