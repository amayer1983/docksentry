# Security

## Overview

- Only the configured `CHAT_ID` can interact with the Telegram bot
- `no-new-privileges` security option recommended
- Zero external dependencies — Python standard library + Docker CLI only (no supply-chain risk)
- Docker credentials mounted read-only
- Web UI password hashed (SHA-256), never stored in plain text
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
