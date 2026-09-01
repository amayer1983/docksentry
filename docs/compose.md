# Docker Compose Support

Docksentry automatically detects containers managed by Docker Compose via container labels and uses the native Compose workflow for updates.

## How It Works

When updating a Compose-managed container:

1. `docker compose pull <service>` — pulls the new image
2. `docker compose up -d --no-deps <service>` — recreates only the updated service
3. Health check and automatic rollback on failure

This is the exact path Compose itself would take, so the service comes back defined by its file rather than by a reconstruction of it.

## Requirements

Docksentry takes the path out of the container's own `com.docker.compose.project.config_files` label and opens **exactly that path** inside its own container. So the rule is always the same, and it has nothing to do with where the files sit on your host:

```
docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' <container>
```

Whatever that prints has to resolve inside Docksentry. Mount the host directory that holds your Compose files onto it.

**If you ran `docker compose` yourself**, the label holds a host path, so the mount is the directory onto itself:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - docksentry_data:/docksentry
  - /home/you/stacks:/home/you/stacks:ro
```

**If a stack manager created it** — Portainer, Dockge, Dockhand — the label holds a path inside *that* container, which does not exist on your host at all. The left side is then your host directory and the right side is the manager's path:

```yaml
  - /share/Container/Dockhand/stacks:/opt/stacks:ro
```

What does not work in either case is choosing the container-side path yourself. `- /home/you/stacks:/stacks:ro` leaves the label reading `/home/you/stacks/docker-compose.yml`, which still does not exist inside Docksentry — so the file counts as unreachable and the rebuild path is taken instead.

## When the Compose file can't be read

If the file is not accessible — a Portainer-managed stack living inside Portainer's own container, a directory that isn't mounted, or a container on a remote host, whose file is on that machine and not this one — Docksentry rebuilds the container from its inspect data with `docker run` instead. That carries nearly everything: ports, volumes, environment, networks, restart policy, capabilities, devices, resource limits, GPUs, log driver.

Nearly, not quite. Docksentry checks each container for the handful of things the rebuild really can't reproduce, and says so only when that container has one of them set:

| Named in the message | Why the rebuild loses it |
|---|---|
| `healthcheck` | only when it's yours and in exec form (`test: ["CMD", …]`) — `docker run` can only produce the shell form, so the check then needs `/bin/sh` in the image. Also when a timing is under a second, which rounds down to "unset". A `CMD-SHELL` check, or one that came with the image, survives untouched. |
| `tmpfs` | the long form (`volumes: - {type: tmpfs, …}`). The short `tmpfs:` list is carried. |
| `blkio_config` | the per-device read/write limits; a plain `blkio_weight` is carried |
| `cgroup_parent` | no flag is emitted, so the new container lands under Docker's default cgroup |
| `device_cgroup_rules` | still out of scope |
| `storage_opt` | driver-specific, and not restored |
| publish all ports (`-P`) | the randomly assigned host ports get pinned instead of reassigned |

Nothing from that list set means no message: the container was rebuilt exactly, and there is nothing to go and repair. Measured on one real host with 22 containers — 18 used to get the note, 3 were actually losing something.

`depends_on`, `profiles` and `deploy:` are not on the list. They never reach the container's inspect data, and none of them changes how a single container runs — which is all a recreate rebuilds.

## Identification

Compose-managed containers are identified by Docker labels:
- `com.docker.compose.project`
- `com.docker.compose.service`
- `com.docker.compose.project.working_dir`
- `com.docker.compose.project.config_files`

Compose-managed containers are marked with a whale icon in update notifications.
