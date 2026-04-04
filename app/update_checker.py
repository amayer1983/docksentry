#!/usr/bin/env python3
"""Docker image update checker and container updater."""

import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
import re
from datetime import datetime


class UpdateChecker:
    def __init__(self, config):
        self.config = config
        self.debug_log = []

    def _debug(self, msg):
        print(msg)
        if self.config.debug:
            self.debug_log.append(msg)

    def get_running_containers(self):
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Image}}"],
            capture_output=True, text=True
        )
        # Get own container name to exclude self
        hostname = os.environ.get("HOSTNAME", "")
        own_name = None
        if hostname:
            own_result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}}", hostname],
                capture_output=True, text=True
            )
            if own_result.returncode == 0:
                own_name = own_result.stdout.strip().lstrip("/")

        containers = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            name, image = line.split("|", 1)
            # Skip self
            if own_name and name == own_name:
                self._debug(f"  Skipped (self): {name}")
                continue
            # Skip images referenced by ID (no tag)
            if re.match(r'^[0-9a-f]{12,}$', image):
                self._debug(f"  Skipped (image ID): {name} ({image})")
                continue
            if name in self.config.exclude_containers:
                self._debug(f"  Skipped (excluded): {name}")
                continue
            if name in self._get_pinned():
                self._debug(f"  Skipped (pinned): {name}")
                continue
            # Detect Docker Compose
            compose_info = self._get_compose_info(name)
            containers.append({"name": name, "image": image, **compose_info})
        return containers

    def _parse_image(self, image):
        """Parse image reference into registry, repository, tag."""
        tag = "latest"
        if ":" in image and not image.endswith(":"):
            parts = image.rsplit(":", 1)
            if "/" not in parts[1]:
                image, tag = parts

        if image.startswith("sha256:"):
            return None, None, None

        # Determine registry
        if "/" not in image:
            return "registry-1.docker.io", f"library/{image}", tag

        first_part = image.split("/")[0]
        if "." in first_part or ":" in first_part or first_part == "localhost":
            registry = first_part
            repository = "/".join(image.split("/")[1:])
        else:
            registry = "registry-1.docker.io"
            repository = image

        return registry, repository, tag

    def _get_auth_token(self, registry, repository):
        """Get authentication token for registry API."""
        try:
            # Docker Hub
            if "docker.io" in registry:
                docker_config = os.environ.get("DOCKER_CONFIG", "/.docker")
                config_file = os.path.join(docker_config, "config.json")
                auth_header = None
                if os.path.isfile(config_file):
                    with open(config_file) as f:
                        cfg = json.load(f)
                    for key in cfg.get("auths", {}):
                        if "docker.io" in key:
                            auth_header = cfg["auths"][key].get("auth")
                            break
                    self._debug(f"  Auth: {'credentials found' if auth_header else 'no credentials'}")

                url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull"
                req = urllib.request.Request(url)
                if auth_header:
                    req.add_header("Authorization", f"Basic {auth_header}")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read()).get("token")

            # GitHub Container Registry
            if "ghcr.io" in registry:
                url = f"https://ghcr.io/token?scope=repository:{repository}:pull"
                with urllib.request.urlopen(url, timeout=15) as resp:
                    return json.loads(resp.read()).get("token")

        except Exception as e:
            self._debug(f"  Auth error: {e}")
        return None

    def _get_remote_digest(self, registry, repository, tag, token):
        """Get remote image digest via registry API HEAD request."""
        if "docker.io" in registry:
            url = f"https://registry-1.docker.io/v2/{repository}/manifests/{tag}"
        elif "ghcr.io" in registry:
            url = f"https://ghcr.io/v2/{repository}/manifests/{tag}"
        else:
            url = f"https://{registry}/v2/{repository}/manifests/{tag}"

        req = urllib.request.Request(url, method="HEAD")
        req.add_header("Accept", ", ".join([
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
        ]))
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                digest = resp.headers.get("Docker-Content-Digest", "")
                return digest
        except Exception as e:
            self._debug(f"  Registry error: {e}")
            return None

    def _get_local_digest(self, image):
        """Get local image digest from RepoDigests."""
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image],
            capture_output=True, text=True
        )
        if result.returncode == 0 and "@" in result.stdout:
            return result.stdout.strip().split("@")[1]
        return None

    def _get_image_size(self, image):
        """Get local image size in human-readable format."""
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Size}}", image],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            try:
                size_bytes = int(result.stdout.strip())
                if size_bytes >= 1073741824:
                    return f"{size_bytes / 1073741824:.1f} GB"
                elif size_bytes >= 1048576:
                    return f"{size_bytes / 1048576:.0f} MB"
                else:
                    return f"{size_bytes / 1024:.0f} KB"
            except ValueError:
                pass
        return "?"

    def _get_image_created(self, image):
        """Get image creation date."""
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Created}}", image],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            created = result.stdout.strip()[:10]  # Just the date part
            return created
        return "?"

    def _get_compose_info(self, name):
        """Detect if container belongs to a Docker Compose stack."""
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{index .Config.Labels \"com.docker.compose.project\"}}||"
             "{{index .Config.Labels \"com.docker.compose.service\"}}||"
             "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}||"
             "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}",
             name],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return {}
        parts = result.stdout.strip().split("||")
        project = parts[0] if len(parts) > 0 else ""
        service = parts[1] if len(parts) > 1 else ""
        config_file = parts[2] if len(parts) > 2 else ""
        working_dir = parts[3] if len(parts) > 3 else ""
        if not project:
            return {}
        return {
            "compose_project": project,
            "compose_service": service,
            "compose_file": config_file,
            "compose_dir": working_dir,
        }

    def _get_pinned(self):
        """Get list of pinned (frozen) container names."""
        if os.path.exists(self.config.pinned_file):
            try:
                with open(self.config.pinned_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return []

    def _save_history(self, name, image, success, detail=""):
        """Append an entry to the update history file."""
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "container": name,
            "image": image,
            "success": success,
            "detail": detail,
        }
        history = []
        if os.path.exists(self.config.history_file):
            try:
                with open(self.config.history_file) as f:
                    history = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        history.append(entry)
        # Keep last 100 entries
        history = history[-100:]
        with open(self.config.history_file, "w") as f:
            json.dump(history, f, indent=2)

    def _wait_healthy(self, name, attempts=6, interval=5):
        """Wait for container to become healthy. Returns (healthy, state, health)."""
        for i in range(attempts):
            time.sleep(interval)
            sc = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True
            )
            state = sc.stdout.strip() if sc.returncode == 0 else ""
            hc = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", name],
                capture_output=True, text=True
            )
            health = hc.stdout.strip() if hc.returncode == 0 else ""
            self._debug(f"  Health check [{i+1}/{attempts}]: state={state}, health={health}")
            if state != "running":
                return False, state, health
            if not health or health == "<no value>":
                return True, state, health
            elif health == "healthy":
                return True, state, health
            elif health == "unhealthy":
                return False, state, health
        return False, state, health

    def check_all(self, bot=None):
        self.debug_log = []
        containers = self.get_running_containers()
        self._debug(f"Checking {len(containers)} containers for updates...")
        updates = []

        for c in containers:
            image = c["image"]
            registry, repository, tag = self._parse_image(image)
            if not registry:
                self._debug(f"  Skipped (unparseable): {c['name']} ({image})")
                continue

            self._debug(f"  Checking: {c['name']} ({registry}/{repository}:{tag})")

            local_digest = self._get_local_digest(image)
            if not local_digest:
                self._debug(f"  Skipped (no local digest): {c['name']}")
                continue

            token = self._get_auth_token(registry, repository)
            remote_digest = self._get_remote_digest(registry, repository, tag, token)

            self._debug(f"  Local:  {local_digest[:30]}...")
            self._debug(f"  Remote: {(remote_digest or 'FAILED')[:30]}...")

            if remote_digest and local_digest != remote_digest:
                size = self._get_image_size(image)
                created = self._get_image_created(image)
                self._debug(f"  → UPDATE AVAILABLE (current: {created}, size: {size})")
                c["size"] = size
                c["created"] = created
                updates.append(c)
            else:
                self._debug(f"  → Up to date")

        # Save pending updates
        with open(self.config.pending_file, "w") as f:
            json.dump(updates, f)
        self._debug(f"Found {len(updates)} updates.")

        # Send debug log via Telegram
        if self.config.debug and bot and self.debug_log:
            log_text = "\n".join(self.debug_log)
            # Split into chunks if too long
            while log_text:
                chunk = log_text[:3500]
                log_text = log_text[3500:]
                bot.send_message(f"```\n{chunk}\n```")

        return updates

    def update_container(self, name, image, compose_project=None, compose_service=None,
                         compose_file=None, compose_dir=None, **kwargs):
        # Try Compose update if container belongs to a stack
        if compose_project and compose_service and compose_file:
            return self._update_compose(name, image, compose_project, compose_service,
                                        compose_file, compose_dir)

        return self._update_standalone(name, image)

    def _update_compose(self, name, image, project, service, config_file, working_dir):
        """Update a container using Docker Compose."""
        self._debug(f"Updating (compose): {name} (project={project}, service={service})...")

        # Get old image info
        old_created = self._get_image_created(image)

        # Check if compose file is accessible
        if not os.path.isfile(config_file):
            self._debug(f"  Compose file not found: {config_file} — falling back to standalone")
            return self._update_standalone(name, image)

        # Pull new image via compose
        pull_cmd = ["docker", "compose", "-f", config_file, "-p", project, "pull", service]
        self._debug(f"  Running: docker compose -f {config_file} -p {project} pull {service}")
        result = subprocess.run(pull_cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            msg = f"Compose pull failed: {result.stderr[:200]}"
            self._save_history(name, image, False, msg)
            return False, msg

        # Get new image info after pull
        new_created = self._get_image_created(image)
        new_size = self._get_image_size(image)

        # Recreate service via compose
        up_cmd = ["docker", "compose", "-f", config_file, "-p", project, "up", "-d", "--no-deps", service]
        self._debug(f"  Running: docker compose -f {config_file} -p {project} up -d --no-deps {service}")
        result = subprocess.run(up_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            msg = f"Compose up failed: {result.stderr[:200]}"
            self._save_history(name, image, False, msg)
            return False, msg

        # Health check
        self._debug(f"  Health check: waiting for {name}...")
        healthy, state, health = self._wait_healthy(name)

        if not healthy:
            # Rollback: recreate with old image
            self._debug(f"  Health check FAILED — rolling back via compose")
            subprocess.run(["docker", "compose", "-f", config_file, "-p", project,
                            "up", "-d", "--no-deps", service],
                           capture_output=True, text=True, timeout=120)
            msg = f"Health check failed (state={state}, health={health}) — rolled back"
            self._save_history(name, image, False, msg)
            return False, msg

        detail = f"📅 {old_created} → {new_created}, 📦 {new_size}"
        self._save_history(name, image, True, f"compose: {detail}")
        return True, f"OK ({detail})"

    def _update_standalone(self, name, image):
        self._debug(f"Updating: {name} ({image})...")

        # Get old image info before pull
        old_created = "?"
        old_inspect = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Created}}", image],
            capture_output=True, text=True
        )
        if old_inspect.returncode == 0:
            old_created = old_inspect.stdout.strip()[:10]

        # Pull new image
        result = subprocess.run(
            ["docker", "pull", image],
            capture_output=True, text=True, timeout=1800
        )
        if result.returncode != 0:
            if "toomanyrequests" in result.stderr:
                msg = "Rate limit erreicht"
                self._save_history(name, image, False, msg)
                return False, f"{msg}. `docker login` auf dem Host ausführen und Credentials mounten."
            msg = f"Pull failed: {result.stderr[:200]}"
            self._save_history(name, image, False, msg)
            return False, msg

        # Get new image info after pull
        new_created = "?"
        new_size = "?"
        new_inspect = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Created}}||{{.Size}}", image],
            capture_output=True, text=True
        )
        if new_inspect.returncode == 0:
            parts = new_inspect.stdout.strip().split("||")
            new_created = parts[0][:10]
            if len(parts) > 1:
                try:
                    size_bytes = int(parts[1])
                    if size_bytes >= 1073741824:
                        new_size = f"{size_bytes / 1073741824:.1f} GB"
                    elif size_bytes >= 1048576:
                        new_size = f"{size_bytes / 1048576:.0f} MB"
                    else:
                        new_size = f"{size_bytes / 1024:.0f} KB"
                except ValueError:
                    pass

        self._debug(f"  Pull OK: {name} ({old_created} → {new_created}, {new_size})")

        # Recreate container: stop, rename old, create new with same config, start, remove old
        try:
            # Get full container config for recreation
            inspect_raw = subprocess.run(
                ["docker", "inspect", name],
                capture_output=True, text=True
            )
            if inspect_raw.returncode != 0:
                return True, "Image pulled. Container inspect failed."

            config = json.loads(inspect_raw.stdout)[0]
            self._debug(f"  Recreating container: {name}")

            # Stop container
            subprocess.run(["docker", "stop", name], capture_output=True, timeout=60)
            self._debug(f"  Stopped: {name}")

            # Rename old container
            old_name = f"{name}_old"
            subprocess.run(["docker", "rename", name, old_name], capture_output=True, timeout=10)
            self._debug(f"  Renamed to: {old_name}")

            # Build docker run command from inspect config
            cmd = ["docker", "run", "-d", "--name", name]

            # Restart policy
            restart = config.get("HostConfig", {}).get("RestartPolicy", {})
            if restart.get("Name"):
                policy = restart["Name"]
                if restart.get("MaximumRetryCount", 0) > 0:
                    policy += f":{restart['MaximumRetryCount']}"
                cmd.extend(["--restart", policy])

            # Network mode
            network_mode = config.get("HostConfig", {}).get("NetworkMode", "")
            if network_mode and network_mode != "default":
                cmd.extend(["--network", network_mode])

            # Environment variables
            for env in config.get("Config", {}).get("Env", []):
                cmd.extend(["-e", env])

            # Volumes/Mounts
            for mount in config.get("Mounts", []):
                if mount["Type"] == "bind":
                    bind = f"{mount['Source']}:{mount['Destination']}"
                    if not mount.get("RW", True):
                        bind += ":ro"
                    cmd.extend(["-v", bind])
                elif mount["Type"] == "volume":
                    bind = f"{mount['Name']}:{mount['Destination']}"
                    if not mount.get("RW", True):
                        bind += ":ro"
                    cmd.extend(["-v", bind])

            # Port mappings
            ports = config.get("HostConfig", {}).get("PortBindings", {}) or {}
            for container_port, bindings in ports.items():
                if bindings:
                    for b in bindings:
                        host_ip = b.get("HostIp", "")
                        host_port = b.get("HostPort", "")
                        if host_ip:
                            cmd.extend(["-p", f"{host_ip}:{host_port}:{container_port}"])
                        else:
                            cmd.extend(["-p", f"{host_port}:{container_port}"])

            # Labels (preserve all)
            for key, value in config.get("Config", {}).get("Labels", {}).items():
                cmd.extend(["--label", f"{key}={value}"])

            # Hostname
            hostname = config.get("Config", {}).get("Hostname", "")
            if hostname and hostname != config.get("Id", "")[:12]:
                cmd.extend(["--hostname", hostname])

            # Security options
            for opt in config.get("HostConfig", {}).get("SecurityOpt", []) or []:
                cmd.extend(["--security-opt", opt])

            # Image
            cmd.append(image)

            # Original command (if not entrypoint-only)
            original_cmd = config.get("Config", {}).get("Cmd")
            if original_cmd:
                cmd.extend(original_cmd)

            self._debug(f"  Run cmd: docker run -d --name {name} ... {image}")

            # Create and start new container
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                self._debug(f"  Run failed: {result.stderr[:300]}")
                # Rollback: restore old container
                subprocess.run(["docker", "rename", old_name, name], capture_output=True, timeout=10)
                subprocess.run(["docker", "start", name], capture_output=True, timeout=60)
                msg = f"Recreate failed: {result.stderr[:200]}"
                self._save_history(name, image, False, msg)
                return False, msg

            # Health check: wait up to 30s for container to be running
            self._debug(f"  Health check: waiting for {name}...")
            healthy, state, health = self._wait_healthy(name)

            if not healthy:
                self._debug(f"  Health check FAILED for {name} — rolling back")
                # Stop failed container, restore old one
                subprocess.run(["docker", "stop", name], capture_output=True, timeout=30)
                subprocess.run(["docker", "rm", name], capture_output=True, timeout=10)
                subprocess.run(["docker", "rename", old_name, name], capture_output=True, timeout=10)
                subprocess.run(["docker", "start", name], capture_output=True, timeout=60)
                msg = f"Health check failed (state={state}, health={health}) — rolled back"
                self._save_history(name, image, False, msg)
                return False, msg

            # Remove old container
            subprocess.run(["docker", "rm", old_name], capture_output=True, timeout=30)
            self._debug(f"  Recreated successfully: {name} (health: {health or 'ok'})")

            detail = f"📅 {old_created} → {new_created}, 📦 {new_size}"
            self._save_history(name, image, True, detail)
            return True, f"OK ({detail})"

        except Exception as e:
            self._debug(f"  Error: {str(e)[:200]}")
            # Try to restore on any failure
            subprocess.run(["docker", "rename", f"{name}_old", name], capture_output=True, timeout=10)
            subprocess.run(["docker", "start", name], capture_output=True, timeout=60)
            self._save_history(name, image, False, str(e)[:200])
            return False, f"Error: {str(e)[:200]}"
