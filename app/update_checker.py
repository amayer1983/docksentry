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
            ["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Labels}}"],
            capture_output=True, text=True
        )
        # Get own container name to exclude self. Robust detection: tries
        # HOSTNAME env first, falls back to /proc/self/cgroup if that's
        # missing or doesn't resolve. The old HOSTNAME-only path silently
        # missed self-detection in some compose / orchestrator setups,
        # leading to the bot updating itself via the regular flow and
        # killing PID 1 (#16).
        own_name = self._own_container_name()

        containers = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) < 2:
                continue
            name, image = parts[0], parts[1]
            labels = self._parse_ps_labels(parts[2] if len(parts) > 2 else "")
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
            # GitOps twin of /pin (#42, @LeeNX): freeze a container from its
            # own compose file. Same effect as the stored pin — never listed,
            # never updated.
            if self.label_bool(labels, "pin") is True:
                self._debug(f"  Skipped (pinned via label): {name}")
                continue
            # Per-container label opt-out (#42, @LeeNX): a GitOps-friendly way
            # to take a container out of Docksentry's scope from the compose
            # file itself — `docksentry.enable=false` or `docksentry.exclude=true`.
            if self.label_bool(labels, "enable") is False or self.label_bool(labels, "exclude") is True:
                self._debug(f"  Skipped (docksentry label): {name}")
                continue
            # Detect Docker Compose
            compose_info = self._get_compose_info(name)
            containers.append({"name": name, "image": image, **compose_info})
        return containers

    @staticmethod
    def _parse_ps_labels(label_str):
        """Parse `docker ps` `{{.Labels}}` output (comma-separated
        key=value) into a dict. Best-effort — malformed entries skipped.
        Label values containing commas can't be represented in this flat
        format, but the `docksentry.*` flags we care about are booleans."""
        out = {}
        for item in (label_str or "").split(","):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    @staticmethod
    def label_bool(labels, key):
        """Interpret a `docksentry.<key>` container label as a bool, or
        None when the label is absent (#42, @LeeNX). Accepts
        true/1/yes/on (case-insensitive) as True, everything else False."""
        v = (labels or {}).get(f"docksentry.{key}")
        if v is None:
            return None
        return v.strip().lower() in ("true", "1", "yes", "on")

    def get_container_labels(self, name):
        """Return all labels of a single container as a dict (empty on any
        failure). Used by the per-container label overrides (#42)."""
        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{json .Config.Labels}}", name],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout) or {}
        except (subprocess.SubprocessError, json.JSONDecodeError):
            pass
        return {}

    @staticmethod
    def image_version_label(image):
        """The `org.opencontainers.image.version` label of a local image, or
        "" if absent/unreadable. Docksentry stamps this on its own images
        (#39), so the self-update message can show `v1.33.1 → v1.33.2`
        instead of opaque dates + image hashes (#41 follow-up)."""
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", "--format",
                 '{{index .Config.Labels "org.opencontainers.image.version"}}', image],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                v = r.stdout.strip()
                if v and v not in ("<no value>", "dev"):
                    return v
        except subprocess.SubprocessError:
            pass
        return ""

    def _parse_image(self, image):
        """Parse image reference into registry, repository, tag.

        Returns ``(None, None, None)`` for references that can't be
        update-checked: bare image IDs and digest-pinned references
        (``repo@sha256:...``). A digest pin is the user explicitly
        freezing the image — "is there something newer?" is meaningless
        for it, and naive parsing would split at the digest's colon and
        produce a garbage repository/tag that fails the registry call
        every cycle (looking like a permanently unreachable registry).
        """
        # Digest-pinned reference — deliberately not updatable.
        if "@" in image:
            return None, None, None

        # Bare image ID. Must be checked BEFORE the tag split: the split
        # would eat the digest as a ":tag", leaving `image == "sha256"`,
        # so this guard used to be dead code and IDs were queried on
        # Docker Hub as the nonsense repository "library/sha256".
        if image.startswith("sha256:"):
            return None, None, None

        tag = "latest"
        if ":" in image and not image.endswith(":"):
            parts = image.rsplit(":", 1)
            if "/" not in parts[1]:
                image, tag = parts

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

    _host_platform_cache = None

    def _host_platform(self):
        """The DAEMON's (os, architecture) — the platform images are pulled
        for — via ``docker version``. Asking the daemon (not Python's
        ``platform`` module) matters because Docksentry itself runs in a
        container: the daemon may sit on a different host than us (socket
        proxy setups). Cached per process; falls back to linux/amd64 when
        the daemon can't say.
        """
        if UpdateChecker._host_platform_cache is None:
            os_name, arch = "linux", "amd64"
            try:
                r = subprocess.run(
                    ["docker", "version", "--format",
                     "{{.Server.Os}}/{{.Server.Arch}}"],
                    capture_output=True, text=True, timeout=10,
                )
                parts = r.stdout.strip().split("/")
                if r.returncode == 0 and len(parts) == 2 and all(parts):
                    os_name, arch = parts
            except (subprocess.SubprocessError, OSError):
                pass
            UpdateChecker._host_platform_cache = (os_name, arch)
        return UpdateChecker._host_platform_cache

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
                self._debug("  Registry error: 401 (token negotiation failed)")
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

    def _registry_get(self, host, registry, repository, path, accept=None):
        """GET a registry resource (manifest or blob) with Bearer-token
        negotiation. Returns raw bytes, or None on failure. Mirrors the
        auth flow of _get_remote_digest but for GET bodies."""
        url = f"https://{host}/v2/{repository}/{path}"

        def _attempt(token=None):
            req = urllib.request.Request(url)
            if accept:
                req.add_header("Accept", accept)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            return urllib.request.urlopen(req, timeout=15)

        try:
            with _attempt() as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code != 401:
                return None
            token = self._negotiate_token(
                e.headers.get("WWW-Authenticate", ""), registry, repository)
            if not token:
                return None
            try:
                with _attempt(token) as resp:
                    return resp.read()
            except Exception:
                return None
        except Exception:
            return None

    def get_remote_image_meta(self, registry, repository, tag):
        """Best-effort {version, created} of the REMOTE image for a tag, read
        from its OCI config blob (the `org.opencontainers.image.version`
        label and the build date). Lets the pre-update "Updates Available"
        notification show `v_old → v_new` before anything is pulled (#44,
        @LeeNX). Returns {} on any failure — the notification just omits the
        version line then. Only called for containers that already have a
        pending update, so the extra registry calls are few."""
        host = "registry-1.docker.io" if "docker.io" in registry else registry
        raw = self._registry_get(host, registry, repository,
                                 f"manifests/{tag}", self._MANIFEST_ACCEPT)
        if not raw:
            return {}
        try:
            man = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        # Multi-arch index → pick the manifest matching the HOST's platform
        # (an ARM host reading the amd64 config would get amd64's metadata;
        # version labels are usually arch-identical, but "usually" isn't a
        # contract). Fall back: first linux entry, else first entry.
        if man.get("manifests"):
            entries = man["manifests"]
            host_os, host_arch = self._host_platform()
            chosen = next((m for m in entries
                           if (m.get("platform") or {}).get("os") == host_os
                           and (m.get("platform") or {}).get("architecture") == host_arch), None)
            chosen = chosen or next((m for m in entries
                                     if (m.get("platform") or {}).get("os") == "linux"), None)
            chosen = chosen or (entries[0] if entries else None)
            if not chosen or not chosen.get("digest"):
                return {}
            raw = self._registry_get(host, registry, repository,
                                     f"manifests/{chosen['digest']}", self._MANIFEST_ACCEPT)
            if not raw:
                return {}
            try:
                man = json.loads(raw)
            except (ValueError, TypeError):
                return {}
        cfg_digest = (man.get("config") or {}).get("digest")
        if not cfg_digest:
            return {}
        blob = self._registry_get(host, registry, repository, f"blobs/{cfg_digest}")
        if not blob:
            return {}
        try:
            cfg = json.loads(blob)
        except (ValueError, TypeError):
            return {}
        labels = (cfg.get("config") or {}).get("Labels") or {}
        version = self._normalize_version_label(
            labels.get("org.opencontainers.image.version", "") or "")
        created = (cfg.get("created") or "")[:10]
        return {"version": version, "created": created}

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

    def has_selfupdate_available(self):
        """Digest-only check whether the running Docksentry image has a newer
        version on the registry. Used by /check to surface a selfupdate hint
        (#2, @famewolf) — get_running_containers filters us out so the regular
        update flow never sees us, and this fills that gap without triggering
        a pull.

        Returns True when local != remote, False otherwise (also on any
        failure — we prefer a missed hint over a false positive)."""
        try:
            cfg = self.inspect_self()
            if not cfg:
                return False
            image = cfg.get("Config", {}).get("Image", "")
            if not image:
                return False
            local_digests = self._get_local_digests(image)
            if not local_digests:
                return False
            registry, repository, tag = self._parse_image(image)
            if not registry:
                return False
            remote_digest = self._get_remote_digest(registry, repository, tag)
            if not remote_digest:
                return False
            return remote_digest not in local_digests
        except Exception:
            return False

    @staticmethod
    def _parse_human_size(s):
        """`docker system df` size string → bytes. "20.1GB" -> 20100000000,
        "20.1GB (50%)" -> same (Docker appends the %-of-total), "0B" -> 0,
        "" / unparseable -> 0. Handles B / KB / MB / GB / TB (SI base 1000,
        same as Docker prints)."""
        if not s:
            return 0
        s = s.strip()
        # Strip Docker's " (NN%)" suffix from `docker system df` JSON output.
        p = s.find("(")
        if p >= 0:
            s = s[:p].strip()
        s = s.replace(" ", "")
        UNITS = {"TB": 1_000_000_000_000, "GB": 1_000_000_000,
                 "MB": 1_000_000, "KB": 1_000, "B": 1}
        for suf, mult in UNITS.items():
            if s.endswith(suf):
                try:
                    return int(float(s[:-len(suf)]) * mult)
                except ValueError:
                    return 0
        try:
            return int(float(s))
        except ValueError:
            return 0

    def reclaimable_bytes(self):
        """Total reclaimable disk space via `docker system df` (bytes). Used
        by the disk warning to tell the user how much space `docker image
        prune` / `/cleanup` could free — famewolf's #2 point: without that
        number the warning looks like noise and gets ignored. Best-effort,
        returns 0 on any failure."""
        try:
            r = subprocess.run(
                ["docker", "system", "df", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return 0
            total = 0
            for line in r.stdout.strip().splitlines():
                try:
                    d = json.loads(line)
                except (ValueError, TypeError):
                    continue
                total += self._parse_human_size(d.get("Reclaimable", ""))
            return total
        except (subprocess.SubprocessError, OSError):
            return 0

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

        # Update state — atomic (v1.22.1, see container_store.atomic_write_json)
        try:
            from container_store import atomic_write_json
            atomic_write_json(
                self.config.disk_warn_state_file,
                {"last_warn": datetime.now().isoformat(timespec="seconds"),
                 "percent": percent},
            )
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
        # Keep last 100 entries — atomic write (v1.22.1)
        history = history[-100:]
        from container_store import atomic_write_json
        atomic_write_json(self.config.history_file, history, indent=2)

    @staticmethod
    def _own_id_candidates():
        """$HOSTNAME + /etc/hostname — the conventional self-reference
        values Docker writes into the container."""
        cands = []
        h = os.environ.get("HOSTNAME", "").strip()
        if h:
            cands.append(h)
        try:
            with open("/etc/hostname") as f:
                hf = f.read().strip()
                if hf and hf not in cands:
                    cands.append(hf)
        except (OSError, IOError):
            pass
        return cands

    @staticmethod
    def resolve_own_id():
        """Best-effort full 64-char container ID of the running Docksentry,
        or "". Source-of-truth for self-detection AND the self-update paths.

        1. Inspect by $HOSTNAME / /etc/hostname — Docker's default sets
           HOSTNAME to the short container ID, so this normally resolves.
        2. Fallback: scan running containers for one whose
           `Config.Hostname` equals $HOSTNAME. Needed where $HOSTNAME is
           NOT an inspect-resolvable reference — e.g. QNAP Container
           Station hands out a hostname that `docker inspect` reports as
           "no such object" (#41, @NotRetarded). The container's own
           Config.Hostname still carries that value, so we can match on it.

        Cgroup parsing is deliberately avoided: unreliable on cgroups v2
        (often just `0::/`), and the mountinfo overlay path is the
        storage-driver ID, not the container ID."""
        for c in UpdateChecker._own_id_candidates():
            try:
                r = subprocess.run(
                    ["docker", "inspect", "--format", "{{.Id}}", c],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    fid = r.stdout.strip()
                    if fid.startswith("sha256:"):
                        fid = fid[len("sha256:"):]
                    if len(fid) == 64:
                        return fid
            except subprocess.SubprocessError:
                continue
        h = os.environ.get("HOSTNAME", "").strip()
        if h:
            try:
                ps = subprocess.run(
                    ["docker", "ps", "-q", "--no-trunc"],
                    capture_output=True, text=True, timeout=10,
                )
                ids = [x for x in ps.stdout.split() if x]
                if ids:
                    r = subprocess.run(
                        ["docker", "inspect", "--format",
                         "{{.Id}}|{{.Config.Hostname}}", *ids],
                        capture_output=True, text=True, timeout=20,
                    )
                    for line in r.stdout.splitlines():
                        fid, _, hn = line.partition("|")
                        if hn.strip() == h and len(fid.strip()) == 64:
                            return fid.strip()
            except subprocess.SubprocessError:
                pass
        return ""

    @staticmethod
    def inspect_self():
        """Full docker-inspect dict of the running Docksentry container, or
        None. Routes through resolve_own_id() so it works even where
        $HOSTNAME isn't directly inspect-resolvable (#41). Used by both
        self-update paths so they no longer rely on a raw
        `docker inspect $HOSTNAME` that can fail."""
        oid = UpdateChecker.resolve_own_id()
        if not oid:
            return None
        try:
            r = subprocess.run(
                ["docker", "inspect", oid],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                if data:
                    return data[0]
        except (subprocess.SubprocessError, json.JSONDecodeError, IndexError):
            pass
        return None

    def _own_container_id(self):
        """Cached instance wrapper around resolve_own_id() — the per-check
        self-detection source-of-truth (any container resolving to this ID
        is "us", regardless of its current docker Name)."""
        if hasattr(self, "_own_id_cache"):
            return self._own_id_cache
        own_id = self.resolve_own_id()
        self._own_id_cache = own_id
        return own_id

    def _own_container_name(self):
        """Return the running Docksentry container's docker name (without
        the leading slash), or empty if we can't figure it out.

        Tries HOSTNAME env first (cheapest), falls back to the
        cgroup-based container ID. Cached after first call."""
        if hasattr(self, "_own_name_cache"):
            return self._own_name_cache
        name = ""
        candidates = []
        h = os.environ.get("HOSTNAME", "").strip()
        if h:
            candidates.append(h)
        cid = self._own_container_id()
        if cid:
            candidates.append(cid)
        for c in candidates:
            try:
                r = subprocess.run(
                    ["docker", "inspect", "--format", "{{.Name}}", c],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    n = r.stdout.strip().lstrip("/")
                    if n:
                        name = n
                        break
            except subprocess.SubprocessError:
                continue
        self._own_name_cache = name
        return name

    def _would_kill_self(self, target_name):
        """Final-line-of-defense check before issuing `docker stop` on a
        container: would the target be us?

        Compares full container IDs rather than names so a user-set
        DOCKSENTRY_CONTAINER_NAME mismatch doesn't bypass the guard.
        Returns False (= safe, proceed) when we can't determine our own
        ID — don't false-positive into refusing every update."""
        own_id = self._own_container_id()
        if not own_id:
            return False
        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.Id}}", target_name],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            # docker inspect on a CONTAINER returns "HEX" with no prefix;
            # on an IMAGE it returns "sha256:HEX". Handle both safely —
            # str.lstrip("sha256:") would chew through any leading char
            # in {s,h,a,2,5,6,:} which silently corrupts a hex ID that
            # happens to start with 2/5/6/a.
            target_id = r.stdout.strip()
            if target_id.startswith("sha256:"):
                target_id = target_id[len("sha256:"):]
            return target_id == own_id
        except subprocess.SubprocessError:
            return False

    def _container_exists(self, name):
        """True if a container with this name still exists (any state).

        Used after a stop attempt to detect the AutoRemove (`--rm`) case:
        such containers are removed by Docker the moment they stop, so a
        slow-to-stop `--rm` container vanishes entirely during our recreate
        flow. Reported by @famewolf in #2 — homarr had `--rm`, was wedged,
        our stop finally reaped it, Docker auto-removed it, and we walked
        away leaving him with nothing. This check lets us recover.
        """
        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.Id}}", name],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except subprocess.SubprocessError:
            # On inspect error we can't be sure — assume it exists so we
            # don't trigger a spurious recreate of a container that's fine.
            return True

    def _rollback_to_old(self, name, old_name):
        """Restore the pre-update container after a failed recreate.

        Single source of truth for all rollback paths (run-failed,
        unhealthy, and the catch-all exception handler). The previous
        inline rollbacks each did `docker rm <name>` (no -f) followed by
        `docker rename <old> <name>` — which silently failed and left the
        user stranded if the new container wouldn't stop (rm without -f
        can't remove a running container, so the rename then collided)
        OR if the rename to `<old>` never happened in the first place
        (then `rename <old> <name>` had nothing to restore).

        Safe ordering, "don't make it worse" first:
          1. If `old_name` doesn't exist we have NO backup — leave
             `name` completely alone. It may be the only container the
             user has, broken or not; destroying it would be strictly
             worse than the failed update.
          2. Otherwise force-remove whatever is at `name` (the broken
             new container, or nothing) — `-f` handles a still-running
             or wedged container — then rename the backup into place and
             start it.

        Returns True when the old container was restored and started.
        """
        if not self._container_exists(old_name):
            self._debug(f"  Rollback: no {old_name} backup to restore — "
                        f"leaving {name} untouched")
            return False
        # -f so a wedged/running broken new container can't block the
        # rename the way the old non-forced `docker rm` did.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        subprocess.run(["docker", "rename", old_name, name], capture_output=True, timeout=10)
        start = subprocess.run(["docker", "start", name], capture_output=True, timeout=60)
        ok = start.returncode == 0
        self._debug(f"  Rollback: restored {old_name} → {name} "
                    f"({'started' if ok else 'start failed'})")
        return ok

    def _stop_container(self, name, inspect_config=None):
        """Stop a container, respecting its own `Config.StopTimeout`.

        Reads StopTimeout from the inspect data (already-fetched config
        dict if available, otherwise via a fresh inspect call), passes
        `--time` to `docker stop` so Docker's grace aligns with what we
        expect, and bounds our subprocess wait at `stop_timeout + 30s`
        (or `DOCKER_STOP_TIMEOUT` from config, whichever is larger).

        On `TimeoutExpired` we fall back to `docker kill -s SIGKILL` so
        the container ends up actually stopped — leaving it hanging
        because we gave up too early was the previous failure mode
        (homarr-style slow shutdowns, reported in #11).

        Returns (ok: bool, detail: str).
        """
        # Per-container stop timeout in seconds. Default to Docker's own
        # default (10s) when the field is missing.
        stop_timeout = 10
        if inspect_config:
            t = inspect_config.get("Config", {}).get("StopTimeout")
            if isinstance(t, (int, float)) and t > 0:
                stop_timeout = int(t)
        else:
            try:
                r = subprocess.run(
                    ["docker", "inspect", "--format", "{{.Config.StopTimeout}}", name],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    raw = r.stdout.strip()
                    if raw and raw != "<no value>":
                        try:
                            stop_timeout = max(1, int(raw))
                        except ValueError:
                            pass
            except subprocess.SubprocessError:
                pass

        # Configurable global floor (default 60s in config.py). Ensures
        # a sensible minimum even when StopTimeout is unset/tiny.
        floor = int(getattr(self.config, "docker_stop_timeout", 60) or 60)
        effective_stop = max(stop_timeout, floor)
        # Subprocess outer timeout = give Docker its grace + headroom
        # for the SIGKILL phase + log flush.
        subprocess_timeout = effective_stop + 30

        self._debug(f"  Stop {name}: effective_stop={effective_stop}s, subprocess={subprocess_timeout}s")
        try:
            r = subprocess.run(
                ["docker", "stop", "--time", str(effective_stop), name],
                capture_output=True, text=True, timeout=subprocess_timeout,
            )
            if r.returncode == 0:
                return True, "stopped"
            err = (r.stderr or "").strip()[:200]
            # Fall through to kill if the stop reported failure but the
            # container is still running.
            self._debug(f"  Stop failed (rc={r.returncode}): {err}")
        except subprocess.TimeoutExpired:
            self._debug(f"  Stop timed out after {subprocess_timeout}s — escalating to kill")

        # Fallback: force-kill so we don't leave the recreate flow
        # half-finished.
        try:
            kill = subprocess.run(
                ["docker", "kill", name],
                capture_output=True, text=True, timeout=15,
            )
            if kill.returncode == 0:
                return True, "killed after stop timeout"
            return False, f"stop+kill both failed: {(kill.stderr or '').strip()[:120]}"
        except subprocess.SubprocessError as e:
            return False, f"kill failed: {e}"

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

    def _restart_count(self, name):
        """Current Docker RestartCount for a container (0 on any error).

        Used by _wait_healthy to detect a post-update crash loop: a
        container whose main process keeps exiting and getting revived
        by its restart policy. A healthcheck stuck in "starting" would
        otherwise hide this and we'd report a broken update as success.
        """
        rc = subprocess.run(
            ["docker", "inspect", "--format", "{{.RestartCount}}", name],
            capture_output=True, text=True
        )
        try:
            return int(rc.stdout.strip())
        except (ValueError, AttributeError):
            return 0

    def _image_id(self, image):
        """Resolved image ID (sha256:...) for an image reference, or ''."""
        r = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True
        )
        return r.stdout.strip() if r.returncode == 0 else ""

    def _container_image_id(self, name):
        """Image ID the named container is actually running, or ''."""
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", name],
            capture_output=True, text=True
        )
        return r.stdout.strip() if r.returncode == 0 else ""

    def _is_trust_running(self, name):
        """Whether the user opted this container into "accept running over
        unhealthy" (#9). A `docksentry.trust-running` label wins over the
        stored toggle (#42, @LeeNX — compose as source of truth); otherwise
        read straight from the data file — the checker only holds `config`,
        not a ContainerStore — and fail closed (default to the strict
        healthcheck behaviour) on any read error."""
        try:
            lab = self.label_bool(self.get_container_labels(name), "trust-running")
            if lab is not None:
                return lab
        except Exception:
            pass
        try:
            path = getattr(self.config, "trust_running_file", None)
            if path and os.path.exists(path):
                with open(path) as f:
                    return name in json.load(f)
        except (ValueError, OSError):
            pass
        return False

    def netns_target_name(self, name):
        """If `name` shares another container's network namespace
        (`NetworkMode=container:<id>`, the Gluetun sidecar pattern), return
        the owner's current NAME. Recreating the sidecar against
        `container:<name>` survives the owner being recreated (new ID),
        which a stored `container:<id>` does not (#2). Returns None when the
        container doesn't share a netns or the owner can't be resolved."""
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", name],
            capture_output=True, text=True)
        if r.returncode != 0:
            return None
        nm = r.stdout.strip()
        if not nm.startswith("container:"):
            return None
        ref = nm.split(":", 1)[1]
        rr = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}}", ref],
            capture_output=True, text=True)
        if rr.returncode != 0:
            return None
        return rr.stdout.strip().lstrip("/") or None

    def recreate_dependent(self, name, netns_name):
        """Recreate a netns-sharing group dependent in place, rejoining the
        head's CURRENT container via `container:<netns_name>`. After the head
        is recreated (new ID) a plain `docker restart` of the sidecar fails —
        it still references the dead old ID (#8). This rebuilds from inspect
        with the SAME image (no pull, no version change), backing up the old
        container first and rolling back on failure. Returns (ok, detail)."""
        # Capture config up front so even an AutoRemove (--rm) container —
        # which vanishes on stop — can still be rebuilt.
        insp = subprocess.run(["docker", "inspect", name],
                              capture_output=True, text=True)
        if insp.returncode != 0:
            return False, "inspect failed"
        try:
            config = json.loads(insp.stdout)[0]
        except (ValueError, IndexError):
            return False, "inspect parse failed"
        image = (config.get("Config") or {}).get("Image") or ""
        if not image:
            return False, "no image in config"

        old_name = f"{name}_old"
        # Clear any stale backup from a previous interrupted run.
        subprocess.run(["docker", "rm", "-f", old_name], capture_output=True, timeout=15)
        self._stop_container(name)
        # Back up by renaming — only if it survived the stop (AutoRemove may
        # have deleted it, in which case we recreate straight from `config`).
        if self._container_exists(name):
            subprocess.run(["docker", "rename", name, old_name],
                           capture_output=True, timeout=10)

        cmd = self._build_run_args(config, image, name,
                                   self._get_image_defaults(image),
                                   netns_name=netns_name,
                                   inherited=self._image_config(config.get("Image")))
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if run.returncode != 0:
            err = run.stderr.strip()[:200]
            self._debug(f"  Dependent recreate failed for {name}: {err}")
            self._rollback_to_old(name, old_name)
            return False, err
        subprocess.run(["docker", "rm", "-f", old_name], capture_output=True, timeout=15)
        return True, "recreated"

    def _wait_healthy(self, name, max_starting=None, interval=10):
        """Wait for container to become healthy.

        `max_starting` defaults to `config.healthcheck_max_starting`
        (600s = 10 min). We also read the image's own
        `Healthcheck.StartPeriod` and use the larger of:
            (configured default, start_period × 1.5)
        so an image declaring `start_period: 5m` doesn't get cut off
        at our default if our default is shorter than what the image
        author thought reasonable.

        Four return outcomes:
            "healthy"  → container reported healthy (or has no
                         healthcheck and is running) AND stayed that way,
                         with no restarts, for `crashloop_stable_seconds`
            "unhealthy"→ healthcheck reported unhealthy, OR container
                         is not running. Caller should roll back.
            "crashloop"→ the container's RestartCount climbed while we
                         waited: its main process keeps exiting and the
                         restart policy keeps reviving it. A failed
                         update masquerading as "starting". Caller should
                         roll back / report failure — never success.
            "starting" → still in `starting` after our wait, with NO
                         restarts. The container is alive but slow.
                         Caller should NOT roll back — leave it in place
                         and warn so Docker's own start_period can decide.

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

        # Baseline restart count. If it climbs while we wait, the new
        # container is crash-looping (exits → restart policy revives it
        # → exits again). A healthcheck stuck in "starting" the whole
        # time would otherwise mask this and we'd wrongly report success
        # — exactly the GitLab DB-migration failure on 2026-06-18.
        baseline_restarts = self._restart_count(name)
        # Per-container opt-in (#9): accept a running-but-unhealthy container
        # instead of rolling back. For brittle healthchecks (VPN-sidecar
        # dependents whose probe hits the wrong namespace) that flap
        # `unhealthy` while actually serving fine. This only relaxes the
        # `health == "unhealthy"` rule — a climbing RestartCount (crash loop)
        # and `state != "running"` are still treated as real failures.
        trust_running = self._is_trust_running(name)
        if trust_running:
            self._debug(f"  trust_running set for {name}: a running-but-unhealthy result will be accepted")
        # Once the container first looks healthy we don't return immediately:
        # we keep watching for `stable_needed` more seconds to be sure it
        # STAYS up. Without this, a container that boots fine and then
        # crashes a few seconds later (slower than a single poll) would slip
        # through as a successful update. 0 disables the confirmation.
        stable_needed = getattr(self.config, "crashloop_stable_seconds", 30)
        healthy_since = None
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
            # Crash-loop check first: a climbing RestartCount means the
            # container keeps dying and being revived, regardless of what
            # the current state/health snapshot happens to show.
            restarts = self._restart_count(name)
            if restarts > baseline_restarts:
                self._debug(f"  Crash loop detected for {name}: RestartCount {baseline_restarts} → {restarts}")
                return "crashloop", state, health
            if state != "running":
                return "unhealthy", state, health
            if health == "unhealthy" and not trust_running:
                return "unhealthy", state, health
            looks_healthy = (not health or health == "<no value>"
                             or health == "healthy"
                             or (trust_running and state == "running"))
            if looks_healthy:
                if healthy_since is None:
                    healthy_since = elapsed
                    self._debug(f"  Looks healthy at {elapsed}s — confirming stable for {stable_needed}s")
                if elapsed - healthy_since >= stable_needed:
                    return "healthy", state, health
                # else: keep observing to rule out a delayed crash loop
            else:
                # health == "starting" → regressed; reset the stable timer
                healthy_since = None
            # keep waiting
        # Timed out. If it ever looked healthy and never restarted, it's slow
        # but stable — accept it. Otherwise it's still genuinely 'starting'.
        if healthy_since is not None:
            return "healthy", state, health
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
                reason = "pinned by digest" if "@" in image else "unparseable"
                self._debug(f"  Skipped ({reason}): {c['name']} ({image})")
                continue

            self._debug(f"  Checking: {c['name']} ({registry}/{repository}:{tag})")

            local_digests = self._get_local_digests(image)
            if not local_digests:
                self._debug(f"  Skipped (no local digest): {c['name']}")
                continue

            remote_digest = self._get_remote_digest(registry, repository, tag)

            self._debug(f"  Local:  {', '.join(d[:30] + '...' for d in local_digests)}")
            self._debug(f"  Remote: {(remote_digest or 'FAILED')[:30]}...")

            if not remote_digest:
                # Treat unknown as unknown — don't claim "up to date" when we
                # couldn't actually reach the registry. An empty string (a 200
                # manifest with no Docker-Content-Digest header) is a failure
                # too, not a real digest that would spuriously mismatch.
                self._debug("  → Check FAILED (registry unreachable / unauthorized)")
                continue

            if remote_digest not in local_digests:
                size = self._get_image_size(image)
                created = self._get_image_created(image)
                self._debug(f"  → UPDATE AVAILABLE (current: {created}, size: {size})")
                c["size"] = size
                c["created"] = created
                # Version info for the "Updates Available" notification (#44):
                # old from the local OCI label (falling back to a SemVer tag),
                # new from the remote image's OCI config. Best-effort, and
                # only for containers that actually have an update.
                old_v = self._get_image_version_label(image)
                if not old_v and self._parse_semver(tag):
                    old_v = tag
                c["old_version"] = old_v
                meta = self.get_remote_image_meta(registry, repository, tag)
                if meta.get("version"):
                    c["new_version"] = meta["version"]
                if meta.get("created"):
                    c["new_created"] = meta["created"]
                updates.append(c)
            else:
                self._debug("  → Up to date")

        # Save pending updates — atomic write (v1.22.1)
        from container_store import atomic_write_json
        atomic_write_json(self.config.pending_file, updates)
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
        # FINAL BACKSTOP against self-kill (#16). `get_running_containers`
        # already filters self out of `check_all`, but third-party callers
        # — Web UI single-update button, callback handlers, anything new
        # we add later — could route around that filter. Comparing full
        # container IDs (via /proc/self/cgroup) catches "we're about to
        # docker-stop ourselves" even when the name-based detection up
        # the stack missed it. Self-updates must go through the dedicated
        # `_do_selfupdate()` helper-container path instead.
        if self._would_kill_self(name):
            self._debug(f"  REFUSED: {name} is this Docksentry container — use /selfupdate")
            msg = "Refused: this is the running Docksentry container — use /selfupdate (or set AUTO_SELFUPDATE=true)"
            self._save_history(name, image, False, msg)
            return False, msg

        # Netns owner resolved to a stable NAME by the batch orchestrator
        # (before the owner was recreated) — threaded into the standalone
        # recreate so Gluetun-style sidecars rejoin the new owner (#2).
        netns_name = kwargs.get("netns_name")

        # Try Compose update if container belongs to a stack
        if compose_project and compose_service and compose_file:
            return self._update_compose(name, image, compose_project, compose_service,
                                        compose_file, compose_dir, netns_name=netns_name)

        return self._update_standalone(name, image, netns_name=netns_name)

    def _update_compose(self, name, image, project, service, config_file, working_dir, netns_name=None):
        """Update a container using Docker Compose."""
        self._debug(f"Updating (compose): {name} (project={project}, service={service})...")

        # Get old image info
        old_created = self._get_image_created(image)
        # OCI image.version label, before pull (#22). Best-effort —
        # ~40 % coverage across real-world stacks.
        old_version = self._get_image_version_label(image)

        # Check if compose file is accessible
        if not os.path.isfile(config_file):
            self._debug(f"  Compose file not found: {config_file} — falling back to standalone")
            return self._update_standalone(name, image, netns_name=netns_name)

        # Base compose invocation. When the stack was originally started from
        # a different directory than the compose file's (label
        # com.docker.compose.project.working_dir ≠ dirname(config_file)),
        # compose resolves `.env` interpolation and env_file paths against
        # the PROJECT directory — without --project-directory our recreate
        # could interpolate ${VARS} differently than the original `up` did
        # (found via lint: `working_dir` was accepted but never used; same
        # recreate-fidelity class as #27/#29).
        compose_base = ["docker", "compose", "-f", config_file, "-p", project]
        if working_dir and os.path.isdir(working_dir) \
                and os.path.realpath(working_dir) != os.path.realpath(os.path.dirname(config_file) or "."):
            compose_base += ["--project-directory", working_dir]

        # Pull new image via compose
        pull_cmd = compose_base + ["pull", service]
        self._debug(f"  Running: {' '.join(pull_cmd)}")
        result = subprocess.run(pull_cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            msg = f"Compose pull failed: {result.stderr[:200]}"
            self._save_history(name, image, False, msg)
            return False, msg

        # Get new image info after pull
        new_created = self._get_image_created(image)
        new_size = self._get_image_size(image)
        new_version = self._get_image_version_label(image)
        self._version_arrow = self._format_version_arrow(old_version, new_version)

        # Recreate service via compose. `--force-recreate` so the container
        # is actually replaced: a plain `up -d` can leave the old container
        # (and old image) running if Compose judges the service "unchanged",
        # so the new image gets pulled but never loaded (#35).
        up_cmd = compose_base + ["up", "-d", "--no-deps", "--force-recreate", service]
        self._debug(f"  Running: {' '.join(up_cmd)}")
        result = subprocess.run(up_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            msg = f"Compose up failed: {result.stderr[:200]}"
            self._save_history(name, image, False, msg)
            return False, msg

        # Verify the running container actually picked up the new image.
        # Pull + recreate can both "succeed" yet leave the container on the
        # previous image (#35) — report that honestly instead of a phantom OK.
        ok, mismatch = self._verify_running_image(name, image)
        if not ok:
            msg = (f"{mismatch}. "
                   f"Try `docker compose -p {project} up -d --force-recreate {service}` manually.")
            self._save_history(name, image, False, msg)
            return False, msg

        # Health check
        self._debug(f"  Health check: waiting for {name}...")
        outcome, state, health = self._wait_healthy(name)

        if outcome in ("unhealthy", "crashloop"):
            # Container actively unhealthy, no longer running, or
            # crash-looping — for the compose path, "rollback" via
            # `compose up` is mostly a no-op (the same compose file
            # produces the same container), so we honestly report
            # "failed in place" instead of claiming a rollback that
            # didn't happen.
            self._debug(f"  Health check FAILED ({outcome}, compose) — container left in place")
            tail = self._tail_logs(name, lines=10)
            if outcome == "crashloop":
                msg = f"Update produced a crash-restart loop (state={state}, health={health}) — left in place (compose)"
            else:
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
            detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}{getattr(self, '_version_arrow', '')}"
            msg = (f"⚠ Updated but still 'starting' after our wait — left in place, "
                   f"Docker will keep checking. ({detail})")
            if tail:
                msg += f"\nLast logs:\n```\n{tail}\n```"
            self._save_history(name, image, True, f"compose: {detail} (slow start)")
            return True, msg

        detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}{getattr(self, '_version_arrow', '')}"
        self._save_history(name, image, True, f"compose: {detail}")
        return True, f"OK ({detail})"

    @staticmethod
    def _build_run_args(config, image, name, image_defaults=None, netns_name=None,
                        inherited=None):
        """Reconstruct the full `docker run` argument list from a
        container's inspect dump. Single source of truth for both the
        standalone update path here and the self-update helper in
        telegram_bot.py.

        v1.19.0 adds network aliases / fixed IPs / MAC / links for the
        primary network (fixes compose-stack restart-loops where
        recreated containers lost their service alias and other stack
        members hit NXDOMAIN). Also: memory, CPU, pids, oom, blkio,
        ulimits, group_add, auto-remove, stop_signal, stop_timeout,
        working_dir, domainname, tty, stdin, healthcheck override,
        and Entrypoint+Cmd image-diff comparison.

        ``image_defaults`` is an optional dict ``{Entrypoint, Cmd}``
        holding the image's own default Entrypoint and Cmd (as read
        from ``docker image inspect``). When provided, Container-level
        Cmd / Entrypoint are restored only when they DIFFER from the
        image's defaults — otherwise we'd lock in the OLD image's
        Cmd/Entrypoint on every update and break image releases that
        change either. When ``image_defaults`` is None, we fall back
        to pre-v1.19.0 behaviour for backward compatibility: restore
        Container.Config.Cmd blindly, never emit --entrypoint. Pass
        ``image_defaults`` from callers that have already inspected
        the image (the typical update path) — _update_standalone()
        does this automatically.

        ``inherited`` is the OLD image's own ``Config`` block (see
        _image_config). Every value the container merely inherited from
        it — Env, Labels, User, WorkingDir, StopSignal, Healthcheck — is
        skipped so the NEW image's defaults win; only genuine user
        overrides are replicated. None means "replicate everything"
        (pre-v1.43.0 behaviour), which is also the fallback whenever the
        old image can no longer be inspected.

        Historical field-coverage gaps closed:
          - v1.18.10 (#27 / @famewolf): CapAdd, Devices, Sysctls,
            Tmpfs, ExtraHosts, Dns*, Privileged, Init, ShmSize,
            ReadonlyRootfs, LogConfig, Runtime, Ipc/Pid/UTS, User.
          - v1.19.0 (internal): network aliases/IPs/MAC/links,
            memory/CPU/pids/oom/blkio/ulimits limits, group_add,
            auto-remove, stop_signal/timeout, working_dir, domainname,
            tty/stdin, healthcheck override, image-diff Cmd/Entrypoint.

        Defaults that match Docker's own defaults (e.g.
        ``LogConfig.Type = "json-file"``, ``Runtime = "runc"``,
        ``IpcMode = "private"``, ``ShmSize = 64 MiB``) are skipped —
        emitting them works but adds noise to the `docker run`
        command in logs.
        """
        host = config.get("HostConfig", {}) or {}
        cfg = config.get("Config", {}) or {}

        args = ["docker", "run", "-d", "--name", name]

        # ── Restart policy ─────────────────────────────────────
        restart = host.get("RestartPolicy") or {}
        if restart.get("Name"):
            policy = restart["Name"]
            if restart.get("MaximumRetryCount", 0) > 0:
                policy += f":{restart['MaximumRetryCount']}"
            args.extend(["--restart", policy])

        # ── Network mode ───────────────────────────────────────
        network_mode = host.get("NetworkMode") or ""
        # Gluetun-style netns sidecars: the stored NetworkMode is
        # `container:<id>` — an ID that DIES when the netns owner (e.g.
        # gluetun) is itself recreated, so the sidecar can't rejoin
        # ("No such container", #2). When the batch orchestrator resolved
        # the owner to a stable NAME before recreating it, use that instead
        # so the sidecar joins the new owner via `container:<name>`.
        if netns_name:
            network_mode = f"container:{netns_name}"
        if network_mode and network_mode != "default":
            args.extend(["--network", network_mode])
        # When inheriting another container's network namespace, Docker
        # forbids the per-container network knobs (--hostname, -p,
        # --add-host, --mac-address, --dns, ...). See #11 for the long
        # explanation. We skip those flags downstream when shares_netns
        # is True.
        shares_netns = network_mode.startswith(("container:", "service:"))

        # ── Primary network aliases, IP, MAC, links ────────────────
        # Compose containers get a service alias (`db`, `app`, `redis`)
        # in their project network. Without --network-alias on recreate
        # the alias is dropped → other services in the same stack hit
        # NXDOMAIN. Paperless-NGX / Nextcloud restart-loops after auto-
        # update were caused by exactly this. Reported (internal).
        # Skip for shared-netns (forbidden by Docker) and "default"/host/
        # bridge/none modes (no per-container aliases anyway).
        if (not shares_netns and network_mode
                and network_mode not in ("default", "host", "bridge", "none")):
            networks = (config.get("NetworkSettings") or {}).get("Networks") or {}
            primary = networks.get(network_mode) or {}
            short_id = (config.get("Id") or "")[:12]
            for alias in (primary.get("Aliases") or []):
                # Docker re-adds the auto-generated short-id alias and
                # the container name automatically — emitting them
                # explicitly is harmless but noisy.
                if not alias or alias == short_id or alias == name:
                    continue
                args.extend(["--network-alias", alias])
            ipam = primary.get("IPAMConfig") or {}
            if ipam.get("IPv4Address"):
                args.extend(["--ip", ipam["IPv4Address"]])
            if ipam.get("IPv6Address"):
                args.extend(["--ip6", ipam["IPv6Address"]])
            # MAC: Config.MacAddress is the user-set value; the
            # per-network entry holds whatever Docker assigned (may be
            # auto-generated). Prefer Config.MacAddress.
            user_mac = cfg.get("MacAddress") or ""
            if user_mac:
                args.extend(["--mac-address", user_mac])
            # Legacy links (deprecated but still used by some stacks)
            for link in (primary.get("Links") or []):
                args.extend(["--link", link])

        # ── Privileged mode ────────────────────────────────────
        # If privileged, all the capability/device fields are implied
        # — emitting them alongside `--privileged` works but is noise.
        privileged = bool(host.get("Privileged"))
        if privileged:
            args.append("--privileged")

        # ── Capabilities (Gluetun cares about this) ────────────
        if not privileged:
            for cap in (host.get("CapAdd") or []):
                # docker inspect stores "CAP_NET_ADMIN", CLI accepts
                # either form — pass the stored value as-is.
                args.extend(["--cap-add", cap])
            for cap in (host.get("CapDrop") or []):
                args.extend(["--cap-drop", cap])

        # ── Devices (Gluetun: /dev/net/tun) ────────────────────
        if not privileged:
            for dev in (host.get("Devices") or []):
                host_path = dev.get("PathOnHost", "")
                cont_path = dev.get("PathInContainer", "")
                perms = dev.get("CgroupPermissions", "rwm")
                if not host_path:
                    continue
                spec = host_path
                if cont_path and cont_path != host_path:
                    spec += f":{cont_path}"
                if perms and perms != "rwm":
                    spec += f":{perms}"
                args.extend(["--device", spec])

        # ── Sysctls ────────────────────────────────────────────
        for key, value in (host.get("Sysctls") or {}).items():
            args.extend(["--sysctl", f"{key}={value}"])

        # ── Tmpfs mounts ───────────────────────────────────────
        for path, opts in (host.get("Tmpfs") or {}).items():
            spec = path
            if opts:
                spec += f":{opts}"
            args.extend(["--tmpfs", spec])

        # ── Environment variables ──────────────────────────────
        # A container's Env is the image's own ENV *merged with* the
        # user's -e overrides, and Docker keeps no record of which is
        # which. Replicating all of it pins the NEW image's defaults to
        # the OLD image's values: an image that carries its version as
        # `ENV APP_VERSION=...` (unifi-os-server, #35) then keeps
        # reporting the old version forever — the new image really is
        # running, but we handed it the old value on the command line.
        # So drop entries the old image already defined verbatim; those
        # were inherited, and the new image should supply them again.
        # A var explicitly set to exactly the image default is
        # indistinguishable from an inherited one and gets dropped too —
        # harmless unless the new image changed that default, which is
        # precisely the case we want the new value to win.
        # inherited is None when the old image is no longer inspectable —
        # then replicate everything (pre-v1.42.0 behaviour).
        inherited_env = set((inherited or {}).get("Env") or ())
        for env in (cfg.get("Env") or []):
            if env in inherited_env:
                continue
            args.extend(["-e", env])

        # ── Volumes / Mounts ───────────────────────────────────
        for mount in (config.get("Mounts") or []):
            mtype = mount.get("Type", "")
            if mtype == "bind":
                src = mount.get("Source", "")
                dst = mount.get("Destination", "")
                if not (src and dst):
                    continue
                bind = f"{src}:{dst}"
                if not mount.get("RW", True):
                    bind += ":ro"
                args.extend(["-v", bind])
            elif mtype == "volume":
                vol = mount.get("Name") or ""
                dst = mount.get("Destination", "")
                if not (vol and dst):
                    continue
                bind = f"{vol}:{dst}"
                if not mount.get("RW", True):
                    bind += ":ro"
                args.extend(["-v", bind])

        # ── Port mappings ──────────────────────────────────────
        # Skipped when sharing another container's netns — those ports
        # belong to the namespace owner, not us.
        if not shares_netns:
            for container_port, bindings in (host.get("PortBindings") or {}).items():
                if not bindings:
                    continue
                for b in bindings:
                    host_ip = b.get("HostIp", "")
                    host_port = b.get("HostPort", "")
                    if host_ip:
                        args.extend(["-p", f"{host_ip}:{host_port}:{container_port}"])
                    else:
                        args.extend(["-p", f"{host_port}:{container_port}"])

        # ── Extra /etc/hosts entries ───────────────────────────
        if not shares_netns:
            for entry in (host.get("ExtraHosts") or []):
                args.extend(["--add-host", entry])

        # ── DNS overrides ──────────────────────────────────────
        if not shares_netns:
            for d in (host.get("Dns") or []):
                args.extend(["--dns", d])
            for d in (host.get("DnsSearch") or []):
                args.extend(["--dns-search", d])
            for d in (host.get("DnsOptions") or []):
                args.extend(["--dns-option", d])

        # ── Labels (user's own only) ───────────────────────────
        # Image LABELs merge into the container's labels indistinguishably
        # from user/compose ones. Replicating all of them pins the OLD
        # image's labels onto the new container — including
        # `org.opencontainers.image.version`, which is exactly what the
        # container detail view reports as "what version is this really?"
        # (#36). So an updated container would keep claiming the old
        # version. Skip labels the old image already carried verbatim;
        # compose/user labels differ from it and survive.
        inherited_labels = (inherited or {}).get("Labels") or {}
        for key, value in (cfg.get("Labels") or {}).items():
            if inherited_labels.get(key) == value:
                continue
            # org.opencontainers.* is image METADATA (version, revision,
            # created, ...) — never legitimate container-level config. It
            # must always come fresh from the new image. Crucially, the
            # value-comparison above can't catch a label that is already
            # stale: pre-v1.43.0 recreates pinned e.g.
            # `org.opencontainers.image.version=1.40.1` explicitly, and
            # that stale value differs from the old image's own label, so
            # it masqueraded as a user override and stuck to the container
            # through every future update (#46, @LeeNX — /status kept
            # reporting the old version forever).
            if key.startswith("org.opencontainers."):
                continue
            args.extend(["--label", f"{key}={value}"])

        # ── Hostname (skipped when sharing netns) ──────────────
        if not shares_netns:
            hostname = cfg.get("Hostname", "")
            # Docker auto-generates a 12-char short ID when no
            # hostname is set — skip emitting that since we'll get a
            # fresh one for the new container anyway.
            if hostname and hostname != config.get("Id", "")[:12]:
                args.extend(["--hostname", hostname])

        # ── User (uid[:gid]) ───────────────────────────────────
        # Dockerfile USER lands here too. Pinning it breaks images that
        # re-harden across versions (root → non-root, or a changed uid):
        # the new image expects its own user, we'd force the old one and
        # produce permission errors. Only a real user override survives.
        user = cfg.get("User", "")
        if user and user != (inherited or {}).get("User", ""):
            args.extend(["--user", user])

        # ── Security options (AppArmor / seccomp / no-new-privileges) ─
        for opt in (host.get("SecurityOpt") or []):
            args.extend(["--security-opt", opt])

        # ── Read-only rootfs ───────────────────────────────────
        if host.get("ReadonlyRootfs"):
            args.append("--read-only")

        # ── Init (pid 1 reaper) ────────────────────────────────
        if host.get("Init"):
            args.append("--init")

        # ── Shared memory size (skip Docker default 64MiB) ─────
        shm = host.get("ShmSize", 0) or 0
        if shm and shm != 67108864:
            args.extend(["--shm-size", str(shm)])

        # ── Namespace modes (skip Docker defaults) ─────────────
        for flag, key, default in (
            ("--ipc", "IpcMode",  ("", "private", "shareable")),
            ("--pid", "PidMode",  ("",)),
            ("--uts", "UTSMode",  ("",)),
        ):
            v = host.get(key, "") or ""
            if v and v not in default:
                args.extend([flag, v])

        # ── Runtime (skip Docker default `runc`) ───────────────
        runtime = host.get("Runtime", "") or ""
        if runtime and runtime != "runc":
            args.extend(["--runtime", runtime])

        # ── Logging driver (skip Docker default json-file/empty) ─
        log = host.get("LogConfig") or {}
        log_type = log.get("Type", "")
        if log_type and log_type not in ("json-file", ""):
            args.extend(["--log-driver", log_type])
        for k, v in (log.get("Config") or {}).items():
            args.extend(["--log-opt", f"{k}={v}"])

        # ── Memory limits ──────────────────────────────────────
        # Compose `mem_limit`, `memswap_limit`, `mem_reservation`.
        # Stored as bytes; emit as bytes (docker accepts "1073741824").
        for key, flag in (
            ("Memory", "--memory"),
            ("MemorySwap", "--memory-swap"),
            ("MemoryReservation", "--memory-reservation"),
            ("KernelMemory", "--kernel-memory"),
            ("KernelMemoryTCP", "--kernel-memory-tcp"),
        ):
            v = host.get(key, 0) or 0
            # MemorySwap == -1 means "unlimited swap" (intentional); 0
            # means "use the kernel default" — skip both.
            if v and v > 0:
                args.extend([flag, str(v)])
        msrl = host.get("MemorySwappiness")
        if msrl is not None and msrl >= 0:
            args.extend(["--memory-swappiness", str(msrl)])

        # ── CPU limits ─────────────────────────────────────────
        # Compose `cpus` is NanoCpus in inspect; --cpus is float.
        nano_cpus = host.get("NanoCpus", 0) or 0
        if nano_cpus > 0:
            args.extend(["--cpus", f"{nano_cpus / 1_000_000_000:g}"])
        for key, flag in (
            ("CpuShares", "--cpu-shares"),
            ("CpuPeriod", "--cpu-period"),
            ("CpuQuota", "--cpu-quota"),
            ("CpuRtPeriod", "--cpu-rt-period"),
            ("CpuRtRuntime", "--cpu-rt-runtime"),
        ):
            v = host.get(key, 0) or 0
            if v > 0:
                args.extend([flag, str(v)])
        for key, flag in (
            ("CpusetCpus", "--cpuset-cpus"),
            ("CpusetMems", "--cpuset-mems"),
        ):
            v = host.get(key, "") or ""
            if v:
                args.extend([flag, v])

        # ── Pids limit ─────────────────────────────────────────
        # Compose `pids_limit`. -1 = unlimited (default), 0 also means
        # default; only emit when positive.
        pids = host.get("PidsLimit", 0) or 0
        if pids and pids > 0:
            args.extend(["--pids-limit", str(pids)])

        # ── OOM controls ───────────────────────────────────────
        oom_adj = host.get("OomScoreAdj", 0) or 0
        if oom_adj:
            args.extend(["--oom-score-adj", str(oom_adj)])
        if host.get("OomKillDisable"):
            args.append("--oom-kill-disable")

        # ── BlkIO weight ───────────────────────────────────────
        blkio = host.get("BlkioWeight", 0) or 0
        if blkio:
            args.extend(["--blkio-weight", str(blkio)])

        # ── Ulimits ────────────────────────────────────────────
        # Compose `ulimits:`; stored as [{Name, Soft, Hard}].
        for ulimit in (host.get("Ulimits") or []):
            uname = ulimit.get("Name", "")
            # Podman inspect reports rlimit names in POSIX form
            # (RLIMIT_NOFILE); the --ulimit flag only accepts the short
            # form (nofile), so replicating the raw name failed every
            # podman standalone recreate with "invalid ulimit type"
            # (#48, @LeeNX). Docker already reports the short form.
            if uname.upper().startswith("RLIMIT_"):
                uname = uname[7:].lower()
            soft = ulimit.get("Soft", 0)
            hard = ulimit.get("Hard", 0)
            if not uname:
                continue
            if soft == hard:
                spec = f"{uname}={soft}"
            else:
                spec = f"{uname}={soft}:{hard}"
            args.extend(["--ulimit", spec])

        # ── Supplementary groups ───────────────────────────────
        # Compose `group_add:`.
        for grp in (host.get("GroupAdd") or []):
            args.extend(["--group-add", grp])

        # ── Auto-remove ────────────────────────────────────────
        # --rm is incompatible with --restart; Docker rejects both.
        # Only emit when no restart policy or policy is "no".
        if (host.get("AutoRemove")
                and (not restart.get("Name") or restart.get("Name") == "no")):
            args.append("--rm")

        # ── Stop signal (compose `stop_signal:`) ───────────────
        # Dockerfile STOPSIGNAL lands here as well — an image that
        # switches to e.g. SIGRTMIN+3 (systemd-based images do) must not
        # be held on its predecessor's signal, or shutdown breaks.
        stop_signal = cfg.get("StopSignal", "")
        if stop_signal and stop_signal != (inherited or {}).get("StopSignal", ""):
            args.extend(["--stop-signal", stop_signal])

        # ── Stop timeout (compose `stop_grace_period:`) ────────
        stop_timeout = cfg.get("StopTimeout")
        if stop_timeout is not None and stop_timeout > 0:
            args.extend(["--stop-timeout", str(stop_timeout)])

        # ── Working directory (compose `working_dir:`) ─────────
        # Dockerfile WORKDIR lands here too; a relocated app directory in
        # the new image would otherwise be overridden by the old path.
        workdir = cfg.get("WorkingDir", "")
        if workdir and workdir != (inherited or {}).get("WorkingDir", ""):
            args.extend(["--workdir", workdir])

        # ── Domainname ─────────────────────────────────────────
        if not shares_netns:
            domainname = cfg.get("Domainname", "")
            if domainname:
                args.extend(["--domainname", domainname])

        # ── TTY / stdin (compose `tty:` / `stdin_open:`) ───────
        if cfg.get("Tty"):
            args.append("-t")
        if cfg.get("OpenStdin"):
            args.append("-i")

        # ── Healthcheck override ───────────────────────────────
        # Compose `healthcheck:` overrides the image's HEALTHCHECK — but
        # so does the image's own HEALTHCHECK: contrary to what this
        # comment claimed until v1.43.0, an image-default healthcheck DOES
        # land in inspect.Config.Healthcheck, with no marker saying so.
        # Replicating it unconditionally pinned the OLD image's
        # healthcheck, and that one bites back: when a new image ships a
        # *fixed* healthcheck, the stale one keeps failing, our
        # post-update health gate treats that as a bad update and rolls
        # back — so the very release that repairs the check can never be
        # installed. Only replicate a genuine override.
        hc = cfg.get("Healthcheck") or {}
        if hc and hc == ((inherited or {}).get("Healthcheck") or {}):
            hc = {}
        hc_test = hc.get("Test") or []
        if hc_test:
            head = hc_test[0]
            if head == "NONE":
                args.append("--no-healthcheck")
            elif head == "CMD" and len(hc_test) > 1:
                # CMD form: docker stores ["CMD", "binary", "arg1", ...].
                # --health-cmd needs a single shell-quoted string.
                import shlex
                args.extend(["--health-cmd",
                             shlex.join(hc_test[1:])])
            elif head == "CMD-SHELL" and len(hc_test) > 1:
                args.extend(["--health-cmd", hc_test[1]])
            # Intervals are nanoseconds in inspect output.
            for key, flag in (
                ("Interval", "--health-interval"),
                ("Timeout", "--health-timeout"),
                ("StartPeriod", "--health-start-period"),
                ("StartInterval", "--health-start-interval"),
            ):
                v = hc.get(key, 0) or 0
                if v > 0:
                    args.extend([flag, f"{v // 1_000_000_000}s"])
            retries = hc.get("Retries", 0) or 0
            if retries > 0:
                args.extend(["--health-retries", str(retries)])

        # ── Entrypoint override ────────────────────────────────
        # Same logic as Cmd below: only restore when the container's
        # entrypoint differs from the image's default (otherwise we'd
        # lock in the OLD image's entrypoint and break image updates
        # that change ENTRYPOINT). When image_defaults is None we
        # preserve historical behaviour: assume user didn't override
        # entrypoint and skip emitting --entrypoint.
        container_entrypoint = cfg.get("Entrypoint") or []
        image_entrypoint = (image_defaults or {}).get("Entrypoint") or []
        if (container_entrypoint
                and image_defaults is not None
                and container_entrypoint != image_entrypoint):
            # --entrypoint takes a single binary; remaining tokens
            # become positional args after the image.
            args.extend(["--entrypoint", container_entrypoint[0]])

        # ── Image (must come before command tokens) ────────────
        args.append(image)

        # ── Entrypoint remaining tokens (if user overrode entrypoint
        #    AND it has multiple tokens, the tail goes here as args).
        if (container_entrypoint
                and image_defaults is not None
                and container_entrypoint != image_entrypoint
                and len(container_entrypoint) > 1):
            args.extend(container_entrypoint[1:])

        # ── Command (only restore when user-overridden) ────────
        # Container.Config.Cmd is set both for user-overridden Cmd AND
        # as an echo of the image's default CMD. Without image_defaults
        # we'd lock in the OLD image's CMD on every update. With
        # image_defaults present, only emit when the container's Cmd
        # actually differs from the image's default.
        container_cmd = cfg.get("Cmd")
        image_cmd = (image_defaults or {}).get("Cmd")
        if container_cmd:
            if image_defaults is None:
                # Pre-v1.19.0 behaviour: blindly restore Cmd.
                args.extend(container_cmd)
            elif container_cmd != image_cmd:
                args.extend(container_cmd)

        return args

    def _get_image_version_label(self, image):
        """Read & normalize `org.opencontainers.image.version` from an
        image's labels. Returns empty string when missing/unusable."""
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", "--format",
                 '{{index .Config.Labels "org.opencontainers.image.version"}}', image],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return self._normalize_version_label(r.stdout.strip())
        except subprocess.SubprocessError:
            pass
        return ""

    @staticmethod
    def _normalize_version_label(raw):
        """Clean up the `org.opencontainers.image.version` label so it
        renders consistently in /history. The label format isn't
        standardized — different upstreams set it to wildly different
        things:
          - n8n, mariadb, paperless-ngx: clean semver like "2.14.2"
          - adguardhome: doubled prefix "vv0.107.73"
          - open-webui: branch name "main"
          - gitlab: short image ID "c66669ef8bf1"

        We strip a single leading 'v' (so adguard's `vv0.107.73`
        becomes `v0.107.73` when re-prefixed) and refuse obvious
        non-version values (anything that looks like a short hex
        image ID or a generic branch name). Returns empty string when
        the label is unset or looks unusable."""
        v = (raw or "").strip()
        if not v or v == "<no value>":
            return ""
        # Strip exactly one leading 'v' so we can re-add it on display
        if v.startswith("v") and len(v) > 1:
            v = v[1:]
        # Reject 12-char hex image IDs and pure branch labels
        if len(v) == 12 and all(c in "0123456789abcdef" for c in v):
            return ""
        if v.lower() in ("main", "master", "develop", "dev", "edge", "latest", "stable"):
            return ""
        return v

    @staticmethod
    def _format_version_arrow(old_version, new_version):
        """Return ` (v{old} → v{new})` when both versions are present
        and differ, otherwise an empty string. Caller appends this to
        the history detail line so containers without a usable
        `image.version` label render the same as today."""
        if old_version and new_version and old_version != new_version:
            return f" (v{old_version} → v{new_version})"
        return ""

    def _get_image_defaults(self, image):
        """Read the image's own default Entrypoint and Cmd from
        ``docker image inspect``. Used by _build_run_args to detect
        whether a container's Cmd/Entrypoint differ from the image's
        defaults (so we don't lock in the OLD image's tokens when the
        image update changes them).

        Returns ``{"Entrypoint": [...]|None, "Cmd": [...]|None}`` or
        ``None`` if inspect fails. Callers passing ``None`` to
        _build_run_args get pre-v1.19.0 behaviour (blind Cmd restore).
        """
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout)
            if not data:
                return None
            cfg = (data[0].get("Config") or {})
            return {
                "Entrypoint": cfg.get("Entrypoint"),
                "Cmd": cfg.get("Cmd"),
            }
        except (subprocess.SubprocessError, json.JSONDecodeError,
                IndexError, ValueError):
            return None

    def _verify_running_image(self, name, image):
        """Whether ``name`` really runs the freshly pulled ``image``.

        Pull + recreate can both "succeed" yet leave the container on the
        previous image (#35) — report that honestly instead of a phantom
        OK. Returns ``(True, "")`` when they match or either ID can't be
        read (fail open: never fail an update on an inspect hiccup).
        """
        pulled_id = self._image_id(image)
        running_id = self._container_image_id(name)
        if pulled_id and running_id and pulled_id != running_id:
            self._debug(f"  Image mismatch: running {running_id[:19]} != pulled {pulled_id[:19]}")
            return False, (f"Pulled the new image but {name} is still running the old one "
                           f"(running {running_id[:19]} != pulled {pulled_id[:19]})")
        return True, ""

    @staticmethod
    def _image_config(ref):
        """An image's own ``Config`` block, or ``None`` if it can't be
        inspected.

        A container's inspect Config is the image's defaults *merged with*
        the user's explicit overrides, and Docker records no distinction
        between the two. Every Dockerfile instruction that lands in Config
        — ENV, LABEL, USER, WORKDIR, STOPSIGNAL, HEALTHCHECK, CMD,
        ENTRYPOINT — is therefore ambiguous on recreate. Comparing against
        the OLD image's Config is what makes them separable: whatever
        matches the old image was inherited and must NOT be replicated, so
        the new image gets to supply its own value.

        Pass the OLD image (the container's ``.Image`` ID) — the question
        is what the running container inherited, not what the new image
        offers. Static so the self-update path can reuse it.
        """
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{json .Config}}", ref],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return None
            return json.loads(r.stdout.strip() or "null")
        except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
            return None

    def _attach_extra_networks(self, container_name, config):
        """Connect a container to its additional (non-primary) networks
        after ``docker run``. The primary network (HostConfig.NetworkMode)
        is handled by --network on `docker run`; anything else in
        NetworkSettings.Networks needs an explicit
        ``docker network connect`` call to re-attach with the correct
        aliases / IPs / links.

        This closes a long-standing silent gap: containers attached to
        more than one network only got their primary network back on
        recreate. Common pattern: app on `frontend` + `backend` nets,
        update would leave it on only `frontend`.
        """
        host = config.get("HostConfig", {}) or {}
        primary = host.get("NetworkMode", "") or ""
        networks = (config.get("NetworkSettings") or {}).get("Networks") or {}
        if len(networks) <= 1:
            return
        short_id = (config.get("Id") or "")[:12]
        for net_name, net_info in networks.items():
            if net_name == primary:
                continue
            cmd = ["docker", "network", "connect"]
            for alias in (net_info.get("Aliases") or []):
                if not alias or alias == short_id or alias == container_name:
                    continue
                cmd.extend(["--alias", alias])
            ipam = net_info.get("IPAMConfig") or {}
            if ipam.get("IPv4Address"):
                cmd.extend(["--ip", ipam["IPv4Address"]])
            if ipam.get("IPv6Address"):
                cmd.extend(["--ip6", ipam["IPv6Address"]])
            for link in (net_info.get("Links") or []):
                cmd.extend(["--link", link])
            cmd.extend([net_name, container_name])
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    self._debug(f"  Network connect to {net_name} failed: "
                                f"{r.stderr[:200]}")
                else:
                    self._debug(f"  Attached {container_name} to {net_name}")
            except subprocess.SubprocessError as e:
                self._debug(f"  Network connect to {net_name} errored: {e}")

    # Static allow-lists for the inspect-coverage auditor. Keep in sync
    # with _build_run_args. Anything outside both sets fires a debug
    # warning so future Docker versions adding new HostConfig keys
    # surface instead of silently lost.
    _HONORED_HOSTCONFIG = frozenset({
        # Network / netns (handled in _build_run_args + _attach_extra_networks)
        "NetworkMode",
        # Capabilities / devices / sysctls / tmpfs / extra-hosts / DNS
        "CapAdd", "CapDrop", "Devices", "Sysctls", "Tmpfs",
        "ExtraHosts", "Dns", "DnsSearch", "DnsOptions", "SecurityOpt",
        # Ports / volumes (Mounts is read off the top-level config)
        "PortBindings", "Binds",
        # Restart / privileged / read-only / init / shm
        "RestartPolicy", "Privileged", "ReadonlyRootfs", "Init", "ShmSize",
        # Namespaces / runtime / logging
        "IpcMode", "PidMode", "UTSMode", "Runtime", "LogConfig",
        # Resource limits
        "Memory", "MemorySwap", "MemoryReservation", "KernelMemory",
        "KernelMemoryTCP", "MemorySwappiness",
        "NanoCpus", "CpuShares", "CpuPeriod", "CpuQuota",
        "CpuRtPeriod", "CpuRtRuntime", "CpusetCpus", "CpusetMems",
        "PidsLimit", "OomScoreAdj", "OomKillDisable", "BlkioWeight",
        "Ulimits", "GroupAdd", "AutoRemove",
    })
    # HostConfig keys we read elsewhere or intentionally don't restore
    # (Docker auto-manages them, or they're system-set metadata).
    _SKIPPED_HOSTCONFIG = frozenset({
        # Internal Docker accounting / runtime state
        "ContainerIDFile", "CgroupParent", "CgroupnsMode", "Cgroup",
        "ConsoleSize", "Isolation", "MaskedPaths", "ReadonlyPaths",
        # Block-IO leaf fields we don't expose; rare in real-world use
        "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps",
        "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps",
        # Device-cgroup-rules / DeviceRequests (GPU): out of scope here,
        # may add in a future release if requested.
        "DeviceCgroupRules", "DeviceRequests",
        # Storage opts (rare, driver-specific)
        "StorageOpt",
        # User-facing duplicates we already handle via top-level Mounts
        "VolumeDriver", "VolumesFrom",
        # PublishAllPorts: covered by PortBindings — Docker doesn't
        # round-trip `-P` separately, the inspect output is concrete
        # bindings either way.
        "PublishAllPorts",
        # PortBindings already in HONORED above — listed there
    })
    _HONORED_CONFIG = frozenset({
        "Env", "Labels", "Hostname", "User", "Cmd", "Entrypoint",
        "WorkingDir", "StopSignal", "StopTimeout", "Domainname",
        "Tty", "OpenStdin", "Healthcheck", "MacAddress",
    })
    _SKIPPED_CONFIG = frozenset({
        # Docker-managed metadata / state
        "AttachStdin", "AttachStdout", "AttachStderr", "StdinOnce",
        "ExposedPorts",          # informational — PortBindings is the real binding
        "Image",                 # the new image is passed in explicitly
        "Volumes",               # legacy anonymous volumes — Mounts covers it
        "OnBuild",               # build-only metadata
        "ArgsEscaped",           # Windows-only
        "NetworkDisabled",       # we handle via NetworkMode
        "Shell",                 # used by image build, not run-time
    })

    def _audit_inspect_coverage(self, config):
        """Walk the inspect dict and log a debug warning for any
        HostConfig or Config key with a non-default value that is
        NEITHER honoured by _build_run_args NOR explicitly skipped.

        This is the audit-mode safety net: when Docker adds new keys
        in future versions, they'll show up here instead of being
        silently dropped on recreate. Users running with DEBUG
        logging can grep for "[audit]" and report findings so we
        can extend coverage.
        """
        host = config.get("HostConfig", {}) or {}
        cfg = config.get("Config", {}) or {}

        def _is_non_default(value):
            # Truthy in the Python sense, plus also flag negative ints
            # (some HostConfig fields like OomScoreAdj are signed).
            if value in (None, "", 0, False, [], {}, ()):
                return False
            return True

        unknown_host = sorted(
            k for k, v in host.items()
            if _is_non_default(v)
            and k not in self._HONORED_HOSTCONFIG
            and k not in self._SKIPPED_HOSTCONFIG
        )
        unknown_cfg = sorted(
            k for k, v in cfg.items()
            if _is_non_default(v)
            and k not in self._HONORED_CONFIG
            and k not in self._SKIPPED_CONFIG
        )
        for k in unknown_host:
            self._debug(f"  [audit] HostConfig.{k} is non-default but not "
                        f"restored on recreate — please report at "
                        f"https://github.com/amayer1983/docksentry/issues")
        for k in unknown_cfg:
            self._debug(f"  [audit] Config.{k} is non-default but not "
                        f"restored on recreate — please report at "
                        f"https://github.com/amayer1983/docksentry/issues")
        # Return structured findings so callers (e.g. the /audit Telegram
        # command added in v1.20.0) can render the same data without
        # relying on DEBUG-only log lines.
        return {
            "host_unknown": unknown_host,
            "config_unknown": unknown_cfg,
        }

    def _update_standalone(self, name, image, netns_name=None):
        self._debug(f"Updating: {name} ({image})...")

        # Get old image info before pull. We also fetch the OCI
        # `image.version` label so the history entry can record a
        # version arrow when the upstream image advertises one (#22).
        # Coverage is ~40 % in real-world stacks (nginx, redis, postgres
        # often don't set the label; n8n, mariadb, adguardhome do) —
        # best-effort, silently skipped when unavailable.
        old_created = "?"
        old_version = ""
        old_inspect = subprocess.run(
            ["docker", "image", "inspect", "--format",
             '{{.Created}}||{{index .Config.Labels "org.opencontainers.image.version"}}', image],
            capture_output=True, text=True
        )
        if old_inspect.returncode == 0:
            parts = old_inspect.stdout.strip().split("||")
            old_created = parts[0][:10]
            if len(parts) > 1:
                old_version = self._normalize_version_label(parts[1])

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
        new_version = ""
        new_inspect = subprocess.run(
            ["docker", "image", "inspect", "--format",
             '{{.Created}}||{{.Size}}||{{index .Config.Labels "org.opencontainers.image.version"}}', image],
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
            if len(parts) > 2:
                new_version = self._normalize_version_label(parts[2])
        # Compose the optional version suffix for the detail strings —
        # appended later by update_container() at all return points so
        # both standalone and compose paths get the same treatment.
        self._version_arrow = self._format_version_arrow(old_version, new_version)

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

            # v1.19.0: surface any inspect keys we don't restore so
            # future Docker versions adding new HostConfig/Config keys
            # show up in debug logs instead of being silently dropped.
            self._audit_inspect_coverage(config)

            # AutoRemove (`--rm`) containers are removed by Docker the
            # instant they stop. For those the normal rename-old /
            # run-new / rollback dance doesn't apply — there's no old
            # container to rename or roll back to once we stop it.
            # Detect it up front so we can branch correctly below.
            auto_remove = bool((config.get("HostConfig") or {}).get("AutoRemove"))

            # Stop container, respecting its own Config.StopTimeout —
            # see _stop_container() and #11 for the slow-shutdown rationale.
            stop_ok, stop_detail = self._stop_container(name, inspect_config=config)
            self._debug(f"  Stop {name}: {stop_detail}")

            # After stopping, check whether the container still exists.
            # AutoRemove containers vanish on stop; a wedged `--rm`
            # container (slow to stop) is exactly how @famewolf lost
            # homarr in #2 — our stop reaped it, Docker auto-removed it,
            # and the old `if not stop_ok: return` path walked away
            # leaving him with nothing, even though we still had the full
            # config in memory. Now: if it vanished, recreate directly
            # from the captured config (the "old" is already gone, so we
            # skip the rename/rollback machinery).
            if not self._container_exists(name):
                self._debug(f"  {name} vanished after stop "
                            f"(AutoRemove={auto_remove}) — recreating from "
                            f"captured config")
                cmd = self._build_run_args(
                    config, image, name, self._get_image_defaults(image),
                    netns_name=netns_name,
                    inherited=self._image_config(config.get("Image")),
                )
                run = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if run.returncode != 0:
                    msg = (f"Container was auto-removed when stopped and the "
                           f"recreate failed: {run.stderr[:200]}")
                    self._save_history(name, image, False, msg)
                    return False, msg
                self._attach_extra_networks(name, config)
                outcome, state, health = self._wait_healthy(name)
                detail = (f"🗓️ {old_created} → {new_created}, 📦 {new_size}"
                          f"{getattr(self, '_version_arrow', '')}")
                # No rollback target exists for a --rm container, so even
                # on an unhealthy result we leave the freshly-created
                # container in place and report honestly.
                if outcome in ("unhealthy", "crashloop"):
                    tail = self._tail_logs(name, lines=10)
                    problem = ("is crash-looping" if outcome == "crashloop"
                               else f"health check failed (state={state}, health={health})")
                    msg = (f"⚠ Recreated after auto-remove, but {problem}. No "
                           f"rollback target exists for a --rm container — "
                           f"left running. ({detail})")
                    if tail:
                        msg += f"\nLast logs:\n```\n{tail}\n```"
                    self._save_history(name, image, False, msg)
                    return False, msg
                ok_img, mismatch = self._verify_running_image(name, image)
                if not ok_img:
                    self._save_history(name, image, False, mismatch)
                    return False, mismatch
                suffix = " (recreated after auto-remove)"
                self._save_history(name, image, True, detail + suffix)
                return True, f"OK ({detail}){suffix}"

            # Container still exists. If the stop itself failed, leave it
            # alone — same as before.
            if not stop_ok:
                return False, f"Couldn't stop container: {stop_detail}"

            # Rename old container
            old_name = f"{name}_old"
            subprocess.run(["docker", "rename", name, old_name], capture_output=True, timeout=10)
            self._debug(f"  Renamed to: {old_name}")

            # Build docker run command from inspect config — single
            # source of truth in `_build_run_args` covers HostConfig +
            # Config + NetworkSettings.Networks. v1.19.0 adds network
            # aliases (compose service hostnames), fixed IPs, MAC,
            # resource limits, healthcheck override and image-diff
            # Cmd/Entrypoint. Image defaults let us avoid locking in
            # the OLD image's Cmd/Entrypoint on update.
            image_defaults = self._get_image_defaults(image)
            cmd = self._build_run_args(config, image, name, image_defaults,
                                       netns_name=netns_name,
                                       inherited=self._image_config(config.get("Image")))
            self._debug(f"  Run cmd: docker run -d --name {name} ... {image}")

            # Create and start new container
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                self._debug(f"  Run failed: {result.stderr[:300]}")
                # Rollback: restore old container (safe — see _rollback_to_old)
                self._rollback_to_old(name, old_name)
                msg = f"Recreate failed: {result.stderr[:200]}"
                self._save_history(name, image, False, msg)
                return False, msg

            # Connect to additional networks (compose stacks often put
            # services on >1 network: app on `frontend` + `backend`).
            # The primary network is handled by --network on docker run;
            # extras need explicit `docker network connect` with their
            # aliases / IPs / links preserved.
            self._attach_extra_networks(name, config)

            # Health check: wait for the new container to come up. Up
            # to `config.healthcheck_max_starting` (default 600s), with
            # the image's own start_period × 1.5 as a floor.
            self._debug(f"  Health check: waiting for {name}...")
            outcome, state, health = self._wait_healthy(name)

            if outcome in ("unhealthy", "crashloop"):
                # Active failure — container died, healthcheck went
                # unhealthy after Docker's start_period elapsed, or the
                # new container is crash-looping. Roll back to the old
                # container so the user isn't left with a broken service.
                self._debug(f"  Health check FAILED ({outcome}) for {name} — rolling back")
                tail = self._tail_logs(name, lines=10)
                # Grab logs first (above), then roll back. _rollback_to_old
                # force-removes the broken new container and restores the
                # backup — safely (won't destroy `name` if no backup
                # exists). Replaces the old stop+rm+rename+start sequence
                # that silently failed when the new container wouldn't stop.
                self._rollback_to_old(name, old_name)
                if outcome == "crashloop":
                    msg = f"Update produced a crash-restart loop (state={state}, health={health}) — rolled back"
                else:
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
                detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}{getattr(self, '_version_arrow', '')}"
                msg = (f"⚠ Updated but still 'starting' after our wait — left running. "
                       f"Docker's own healthcheck will keep checking. ({detail})")
                if tail:
                    msg += f"\nLast logs:\n```\n{tail}\n```"
                self._save_history(name, image, True, f"{detail} (slow start)")
                return True, msg

            # Verify the new container really runs the pulled image before
            # we drop the rollback target (#35). Checked here, while
            # `<name>_old` still exists, so a mismatch can be undone.
            ok_img, mismatch = self._verify_running_image(name, image)
            if not ok_img:
                self._rollback_to_old(name, old_name)
                msg = f"{mismatch} — rolled back"
                self._save_history(name, image, False, msg)
                return False, msg

            # Remove old container
            subprocess.run(["docker", "rm", old_name], capture_output=True, timeout=30)
            self._debug(f"  Recreated successfully: {name} (health: {health or 'ok'})")

            detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}{getattr(self, '_version_arrow', '')}"
            self._save_history(name, image, True, detail)
            return True, f"OK ({detail})"

        except Exception as e:
            self._debug(f"  Error: {str(e)[:200]}")
            # Try to restore on any failure. Use the literal "<name>_old"
            # (not the `old_name` local, which may be undefined if the
            # exception fired before the rename step). _rollback_to_old
            # leaves `name` untouched when no "<name>_old" backup exists —
            # so if the exception happened before we ever stopped/renamed
            # the original, the user's still-running container is safe.
            self._rollback_to_old(name, f"{name}_old")
            self._save_history(name, image, False, str(e)[:200])
            return False, f"Error: {str(e)[:200]}"
