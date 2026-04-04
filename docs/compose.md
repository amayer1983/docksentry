# Docker Compose Support

Docksentry automatically detects containers managed by Docker Compose via container labels and uses the native Compose workflow for updates.

## How It Works

When updating a Compose-managed container:

1. `docker compose pull <service>` — pulls the new image
2. `docker compose up -d --no-deps <service>` — recreates only the updated service
3. Health check and automatic rollback on failure

This preserves all Compose-specific configuration (depends_on, networks, deploy settings) that would be lost with a plain `docker run` recreation.

## Requirements

The Compose file must be accessible from inside the bot container. Mount the directory containing your `docker-compose.yml`:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - docksentry_data:/data
  - /path/to/your/stacks:/stacks:ro
```

## Portainer Stacks

If the Compose file is not accessible (e.g. Portainer-managed stacks stored in a database), the bot automatically falls back to the standard `docker run` recreation method. This still works — it just doesn't use the Compose workflow.

## Identification

Compose-managed containers are identified by Docker labels:
- `com.docker.compose.project`
- `com.docker.compose.service`
- `com.docker.compose.project.working_dir`
- `com.docker.compose.project.config_files`

Compose-managed containers are marked with a whale icon in update notifications.
