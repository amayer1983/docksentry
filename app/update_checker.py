#!/usr/bin/env python3
"""Docker image update checker and container updater."""

import json
import os
import shutil
import subprocess
import time
import urllib.request
import urllib.parse
import re
from datetime import datetime, timedelta


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
            # Resolve images referenced by ID via container inspect
            if re.match(r'^[0-9a-f]{12,}$', image):
                resolved = subprocess.run(
                    ["docker", "inspect", "--format", "{{.Config.Image}}", name],
                    capture_output=True, text=True
                )
                if resolved.returncode == 0 and resolved.stdout.strip() and \
                   not re.match(r'^[0-9a-f]{12,}$', resolved.stdout.strip()):
                    image = resolved.stdout.strip()
                    self._debug(f"  Resolved image ID: {name} → {image}")
                else:
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

    # Standard Accept headers for manifest negotiation (multi-arch + single-arch,
    # both Docker and OCI). Sent on every manifest HEAD request.
    _MANIFEST_ACCEPT = ", ".join([
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ])

    def _get_docker_credentials(self, registry):
        """Read Basic-Auth credentials for a registry from docker config.json,
        if present. Returns the base64-encoded `auth` string or None."""
        docker_config = os.environ.get("DOCKER_CONFIG", "/.docker")
        config_file = os.path.join(docker_config, "config.json")
        if not os.path.isfile(config_file):
            return None
        try:
            with open(config_file) as f:
                cfg = json.load(f)
            auths = cfg.get("auths", {}) or {}
            # Try exact host match first, then substring matches (handles
            # entries like "https://index.docker.io/v1/" for "registry-1.docker.io").
            for key, val in auths.items():
                if key == registry and val.get("auth"):
                    return val["auth"]
            for key, val in auths.items():
                if (registry in key or key in registry) and val.get("auth"):
                    return val["auth"]
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_www_authenticate(header):
        """Parse a `WWW-Authenticate: Bearer ...` header into a dict.

        Example input:
            Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:linuxserver/plex:pull"
        Output:
            {"realm": "https://ghcr.io/token", "service": "ghcr.io",
             "scope": "repository:linuxserver/plex:pull"}
        Returns {} for non-Bearer challenges or malformed headers.
        """
        if not header or not header.strip().lower().startswith("bearer "):
            return {}
        params = {}
        # Match key="value" pairs (the Docker spec always quotes values).
        for key, value in re.findall(r'(\w+)="([^"]*)"', header[len("Bearer "):]):
            params[key.lower()] = value
        return params

    def _negotiate_token(self, www_auth, registry, repository):
        """Fetch a Bearer token from the realm advertised in WWW-Authenticate.

        Falls back to building a default scope (`repository:<repo>:pull`) if
        the server doesn't include one. Uses Basic-Auth credentials from
        docker config.json if available (for private registries).
        """
        params = self._parse_www_authenticate(www_auth)
        realm = params.get("realm")
        if not realm:
            return None

        query = []
        if "service" in params:
            query.append(("service", params["service"]))
        # Use the scope from the challenge if present; otherwise build a
        # sensible default. Scope can repeat, so handle the list case too.
        scope = params.get("scope") or f"repository:{repository}:pull"
        query.append(("scope", scope))
        token_url = realm + "?" + urllib.parse.urlencode(query)

        req = urllib.request.Request(token_url)
        auth_header = self._get_docker_credentials(registry)
        if auth_header:
            req.add_header("Authorization", f"Basic {auth_header}")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                # Some registries return "token", others "access_token".
                return data.get("token") or data.get("access_token")
        except Exception as e:
            self._debug(f"  Token negotiation failed: {e}")
            return None

    def _get_remote_digest(self, registry, repository, tag):
        """Fetch the remote manifest digest for a tag.

        Implements the Docker Registry V2 Bearer token flow:
            1. anonymous HEAD on /v2/<repo>/manifests/<tag>
            2. on 401, parse WWW-Authenticate, fetch Bearer token
            3. retry HEAD with `Authorization: Bearer <token>`

        This works generically for Docker Hub, GHCR, lscr.io, quay.io,
        gcr.io, registry.gitlab.com and any spec-compliant registry — no
        per-host hardcoding required.
        """
        if "docker.io" in registry:
            host = "registry-1.docker.io"
        else:
            host = registry
        url = f"https://{host}/v2/{repository}/manifests/{tag}"

        def _attempt(token=None):
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("Accept", self._MANIFEST_ACCEPT)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            return urllib.request.urlopen(req, timeout=15)

        try:
            with _attempt() as resp:
                return resp.headers.get("Docker-Content-Digest", "")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                self._debug(f"  Registry error: HTTP {e.code} {e.reason}")
                return None
            # 401 — negotiate a Bearer token from the WWW-Authenticate header
            www_auth = e.headers.get("WWW-Authenticate", "")
            token = self._negotiate_token(www_auth, registry, repository)
            if not token:
                self._debug(f"  Registry error: 401 (token negotiation failed)")
                return None
            try:
                with _attempt(token) as resp:
                    return resp.headers.get("Docker-Content-Digest", "")
            except urllib.error.HTTPError as e2:
                self._debug(f"  Registry error after auth: HTTP {e2.code} {e2.reason}")
                return None
            except Exception as e2:
                self._debug(f"  Registry error after auth: {e2}")
                return None
        except Exception as e:
            self._debug(f"  Registry error: {e}")
            return None

    # Pattern that captures SemVer in a tag: "1.2.3", "v1.2.3", "1.2.3-rc1",
    # "redis-7.0.5", "alpine-3.19.0" all match. Suffixes after the version
    # (like "-rc1") are kept in `pre`, used for ordering / filtering.
    _SEMVER_RE = re.compile(
        r"^(?:.*?-)?v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?P<pre>[-+][\w.\-+]*)?$"
    )

    @classmethod
    def _parse_semver(cls, tag):
        """Parse tag into (major, minor, patch, pre) tuple or None.
        `pre` is the pre-release/build suffix as a string (or "")."""
        if not tag:
            return None
        m = cls._SEMVER_RE.match(tag.strip())
        if not m:
            return None
        return (int(m.group("major")), int(m.group("minor")), int(m.group("patch")),
                m.group("pre") or "")

    def _list_remote_tags(self, registry, repository):
        """GET /v2/<repo>/tags/list with Bearer token negotiation. Returns
        a list of tag strings, or [] on failure. No pagination support yet —
        most registries return at least the first ~100 tags inline, which is
        enough for SemVer Major-detection in practice."""
        host = "registry-1.docker.io" if "docker.io" in registry else registry
        url = f"https://{host}/v2/{repository}/tags/list"

        def _attempt(token=None):
            req = urllib.request.Request(url)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            return urllib.request.urlopen(req, timeout=15)

        try:
            with _attempt() as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code != 401:
                self._debug(f"  Tag list error: HTTP {e.code} {e.reason}")
                return []
            www_auth = e.headers.get("WWW-Authenticate", "")
            token = self._negotiate_token(www_auth, registry, repository)
            if not token:
                return []
            try:
                with _attempt(token) as resp:
                    data = json.loads(resp.read())
            except Exception as e2:
                self._debug(f"  Tag list error after auth: {e2}")
                return []
        except Exception as e:
            self._debug(f"  Tag list error: {e}")
            return []
        return data.get("tags") or []

    def get_highest_semver_tag(self, registry, repository, current_tag):
        """Return (best_tag, best_semver_tuple) — the highest SemVer-tagged
        version available on the registry that uses the same naming scheme
        as `current_tag`. Returns (None, None) if no comparable version is
        found.

        "Same scheme" = same prefix (everything before the first digit) and
        no pre-release suffix. So `redis:7.0.5` matches `7.0.6` but not
        `7.0.6-alpine` or `next-7.0.6`.
        """
        cur = self._parse_semver(current_tag)
        if cur is None:
            return None, None
        # Determine the prefix of current_tag (e.g. "v" or "redis-")
        m = self._SEMVER_RE.match(current_tag.strip())
        prefix = current_tag.strip()[:m.start("major")] if m else ""

        tags = self._list_remote_tags(registry, repository)
        candidates = []
        for t in tags:
            ts = t.strip()
            # Must share the same prefix
            if prefix and not ts.startswith(prefix):
                continue
            parsed = self._parse_semver(ts)
            if parsed is None:
                continue
            # Skip pre-release / build-metadata variants
            if parsed[3]:
                continue
            candidates.append((parsed, ts))
        if not candidates:
            return None, None
        candidates.sort(reverse=True)
        best_parsed, best_tag = candidates[0]
        return best_tag, best_parsed

    def _get_local_digests(self, image):
        """Get all local image digests from RepoDigests."""
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .RepoDigests}}", image],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return []
        try:
            repo_digests = json.loads(result.stdout.strip())
            return [d.split("@")[1] for d in repo_digests if "@" in d]
        except (json.JSONDecodeError, IndexError):
            return []

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

    def cleanup_images(self):
        """Run image cleanup with optional pre-prune local-image backup.

        Behaviour controlled by config:
          cleanup_grace_hours      — `until=Xh` filter (default 24)
          cleanup_backup_local_only — if True, save locally-built images
                                       (no RepoDigests) as tarballs before
                                       removing them. Tarballs older than
                                       `cleanup_backup_days` are themselves
                                       deleted on every cleanup run.
          cleanup_backup_days      — retention for backup tarballs

        Returns (success: bool, message: str). Message is multi-line and
        includes the reclaim total, list of removed image tags (truncated),
        and a note about backup activity if relevant.
        """
        try:
            grace = int(self.config.cleanup_grace_hours or 24)
        except (ValueError, TypeError):
            grace = 24

        backup_msg = ""
        if self.config.cleanup_backup_local_only:
            try:
                backed_up = self._backup_local_unused_images()
                if backed_up:
                    backup_msg = f" Backed up {len(backed_up)} local image(s) → {self.config.cleanup_backup_dir}."
                self._prune_old_backups()
            except Exception as e:
                self._debug(f"  Backup step failed: {e}")
                # Continue with prune anyway — backups are nice-to-have

        try:
            result = subprocess.run(
                ["docker", "image", "prune", "-a", "--force", "--filter", f"until={grace}h"],
                capture_output=True, text=True, timeout=180
            )
            if result.returncode != 0:
                return False, f"Cleanup failed: {result.stderr.strip()[:200]}"
            lines = result.stdout.strip().split("\n")
            space_line = next((l for l in lines if "reclaimed" in l.lower()), "")
            deleted_lines = [l for l in lines if l.startswith(("Deleted: sha256:", "Untagged: "))]
            untagged = sorted({l[len("Untagged: "):].split("@")[0]
                               for l in lines if l.startswith("Untagged: ")})

            if not space_line:
                return True, "Nothing to clean up." + backup_msg
            msg = space_line + backup_msg
            if untagged:
                preview = ", ".join(untagged[:6])
                if len(untagged) > 6:
                    preview += f", +{len(untagged) - 6} more"
                msg += f"\nRemoved: {preview}"
            return True, msg
        except Exception as e:
            return False, f"Cleanup error: {str(e)[:200]}"

    def _backup_local_unused_images(self):
        """Save unused, locally-built images (no RepoDigests) as tarballs.

        Returns list of (image_id, tarball_path) for what was backed up.
        Images that would be pulled-back-from-registry instead of needing
        local restore are skipped — they're considered "safe to delete".
        """
        os.makedirs(self.config.cleanup_backup_dir, exist_ok=True)

        # IDs of all images currently in use (running or stopped containers)
        ps_result = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ImageID}}"],
            capture_output=True, text=True, timeout=30
        )
        used_ids = {l.strip() for l in ps_result.stdout.strip().split("\n") if l.strip()}

        # All images, full inspect form (need RepoDigests + RepoTags)
        ls_result = subprocess.run(
            ["docker", "image", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=30
        )
        all_ids = [l.strip() for l in ls_result.stdout.strip().split("\n") if l.strip()]
        # De-dup (image ls can list same ID multiple times for multiple tags)
        all_ids = list(dict.fromkeys(all_ids))

        backed_up = []
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        run_dir = os.path.join(self.config.cleanup_backup_dir, timestamp)

        for img_id in all_ids:
            if img_id in used_ids:
                continue
            inspect = subprocess.run(
                ["docker", "image", "inspect", img_id],
                capture_output=True, text=True, timeout=30
            )
            if inspect.returncode != 0:
                continue
            try:
                meta = json.loads(inspect.stdout)[0]
            except (json.JSONDecodeError, IndexError):
                continue
            repo_digests = meta.get("RepoDigests") or []
            repo_tags = [t for t in (meta.get("RepoTags") or []) if t and t != "<none>:<none>"]

            # Skip images that have a RepoDigest — they live in a registry,
            # `docker pull` can recreate them, no need to backup.
            if repo_digests:
                continue
            # Skip dangling-only images — they're build leftovers, not worth
            # preserving.
            if not repo_tags:
                continue

            # This is a locally-built, unused, tagged image — back it up.
            os.makedirs(run_dir, exist_ok=True)
            safe_name = repo_tags[0].replace("/", "_").replace(":", "_")
            tarball = os.path.join(run_dir, f"{safe_name}.tar")
            save = subprocess.run(
                ["docker", "image", "save", "-o", tarball, img_id],
                capture_output=True, text=True, timeout=600
            )
            if save.returncode == 0:
                backed_up.append((img_id, tarball))
                self._debug(f"  Backed up: {repo_tags[0]} → {tarball}")
            else:
                self._debug(f"  Backup failed for {img_id}: {save.stderr[:200]}")
        return backed_up

    def _prune_old_backups(self):
        """Delete backup directories older than cleanup_backup_days."""
        backup_dir = self.config.cleanup_backup_dir
        if not os.path.isdir(backup_dir):
            return
        try:
            days = int(self.config.cleanup_backup_days or 7)
        except (ValueError, TypeError):
            days = 7
        cutoff = time.time() - days * 86400
        for entry in os.listdir(backup_dir):
            path = os.path.join(backup_dir, entry)
            try:
                if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                    # Recursive delete: rmtree
                    import shutil
                    shutil.rmtree(path, ignore_errors=True)
                    self._debug(f"  Pruned old backup: {path}")
            except OSError:
                pass

    def get_disk_usage(self):
        """Return (used_percent, free_bytes, total_bytes) for the data dir's
        underlying filesystem. Used as a proxy for the Docker storage volume
        — in most setups they share the same disk."""
        try:
            usage = shutil.disk_usage(self.config.data_dir)
            percent = round(usage.used * 100 / usage.total, 1) if usage.total else 0
            return percent, usage.free, usage.total
        except OSError:
            return 0, 0, 0

    def check_disk_usage(self):
        """Check disk usage and decide if a warning should be emitted now.

        Rate-limited to one warning per day per threshold. State is stored
        in disk_warn_state.json. Returns (action, percent, free_gb) where
        action is one of:
          "ok"     — below threshold, no notification
          "warn"   — above warn threshold, notify
          "silent" — above threshold but already warned today
        """
        try:
            threshold = int(self.config.disk_warn_percent or 85)
        except (ValueError, TypeError):
            threshold = 85

        percent, free, total = self.get_disk_usage()
        if percent < threshold:
            return "ok", percent, free / 1024**3

        # Throttle: only one warn per day for the same threshold-bucket
        state = {}
        try:
            if os.path.exists(self.config.disk_warn_state_file):
                with open(self.config.disk_warn_state_file) as f:
                    state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

        last_warn_iso = state.get("last_warn", "")
        try:
            last_warn = datetime.fromisoformat(last_warn_iso) if last_warn_iso else None
        except ValueError:
            last_warn = None

        if last_warn and (datetime.now() - last_warn) < timedelta(hours=23):
            return "silent", percent, free / 1024**3

        # Update state
        try:
            with open(self.config.disk_warn_state_file, "w") as f:
                json.dump({"last_warn": datetime.now().isoformat(timespec="seconds"),
                           "percent": percent}, f)
            os.chmod(self.config.disk_warn_state_file, 0o600)
        except OSError:
            pass
        return "warn", percent, free / 1024**3

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

    def _get_start_period_seconds(self, name):
        """Read the image's own Healthcheck.StartPeriod from `docker
        inspect` (nanoseconds), return as float seconds. Returns 0 if
        no healthcheck is defined or the field is unset — Docker's
        default is also 0s. Used to bound our max_starting wait so we
        respect what the image author declared."""
        try:
            r = subprocess.run(
                ["docker", "inspect",
                 "--format", "{{if .Config.Healthcheck}}{{.Config.Healthcheck.StartPeriod}}{{end}}",
                 name],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return 0.0
            raw = r.stdout.strip()
            if not raw or raw == "0":
                return 0.0
            return float(int(raw)) / 1e9
        except (subprocess.SubprocessError, ValueError):
            return 0.0

    def _tail_logs(self, name, lines=10):
        """Return the last N log lines as a single string, trimmed for
        Telegram. Best-effort — failures return empty string. Used to
        attach diagnostic context to health-check warnings so the user
        can see in chat what the container was last doing instead of
        having to SSH to the host."""
        try:
            r = subprocess.run(
                ["docker", "logs", "--tail", str(lines), name],
                capture_output=True, text=True, timeout=10,
            )
            # docker logs interleaves stdout+stderr — combine both
            text = (r.stdout or "") + (r.stderr or "")
            text = text.strip()
            if not text:
                return ""
            # Hard cap: ~1500 chars so the Telegram message stays under
            # the 4096-char limit even with other fields stuffed in.
            if len(text) > 1500:
                text = "…" + text[-1500:]
            return text
        except subprocess.SubprocessError:
            return ""

    def _wait_healthy(self, name, max_starting=None, interval=10):
        """Wait for container to become healthy.

        `max_starting` defaults to `config.healthcheck_max_starting`
        (600s = 10 min). We also read the image's own
        `Healthcheck.StartPeriod` and use the larger of:
            (configured default, start_period × 1.5)
        so an image declaring `start_period: 5m` doesn't get cut off
        at our default if our default is shorter than what the image
        author thought reasonable.

        Three return outcomes (instead of the old two-value boolean):
            "healthy"  → container reported healthy (or has no
                         healthcheck and is running)
            "unhealthy"→ healthcheck reported unhealthy, OR container
                         is not running. Caller should roll back.
            "starting" → still in `starting` after our wait. The
                         container is alive but slow. Caller should
                         NOT roll back — leave it in place and warn
                         the user so Docker's own start_period can
                         eventually decide.

        Returns (outcome, state, health).
        """
        if max_starting is None:
            max_starting = getattr(self.config, "healthcheck_max_starting", 600)
        # Respect the image's own start_period (×1.5 so we give Docker's
        # health system a chance to flip the bit before we step in)
        image_start = self._get_start_period_seconds(name)
        effective = max(int(max_starting), int(image_start * 1.5))
        if effective != max_starting:
            self._debug(f"  Effective health timeout: {effective}s (default {max_starting}s, image start_period {image_start:.0f}s)")

        elapsed = 0
        check = 0
        state = ""
        health = ""
        while elapsed < effective:
            time.sleep(interval)
            elapsed += interval
            check += 1
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
            self._debug(f"  Health check [{check}, {elapsed}s/{effective}s]: state={state}, health={health}")
            if state != "running":
                return "unhealthy", state, health
            if not health or health == "<no value>":
                return "healthy", state, health
            if health == "healthy":
                return "healthy", state, health
            if health == "unhealthy":
                return "unhealthy", state, health
            # health == "starting" → keep waiting
        # Timed out with status still "starting" — container is alive
        # but slow. Don't roll back; let the caller report a warning.
        return "starting", state, health

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

            local_digests = self._get_local_digests(image)
            if not local_digests:
                self._debug(f"  Skipped (no local digest): {c['name']}")
                continue

            remote_digest = self._get_remote_digest(registry, repository, tag)

            self._debug(f"  Local:  {', '.join(d[:30] + '...' for d in local_digests)}")
            self._debug(f"  Remote: {(remote_digest or 'FAILED')[:30]}...")

            if remote_digest is None:
                # Treat unknown as unknown — don't claim "up to date" when we
                # couldn't actually reach the registry.
                self._debug(f"  → Check FAILED (registry unreachable / unauthorized)")
                continue

            if remote_digest not in local_digests:
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
        outcome, state, health = self._wait_healthy(name)

        if outcome == "unhealthy":
            # Container actively unhealthy or no longer running — for
            # the compose path, "rollback" via `compose up` is mostly a
            # no-op (the same compose file produces the same container),
            # so we honestly report "failed in place" instead of
            # claiming a rollback that didn't happen.
            self._debug(f"  Health check FAILED (compose) — container left in place")
            tail = self._tail_logs(name, lines=10)
            msg = f"Health check failed (state={state}, health={health}) — container left in place (compose)"
            if tail:
                msg += f"\nLast logs:\n```\n{tail}\n```"
            self._save_history(name, image, False, msg)
            return False, msg

        if outcome == "starting":
            # Container is alive but still in 'starting' after our
            # timeout. Don't roll back — let Docker's own start_period
            # decide. Report as a warning so the user knows to keep an
            # eye on it, but treat as a soft success so the group-abort
            # logic doesn't skip the rest of the group.
            tail = self._tail_logs(name, lines=10)
            detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}"
            msg = (f"⚠ Updated but still 'starting' after our wait — left in place, "
                   f"Docker will keep checking. ({detail})")
            if tail:
                msg += f"\nLast logs:\n```\n{tail}\n```"
            self._save_history(name, image, True, f"compose: {detail} (slow start)")
            return True, msg

        detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}"
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

            # When a container inherits another container's network namespace
            # (Gluetun / VPN-sidecar pattern: `network_mode: "container:gluetun"`
            # or `service:gluetun`), Docker REJECTS per-container network
            # options because they all belong to the namespace owner. The
            # rejected list includes --hostname, -p/--publish, --add-host,
            # --mac-address, --dns. Trying to set them yields:
            #   "conflicting options: hostname and the network mode"
            # Reported by @famewolf in #2.
            shares_netns = network_mode.startswith(("container:", "service:"))

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

            # Port mappings (skipped when sharing another container's netns —
            # those ports belong to the namespace owner, not us)
            if not shares_netns:
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

            # Hostname (skipped when sharing another container's netns)
            if not shares_netns:
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

            # Health check: wait for the new container to come up. Up
            # to `config.healthcheck_max_starting` (default 600s), with
            # the image's own start_period × 1.5 as a floor.
            self._debug(f"  Health check: waiting for {name}...")
            outcome, state, health = self._wait_healthy(name)

            if outcome == "unhealthy":
                # Active failure — container died, or healthcheck went
                # unhealthy after Docker's start_period elapsed. Roll
                # back to the old container so the user isn't left with
                # a broken service.
                self._debug(f"  Health check FAILED for {name} — rolling back")
                tail = self._tail_logs(name, lines=10)
                subprocess.run(["docker", "stop", name], capture_output=True, timeout=30)
                subprocess.run(["docker", "rm", name], capture_output=True, timeout=10)
                subprocess.run(["docker", "rename", old_name, name], capture_output=True, timeout=10)
                subprocess.run(["docker", "start", name], capture_output=True, timeout=60)
                msg = f"Health check failed (state={state}, health={health}) — rolled back"
                if tail:
                    msg += f"\nLast logs:\n```\n{tail}\n```"
                self._save_history(name, image, False, msg)
                return False, msg

            if outcome == "starting":
                # Container is alive but still in 'starting' after our
                # wait. Don't roll back — slow-startup apps like GitLab
                # / Nextcloud / Mastodon legitimately need 10-15 minutes
                # for first-boot migrations etc. We leave the new
                # container running, drop the old one (it would never
                # come back anyway since the rolled-forward state is
                # what the user wants), and report as a warning so the
                # user knows to keep an eye on it.
                tail = self._tail_logs(name, lines=10)
                subprocess.run(["docker", "rm", old_name], capture_output=True, timeout=30)
                detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}"
                msg = (f"⚠ Updated but still 'starting' after our wait — left running. "
                       f"Docker's own healthcheck will keep checking. ({detail})")
                if tail:
                    msg += f"\nLast logs:\n```\n{tail}\n```"
                self._save_history(name, image, True, f"{detail} (slow start)")
                return True, msg

            # Remove old container
            subprocess.run(["docker", "rm", old_name], capture_output=True, timeout=30)
            self._debug(f"  Recreated successfully: {name} (health: {health or 'ok'})")

            detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}"
            self._save_history(name, image, True, detail)
            return True, f"OK ({detail})"

        except Exception as e:
            self._debug(f"  Error: {str(e)[:200]}")
            # Try to restore on any failure
            subprocess.run(["docker", "rename", f"{name}_old", name], capture_output=True, timeout=10)
            subprocess.run(["docker", "start", name], capture_output=True, timeout=60)
            self._save_history(name, image, False, str(e)[:200])
            return False, f"Error: {str(e)[:200]}"
