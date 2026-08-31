# Docker Compose Support

Docksentry automatically detects containers managed by Docker Compose via container labels and uses the native Compose workflow for updates.

## How It Works

When updating a Compose-managed container:

1. `docker compose pull <service>` — pulls the new image
2. `docker compose up -d --no-deps <service>` — recreates only the updated service
3. Health check and automatic rollback on failure

This is the exact path Compose itself would take, so the service comes back defined by its file rather than by a reconstruction of it.

## Requirements

The Compose file has to be readable from inside the Docksentry container **at the same absolute path it has on the host**. Docksentry takes that path out of the container's own `com.docker.compose.project.config_files` label and opens exactly it — there is nothing to guess from about where you mounted it.

So mount the directory onto itself:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
  - docksentry_data:/data
  - /home/you/stacks:/home/you/stacks:ro
```

`- /home/you/stacks:/stacks:ro` does not work. The label still reads `/home/you/stacks/docker-compose.yml`, and that path does not exist inside the container — so the file counts as unreachable and the rebuild path is taken instead.

To see which path a container reports:

```
docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' <container>
```

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
