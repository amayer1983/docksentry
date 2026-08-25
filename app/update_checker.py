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

# The container CLI seam. Module-level so the @staticmethod/@classmethod
# helpers (which have no self) can reach the shared backend too.
import container_backend as _cb
from errfmt import clip


#: The names Docker Hub answers to. `docker login` writes the legacy v1
#: index URL, while image references resolve to `registry-1.docker.io`, so
#: without this the one credential nearly everyone has would never be found.
_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io",
                "registry.hub.docker.com"}


def _auth_host(entry):
    """The bare host a `config.json` auths key or a registry name refers to.

    `https://index.docker.io/v1/` and `registry-1.docker.io` both mean Docker
    Hub; `eu.gcr.io` and `gcr.io` do NOT mean each other.

    That second half is the point. The previous version matched with
    `registry in key or key in registry`, which handed `eu.gcr.io`'s
    credentials to anyone asking about `gcr.io`, and `myregistry.example.com`'s
    to anyone asking about `example.com` — a private registry's Basic-Auth
    header sent to a different operator entirely. Measured, not theorised.
    It also failed at the job the substring match was written for:
    `registry-1.docker.io` never matched `https://index.docker.io/v1/`,
    because neither string contains the other.

    Found by sweeping Watchtower's issue history (watchtower#376), which is
    the same class of bug as the `git.` prefix that used to match
    `digit.example.com` in the link resolver: a hostname is a structure, not
    a substring.
    """
    host = (entry or "").strip()
    if "//" in host:
        host = host.split("//", 1)[1]
    host = host.split("/", 1)[0].strip().lower()
    return "registry-1.docker.io" if host in _HUB_ALIASES else host





class ContainerListUnavailable(Exception):
    """`docker ps` did not answer, so we do not know what is running.

    Deliberately an exception rather than an empty list. The two states —
    "this host runs nothing" and "this host could not be reached" — look
    identical downstream, and treating the second as the first is what made
    an unreachable daemon report zero updates, wipe that host's pending
    list and send "everything up to date" (wud#570, wud#711). A caller that
    forgets to handle this gets a traceback; one that returns [] gets a
    quiet lie.
    """


def parse_mirrors(entries):
    """`["docker.io=mirror.internal", ...]` -> `{"docker.io": "mirror.internal"}`.

    Malformed entries are dropped rather than guessed at: a mirror map with
    a typo should lose that one line, not silently redirect lookups to
    something the operator did not write.
    """
    out = {}
    for e in entries or []:
        e = (e or "").strip()
        if "=" not in e:
            continue
        origin, mirror = e.split("=", 1)
        origin, mirror = origin.strip(), mirror.strip()
        if origin and mirror:
            out[origin] = mirror
    return out


def mirror_host(host, mirrors):
    """The host to ASK about `host`, which may be a mirror of it.

    Only affects lookups. Pulling still goes through the daemon with the
    container's own image reference, which is deliberate — see the note on
    `_effective_host`.

    Docker Hub answers to several names, so a mirror written for any of
    them applies to the canonical one the lookup code uses.
    """
    if not mirrors:
        return host
    if host in mirrors:
        return mirrors[host]
    if host == "registry-1.docker.io":
        for alias in ("docker.io", "index.docker.io", "registry.hub.docker.com"):
            if alias in mirrors:
                return mirrors[alias]
    return host


def registry_scheme(host, insecure_list):
    """`"https"` or `"http"` for a registry host.

    Always https unless the operator has NAMED this host in
    `INSECURE_REGISTRIES`. Never guessed, never a fallback-on-failure: a
    tool that silently retries over plain HTTP when TLS fails is a tool
    that will hand credentials to whoever answers. Docker's own client
    takes the same position — an insecure registry has to be listed.

    Without this the `https://` in every registry URL was hardcoded, so a
    local or internal HTTP-only registry reported "unreachable /
    unauthorized" on every cycle while `docker pull` worked, and there was
    no setting to change it (watchtower#277/#497/#767, diun#357).
    """
    return "http" if name_matches(host, insecure_list or []) else "https"


def name_matches(name, patterns):
    """Whether `name` matches any of `patterns`.

    Shell-style wildcards (`*`, `?`, `[abc]`) via `fnmatch`, not regex.
    Deliberate: the thing people reach for is `systemd-*`, and a regex that
    silently matches more than intended is a bad failure mode for a setting
    whose whole job is to stop Docksentry touching a container. A pattern
    with no wildcard in it behaves exactly like the exact-name matching
    this replaces, so existing EXCLUDE_CONTAINERS values keep working
    unchanged (#55, @LeeNX).
    """
    if not patterns:
        return False
    from fnmatch import fnmatchcase
    for p in patterns:
        p = (p or "").strip()
        if not p:
            continue
        if p == name or fnmatchcase(name, p):
            return True
    return False


class UpdateChecker:
    @property
    def backend(self):
        """The container CLI backend for this checker.

        Falls back to the process-wide default when `__init__` never ran:
        tests build a bare `UpdateChecker.__new__(UpdateChecker)` to
        exercise pure helpers, and a read shouldn't explode on those. Same
        reasoning as the no-self helpers using `_cb.default_backend()`.
        """
        b = getattr(self, "_backend", None)
        return b if b is not None else _cb.default_backend()

    def __init__(self, config, backend=None):
        self.config = config
        # Container CLI seam: every container command in this file — reads
        # and writes — goes through it, so the binary name and argv
        # construction live in one place (podman / remote hosts become a
        # backend swap instead of edits in ninety places).
        #
        # `backend` is how multi-host works (#7): one checker per host, each
        # handed a backend pointed at that host. Left unset it builds the
        # local one, which is the single-host case and unchanged.
        self._backend = backend if backend is not None else _cb.get_backend(config)
        self.debug_log = []
        # Per-run scratch state. Cleared at the top of check_all so a long
        # running process never serves stale data from an earlier sweep.
        self._repo_digest_cache = {}
        self._tag_list_cache = {}
        self._token_cache = {}
        self._auth_kind = "anonymous"

    def _t(self, key, **kw):
        """A user-facing line, in the configured language.

        The checker's results are read by people in a chat, so its words
        belong in the shared translations like everyone else's (#63).
        Resolved per call from `config.language`, so /lang applies at
        once. Falls back to the key when no config is around — the bare
        `__new__` instances a few tests build.
        """
        from i18n import get_translator
        lang = getattr(getattr(self, "config", None), "language", "en") or "en"
        return get_translator(lang)(key, **kw)

    def _debug(self, msg):
        """A diagnostic line. Always printed, on purpose.

        Tempting to hide these behind `debug`, and wrong: these are the
        lines that say something *happened* — a rollback, a rename that
        timed out, a stop that had to escalate to kill. @famewolf's
        `Stop gluetun-nzbhydra2: effective_stop=60s, subprocess=90s` came
        from a debug-OFF log and is what finally made #2 readable. A
        diagnostic nobody has switched on is not a diagnostic.

        For the per-container bookkeeping of an ordinary scan, use
        `_trace` instead.
        """
        print(msg)
        if self.config.debug:
            self.debug_log.append(msg)

    def _trace(self, msg):
        """Bookkeeping from a routine scan — only with debug on.

        `Skipped (self): DockSentry` and its siblings say nothing except
        "the loop ran". They appeared in every log, on every check, and
        @NotRetarded reasonably asked in #2 what was being skipped and
        why — which is the tell that a line is costing more attention
        than it is worth. Nothing here reports an event; each one is a
        restatement of configuration the user already set.
        """
        if self.config.debug:
            print(msg)
            self.debug_log.append(msg)

    def _diag_on(self):
        """Whether the verbose registry diagnostics (#53, @LeeNX) are on.

        Guard for anything that costs a subprocess or an HTTP request before
        it can be handed to _vdebug — an f-string argument is evaluated
        whether or not the line ends up being printed.
        """
        return bool(getattr(self.config, "debug", False))

    def _vdebug(self, msg):
        """Verbose diagnostic line — DEBUG only.

        Deliberately NOT _debug: that one prints unconditionally and lets
        `config.debug` decide only whether the line is also collected for
        Telegram / the Web UI console. Everything added for #53 is here
        instead, because it is a lot of text per container and nobody who
        didn't ask for it should find it in `docker logs docksentry`.
        """
        if not self._diag_on():
            return
        self._debug(msg)

    def _labels_for(self, names):
        """`{name: {label: value}}` for several containers, in one call.

        NOT parsed out of `docker ps --format {{.Labels}}`, which is what
        this replaces, because that flat format loses data in two ways and
        both were measured:

          * a label VALUE containing a newline truncates the `ps` line, so
            the whole rest of that container's labels vanishes. A container
            carrying `docksentry.pin=true` alongside a multi-line
            description parsed to just the description — the explicit pin
            was silently ignored and the container stayed updatable.
          * a label value containing `, key=value` parses as two labels. So
            `LABEL description="Handy tool, docksentry.enable=false"` in
            somebody else's image sets a docksentry flag on their behalf —
            baked into the image, not chosen by the operator.

        `docker inspect` takes many refs at once and answers in JSON, so
        this costs one extra call per sweep rather than one per container.
        (wud#1113, wud#921.)
        """
        names = [n for n in names if n]
        if not names:
            return {}
        try:
            r = self.backend.inspect(names, timeout=60)
            if r.returncode != 0:
                self._debug(f"  Label read failed (rc={r.returncode}) — "
                            f"docksentry.* labels will not apply this sweep")
                return {}
            data = json.loads(r.stdout)
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            # Loud, not silent: labels failing to load means every
            # label-based pin and exclusion stops applying, which is
            # exactly the kind of quiet fail-open this change is about.
            self._debug(f"  Label read failed ({e}) — docksentry.* labels "
                        f"will not apply this sweep")
            return {}
        out = {}
        for entry in data if isinstance(data, list) else []:
            cfg = (entry or {}).get("Config") or {}
            nm = ((entry or {}).get("Name") or "").lstrip("/")
            if nm:
                out[nm] = cfg.get("Labels") or {}
        return out

    def get_running_containers(self, include_self=False, timeout=None):
        # `{{.Labels}}` deliberately dropped from this format string — see
        # `_labels_for`. Names and image references cannot contain a
        # newline, so without it a `ps` line can no longer be truncated
        # mid-container either.
        result = self.backend.run(
            ["ps", "--format", "{{.Names}}|{{.Image}}"], timeout=timeout)
        # A failing `ps` — daemon down, socket-proxy denial, remote host
        # unreachable — produced empty stdout and therefore an empty list,
        # which is indistinguishable from a host that genuinely runs
        # nothing. The monitor has checked this since it was written; the
        # update path never did.
        if result.returncode != 0:
            err = (getattr(result, "stderr", "") or "").strip()[:200]
            raise ContainerListUnavailable(
                f"could not list containers (rc={result.returncode}): {err}")
        # Get own container name to exclude self. Robust detection: tries
        # HOSTNAME env first, falls back to /proc/self/cgroup if that's
        # missing or doesn't resolve. The old HOSTNAME-only path silently
        # missed self-detection in some compose / orchestrator setups,
        # leading to the bot updating itself via the regular flow and
        # killing PID 1 (#16).
        own_name = self._own_container_name()

        rows = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            rows.append((parts[0], parts[1]))
        all_labels = self._labels_for([n for n, _ in rows])

        containers = []
        for name, image in rows:
            labels = all_labels.get(name) or {}
            # Skip self — on the UPDATE path, where it protects PID 1
            # (#16). A read is a different matter: `/status` hiding the
            # very container that answers it confused @NotRetarded twice
            # over — first "what is Skipped (self)?", then "Docksentry
            # doesn't see itself in /status" (#2). Readers pass
            # include_self=True; the update path keeps the default.
            if own_name and name == own_name and not include_self:
                self._trace(f"  Skipped (self): {name}")
                continue
            # Resolve images referenced by ID via container inspect
            if re.match(r'^[0-9a-f]{12,}$', image):
                resolved = self.backend.run(
                    ["inspect", "--format", "{{.Config.Image}}", name])
                if resolved.returncode == 0 and resolved.stdout.strip() and \
                   not re.match(r'^[0-9a-f]{12,}$', resolved.stdout.strip()):
                    image = resolved.stdout.strip()
                    self._trace(f"  Resolved image ID: {name} → {image}")
                else:
                    self._trace(f"  Skipped (image ID): {name} ({image})")
                    continue
            if name_matches(name, self.config.exclude_containers):
                self._trace(f"  Skipped (excluded): {name}")
                continue
            if name in self._get_pinned():
                self._trace(f"  Skipped (pinned): {name}")
                continue
            # GitOps twin of /pin (#42, @LeeNX): freeze a container from its
            # own compose file. Same effect as the stored pin — never listed,
            # never updated.
            if self.label_bool(labels, "pin") is True:
                self._trace(f"  Skipped (pinned via label): {name}")
                continue
            # Per-container label opt-out (#42, @LeeNX): a GitOps-friendly way
            # to take a container out of Docksentry's scope from the compose
            # file itself — `docksentry.enable=false` or `docksentry.exclude=true`.
            if self.label_bool(labels, "enable") is False or self.label_bool(labels, "exclude") is True:
                self._trace(f"  Skipped (docksentry label): {name}")
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
            r = self.backend.run(
                ["inspect", "--format", "{{json .Config.Labels}}", name], timeout=10)
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
            r = _cb.default_backend().run(
                ["image", "inspect", "--format",
                 '{{index .Config.Labels "org.opencontainers.image.version"}}', image], timeout=10)
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
            want = _auth_host(registry)
            for key, val in auths.items():
                if _auth_host(key) == want and val.get("auth"):
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

        Returns a complete `Authorization` HEADER VALUE — `Bearer <tok>` or
        `Basic <b64>` — not a bare token. Two schemes reach this function
        now, and letting each of the three call sites prepend "Bearer "
        itself is how one of them would eventually send `Bearer` in front
        of Basic credentials.

        Tokens are cached per (registry, repository) for the rest of the run,
        honouring the `expires_in` the registry hands out (minus a safety
        margin). Without it every single registry access pays the full three
        round-trips — 401, token, retry — and get_remote_image_meta alone
        makes three accesses per container.
        """
        # A registry can answer with `Basic` instead of `Bearer` — the
        # stock `registry:2` behind htpasswd does exactly that, and so do
        # Nexus and Artifactory in their simpler modes. There is no token
        # to negotiate: the credentials go straight to the registry. Before
        # this, `_parse_www_authenticate` returned {} for anything not
        # Bearer, negotiation returned None, and the credentials already
        # sitting in config.json were never sent — so the whole class of
        # private registry was permanently uncheckable while `docker pull`
        # worked fine, which is a confusing pair of facts to be handed
        # (diun#357, diun#5, wud#797).
        if (www_auth or "").strip().lower().startswith("basic"):
            auth = self._get_docker_credentials(registry)
            if not auth:
                self._debug(f"  Registry wants Basic auth but no credentials "
                            f"are configured for {registry}")
                return None
            self._auth_kind = "basic"
            # A full header value, not a bare token: see the note on the
            # return type below.
            return f"Basic {auth}"

        params = self._parse_www_authenticate(www_auth)
        realm = params.get("realm")
        if not realm:
            return None

        auth_header = self._get_docker_credentials(registry)
        self._auth_kind = "credentials from config" if auth_header else "bearer"

        key = (registry, repository)
        cached = self._token_cache.get(key)
        if cached and cached[1] > time.time():
            self._vdebug(f"      auth: reusing cached {self._auth_kind} token")
            return cached[0]

        query = []
        if "service" in params:
            query.append(("service", params["service"]))
        # Use the scope from the challenge if present; otherwise build a
        # sensible default. Scope can repeat, so handle the list case too.
        scope = params.get("scope") or f"repository:{repository}:pull"
        query.append(("scope", scope))
        token_url = realm + "?" + urllib.parse.urlencode(query)

        # Realm and scope are useful (a wrong scope is a classic cause of a
        # 401 loop), the token never is — so the challenge gets logged and
        # the response does not. Scope is truncated because on private
        # registries it can carry a full internal project path.
        self._vdebug(f"      auth: {self._auth_kind} challenge from {realm} "
                     f"(scope {self._short(scope, 60)})")

        req = urllib.request.Request(token_url)
        if auth_header:
            req.add_header("Authorization", f"Basic {auth_header}")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                # Some registries return "token", others "access_token".
                token = data.get("token") or data.get("access_token")
        except Exception as e:
            self._debug(f"  Token negotiation failed: {e}")
            return None
        if token:
            try:
                ttl = int(data.get("expires_in") or 60)
            except (TypeError, ValueError):
                ttl = 60
            # 10s of slack so we never present a token that expires in
            # flight; a too-short TTL just costs us a renegotiation.
            self._token_cache[key] = (f"Bearer {token}",
                                      time.time() + max(1, ttl - 10))
            return f"Bearer {token}"
        return None

    def _forget_token(self, registry, repository):
        """Drop a cached token after it was rejected. A registry may expire a
        token earlier than its own `expires_in` promised; without this the
        whole run would keep replaying the stale one."""
        self._token_cache.pop((registry, repository), None)

    @staticmethod
    def _short(text, limit):
        """Truncate for log output (never for comparisons)."""
        text = str(text or "")
        return text if len(text) <= limit else text[:limit - 3] + "..."

    #: Platform per host, keyed by the backend's host name. This used to be
    #: one process-wide value, which was fine while there was one daemon and
    #: silently wrong the moment there were two: the first host to ask
    #: cached its architecture and every other host reused it. On a mixed
    #: fleet — an amd64 box and an arm64 Pi, which is a completely ordinary
    #: homelab — the Pi would then be compared against amd64 digests and get
    #: the wrong verdict on every multi-arch image. Asked by @LeeNX in #7
    #: before anyone had run it that way.
    _host_platform_cache = {}

    def _host_platform(self):
        """The DAEMON's (os, architecture) — the platform images are pulled
        for — via ``docker version``. Asking the daemon (not Python's
        ``platform`` module) matters because Docksentry itself runs in a
        container: the daemon may sit on a different host than us (socket
        proxy setups, and every remote host in a multi-host setup).

        Cached per host for the process lifetime; falls back to
        linux/amd64 when the daemon can't say.
        """
        key = getattr(self.backend, "name", "") or "local"
        cached = UpdateChecker._host_platform_cache.get(key)
        if cached is None:
            os_name, arch = "linux", "amd64"
            try:
                r = self.backend.run(
                    ["version", "--format",
                     "{{.Server.Os}}/{{.Server.Arch}}"], timeout=10)
                # Split on the FIRST slash only: a value like
                # `linux/arm/v7` is three fields, and requiring exactly two
                # made it fall through to the amd64 default — silently
                # comparing an ARM host against amd64 digests. Falling back
                # is right when the daemon says nothing; it is wrong when
                # the daemon answered and we merely failed to parse it.
                parts = r.stdout.strip().split("/", 1)
                if r.returncode == 0 and len(parts) == 2 and all(parts):
                    os_name, arch = parts
            except (subprocess.SubprocessError, OSError):
                pass
            cached = (os_name, arch)
            UpdateChecker._host_platform_cache[key] = cached
        return cached

    #: host name -> "1" | "2". A dict, not a scalar: see _cgroup_version.
    _cgroup_version_cache = {}

    @classmethod
    def _cgroup_version(cls, backend=None):
        """The DAEMON's cgroup version — "1" or "2" — via ``docker info``.

        Cached PER HOST, not per process. The answer feeds `_build_run_args`
        when a container is recreated, and on a mixed fleet — a cgroup-v1
        NAS beside a cgroup-v2 laptop, an ordinary homelab — one shared
        cache let whichever host was asked first decide for all of them.
        Knobs like `memory.swappiness` are v1-only, so getting it wrong
        either drops a setting the container had or emits one the daemon
        rejects. Third time a per-process cache has been a multi-host trap
        here (HostScopedStore, then `_host_platform_cache`), which is why
        the key is explicit rather than implied.

        On any error we assume "1" — the conservative choice, since
        over-suppressing v1-only knobs on a real v1 host would be the
        regression. Still a classmethod so the self-update path in
        telegram_bot can call it without an instance; that path is always
        local, and omitting the argument keeps the local backend.
        """
        b = backend if backend is not None else _cb.default_backend()
        key = getattr(b, "name", "") or "local"
        cached = cls._cgroup_version_cache.get(key)
        if cached is None:
            version = "1"
            try:
                r = b.run(["info", "--format", "{{.CgroupVersion}}"], timeout=10)
                out = r.stdout.strip()
                if r.returncode == 0 and out in ("1", "2"):
                    version = out
            except (subprocess.SubprocessError, OSError):
                pass
            cls._cgroup_version_cache[key] = version
            cached = version
        return cached

    _daemon_net_cache = None

    @classmethod
    def _daemon_net_info(cls):
        """The daemon's registry mirrors and proxy settings, from
        ``docker info``. Returns a dict with "mirrors" and "proxy", both
        already formatted for the log.

        Same failure policy as _cgroup_version: a restrictive socket proxy
        may refuse /info outright, and a check that would otherwise have
        worked must not die over a diagnostic. We write "unknown" and move
        on. Cached per process — this never changes while we run.
        """
        if cls._daemon_net_cache is None:
            info = {"mirrors": "unknown", "proxy": "unknown"}
            try:
                r = _cb.default_backend().run(
                    ["info", "--format",
                     "{{json .RegistryConfig.Mirrors}}\t{{.HTTPProxy}}\t"
                     "{{.HTTPSProxy}}\t{{.NoProxy}}"], timeout=10)
                # Strip newlines only — an unset proxy is an EMPTY field, and
                # a plain .strip() would eat the trailing tabs along with it
                # and leave us with fewer columns than we asked for.
                parts = [p.strip() for p in r.stdout.strip("\r\n").split("\t")]
                if r.returncode == 0 and len(parts) == 4:
                    try:
                        mirrors = json.loads(parts[0]) or []
                    except (ValueError, TypeError):
                        mirrors = []
                    info["mirrors"] = ", ".join(mirrors) if mirrors else "none"
                    proxies = [f"{label}={cls._mask_url(v)}"
                               for label, v in (("http", parts[1]),
                                                ("https", parts[2]),
                                                ("no_proxy", parts[3]))
                               if v and v != "<no value>"]
                    info["proxy"] = ", ".join(proxies) if proxies else "none"
            except (subprocess.SubprocessError, OSError):
                pass
            cls._daemon_net_cache = info
        return cls._daemon_net_cache

    @staticmethod
    def _mask_url(url):
        """Strip userinfo from a URL before it is logged. Proxy settings
        routinely carry `http://user:password@proxy:3128`, and a debug log
        ends up pasted into a GitHub issue."""
        url = str(url or "")
        if "@" in url and "//" in url:
            head, _, tail = url.partition("//")
            _, _, hostpart = tail.rpartition("@")
            return f"{head}//***@{hostpart}"
        return url

    def _proxy_environment(self):
        """Proxy variables set inside OUR container.

        Worth a line of its own because ``urllib.request.urlopen`` uses the
        default opener, and that one silently picks up ``http_proxy`` /
        ``https_proxy`` from the environment. So a proxy nobody configured
        in Docksentry can still sit between us and the registry, and until
        now nothing said so.
        """
        seen = {}
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "no_proxy"):
            val = os.environ.get(var)
            if val:
                seen[var] = self._mask_url(val)
        if not seen:
            return "none"
        return ", ".join(f"{k}={v}" for k, v in seen.items())

    def _registry_environment(self):
        """One line covering everything that sits between us and a registry —
        printed once per check run (#53, @LeeNX)."""
        os_name, arch = self._host_platform()
        daemon = self._daemon_net_info()
        return (f"host {os_name}/{arch}, mirrors: {daemon['mirrors']}, "
                f"daemon proxy: {daemon['proxy']}, "
                f"our proxy: {self._proxy_environment()}")

    def _log_registry_response(self, resp, url):
        """Describe a registry response for the debug log (#53, @LeeNX).

        Status, content type and the effective URL are the three things that
        tell you what actually answered: a `manifest.list`/`image.index`
        content type means a multi-arch index came back, and a URL that no
        longer matches the one we asked for means a mirror, a proxy or a CDN
        redirect sits in the path. Which is precisely the class of problem
        that used to leave nothing but two truncated hashes behind.

        Only the auth CATEGORY is named — never the token, never the
        Basic-Auth header.
        """
        if not self._diag_on():
            return
        try:
            status = getattr(resp, "status", None) or getattr(resp, "code", "?")
            ctype = resp.headers.get("Content-Type", "?")
            final = getattr(resp, "url", "") or url
        except Exception:
            return
        self._vdebug(f"      HTTP {status}, auth {self._auth_kind}, "
                     f"content-type {self._short(ctype, 70)}")
        if final != url:
            self._vdebug(f"      redirected to {self._short(final, 120)}")

    @staticmethod
    def _describe_registry_error(exc):
        """A short, human reason for a failed registry lookup.

        The per-container line used to read "registry unreachable /
        unauthorized" whatever had happened — a rate limit, a deleted tag, a
        server error and a TLS failure all looked identical, which sent
        people hunting for an auth problem they did not have. The detail was
        logged, but only to the debug log nobody has on. Same class of
        defect as a setting that silently does not apply: the logic was
        right, the message was not (diun#94, diun#245, wud#419).
        """
        code = getattr(exc, "code", None)
        if code == 429:
            return "rate limited by the registry (HTTP 429)"
        if code in (401, 403):
            return f"not authorised (HTTP {code})"
        if code == 404:
            return "tag or repository not found (HTTP 404)"
        if isinstance(code, int) and 500 <= code < 600:
            return f"registry server error (HTTP {code})"
        if isinstance(code, int):
            return f"HTTP {code}"
        text = str(exc)
        low = text.lower()
        if "certificate verify failed" in low:
            # The single most common private-registry setup: a real cert
            # signed by the operator's own CA. It works — Python reads
            # SSL_CERT_FILE — but nobody guesses that, and the message used
            # to end at "TLS error", which reads as "unsupported". Worse,
            # the setting we *do* document, INSECURE_REGISTRIES, is the
            # wrong answer here: it downgrades to plain HTTP, so it either
            # fails outright against a TLS-only port or sends the Basic
            # credentials in clear text. Measured against a self-signed
            # `registry:2`: verify failure without it, digest and tag list
            # with it. (wud#604, wud#111, wud#52.)
            return ("TLS certificate not trusted — if this registry uses a "
                    "private CA, point SSL_CERT_FILE at its PEM bundle "
                    "(INSECURE_REGISTRIES is not the fix; it drops TLS)")
        if "certificate" in low or "ssl" in low:
            return f"TLS error ({text[:60]})"
        if "timed out" in low or "timeout" in low:
            return "timed out"
        if "name or service not known" in low or "nodename" in low:
            return "DNS lookup failed"
        return text[:70] or "unknown error"

    def _scheme_for(self, host):
        """https, or http for a host the operator listed as insecure."""
        return registry_scheme(
            host, getattr(self.config, "insecure_registries", []))

    def _effective_host(self, host):
        """The host to ASK about `host` — itself, or a configured mirror.

        Checks go out over urllib, straight to the registry named in the
        image reference, and therefore ignore the daemon's own
        `registry-mirrors` entirely. On a network where only the mirror is
        reachable — air-gapped, or behind a proxy that allows one host —
        `docker pull` works and Docksentry reports "unreachable" forever,
        which is the same confusing pair of facts as the HTTP-only
        registries (#34, @LeeNX).

        Deliberately lookups ONLY. Pulling still hands the container's own
        image reference to the daemon, because pulling from somewhere else
        would rewrite that reference — `nginx:1.25` becomes
        `mirror.internal/nginx:1.25` — and then the container no longer
        matches its own compose file and the next check compares against
        something different again. Docker's `registry-mirrors` in
        daemon.json is the right place for the pull side: it covers every
        pull on the host rather than only ours.
        """
        return mirror_host(
            host, parse_mirrors(getattr(self.config, "registry_mirrors", [])))

    def _get_remote_digest(self, registry, repository, tag):
        """Fetch the remote manifest digest for a tag.

        Implements the Docker Registry V2 Bearer token flow:
            1. anonymous HEAD on /v2/<repo>/manifests/<tag>
            2. on 401, parse WWW-Authenticate, fetch Bearer token
            3. retry HEAD with `Authorization: Bearer <token>`

        This works generically for Docker Hub, GHCR, lscr.io, quay.io,
        gcr.io, registry.gitlab.com and any spec-compliant registry — no
        per-host hardcoding required.

        Stays a HEAD on purpose. A GET would hand us the manifest body (and
        with it the version) in one shot, but GETs count against Docker Hub's
        pull budget and HEADs do not — the tag-resolution done for #53 is
        gated for exactly that reason, and it would be pointless to pay the
        toll on the base check instead.
        """
        if "docker.io" in registry:
            host = "registry-1.docker.io"
        else:
            host = registry
        host = self._effective_host(host)
        scheme = self._scheme_for(host)
        url = f"{scheme}://{host}/v2/{repository}/manifests/{tag}"
        self._auth_kind = "anonymous"
        self._vdebug(f"    HEAD {url}")

        def _attempt(token=None):
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("Accept", self._MANIFEST_ACCEPT)
            if token:
                req.add_header("Authorization", token)
            return urllib.request.urlopen(req, timeout=15)

        try:
            with _attempt() as resp:
                self._log_registry_response(resp, url)
                return resp.headers.get("Docker-Content-Digest", "")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                self._debug(f"  Registry error: HTTP {e.code} {e.reason}")
                self._last_registry_error = self._describe_registry_error(e)
                return None
            # 401 — negotiate a Bearer token from the WWW-Authenticate header
            www_auth = e.headers.get("WWW-Authenticate", "")
            token = self._negotiate_token(www_auth, registry, repository)
            if not token:
                self._debug("  Registry error: 401 (token negotiation failed)")
                self._last_registry_error = "authentication failed"
                return None
            try:
                with _attempt(token) as resp:
                    self._log_registry_response(resp, url)
                    return resp.headers.get("Docker-Content-Digest", "")
            except urllib.error.HTTPError as e2:
                if e2.code == 401:
                    self._forget_token(registry, repository)
                self._debug(f"  Registry error after auth: HTTP {e2.code} {e2.reason}")
                self._last_registry_error = self._describe_registry_error(e2)
                return None
            except Exception as e2:
                self._debug(f"  Registry error after auth: {e2}")
                self._last_registry_error = self._describe_registry_error(e2)
                return None
        except Exception as e:
            self._debug(f"  Registry error: {e}")
            self._last_registry_error = self._describe_registry_error(e)
            return None

    def _registry_get(self, host, registry, repository, path, accept=None):
        """GET a registry resource (manifest or blob) with Bearer-token
        negotiation. Returns raw bytes, or None on failure. Mirrors the
        auth flow of _get_remote_digest but for GET bodies.

        Unlike the HEAD of the base check, these GETs DO count against Docker
        Hub's anonymous pull budget — see the gate in check_all."""
        host = self._effective_host(host)
        scheme = self._scheme_for(host)
        url = f"{scheme}://{host}/v2/{repository}/{path}"
        self._auth_kind = "anonymous"
        self._vdebug(f"    GET {url}")

        def _attempt(token=None):
            req = urllib.request.Request(url)
            if accept:
                req.add_header("Accept", accept)
            if token:
                req.add_header("Authorization", token)
            return urllib.request.urlopen(req, timeout=15)

        try:
            with _attempt() as resp:
                self._log_registry_response(resp, url)
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code != 401:
                self._vdebug(f"      HTTP {e.code} {e.reason}")
                return None
            token = self._negotiate_token(
                e.headers.get("WWW-Authenticate", ""), registry, repository)
            if not token:
                return None
            try:
                with _attempt(token) as resp:
                    self._log_registry_response(resp, url)
                    return resp.read()
            except urllib.error.HTTPError as e2:
                if e2.code == 401:
                    self._forget_token(registry, repository)
                self._vdebug(f"      HTTP {e2.code} {e2.reason} after auth")
                return None
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
    #: The patch component is optional, because a great many images never
    #: publish one. `postgres` is the clearest case: 32 two-component tags
    #: on Docker Hub and not a single three-component one — measured — so
    #: requiring three numbers meant the advisory could never fire for the
    #: images people pin most, which are the databases.
    _SEMVER_RE = re.compile(
        r"^(?:.*?-)?v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
        r"(?P<pre>[-+][\w.\-+]*)?$"
    )

    #: A first component this large is a year, not a major version.
    #: Unproven precaution: none of postgres, mariadb, redis or mysql
    #: carries a tag like `2024.11` — measured over 300 tags each. It
    #: costs one comparison and it is the shape most likely to be
    #: mistaken for a version now that two components parse.
    _YEARLIKE_MAJOR = 1000

    @classmethod
    def _parse_semver(cls, tag):
        """Parse tag into (major, minor, patch, pre) tuple or None.
        `pre` is the pre-release/build suffix as a string (or "")."""
        if not tag:
            return None
        m = cls._SEMVER_RE.match(tag.strip())
        if not m:
            return None
        # A missing patch counts as 0, so `16.3` and `16.3.0` order and
        # compare identically. The SHAPE is kept apart from the value —
        # see `_semver_components` — because "same number of components"
        # is a matching rule, not an ordering one.
        return (int(m.group("major")), int(m.group("minor")),
                int(m.group("patch") or 0), m.group("pre") or "")

    @classmethod
    def _semver_components(cls, tag):
        """2 or 3 — how many numbers the tag actually spells out. None if
        it does not parse at all.

        Someone pinned to `redis:7.2` means that line, and the equivalent
        step is `7.4`, not `7.4.1`. Comparing across shapes would answer a
        question nobody asked, and in a repository carrying both — redis
        has 9 two-component tags and 53 three-component ones, measured —
        it would answer it constantly.
        """
        if not tag:
            return None
        m = cls._SEMVER_RE.match(tag.strip())
        if not m:
            return None
        return 3 if m.group("patch") is not None else 2

    @classmethod
    def _bump_level(cls, old_version, new_version):
        """Classify a version change as "major", "minor" or "patch", or None
        when it can't be determined. Powers the per-container update policy
        gate (v1.53.0) that caps which bump levels auto-apply.

        Both sides go through _parse_semver, which strips a leading `v` /
        naming prefix. Comparison is on the (major, minor, patch) tuple:
        differing major → "major"; same major, differing minor → "minor";
        same major+minor, differing patch → "patch". Returns None when
        either side is empty / unparseable, or the two are equal — the
        caller treats None as "allow" (fail-open, never skip something we
        couldn't classify)."""
        a = cls._parse_semver(old_version or "")
        b = cls._parse_semver(new_version or "")
        if a is None or b is None:
            return None
        if a[0] != b[0]:
            return "major"
        if a[1] != b[1]:
            return "minor"
        if a[2] != b[2]:
            return "patch"
        return None

    def _list_remote_tags(self, registry, repository):
        """GET /v2/<repo>/tags/list with Bearer token negotiation. Returns
        a list of tag strings, or [] on failure.

        Paginated via the `Link: rel="next"` header. The docstring used to
        say the first ~100 tags were "enough in practice", and on Docker Hub
        that holds — it answers with everything (1385 for `library/postgres`).
        On a registry that paginates it is simply false, and silently so:
        `ghcr.io/home-assistant/home-assistant` returns exactly 100 tags,
        all from 2021, whose highest parseable version is `2021.7.1` while
        the project is on 2025.x. Every major-bump decision there was made
        against a four-year-old view, so the confirmation gate the operator
        opted into never fired. Works on Hub, silently truncates elsewhere —
        which is why it survived. (diun#43, diun#518, diun#653.)
        """
        host = "registry-1.docker.io" if "docker.io" in registry else registry
        host = self._effective_host(host)
        scheme = self._scheme_for(host)
        url = f"{scheme}://{host}/v2/{repository}/tags/list"

        # One crawl per repository per run. A compose stack with five
        # containers from the same image would otherwise walk 44 pages five
        # times over. Cleared at the top of check_all with the other
        # per-run scratch state, so a long-running process never answers
        # from a sweep an hour ago.
        cache = getattr(self, "_tag_list_cache", None)
        if cache is None:
            cache = self._tag_list_cache = {}
        if url in cache:
            return cache[url]

        def _attempt(token=None):
            req = urllib.request.Request(url)
            if token:
                req.add_header("Authorization", token)
            return urllib.request.urlopen(req, timeout=15)

        last_resp_headers = None
        last_token = None
        try:
            with _attempt() as resp:
                data = json.loads(resp.read())
                last_resp_headers = resp.headers
        except urllib.error.HTTPError as e:
            if e.code != 401:
                self._debug(f"  Tag list error: HTTP {e.code} {e.reason}")
                return []
            www_auth = e.headers.get("WWW-Authenticate", "")
            token = self._negotiate_token(www_auth, registry, repository)
            if not token:
                return []
            last_token = token
            try:
                with _attempt(token) as resp:
                    data = json.loads(resp.read())
                    last_resp_headers = resp.headers
            except Exception as e2:
                self._debug(f"  Tag list error after auth: {e2}")
                return []
        except Exception as e:
            self._debug(f"  Tag list error: {e}")
            return []
        tags = list(data.get("tags") or [])

        # Follow `Link: rel="next"` until the registry stops offering one.
        # Bounded, but not tightly: registries hand out the OLDEST tags
        # first, so a low cap truncates at exactly the end that matters.
        # Measured on `ghcr.io/home-assistant/home-assistant` — 44 pages,
        # 4379 tags, 15 seconds — and only the full crawl reaches the
        # current version. A ten-page cap stopped at 2023.5.4 while the
        # project was on 2026.7.4, which is barely better than the single
        # page it replaced. 60 pages leaves headroom over the largest repo
        # found and still stops a runaway or looping registry.
        #
        # The cost is bounded by when this runs: tag listing feeds the
        # major-bump gate, which only fires for a container that actually
        # has an update — not on every check of every container.
        seen_urls = {url}
        page_url, pages = self._next_tag_page(last_resp_headers, host, scheme), 0
        while page_url and pages < 60:
            if page_url in seen_urls:
                break
            seen_urls.add(page_url)
            pages += 1
            try:
                req = urllib.request.Request(page_url)
                if last_token:
                    req.add_header("Authorization", last_token)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    page = json.loads(resp.read())
                    tags.extend(page.get("tags") or [])
                    page_url = self._next_tag_page(resp.headers, host, scheme)
            except Exception as e:
                # A failed continuation is not a failed lookup — keep what
                # we have rather than throwing away the first page too.
                self._debug(f"  Tag list pagination stopped: {e}")
                break
        if pages:
            self._vdebug(f"  Tag list: {len(tags)} tags over {pages + 1} pages")
        cache[url] = tags
        return tags

    @staticmethod
    def _next_tag_page(headers, host, scheme="https"):
        """The absolute URL from a `Link: </v2/...>; rel="next"` header.

        Registries send a path, not a URL, so it has to be joined onto the
        host we asked. Returns None when there is no next page or the header
        is not the shape we expect — an odd Link header should end
        pagination, never redirect the crawl somewhere else.
        """
        try:
            link = headers.get("Link") or ""
        except AttributeError:
            return None
        if 'rel="next"' not in link:
            return None
        start = link.find("<")
        end = link.find(">", start + 1)
        if start < 0 or end < 0:
            return None
        path = link[start + 1:end].strip()
        if path.startswith("http://") or path.startswith("https://"):
            return path if path.startswith(f"{scheme}://{host}/") else None
        if not path.startswith("/"):
            return None
        return f"{scheme}://{host}{path}"

    def _save_advisories(self, advisories):
        """Persist "a newer version exists" notes, host-scoped.

        Own file rather than a flag on the pending list, because everything
        that reads pending updates treats an entry as something to apply.
        A pinned container is not that — it is running what it was asked to
        run, and the note is for the reader, not for the updater.
        """
        path = getattr(self.config, "advisories_file", "")
        if not path:
            return
        try:
            from container_store import atomic_write_json, LOCAL_HOST
            host = getattr(self.backend, "name", LOCAL_HOST) or LOCAL_HOST
            data = {}
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        data = json.load(f) or {}
                except (OSError, ValueError):
                    data = {}
            if not isinstance(data, dict):
                data = {}
            # This host's slice is replaced wholesale; the others are left
            # alone, since each host scans on its own schedule (#7).
            data[host] = advisories
            atomic_write_json(path, data)
        except Exception as e:
            self._debug(f"  (could not save version advisories: {e})")

    def read_advisories(self):
        """All hosts' advisories as {display_name: {...}}."""
        path = getattr(self.config, "advisories_file", "")
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        from container_store import host_key, LOCAL_HOST
        out = {}
        for host, entries in data.items():
            for name, info in (entries or {}).items():
                out[host_key(host, name) if host != LOCAL_HOST else name] = info
        return out

    def _newer_version_available(self, registry, repository, tag):
        """A higher SemVer tag than the one this container is pinned to.

        Returns the tag string, or "" when there is nothing to say — which
        covers the common cases: a moving tag like `:latest` (there is no
        "newer" for it, the digest check already answers that), a tag that
        does not parse as SemVer, and a registry that cannot be listed.

        Deliberately advisory. Nothing downstream may treat this as a
        pending update: the container is running exactly what its compose
        file asks for.

        Within the SAME major version, deliberately. `postgres:16.3`
        should hear about `16.4`, not about `17.0`: a Postgres major is
        not a tag change, it is `pg_upgrade`, and a container that
        swapped the tag alone would not open its old data directory. The
        badge says "there is a newer one", and pointing it at something
        that cannot be taken by changing a tag makes it say the wrong
        thing. Someone at the top of their own line therefore sees
        nothing, which is the honest answer: within what you pinned,
        you are current.

        This applies to the advisory only. `_is_major_bump` asks the same
        function WITHOUT the restriction — its entire job is spotting a
        major, and capping it there would have quietly disabled
        major-confirmation for everyone.
        """
        try:
            if self._parse_semver(tag) is None:
                return ""
            best, best_parsed = self.get_highest_semver_tag(
                registry, repository, tag, same_major=True)
            cur = self._parse_semver(tag)
            if not best_parsed or best_parsed[:3] <= cur[:3]:
                return ""
            return best
        except Exception as e:
            # Never let an advisory lookup break a check that had already
            # succeeded.
            self._debug(f"  (newer-version lookup failed: {str(e)[:80]})")
            return ""

    #: How many major versions ahead a candidate tag may be before it is
    #: read as a different numbering scheme rather than a newer release.
    MAX_PLAUSIBLE_MAJOR_JUMP = 3

    def get_highest_semver_tag(self, registry, repository, current_tag,
                               *, same_major=False):
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
        cur_components = self._semver_components(current_tag)

        tags = self._list_remote_tags(registry, repository)
        candidates = []
        for t in tags:
            ts = t.strip()
            parsed = self._parse_semver(ts)
            if parsed is None:
                continue
            # Prefixes must be EQUAL, not merely a starting substring —
            # and an empty prefix is a prefix, not an absence of one. The
            # old test read `if prefix and not ts.startswith(prefix)`, so a
            # tag beginning with a digit (`4.6.5`, the common case) skipped
            # the check entirely and matched anything the SemVer regex
            # would swallow, including `arm64v8-…` since the pattern allows
            # a leading `something-`.
            #
            # Measured on linuxserver/qbittorrent:4.6.5 before the fix:
            # the highest "matching" tag came back as arm64v8-20.04.1 —
            # an Ubuntu version, on the wrong architecture — and
            # _is_major_bump therefore reported major=True for every
            # ordinary patch update. Anyone running linuxserver images with
            # major-confirmation on was being asked to confirm each one.
            cm = self._SEMVER_RE.match(ts)
            if not cm or ts[:cm.start("major")] != prefix:
                continue
            # The suffix is the same axis as the prefix, at the other end
            # of the tag, and it was getting the same treatment: candidates
            # carrying one were dropped outright, so `nextcloud:29.0.4-apache`
            # was compared against the plain `32.0.13` — a different image
            # variant. Measured. Equality, both ways: an `-apache` tag
            # matches only `-apache` tags, and a bare tag matches only bare
            # ones, which is what kept pre-releases out in the first place.
            if (parsed[3] or "") != (cur[3] or ""):
                continue
            # Same number of components, for the reason in
            # `_semver_components`: `7.2` belongs with `7.4`, not `7.4.1`.
            if self._semver_components(ts) != cur_components:
                continue
            # A four-digit first number is a date, not a release. See
            # _YEARLIKE_MAJOR — precaution, not a measured case.
            if parsed[0] >= self._YEARLIKE_MAJOR:
                continue
            # `same_major` is the advisory's restriction and nothing
            # else's — see _newer_version_available for why, and note
            # that _is_major_bump must NOT pass it or it could never
            # detect the thing it exists to detect.
            if same_major and parsed[0] != cur[0]:
                continue
            candidates.append((parsed, ts))
        if not candidates:
            return None, None
        candidates.sort(reverse=True)
        # Some repositories carry two numbering schemes under the same
        # shape. linuxserver/qbittorrent tags both the application version
        # (4.6.5) and its Ubuntu base (20.04.1); nothing in the tag text
        # tells them apart. Taking the highest would answer "4.6.5 → 20.04.1"
        # and, worse, make _is_major_bump hold every ordinary update for
        # confirmation — measured before this guard.
        #
        # A HEURISTIC, and named as one: a real release series does not
        # leap this far at once. Genuine major jumps are +1, occasionally
        # +2 (radarr 5 → 6 is real and must survive). Anything far beyond
        # that is much more likely to be a second scheme in the same
        # repository. It can be wrong in both directions, which is why it
        # only ever suppresses an advisory or a confirmation prompt and
        # never causes an update to be applied.
        for parsed, tag in candidates:
            if parsed[0] - cur[0] <= self.MAX_PLAUSIBLE_MAJOR_JUMP:
                return tag, parsed
        return None, None

    def _get_local_repo_digests(self, image):
        """RepoDigests as Docker reports them — `repo@sha256:…`, prefix and
        all. Cached per run, so asking for both this and the bare digests
        costs one `docker inspect`, not two.

        The prefix is what makes a logged digest actionable (#53, @LeeNX):
        `gitea/runner@sha256:66d8…` can be pasted straight into
        `docker manifest inspect`; a bare hash next to another bare hash
        can't be checked against anything.
        """
        cached = self._repo_digest_cache.get(image)
        if cached is not None:
            return cached
        digests = []
        try:
            result = self.backend.run(
                ["inspect", "--format", "{{json .RepoDigests}}", image])
            if result.returncode == 0:
                digests = [d for d in json.loads(result.stdout.strip() or "[]")
                           if isinstance(d, str) and "@" in d]
        except (json.JSONDecodeError, TypeError, subprocess.SubprocessError, OSError):
            digests = []
        self._repo_digest_cache[image] = digests
        return digests

    def _get_local_digests(self, image):
        """Get all local image digests from RepoDigests (bare `sha256:…`).

        Signature and contract unchanged — has_selfupdate_available and the
        digest comparison in check_all both compare against the remote
        `Docker-Content-Digest`, which carries no repository prefix.
        """
        return [d.split("@")[1] for d in self._get_local_repo_digests(image)]

    def _get_image_size(self, image):
        """Get local image size in human-readable format."""
        result = self.backend.run(
            ["image", "inspect", "--format", "{{.Size}}", image])
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
        result = self.backend.run(
            ["image", "inspect", "--format", "{{.Created}}", image])
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
            # This one runs outside check_all, on a checker that lives for
            # the whole process — so drop the per-run RepoDigests cache
            # first, or a re-pull between two calls would go unnoticed.
            self._repo_digest_cache = {}
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
        """What `/cleanup` COULD free, in bytes — an upper bound.

        Not a prediction, and the wording of the message says so. The
        runtime's own `system df` is the only source for this and it is
        optimistic in two directions we cannot correct for: it ignores
        the grace period, and on Podman it counted 112 MB on a host with
        exactly one unused image, where the prune then freed 8.5 MB
        (#63, owner-reported, measured on his two hosts).

        IMAGES ONLY, because that is all `/cleanup` prunes. It used to sum
        every row `docker system df` prints, which on a real machine reads
        like this:

            Images          219MB
            Containers      508kB
            Local Volumes  13.93GB
            Build Cache     7.53GB

        — and answered "20.2 GB reclaimable". Then `/cleanup` ran and
        freed nothing, because 13.93 GB of that is VOLUMES, which are
        data and which nothing here will ever delete, and 7.53 GB is
        build cache, which `image prune` does not touch either.

        A dry run that promises twenty gigabytes and delivers zero is
        worse than no dry run: it sends you looking for a bug in the
        cleanup. `reclaimable_breakdown` carries the rest, for a message
        that wants to say where the other space is sitting.

        `reclaimable_breakdown` now RAISES when `system df` fails so the
        `/checkimages` dry-run can report an unreadable host honestly. This
        figure feeds the auto-cleanup disk warning instead, where a host we
        cannot measure means "no reclaim hint / don't act" — so the raise is
        swallowed back to 0 here, and only here.
        """
        try:
            return self.reclaimable_breakdown().get("images", 0)
        except Exception:
            return 0

    @staticmethod
    def _human_bytes(n):
        """`8.5 MB` — one decimal, a space, the unit.

        Ours rather than the runtime's, because Docker prints `8.534MB`
        and a dot is the thousands separator in half of Europe. Reading
        that as 8534 MB is not a mistake; it is what the string says
        there."""
        try:
            n = float(n)
        except (TypeError, ValueError):
            return ""
        if n <= 0:
            return "0 B"
        # Whole numbers up to megabytes — a megabyte is not worth a
        # decimal any more, as the owner put it. Gigabytes keep one,
        # because 0.5 GB is half a gigabyte and rounding it to "0" or "1"
        # loses the half that matters.
        for unit in ("B", "kB", "MB"):
            if n < 1000:
                return f"{n:.0f} {unit}"
            n /= 1000
        for unit in ("GB", "TB"):
            if n < 1000:
                return f"{n:.1f} {unit}"
            n /= 1000
        return f"{n:.1f} PB"

    def grace_holds_back(self):
        """How many unused images the grace period is protecting.

        `(prunable, held, grace_hours)` — COUNTS, not bytes, and that is
        deliberate: image sizes overlap, because layers are shared. Summing
        `docker images` sizes on this machine gives 5.4 GB where
        `system df` correctly reports 224 MB actually reclaimable, so a
        byte figure per image would be a confident lie. The count is exact
        and answers the question that matters: is the cleanup going to do
        anything right now, or is everything still inside its grace?

        This is what turned "224 MB reclaimable" into a cleanup that freed
        nothing: thirty-three unused images on a machine that builds a
        lot, every one younger than the 72-hour CLEANUP_GRACE_HOURS
        (#63, owner-reported).
        """
        grace = int(getattr(self.config, "cleanup_grace_hours", 24) or 24)
        try:
            r = self.backend.run(
                ["images", "--filter", "dangling=true", "--format",
                 "{{.CreatedAt}}"], timeout=20)
            if getattr(r, "returncode", 1) != 0:
                return 0, 0, grace
        except (subprocess.SubprocessError, OSError):
            return 0, 0, grace
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=grace)
        prunable = held = 0
        for line in (r.stdout or "").strip().splitlines():
            when = None
            for fmt in ("%Y-%m-%d %H:%M:%S %z %Z", "%Y-%m-%d %H:%M:%S %z"):
                try:
                    when = datetime.strptime(line.strip(), fmt)
                    break
                except ValueError:
                    continue
            if when is None or when < cutoff:
                prunable += 1        # undatable: assume the prune takes it
            else:
                held += 1
        return prunable, held, grace

    def reclaimable_breakdown(self):
        """`docker system df` per type, in bytes: images, containers,
        volumes, build_cache.

        RAISES when the `system df` command itself fails — a non-zero exit
        (a socket-proxy that blocks `/system/df` answers HTTP 403, so the
        CLI exits 1) or a subprocess error. The human-facing caller
        (`container_flags.reclaimable`) turns that into an explicit "host
        could not be checked" instead of a silent "nothing to reclaim": an
        endpoint we are blocked from reading as "all clean" is the exact
        false-negative the update path guards against too (wud#116,
        wud#419). `reclaimable_bytes` deliberately swallows the raise back
        to 0 for the auto-cleanup path, where "can't read it" means "don't
        act". A run that SUCCEEDS but reports nothing reclaimable returns a
        normal (possibly empty) dict — that is a real zero, not a failure.

        Stage 2 (2.19.0, deferred): compute the reclaimable IMAGE figure
        ourselves from `docker images` + container reference counting
        (IMAGES=1, which a socket-proxy already grants) so the dry-run does
        not depend on `/system/df` at all — see [[project_core_refactor_pending]].
        """
        r = self.backend.run(
            ["system", "df", "--format", "{{json .}}"], timeout=15)
        if getattr(r, "returncode", 1) != 0:
            err = (getattr(r, "stderr", "") or "").strip()
            low = err.lower()
            if "403" in low or "forbidden" in low:
                # Kept short: the human-facing reply clips the error to 80
                # chars, and SYSTEM=1 is the actionable half — it must survive.
                raise RuntimeError(
                    "system df blocked (403) — socket-proxy may need SYSTEM=1")
            raise RuntimeError(err[:70] or "docker system df failed")
        out = {}
        for line in (r.stdout or "").strip().splitlines():
            try:
                d = json.loads(line)
            except (ValueError, TypeError):
                continue
            kind = str(d.get("Type", "")).strip().lower()
            key = {"images": "images", "containers": "containers",
                   "local volumes": "volumes",
                   "build cache": "build_cache"}.get(kind)
            if key:
                out[key] = self._parse_human_size(d.get("Reclaimable", ""))
        return out

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
                    backup_msg = self._t(
                        "cleanup_backed_up", count=len(backed_up),
                        dir=self.config.cleanup_backup_dir)
                self._prune_old_backups()
            except Exception as e:
                self._debug(f"  Backup step failed: {e}")
                # Continue with prune anyway — backups are nice-to-have

        try:
            result = self.backend.image_prune(
                all=True, force=True, until=f"{grace}h", timeout=180)
            if result.returncode != 0:
                return False, self._t("cleanup_failed",
                                      error=result.stderr.strip()[:200])
            lines = result.stdout.strip().split("\n")
            space_line = next((l for l in lines if "reclaimed" in l.lower()), "")
            untagged = sorted({l[len("Untagged: "):].split("@")[0]
                               for l in lines if l.startswith("Untagged: ")})

            if not space_line:
                return True, self._t("cleanup_none") + backup_msg
            # Docker says "Total reclaimed space: 662.9MB" in English no
            # matter what language the reader has set. Keep the number,
            # say the sentence ourselves (#63).
            # Reformatted rather than passed through. Docker writes
            # "8.534MB" — a dot as the decimal point and no space before
            # the unit — which in German reads as 8534 MB, and did: the
            # owner read a 8.5 MB cleanup as 8 GB (#63). One decimal, a
            # space, and the unit says the same thing unambiguously in
            # every language that uses a comma.
            raw = space_line.split(":", 1)[-1].strip() if ":" in space_line \
                else space_line.strip()
            size = self._human_bytes(self._parse_human_size(raw)) or raw
            msg = self._t("cleanup_reclaimed", size=size) + backup_msg
            if untagged:
                preview = ", ".join(untagged[:6])
                if len(untagged) > 6:
                    preview += ", " + self._t("cleanup_more",
                                              count=len(untagged) - 6)
                msg += "\n" + self._t("cleanup_removed", images=preview)
            return True, msg
        except Exception as e:
            return False, self._t("cleanup_error", error=str(e)[:200])

    def _backup_local_unused_images(self):
        """Save unused, locally-built images (no RepoDigests) as tarballs.

        Returns list of (image_id, tarball_path) for what was backed up.
        Images that would be pulled-back-from-registry instead of needing
        local restore are skipped — they're considered "safe to delete".
        """
        os.makedirs(self.config.cleanup_backup_dir, exist_ok=True)

        # IDs of all images currently in use (running or stopped containers)
        ps_result = self.backend.run(
            ["ps", "-a", "--no-trunc", "--format", "{{.ImageID}}"], timeout=30)
        used_ids = {l.strip() for l in ps_result.stdout.strip().split("\n") if l.strip()}

        # All images, full inspect form (need RepoDigests + RepoTags)
        ls_result = self.backend.run(
            ["image", "ls", "-a", "--no-trunc", "--format", "{{.ID}}"], timeout=30)
        all_ids = [l.strip() for l in ls_result.stdout.strip().split("\n") if l.strip()]
        # De-dup (image ls can list same ID multiple times for multiple tags)
        all_ids = list(dict.fromkeys(all_ids))

        backed_up = []
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        run_dir = os.path.join(self.config.cleanup_backup_dir, timestamp)

        for img_id in all_ids:
            if img_id in used_ids:
                continue
            inspect = self.backend.run(
                ["image", "inspect", img_id], timeout=30)
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
            save = self.backend.image_save(img_id, tarball, timeout=600)
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
        result = self.backend.run(
            ["inspect", "--format",
             "{{index .Config.Labels \"com.docker.compose.project\"}}||"
             "{{index .Config.Labels \"com.docker.compose.service\"}}||"
             "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}||"
             "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}",
             name])
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
        """Pinned (frozen) container names **on this checker's host**.

        `pinned_containers.json` is one flat list holding every managed
        host's pins, keyed `nas/nginx` for remote hosts and left bare for
        the local one (#7 — see `container_store.host_key`). Reading it raw
        and matching bare names, as this did, meant a remote host's pins
        were invisible to it while the local host's pins silently applied
        everywhere. The checker knows which host it is — its backend does —
        so it filters to its own entries and hands back plain names, which
        is what `get_running_containers` compares against.

        Still a direct file read rather than a `ContainerStore` call: the
        checker is constructed with a `config` only and has never owned a
        store. `split_host_key` is the same function the store uses, so the
        two cannot drift apart on what a key means.
        """
        from container_store import LOCAL_HOST, split_host_key
        raw = []
        if os.path.exists(self.config.pinned_file):
            try:
                with open(self.config.pinned_file) as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, IOError):
                raw = []
        if not isinstance(raw, list):
            return []
        mine = getattr(self.backend, "name", LOCAL_HOST) or LOCAL_HOST
        out = []
        for key in raw:
            if not isinstance(key, str):
                continue
            host, name = split_host_key(key)
            if host == mine:
                out.append(name)
        return out

    def _mark_inflight(self, name, old_name, image):
        """Record that this container is between its two names.

        Best-effort: a failed write must never stop the update it was
        describing. The window it covers is short but real — stop, rename,
        build the run arguments (which touches the registry), run.
        """
        try:
            from container_store import atomic_write_json
            path = getattr(self.config, "inflight_file", "")
            if path:
                atomic_write_json(path, {
                    "name": name, "old_name": old_name, "image": image,
                    "host": getattr(getattr(self, "backend", None), "name", ""),
                    "ts": time.time(),
                })
        except Exception as e:
            self._debug(f"  Could not journal the in-flight update: {e}")

    def _clear_inflight(self):
        """The swap landed, one way or the other."""
        try:
            path = getattr(self.config, "inflight_file", "")
            if path and os.path.exists(path):
                os.unlink(path)
        except OSError as e:
            self._debug(f"  Could not clear the in-flight marker: {e}")

    def _save_history(self, name, image, success, detail=""):
        """Append an entry to the update history file."""
        from container_store import LOCAL_HOST as _LOCAL
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "container": name,
            "image": image,
            "success": success,
            "detail": detail,
        }
        # Which box this happened on (#7). Only written for remote hosts:
        # an entry without the key reads as local, so every history file
        # written before multi-host stays valid and the Web UI's history
        # page needs no migration.
        _host = getattr(self.backend, "name", _LOCAL) or _LOCAL
        if _host != _LOCAL:
            entry["host"] = _host
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
                r = _cb.default_backend().run(
                    ["inspect", "--format", "{{.Id}}", c], timeout=5)
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
                ps = _cb.default_backend().run(
                    ["ps", "-q", "--no-trunc"], timeout=10)
                ids = [x for x in ps.stdout.split() if x]
                if ids:
                    r = _cb.default_backend().run(
                        ["inspect", "--format",
                         "{{.Id}}|{{.Config.Hostname}}", *ids], timeout=20)
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
            r = _cb.default_backend().run(
                ["inspect", oid], timeout=10)
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
                r = self.backend.run(
                    ["inspect", "--format", "{{.Name}}", c], timeout=5)
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
            r = self.backend.run(
                ["inspect", "--format", "{{.Id}}", target_name], timeout=5)
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
            r = self.backend.run(
                ["inspect", "--format", "{{.Id}}", name], timeout=10)
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
        #
        # Every step guarded, because this is the last line of defence and
        # it used to be the one place with none. `_update_standalone` calls
        # this from its `except Exception:` handler — so a timeout raised
        # in here escaped that handler, skipping the rename, the history
        # write and the in-flight clear, and left the container as
        # `<name>_old` with nothing under its own name. The same shape as
        # #2, arriving through the code meant to prevent it.
        try:
            self.backend.rm(name, force=True,
                            timeout=self._lifecycle_timeout())
        except subprocess.SubprocessError as e:
            # Not fatal on its own: there may be nothing at `name` at all,
            # and the rename below will say so if it matters.
            self._debug(f"  Rollback: removing {name} failed ({e}) — "
                        f"trying the rename anyway")
        if not self._rename_container(old_name, name):
            self._debug(f"  Rollback: could not restore {old_name} → {name}")
            return False
        try:
            start = self.backend.start(name,
                                       timeout=self._lifecycle_timeout())
            ok = start.returncode == 0
        except subprocess.SubprocessError as e:
            # Restored but not started. Say so honestly rather than
            # claiming a rollback that left the service down — and never
            # raise, or the caller's own cleanup is skipped again.
            self._debug(f"  Rollback: restored {name} but start failed ({e})")
            ok = False
        self._debug(f"  Rollback: restored {old_name} → {name} "
                    f"({'started' if ok else 'start failed'})")
        return ok

    def _rename_container(self, src, dst):
        """Rename `src` to `dst`. Returns True if it ended up renamed.

        A timeout here is OURS, not the daemon's. Docker carries on after
        we stop waiting, so "the command timed out" and "the rename did
        not happen" are different statements — and treating the first as
        the second is what cost @famewolf ten days (#2). His log:

            Fixing dependent gluetun-nzbhydra2 crashed: Command 'docker
            rename gluetun-nzbhydra2 gluetun-nzbhydra2_old' timed out
            after 10 seconds

        The rename then completed. The exception escaped the recreate, so
        the container was never rebuilt and never rolled back, and from
        then on it existed only as `<name>_old` while every later run
        tried to restart a name that was gone.

        So: on a timeout, look at what is actually there before deciding.
        """
        try:
            r = self.backend.rename(src, dst, timeout=self._lifecycle_timeout())
            return getattr(r, "returncode", 1) == 0
        except subprocess.TimeoutExpired:
            # Give the daemon the moment it needed, then check reality.
            #
            # NOT through `_container_exists`: that answers "probably yes"
            # when its own inspect fails, which is the right instinct for
            # its own job and exactly wrong here. The daemon being too busy
            # to answer is the very condition that got us into this branch,
            # so both probes could fail and "exists AND not exists" would
            # read as "the rename did not happen" — reporting failure for
            # a rename that worked, which is the bug this method exists to
            # prevent. `_container_probe` says "I could not tell" instead.
            time.sleep(2)
            done = self._renamed(src, dst)
            if done is None:
                time.sleep(5)
                done = self._renamed(src, dst)
            self._debug(f"  Rename {src}→{dst} timed out; "
                        f"actually renamed: {done}")
            # Only claim success on a positive observation. "Could not
            # tell" counts as failure, and the caller says so — a
            # dependent left as `<name>_old` by that is picked back up by
            # `recover_dependent` on the next run, so being wrong here
            # costs a cycle rather than the container.
            return done is True
        except subprocess.SubprocessError as e:
            self._debug(f"  Rename {src}→{dst} failed: {e}")
            return False

    def _container_probe(self, name):
        """True, False, or None when the daemon would not say.

        `_container_exists` collapses "I could not ask" into "it exists",
        which is the safe answer for deciding whether to recreate and the
        wrong one for deciding whether a rename completed. This keeps the
        three states apart so a caller can tell not-there from don't-know.
        """
        try:
            r = self.backend.run(["inspect", "--format", "{{.Id}}", name],
                                 timeout=self._lifecycle_timeout())
            return r.returncode == 0
        except subprocess.SubprocessError:
            return None

    def _renamed(self, src, dst):
        """Did `src` end up as `dst`? True / False / None if unknowable."""
        there = self._container_probe(dst)
        gone = self._container_probe(src)
        if there is None or gone is None:
            return None
        return there and not gone

    #: A writable layer past this is no longer "caches and temp files" —
    #: it is an application storing data somewhere a recreate destroys.
    LAYER_WARN_BYTES = 500 * 1000 * 1000

    def _layer_farewell(self, name):
        """"⚠ N GB had been written inside the old container…" — or "".

        Measured BEFORE the recreate destroys the writable layer (13 ms,
        `inspect --size`), because afterwards the evidence is gone with
        the layer. Threshold, not always: a few MB of caches is the
        normal state of the world and not worth a warning. Shared by the
        standalone and compose paths — both destroy the layer the same
        way.
        """
        try:
            r = self.backend.run(["inspect", "--size", "--format",
                                  "{{.SizeRw}}", name], timeout=15)
            raw = (getattr(r, "stdout", "") or "").strip()
            if getattr(r, "returncode", 1) == 0 and \
                    raw.lstrip("-").isdigit():
                b = int(raw)
                if b >= self.LAYER_WARN_BYTES:
                    return (f"\n⚠ {b / 1e9:.1f} GB had been written "
                            f"inside the old container (not in a volume) "
                            f"— discarded with it. If that was data, it "
                            f"belongs in a volume.")
        except Exception:
            pass
        return ""

    def _lifecycle_timeout(self):
        """How long to let `kill` / `rm -f` / `rename` run before giving up.

        These were hard-coded at 15 seconds and that is not enough for a
        big container: @famewolf hit `docker kill ollama` timing out at 15s
        again and again, and the same on `docker rm -f` for byparr and
        metube (#2). A model loaded in VRAM, a slow storage driver or a
        busy daemon all make the reap take longer than the command that
        asks for it.

        Derived from `DOCKER_STOP_TIMEOUT` rather than a second setting,
        because someone whose containers are slow to stop has already
        raised that one and would have to discover this separately. Never
        below 30 seconds — double the old value even for a default
        install, since the reports came from defaults.
        """
        floor = int(getattr(self.config, "docker_stop_timeout", 60) or 60)
        return max(30, floor)

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
                r = self.backend.run(
                    ["inspect", "--format", "{{.Config.StopTimeout}}", name], timeout=5)
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
            r = self.backend.stop(name, time=effective_stop,
                                  timeout=subprocess_timeout)
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
            kill = self.backend.kill(name, timeout=self._lifecycle_timeout())
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
            r = self.backend.run(
                ["inspect",
                 "--format", "{{if .Config.Healthcheck}}{{.Config.Healthcheck.StartPeriod}}{{end}}",
                 name], timeout=5)
            if r.returncode != 0:
                return 0.0
            raw = r.stdout.strip()
            if not raw or raw == "0":
                return 0.0
            return float(int(raw)) / 1e9
        except (subprocess.SubprocessError, ValueError):
            return 0.0

    def _health_output(self, name, entries=2):
        """What the *healthcheck* said, which is not what the container said.

        A failed health check used to be reported with a tail of the
        container's stdout — and for a container that starts perfectly
        and then fails its probe, that tail looks immaculate. The owner
        hit exactly that on `ollama`: rolled back for
        `health=unhealthy`, with ten lines of a textbook-clean startup
        underneath it and nothing to act on.

        The probe's own output lives in `.State.Health.Log[].Output`,
        with the exit code of the command Docker ran. That is the thing
        that failed, so that is the thing to show.

        Returns "" when there is nothing to say — no healthcheck, a
        runtime that does not report one, an inspect that would not
        answer. Podman fills the same field, under `Healthcheck` on
        older versions, so both spellings are tried.
        """
        import json as _json
        for field in (".State.Health", ".State.Healthcheck"):
            try:
                r = self.backend.run(
                    ["inspect", "--format", "{{json " + field + "}}", name],
                    timeout=10)
            except (subprocess.SubprocessError, OSError):
                continue
            raw = (getattr(r, "stdout", "") or "").strip()
            if getattr(r, "returncode", 1) != 0 or raw in ("", "null", "<no value>"):
                continue
            try:
                data = _json.loads(raw)
            except ValueError:
                continue
            log = (data or {}).get("Log") or []
            out = []
            for entry in log[-entries:]:
                text = " ".join(str(entry.get("Output", "")).split())
                code = entry.get("ExitCode")
                if text:
                    out.append(f"exit {code}: {text[:300]}")
                elif code not in (None, 0):
                    out.append(f"exit {code} (no output)")
            if out:
                return "\n".join(out)
        return ""

    def _tail_logs(self, name, lines=10, *, none_on_error=False):
        """Return the last N log lines as a single string, trimmed for
        Telegram. Best-effort — failures return empty string. Used to
        attach diagnostic context to health-check warnings so the user
        can see in chat what the container was last doing instead of
        having to SSH to the host.

        `none_on_error=True` returns None on a real fetch failure — a
        non-zero `docker logs` exit (host unreachable, container gone) or
        a subprocess error — so the caller can tell it apart from a
        container that ran but logged nothing (which returns ""). The
        mass-death digest needs the distinction: reporting a silent
        container as "host unreachable" is a scary false alarm (#63,
        @famewolf). The default path is unchanged for every other caller.
        """
        try:
            # The comment that used to sit here said "docker logs
            # interleaves stdout+stderr — combine both" and then
            # concatenated the two captured streams. That is not
            # interleaving: it is all of stdout followed by all of
            # stderr, so a container writing to both had its diagnostic
            # context reordered out of the sequence it happened in —
            # exactly the wrong thing for a crash report. `backend.logs()`
            # merges them at the pipe, in the order the container wrote.
            r = self.backend.logs(name, tail=lines, timeout=10)
            if none_on_error and getattr(r, "returncode", 0) != 0:
                # A failed fetch (unreachable host, no such container) — not
                # a silent one. Only surfaced when the caller asked to tell
                # them apart; the default path keeps its old behaviour.
                return None
            text = (r.stdout or "") + (r.stderr or "")
            text = text.strip()
            if not text:
                return ""
            text = self._collapse_repeats(text)
            # Hard cap: ~1500 chars so the Telegram message stays under
            # the 4096-char limit even with other fields stuffed in.
            if len(text) > 1500:
                text = "…" + text[-1500:]
            return text
        except subprocess.SubprocessError:
            return None if none_on_error else ""

    @staticmethod
    def _state_note(state, health):
        """`state=restarting` or `state=running, health=unhealthy`.

        A container without a health probe has no health to report, and
        the bare `health=` we used to print read like a broken template
        rather than like "there is nothing here" (#63, the owner's tika
        rollback, which is exactly that case).
        """
        return f"state={state}" + (f", health={health}" if health else "")

    @staticmethod
    def _collapse_repeats(text):
        """Fold a repeating tail into one copy, counted.

        A crash-restart loop writes the same thing on every attempt, so
        the log tail is one stack trace five times over — which is how
        the owner's `tika` rollback filled a phone screen with a single
        error (#63). Folding it keeps the whole diagnostic and leaves
        room for the lines that differ, which are the ones worth reading.

        Blocks, not lines: the repeat is `Error: …` / `Caused by: …` /
        `…ClassNotFoundException: …` over and over, and a line-by-line
        fold sees no two identical lines in a row at all.
        """
        lines = (text or "").split("\n")
        n = len(lines)
        if n < 4:
            return text
        # Smallest block that the tail repeats — smallest, so the count is
        # as high as it can honestly be.
        for size in range(1, n // 2 + 1):
            block = lines[-size:]
            reps = 1
            while (reps + 1) * size <= n and \
                    lines[-(reps + 1) * size:-reps * size] == block:
                reps += 1
            if reps >= 3:
                head = lines[:n - reps * size]
                folded = block + [f"   (the {size} line(s) above repeated "
                                  f"{reps}× — folded)"]
                return "\n".join(head + folded).strip()
        return text

    def _restart_count(self, name):
        """Current Docker RestartCount for a container (0 on any error).

        Used by _wait_healthy to detect a post-update crash loop: a
        container whose main process keeps exiting and getting revived
        by its restart policy. A healthcheck stuck in "starting" would
        otherwise hide this and we'd report a broken update as success.
        """
        rc = self.backend.run(
            ["inspect", "--format", "{{.RestartCount}}", name])
        try:
            return int(rc.stdout.strip())
        except (ValueError, AttributeError):
            return 0

    def _image_id(self, image):
        """Resolved image ID (sha256:...) for an image reference, or ''."""
        r = self.backend.run(
            ["image", "inspect", "--format", "{{.Id}}", image])
        return r.stdout.strip() if r.returncode == 0 else ""

    def _container_image_id(self, name):
        """Image ID the named container is actually running, or ''.

        Returns '' on any failure — since #53 this runs for every container
        on every check sweep (not just during an update), so an inspect
        hiccup must degrade to '' and let check_all fall back to the tag
        image, never abort the whole run. `_verify_running_image` already
        fail-opens on ''.
        """
        try:
            r = self.backend.run(
                ["inspect", "--format", "{{.Image}}", name])
        except (subprocess.SubprocessError, OSError):
            return ""
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
        r = self.backend.run(
            ["inspect", "--format", "{{.HostConfig.NetworkMode}}", name])
        if r.returncode != 0:
            return None
        nm = r.stdout.strip()
        if not nm.startswith("container:"):
            return None
        ref = nm.split(":", 1)[1]
        rr = self.backend.run(
            ["inspect", "--format", "{{.Name}}", ref])
        if rr.returncode != 0:
            return None
        return rr.stdout.strip().lstrip("/") or None

    def recover_dependent(self, name):
        """Put `<name>_old` back as `<name>` and start it. True if healed.

        The other half of the fix for #2. A recreate interrupted between
        the rename and the rebuild leaves the container under `<name>_old`
        and nothing under `<name>`, and until now nothing ever looked
        again: every later run saw no such container, tried a restart, and
        failed with the same line. Ten days, for @famewolf.

        `recovery.py` already heals exactly this shape for the main update
        path, but it is driven by an in-flight note that only that path
        writes — so the dependent recreate was never covered by it. This
        is the same repair, decided from what is on the machine rather
        than from a note.

        Careful about what it will touch: only `<name>_old`, only when
        `<name>` itself is absent, and only for a container the caller
        already knows is a group dependent. A stray `*_old` belonging to
        somebody else is none of our business.
        """
        old_name = f"{name}_old"
        if self._container_exists(name) or not self._container_exists(old_name):
            return False
        if not self._rename_container(old_name, name):
            return False
        try:
            self.backend.run(["start", name], timeout=self._lifecycle_timeout())
        except subprocess.SubprocessError as e:
            self._debug(f"  Recovered {name} but could not start it: {e}")
        return True

    def recreate_dependent(self, name, netns_name):
        """Recreate a netns-sharing group dependent in place, rejoining the
        head's CURRENT container via `container:<netns_name>`. After the head
        is recreated (new ID) a plain `docker restart` of the sidecar fails —
        it still references the dead old ID (#8). This rebuilds from inspect
        with the SAME image (no pull, no version change), backing up the old
        container first and rolling back on failure. Returns (ok, detail)."""
        # Capture config up front so even an AutoRemove (--rm) container —
        # which vanishes on stop — can still be rebuilt.
        insp = self.backend.run(["inspect", name])
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
        self.backend.rm(old_name, force=True, timeout=self._lifecycle_timeout())
        self._stop_container(name)
        # Back up by renaming — only if it survived the stop (AutoRemove may
        # have deleted it, in which case we recreate straight from `config`).
        #
        # Through `_rename_container`, which survives a timeout and checks
        # what actually happened. Called bare, a `TimeoutExpired` escaped
        # this whole function: no rebuild, no rollback, and the container
        # left under `<name>_old` for good (#2, @famewolf).
        if self._container_exists(name):
            if not self._rename_container(name, old_name):
                return False, ("could not rename to " + old_name +
                               " — container left untouched")

        cmd = self._build_run_args(config, image, name,
                                   self._get_image_defaults(image),
                                   netns_name=netns_name,
                                   inherited=self._image_config(config.get("Image")),
                                   cgroup_version=self._cgroup_version(self.backend))
        # _build_run_args keeps returning argv that starts with the CLI name
        # (tests assert on it directly); the backend prepends its own → [1:].
        run = self.backend.run(cmd[1:], timeout=120)
        if run.returncode != 0:
            err = run.stderr.strip()[:200]
            self._debug(f"  Dependent recreate failed for {name}: {err}")
            self._rollback_to_old(name, old_name)
            return False, err
        self.backend.rm(old_name, force=True, timeout=self._lifecycle_timeout())
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
            sc = self.backend.run(
                ["inspect", "--format", "{{.State.Status}}", name])
            state = sc.stdout.strip() if sc.returncode == 0 else ""
            hc = self.backend.run(
                ["inspect", "--format", "{{.State.Health.Status}}", name])
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

    def check_all(self, bot=None, only=None):
        """Check running containers for image updates.

        ``only`` (set/list of container names) scopes the run to those
        containers — the Web UI's per-container check button. Names that
        aren't currently running are simply skipped. ``only=None`` keeps
        the old behaviour: check everything.
        """
        self.debug_log = []
        self._repo_digest_cache = {}
        self._tag_list_cache = {}
        self._token_cache = {}
        diag = self._diag_on()
        # Resolving a digest to its version means GETting 2-3 manifests plus
        # a config blob per container, and GETs — unlike the HEAD the digest
        # check itself uses — count against Docker Hub's anonymous budget of
        # 100 per hour. Thirty containers on a quarter-hourly cron would be
        # ~240 requests an hour: Docksentry would rate-limit itself into 429s
        # and start missing updates for real, which is the exact failure it
        # is supposed to explain. So it runs only for an explicit
        # single-container check, or when someone turned DEBUG on.
        resolve_versions = only is not None or diag
        # …and where the answer goes. An explicit single-container check is a
        # deliberate act that already spent that container's quota, so its
        # result belongs in the log whether or not DEBUG is on — resolving a
        # version and then printing nothing would be paying for an answer and
        # throwing it away. In a full sweep the same line stays behind DEBUG,
        # where it only runs because someone asked for the noise.
        _vlog = self._debug if only is not None else self._vdebug
        if diag:
            self._vdebug(f"Environment: {self._registry_environment()}")
        containers = self.get_running_containers()
        if only is not None:
            wanted = set(only)
            containers = [c for c in containers if c["name"] in wanted]
            self._debug(f"Scoped check: {len(containers)} of {len(wanted)} "
                        f"requested container(s) are running")
        self._debug(f"Checking {len(containers)} containers for updates...")
        updates = []
        #: name -> {"current": tag, "newer": tag}. Advisory only: a pinned
        #: version tag is doing exactly what its compose file asks for, and
        #: nothing downstream may promote these into pending updates.
        advisories = {}
        #: Containers whose registry check could not be completed this run.
        #: Their previously-known pending entries are carried over rather
        #: than dropped — see the write below.
        failed_checks = set()

        for c in containers:
            # One container per try. A slow `docker image inspect`
            # raises TimeoutExpired out of an unguarded helper, and
            # without this it took the WHOLE sweep with it — every
            # container already checked, results discarded, and the
            # pending file never written. `_process_update_batch` has
            # guarded each container since it was written; the check
            # loop never did (wud#490, wud#422, wud#551, wud#658).
            try:
                # Reset per container: without this a later failure with no
                # detail would inherit the previous container's reason.
                self._last_registry_error = ""
                image = c["image"]
                registry, repository, tag = self._parse_image(image)
                if not registry:
                    reason = "pinned by digest" if "@" in image else "unparseable"
                    self._trace(f"  Skipped ({reason}): {c['name']} ({image})")
                    continue

                self._trace(f"  Checking: {c['name']} ({registry}/{repository}:{tag})")

                # #53 (@LeeNX): compare the image the container is ACTUALLY
                # running against the registry — NOT whatever the tag currently
                # resolves to. When someone pulls `:latest` forward but never
                # recreates the container, the tag and the running image drift
                # apart: the tag-based check then reads tag==remote and reports
                # "up to date" while the container keeps running the old image,
                # so Docksentry stays blind to a real available update. The Web
                # UI already keys on the running image ID (#46) — the check has
                # to do the same. `run_id` is the container's `.Image` (a
                # sha256:… ID); its RepoDigests are the local side of the
                # comparison.
                run_id = self._container_image_id(c["name"])
                running_digests = self._get_local_digests(run_id) if run_id else []
                if running_digests:
                    local_digests = running_digests
                    local_ref = run_id
                else:
                    # The running image carries no RepoDigests — it was built
                    # locally, or the tag that produced it was since removed /
                    # moved so the old image is now digestless — or we couldn't
                    # read the running image ID at all.
                    #
                    # Before falling back to the tag we still catch the exact #53
                    # (@LeeNX) shape the digest check above misses: `:latest` was
                    # pulled forward to a NEW image but the container was never
                    # recreated, so the OLD image it keeps running lost its
                    # RepoDigest (it's now dangling) and can't be compared by
                    # digest. We can still compare IMAGE IDs. If the TAG is a real
                    # registry image (it has RepoDigests — so a newer image really
                    # is pullable, this isn't a locally-built tag the user rebuilds
                    # but never recreates) AND both image IDs are readable AND the
                    # container runs a DIFFERENT id than the tag now points at,
                    # then the container is simply behind the tag. The newer image
                    # is already local (the tag has it); only a recreate is
                    # missing → report the update. Old side = the running image,
                    # new side = the tag / remote.
                    tag_id = self._image_id(image)
                    tag_repo_digests = self._get_local_repo_digests(image)
                    if tag_repo_digests and run_id and tag_id and run_id != tag_id:
                        local_ref = run_id
                        if self._diag_on():
                            run_desc = (self._get_image_version_label(run_id)
                                        or self._short(run_id, 19))
                            tag_desc = (self._get_image_version_label(image)
                                        or self._short(tag_id, 19))
                            self._vdebug(
                                f"  Running image {run_desc} differs from the tag "
                                f"image {tag_desc} and has no digest to compare — "
                                f"container is behind the tag (not recreated after "
                                f"the tag moved), recreate needed (#53)")
                        # Same shape as the normal UPDATE-AVAILABLE branch below,
                        # but keyed on the running image: size/created/version come
                        # from what the container actually runs, the new version
                        # from the remote's OCI metadata.
                        size = self._get_image_size(local_ref)
                        created = self._get_image_created(local_ref)
                        self._debug(f"  → UPDATE AVAILABLE (current: {created}, size: {size})")
                        c["size"] = size
                        c["created"] = created
                        old_v = self._get_image_version_label(local_ref)
                        if not old_v and self._parse_semver(tag):
                            old_v = tag
                        c["old_version"] = old_v
                        meta = self.get_remote_image_meta(registry, repository, tag)
                        if meta.get("version"):
                            c["new_version"] = meta["version"]
                        if meta.get("created"):
                            c["new_created"] = meta["created"]
                        if resolve_versions:
                            _vlog(f"    remote :{tag} is version "
                                  f"{meta.get('version') or '?'} "
                                  f"(built {meta.get('created') or '?'}), "
                                  f"local is {old_v or '?'}")
                        updates.append(c)
                        continue
                    # Fall back to the tag image (today's behaviour) rather than
                    # risk a false "update available": a missing digest turning
                    # into a phantom update would be noise for every user, strictly
                    # worse than the status quo. This covers a locally-built tag
                    # (no RepoDigests → nothing to pull), a container already on the
                    # tag image (run_id == tag_id), and an unreadable image id.
                    local_digests = self._get_local_digests(image)
                    local_ref = image
                    if run_id:
                        self._vdebug(f"  Running image {self._short(run_id, 19)} has "
                                     f"no repo digest — falling back to the tag {image} "
                                     f"for {c['name']}")

                if not local_digests:
                    self._debug(f"  Skipped (no local digest): {c['name']}")
                    continue

                remote_digest = self._get_remote_digest(registry, repository, tag)

                # Full digests, with the repository prefix Docker reports them
                # under. Truncating to 30 chars saved a line break and cost the
                # reader any way of checking the value against
                # `docker manifest inspect` (#53, @LeeNX). The prefix falls back
                # to the parsed repository if RepoDigests is unavailable.
                local_shown = (self._get_local_repo_digests(local_ref)
                               or [f"{repository}@{d}" for d in local_digests])
                self._debug(f"  Local:  {', '.join(local_shown)}")
                self._debug(f"  Remote: {remote_digest or 'FAILED'}")

                # Spell out a tag/running-image divergence — the #53 failure
                # class itself — in one human-readable line. Gated behind DEBUG
                # (the same gate as the rest of the #53 diagnostics) and only
                # emitted when the running image really differs from what the tag
                # points at, so the normal case stays silent.
                if self._diag_on() and local_ref != image:
                    tag_digests = self._get_local_digests(image)
                    if set(local_digests) != set(tag_digests):
                        run_desc = (self._get_image_version_label(local_ref)
                                    or self._short(run_id, 19))
                        tag_desc = (self._get_image_version_label(image)
                                    or (self._short(tag_digests[0], 19) if tag_digests else "?"))
                        self._vdebug(f"  Container runs {run_desc}, but tag {image} "
                                     f"points at {tag_desc} — container was not "
                                     f"recreated after the tag moved (#53)")

                if not remote_digest:
                    # Treat unknown as unknown — don't claim "up to date" when we
                    # couldn't actually reach the registry. An empty string (a 200
                    # manifest with no Docker-Content-Digest header) is a failure
                    # too, not a real digest that would spuriously mismatch.
                    why = getattr(self, "_last_registry_error", "") or "reason unknown"
                    self._debug(f"  → Check FAILED: {why}")
                    # Remember it. A full scan rewrites this host's slice of the
                    # pending file from `updates` alone, so a container whose
                    # check failed silently LOSES an update we already knew
                    # about — the Web UI badge and the update button vanish and
                    # the next report says everything is current. Being unable
                    # to check is not evidence that nothing is pending
                    # (wud#116, wud#419, wud#945).
                    failed_checks.add(c["name"])
                    continue

                if remote_digest not in local_digests:
                    # Size / created / version describe the image the container
                    # is running (local_ref) — in the #53 divergence case the tag
                    # image is the NEW one, so reading the tag here would print
                    # e.g. "2.3.0 → 2.3.0". In the normal case local_ref is the
                    # tag image, so this is unchanged.
                    size = self._get_image_size(local_ref)
                    created = self._get_image_created(local_ref)
                    self._debug(f"  → UPDATE AVAILABLE (current: {created}, size: {size})")
                    c["size"] = size
                    c["created"] = created
                    # Version info for the "Updates Available" notification (#44):
                    # old from the local OCI label (falling back to a SemVer tag),
                    # new from the remote image's OCI config. Best-effort, and
                    # only for containers that actually have an update.
                    old_v = self._get_image_version_label(local_ref)
                    if not old_v and self._parse_semver(tag):
                        old_v = tag
                    c["old_version"] = old_v
                    meta = self.get_remote_image_meta(registry, repository, tag)
                    if meta.get("version"):
                        c["new_version"] = meta["version"]
                    if meta.get("created"):
                        c["new_created"] = meta["created"]
                    if resolve_versions:
                        # The same line the up-to-date branch prints, so both
                        # verdicts can be read the same way.
                        _vlog(f"    remote :{tag} is version "
                              f"{meta.get('version') or '?'} "
                              f"(built {meta.get('created') or '?'}), "
                              f"local is {old_v or '?'}")
                    updates.append(c)
                else:
                    self._debug("  → Up to date")
                    if diag:
                        # Local, so free: what the digest above actually is.
                        self._vdebug(f"    local image built {self._get_image_created(local_ref)}"
                                     f", size {self._get_image_size(local_ref)}")
                    if resolve_versions:
                        # THE point of #53: "up to date" reads like a claim you
                        # have to take on faith as long as the log shows nothing
                        # but a hash. Naming the version behind it — 66d8096… is
                        # 2.3.0 — settles the question in one line.
                        meta = self.get_remote_image_meta(registry, repository, tag)
                        if meta.get("version"):
                            _vlog(f"    remote :{tag} is version {meta['version']}"
                                  f" (built {meta.get('created') or '?'})")
                        elif meta.get("created"):
                            _vlog(f"    remote :{tag} carries no version label"
                                  f" (built {meta['created']})")
                        else:
                            _vlog(f"    remote :{tag} version could not be read")

                    # A pinned version tag is immutable, so its digest never
                    # moves and this branch is reached forever — including
                    # long after a newer version has shipped. "Up to date"
                    # then means "this tag has not been rebuilt", which is
                    # not what a reader hears (#33, @LeeNX, who asked for
                    # exactly this to be explained and was answered on a
                    # different question).
                    #
                    # Reported, never acted on: pinning 1.25.3 is a
                    # statement of intent, and jumping to 1.26 unasked would
                    # override it. The tag listing costs nothing against
                    # Docker Hub's pull budget — measured: /tags/list
                    # returns no ratelimit headers at all and leaves the
                    # manifest budget untouched at 100/hour — and is cached
                    # per repository per run, so twenty containers from five
                    # repositories cost five listings.
                    newer = self._newer_version_available(registry, repository, tag)
                    if newer:
                        advisories[c["name"]] = {"current": tag, "newer": newer}
                        self._debug(f"  → Up to date on :{tag}, but {newer} exists")

            except Exception as e:
                # Treated exactly like a failed registry check: the
                # container keeps whatever pending entry it already
                # had, because being unable to look is not evidence
                # that nothing is pending.
                self._debug(f"  → Check FAILED: {e}")
                failed_checks.add(c["name"])
                continue
        # Advisories are written on a full scan only. A single-container
        # check knows nothing about the other containers, and replacing the
        # whole file from it would erase every other entry.
        if only is None:
            self._save_advisories(advisories)

        # Save pending updates — atomic write (v1.22.1)
        from container_store import atomic_write_json, LOCAL_HOST
        # Which host these updates are about (#7). Single-host installs
        # tag everything "local", which is also what an entry written by
        # an older version (no `host` key at all) is read as — so the file
        # format is unchanged in practice and needs no migration.
        host_name = getattr(self.backend, "name", LOCAL_HOST) or LOCAL_HOST
        for u in updates:
            u["host"] = host_name

        def _host_of(entry):
            return entry.get("host") or LOCAL_HOST

        def _read_pending():
            if not os.path.exists(self.config.pending_file):
                return []
            try:
                with open(self.config.pending_file) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                return []
            return data if isinstance(data, list) else []

        if only is None:
            # A full scan is authoritative for THIS host only. With several
            # hosts managed, each one scans on its own schedule, so wiping
            # the whole file here would delete the other hosts' pending
            # updates — and with them their Web UI badges and buttons.
            prev = _read_pending()
            others = [u for u in prev
                      if isinstance(u, dict) and _host_of(u) != host_name]
            # Carry over what we could not re-verify. Without this, one 429
            # from Docker Hub is enough to make a real pending update
            # disappear from the UI until the next successful sweep.
            found = {u.get("name") for u in updates}
            carried = [u for u in prev
                       if isinstance(u, dict) and _host_of(u) == host_name
                       and u.get("name") in failed_checks
                       and u.get("name") not in found]
            if carried:
                self._debug(f"  Kept {len(carried)} pending update(s) whose "
                            f"check could not be completed this run")
            atomic_write_json(self.config.pending_file,
                              others + updates + carried)
        else:
            # A scoped run only knows about the containers it actually
            # checked. Writing `updates` wholesale here would wipe every
            # OTHER container's pending entry — and with it its update
            # badge and update button in the Web UI. So: replace the
            # entries of the checked names, pass the rest through
            # untouched. A missing/corrupt file is treated as empty, same
            # as everywhere else that reads this file.
            checked = {c["name"] for c in containers}
            # Only OUR host's entries for the checked names get replaced —
            # another host may legitimately run a container of the same name.
            merged = [u for u in _read_pending()
                      if isinstance(u, dict)
                      and not (_host_of(u) == host_name
                               and u.get("name") in checked)]
            merged.extend(updates)
            atomic_write_json(self.config.pending_file, merged)
        self._debug(f"Found {len(updates)} updates.")

        # Send the debug trace to Telegram ONLY when a check actually
        # failed (#63). With debug on, dumping the full registry HTTP trace
        # of every container on every check turned a routine /check into
        # pages of code blocks — the owner hit exactly that on an instance
        # with debug left on, 21 containers deep. A local-only image with
        # no registry is a clean "Skipped (no local digest)", NOT a
        # failure (measured), so a normal check stays silent; the trace
        # comes through only when there is a real registry / auth / network
        # problem to diagnose, which is the only time anyone wanted it. The
        # full log is in the console (`docker logs`) and the Web UI Logs
        # page regardless, so nothing is lost by holding it back here.
        if self.config.debug and bot and self.debug_log and failed_checks:
            log_text = "\n".join(self.debug_log)
            # Split into chunks if too long
            while log_text:
                chunk = log_text[:3500]
                log_text = log_text[3500:]
                bot.send_message(f"```\n{chunk}\n```")

        return updates

    def is_monitor_only(self, name, labels=None):
        """Whether this container is watched but must never be updated.

        Quadlets and other systemd-managed containers are the case that
        prompted it (#55, @LeeNX): systemd owns them, and recreating one
        behind its back leaves two things with an opinion about what should
        be running. The existing exits all mean "stop looking" — `pin`,
        `enable=false` and `exclude` drop the container from the scan
        entirely, so you lose the version and update information that is
        the whole reason for watching it. This one means "look, report,
        never touch".

        `MONITOR_ONLY_CONTAINERS` takes wildcard patterns for people who
        cannot easily add labels — a quadlet unit file can carry `Label=`,
        but "edit every unit file" is no answer for a fleet. The
        `docksentry.monitor-only` label wins where it is set, matching how
        every other `docksentry.*` label behaves.
        """
        lab = self.label_bool(labels, "monitor-only") if labels else None
        if lab is not None:
            return lab
        return name_matches(name, getattr(self.config, "monitor_only_containers", []))

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

        # Second backstop, same shape and for the same reason: every caller
        # passes through here, so one check covers the scheduler, the Web UI
        # button, the bots and anything added later. A monitor-only
        # container must not be updated by ANY route — not automatically,
        # not by someone clicking. "Never automatically" would be a
        # half-exception, and the update is wrong no matter who asks for it
        # (#55, @LeeNX).
        try:
            if self.is_monitor_only(name, self.get_container_labels(name)):
                msg = ("Refused: this container is monitor-only "
                       "(MONITOR_ONLY_CONTAINERS or docksentry.monitor-only) "
                       "— Docksentry watches it but never updates it")
                self._debug(f"  REFUSED: {name} is monitor-only")
                self._save_history(name, image, False, msg)
                return False, msg
        except Exception as e:
            # A failure to read labels must not silently ALLOW an update
            # that the operator asked to be impossible — but neither should
            # it break updates for everyone else, so fall back to the
            # pattern list alone, which needs no daemon call.
            self._debug(f"  monitor-only label check failed ({e}); using patterns only")
            if self.is_monitor_only(name):
                msg = "Refused: this container is monitor-only"
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

    @staticmethod
    def _compose_files(config_file):
        """The compose files behind `config_files`, as a list.

        Docker joins multiple compose files into ONE label value separated
        by commas — the canonical `docker-compose.yml` plus an
        `override.yml` produces `/a/one.yml,/a/two.yml`. Treated as a single
        path, `os.path.isfile()` says no, and the stack silently drops out
        of the compose path into the standalone `docker run` recreate.
        That loses compose semantics on exactly the setup the Compose docs
        recommend. Found by sweeping dockcheck's issue history (dc#27) and
        confirmed on four containers on this machine.

        A path may legitimately contain a comma, and the label format gives
        no way to tell that apart. So the split is only trusted when EVERY
        piece resolves to a real file; otherwise the original string is
        returned untouched and the caller's existing check decides. A wrong
        split would deploy from the wrong file, which is far worse than the
        fallback it replaces.
        """
        if not config_file:
            return []
        if "," not in config_file:
            return [config_file]
        parts = [p.strip() for p in config_file.split(",") if p.strip()]
        if parts and all(os.path.isfile(p) for p in parts):
            return parts
        return [config_file]

    def _update_compose(self, name, image, project, service, config_file, working_dir, netns_name=None):
        """Update a container using Docker Compose."""
        self._debug(f"Updating (compose): {name} (project={project}, service={service})...")

        # Get old image info
        old_created = self._get_image_created(image)
        # OCI image.version label, before pull (#22). Best-effort —
        # ~40 % coverage across real-world stacks.
        old_version = self._get_image_version_label(image)

        # Compose is LOCAL-ONLY, deliberately (#7).
        #
        # `docker compose -f <path>` parses the file on the machine running
        # the CLI and applies the result to the target daemon. For a remote
        # host the path in the container's labels describes THAT host's
        # filesystem, which we can't see. Two ways that goes wrong: the path
        # doesn't exist here and we'd silently drop to a standalone recreate,
        # or — far worse — a file happens to exist at the same path locally
        # (`/opt/stacks/...` is hardly unique) and we'd deploy OUR service
        # definition onto someone else's box.
        #
        # So a remote container is recreated from its own inspect data
        # instead, which is the same fallback used when a local compose file
        # isn't mounted. It loses compose-only metadata, and that is a
        # documented limitation rather than a silent wrong-file deploy.
        from container_store import LOCAL_HOST
        host_name = getattr(self.backend, "name", LOCAL_HOST) or LOCAL_HOST
        if host_name != LOCAL_HOST:
            self._debug(f"  {name} is compose-managed on {host_name}; compose "
                        f"files are only readable on the local host — "
                        f"recreating from inspect data instead")
            return self._update_standalone(name, image, netns_name=netns_name)

        # Check if compose file is accessible
        compose_files = self._compose_files(config_file)
        if not compose_files or not all(os.path.isfile(f) for f in compose_files):
            # Say it, do not just log it. Docksentry runs in a container,
            # so a compose file living on the host is invisible unless it
            # is mounted in — and then the update silently changes
            # strategy, rebuilding a compose-managed container from its
            # inspect data with `docker run`. That is a different code
            # path with different failure modes, and the owner has no way
            # to know it was taken (#2, @famewolf). One line in the
            # result is the difference between "why did this fail?" and
            # "ah, I need to mount that directory."
            self._debug(f"  Compose file not found: {config_file} — falling back to standalone")
            ok, msg = self._update_standalone(name, image,
                                              netns_name=netns_name)
            note = self._t("compose_fallback", file=config_file or "?")
            return ok, f"{msg}\n{note}"

        # Base compose invocation. When the stack was originally started from
        # a different directory than the compose file's (label
        # com.docker.compose.project.working_dir ≠ dirname(config_file)),
        # compose resolves `.env` interpolation and env_file paths against
        # the PROJECT directory — without --project-directory our recreate
        # could interpolate ${VARS} differently than the original `up` did
        # (found via lint: `working_dir` was accepted but never used; same
        # recreate-fidelity class as #27/#29).
        # One -f per file, in the order Docker recorded them — compose
        # applies overrides left to right, so the order is not cosmetic.
        compose_base = ["docker", "compose"]
        for f in compose_files:
            compose_base += ["-f", f]
        compose_base += ["-p", project]
        if working_dir and os.path.isdir(working_dir) \
                and os.path.realpath(working_dir) != os.path.realpath(
                    os.path.dirname(compose_files[0]) or "."):
            compose_base += ["--project-directory", working_dir]

        # Pull new image via compose
        pull_cmd = compose_base + ["pull", service]
        self._debug(f"  Running: {' '.join(pull_cmd)}")
        # compose_base still carries the leading CLI name so the debug line
        # above shows the full command; the backend prepends its own, hence
        # the [1:].
        result = self.backend.run(pull_cmd[1:], timeout=1800)
        if result.returncode != 0:
            msg = f"Compose pull failed: {result.stderr[:200]}"
            self._save_history(name, image, False, msg)
            return False, msg

        # Get new image info after pull
        new_created = self._get_image_created(image)
        new_size = self._get_image_size(image)
        new_version = self._get_image_version_label(image)
        self._version_arrow = self._format_version_arrow(old_version, new_version)

        layer_note = self._layer_farewell(name)

        # Recreate service via compose. `--force-recreate` so the container
        # is actually replaced: a plain `up -d` can leave the old container
        # (and old image) running if Compose judges the service "unchanged",
        # so the new image gets pulled but never loaded (#35).
        up_cmd = compose_base + ["up", "-d", "--no-deps", "--force-recreate", service]
        self._debug(f"  Running: {' '.join(up_cmd)}")
        result = self.backend.run(up_cmd[1:], timeout=120)
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
            probe = self._health_output(name)
            if outcome == "crashloop":
                msg = (f"Update produced a crash-restart loop "
                       f"({self._state_note(state, health)}) "
                       f"— left in place (compose)")
            else:
                msg = (f"Health check failed "
                       f"({self._state_note(state, health)}) "
                       f"— container left in place (compose)")
            if probe:
                msg += f"\nHealth check said:\n```\n{probe}\n```"
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
        return True, f"OK ({detail}){layer_note}"

    @staticmethod
    def _build_run_args(config, image, name, image_defaults=None, netns_name=None,
                        inherited=None, cgroup_version=None):
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
        # A Podman pod member looks exactly like a Gluetun-style sidecar
        # from `NetworkMode` alone — `container:<id>` — but the id is the
        # pod's INFRA container, and Podman refuses to let a container
        # join it that way. Measured, podman 4.9.3:
        #
        #   podman run --network container:<infra-id> …
        #   Error: container dependency <infra-id> is part of a pod, but
        #   container is not: invalid argument
        #
        # So every update of a container in a pod failed. Not silently —
        # the recreate reports the error, and the rollback restores the
        # renamed original, still a pod member, measured and confirmed —
        # but it could never succeed either.
        #
        # `Pod` carries the pod id; Docker's inspect has no such key, so
        # this is inert on Docker. `--pod` accepts the id (verified), and
        # a pod cannot be recreated out from under its own member the way
        # a Gluetun head can, so there is no stale-id problem here.
        #
        # `netns_name` wins when set, because that is the orchestrator
        # having resolved a head container by NAME for the sidecar case,
        # which a pod member never is.
        pod_id = "" if netns_name else (config.get("Pod") or "")
        if pod_id:
            args.extend(["--pod", pod_id])
        elif network_mode and network_mode != "default":
            args.extend(["--network", network_mode])
        # When inheriting another container's network namespace, Docker
        # forbids the per-container network knobs (--hostname, -p,
        # --add-host, --mac-address, --dns, ...). See #11 for the long
        # explanation. We skip those flags downstream when shares_netns
        # is True.
        # A pod member is in the same position: the namespace belongs to
        # the pod's infra container, and Podman rejects the per-container
        # network knobs for it just as Docker does for `container:`. Its
        # NetworkMode already starts with `container:`, so this is true
        # either way — stated explicitly so that a later change to how
        # pods are detected cannot quietly drop it.
        shares_netns = bool(pod_id) or network_mode.startswith(
            ("container:", "service:"))

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

        # ── Cgroup namespace mode ──────────────────────────────
        # "private" is Docker's default and carries itself; an explicit
        # `--cgroupns host` is a real choice and used to be silently
        # dropped. Surfaced by the audit rework: it sat in the skip list.
        if (host.get("CgroupnsMode") or "") == "host":
            args.extend(["--cgroupns", "host"])

        # ── GPU (DeviceRequests → --gpus) ──────────────────────
        # The one field whose loss the owner measured on his own server:
        # his ollama was recreated without its GPU, the NVIDIA runtime
        # therefore never injected `nvidia-smi`, his healthcheck probes
        # exactly that binary, and every update rolled back forever.
        # The old comment here said "may add in a future release if
        # requested" — his server requested.
        args.extend(UpdateChecker._gpus_args(host.get("DeviceRequests")))

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
                opts = []
                if not mount.get("RW", True):
                    opts.append("ro")
                # Mount propagation. Without this a `:rslave` bind comes
                # back as `rprivate`, and the container silently stops
                # seeing host mounts that appear later — the classic
                # symptom being a media stack that no longer notices a
                # disk mounted after it started. `rprivate` is Docker's
                # default, so emitting it would be noise; everything else
                # was asked for explicitly and has to be replayed.
                # Measured before fixing: `-v /tmp:/host:ro,rslave` came
                # back out as `-v /tmp:/host:ro`. (watchtower#221,
                # ouroboros#1-#5 — both sweeps found it independently.)
                prop = (mount.get("Propagation") or "").strip()
                if prop and prop != "rprivate":
                    opts.append(prop)
                bind = f"{src}:{dst}" + (":" + ",".join(opts) if opts else "")
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
        # Podman reports the DEFAULT namespace mode as "private" (and
        # sometimes "default") where Docker reports "" — and `--pid private`
        # / `--uts private` are not valid run values, so replicating them
        # bricked podman self-update/recreate ("invalid PID mode", #49
        # @LeeNX). IpcMode already skipped "private"; PID/UTS were the gap.
        # Real values like "host" / "container:<id>" still pass through.
        for flag, key, default in (
            ("--ipc", "IpcMode",  ("", "private", "shareable", "default")),
            ("--pid", "PidMode",  ("", "private", "default")),
            ("--uts", "UTSMode",  ("", "private", "default")),
        ):
            v = host.get(key, "") or ""
            if v and v not in default:
                args.extend([flag, v])

        # ── Runtime (skip each CLI's own default) ──────────────
        # Docker reports `runc`. Podman reports `oci` — which is not the
        # name of a runtime at all but its generic label for "whatever
        # `runtime` is configured", and feeding it back fails outright:
        #
        #   Error: default OCI runtime "oci" not found: invalid argument
        #
        # Measured, podman 4.9.3, on an ordinary container with nothing
        # unusual about it. Passing neither is what both CLIs want when
        # the container is on the default runtime — the flag exists for
        # the person who deliberately picked `crun` or `kata`, and their
        # value comes through untouched.
        runtime = host.get("Runtime", "") or ""
        if runtime and runtime not in ("runc", "oci"):
            args.extend(["--runtime", runtime])

        # ── Logging driver ──────────────────────────────────────
        # Skipping `json-file` as "the default" is only safe while the
        # container carries no log options: json-file is the FACTORY
        # default, but daemon.json can set any other driver as the
        # daemon's default. @famewolf's llama-server had json-file with
        # `max-size` while his daemon default was journald — dropping
        # the driver flag but keeping the opts handed journald an option
        # it refuses, and the recreate died with "unknown log opt
        # 'max-size' for journald log driver" (#2). Options only mean
        # anything next to their driver, so the opts imply the flag.
        log = host.get("LogConfig") or {}
        log_type = log.get("Type", "")
        log_opts = log.get("Config") or {}
        if log_type and (log_opts or log_type != "json-file"):
            args.extend(["--log-driver", log_type])
        for k, v in log_opts.items():
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
            # 0 means "use the kernel default" — nothing to say.
            #
            # -1 means "unlimited", and it has to be said OUT LOUD. The
            # comment here used to call skipping it intentional, on the
            # assumption that omitting the flag was a no-op. It is not:
            # measured on a live container, `--memory=256m --memory-swap=-1`
            # inspects as MemorySwap -1, while `--memory=256m` alone
            # inspects as 536870912 — Docker's 2x default. So a container
            # explicitly given unlimited swap quietly acquired a swap cap
            # on its first update, and could be OOM-killed afterwards where
            # it was not before. Only MemorySwap has an "unlimited"
            # sentinel; the others are byte counts.
            if v and v > 0:
                args.extend([flag, str(v)])
            elif v == -1 and key == "MemorySwap":
                args.extend([flag, "-1"])
        # memory.swappiness is a cgroup-v1-only control; it doesn't exist
        # on cgroup v2, so crun/podman rejects --memory-swappiness on a
        # cgroup-v2 host regardless of the value (#50). cgroup_version is
        # None for callers that can't detect it (assume v1 / emit as
        # before); the real recreate paths thread the daemon's version in.
        msrl = host.get("MemorySwappiness")
        if msrl is not None and msrl >= 0 and cgroup_version != "2":
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

        # argv is strings, by definition — and a non-string in it does not
        # fail here, it fails inside `subprocess.run` with
        #
        #   TypeError: expected str, bytes or os.PathLike object, not int
        #
        # raised before the CLI is ever executed, from a frame that says
        # nothing about which field was wrong.
        #
        # Not hypothetical. Podman reports `Config.StopSignal` as the
        # NUMBER 15 where Docker reports the string "SIGTERM" (measured,
        # podman 4.9.3), so `--stop-signal 15` went into the list as an
        # int and every recreate on Podman threw before it started —
        # every container, not only the pod members that led here. Both
        # CLIs accept the numeric form on the command line; it only ever
        # needed to be a string.
        #
        # Podman 5.0.0 made the CLI report the signal NAME, a documented
        # breaking change, so 4.x is where that sentence holds. It is not
        # obsolete though: the Docker-compat endpoint still returns a
        # numeric *string* as of v6.0.2, and a client asking on an older
        # API version still gets the int. Three representations of one
        # field, by access path — which is the argument for coercing
        # rather than for special-casing a version.
        #
        # Coercing the whole list rather than that one field, because the
        # next inspect difference between the two CLIs will land the same
        # way, and a TypeError from inside subprocess is the worst place
        # to learn about it.
        return [a if isinstance(a, str) else str(a) for a in args]

    def _get_image_version_label(self, image):
        """Read & normalize `org.opencontainers.image.version` from an
        image's labels. Returns empty string when missing/unusable."""
        try:
            r = self.backend.run(
                ["image", "inspect", "--format",
                 '{{index .Config.Labels "org.opencontainers.image.version"}}', image], timeout=5)
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
            r = self.backend.run(
                ["image", "inspect", image], timeout=10)
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
            r = _cb.default_backend().run(
                ["image", "inspect", "--format", "{{json .Config}}", ref], timeout=10)
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
                r = self.backend.run(cmd[1:], timeout=15)
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
            # GPU — carried via _gpus_args since the ollama incident.
        "DeviceRequests",
        # Carried via --cgroupns when set to "host"; "private" is the
        # daemon default and needs no flag.
        "CgroupnsMode",
        # Derived, not settable: Docker computes these two from
        # --privileged and --security-opt systempaths, both of which we
        # carry — so on the recreated container Docker recomputes the
        # same values. Reporting them as "would be lost" was wrong
        # twice over: they are on EVERY container, and they are not
        # lost. The owner's first /audit of a stock ollama listed four
        # findings, all four of them Docker's own defaults.
        "MaskedPaths", "ReadonlyPaths",
    })
    # HostConfig keys we read elsewhere or intentionally don't restore
    # (Docker auto-manages them, or they're system-set metadata).
    _SKIPPED_HOSTCONFIG = frozenset({
        # Internal Docker accounting / runtime state
        "ContainerIDFile", "CgroupParent", "Cgroup",
        "ConsoleSize", "Isolation",
        # Block-IO leaf fields we don't expose; rare in real-world use
        "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps",
        "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps",
        # Device-cgroup-rules: still out of scope. DeviceRequests left
        # this list when the GPU flag started being carried — see
        # _gpus_args, and #62's neighbour on the owner's own server.
        "DeviceCgroupRules",
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

    @staticmethod
    def _gpus_args(device_requests):
        """`HostConfig.DeviceRequests` → the `--gpus` flag that made it.

        Built against Docker's documented shapes, not sampled from a live
        GPU machine — this development box has none, which is worth
        saying out loud. The four forms `docker run` produces:

          --gpus all             → Count -1, no DeviceIDs
          --gpus 2               → Count 2
          --gpus "device=0,1"    → DeviceIDs ["0","1"]
          --gpus 'all,capabilities=utility'
                                 → Capabilities beyond the implicit gpu

        The flag's value is CSV to Docker's parser, so a field that
        itself contains commas (a device list, a capability list) must
        arrive as a quoted CSV field — literal double quotes inside the
        argv element. That is not shell quoting; we exec without a
        shell, and the quotes are part of the value.

        One request only: `--gpus` is a single-value flag, and a second
        DeviceRequests entry has no CLI spelling. In that case nothing
        is emitted and the audit reports the field instead — dropping
        half a GPU config silently would be this bug all over again.
        """
        reqs = device_requests or []
        if len(reqs) != 1 or not isinstance(reqs[0], dict):
            return []
        req = reqs[0]
        driver = (req.get("Driver") or "").strip()
        count = req.get("Count") or 0
        ids = [str(i) for i in (req.get("DeviceIDs") or []) if str(i)]
        caps = sorted({c for group in (req.get("Capabilities") or [])
                       for c in group if c and c != "gpu"})
        options = req.get("Options") or {}

        # The canonical `--gpus all`, byte for byte the common case.
        if count == -1 and not ids and not caps and not options and \
                driver in ("", "nvidia"):
            return ["--gpus", "all"]

        fields = []
        if driver and driver != "nvidia":
            fields.append(f"driver={driver}")
        if ids:
            joined = ",".join(ids)
            fields.append(f'"device={joined}"' if "," in joined
                          else f"device={joined}")
        elif count:
            fields.append("count=all" if count == -1 else f"count={count}")
        if caps:
            joined = ",".join(["gpu"] + caps)
            fields.append(f'"capabilities={joined}"')
        for k, v in sorted(options.items()):
            fields.append(f"{k}={v}")
        return ["--gpus", ",".join(fields)] if fields else []

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

        # Values Docker writes into every container unasked. Truthy, so
        # the emptiness test above cannot catch them — measured on a
        # stock nginx: CgroupnsMode "private", ConsoleSize [0,0], the
        # standard MaskedPaths list. An audit that reports what Docker
        # does to every container is an audit people learn to ignore.
        _DOCKER_DEFAULTS = {
            "CgroupnsMode": ("private", "host"),  # host = daemon config,
                                                   # carried either way
            "ConsoleSize": ([0, 0],),
        }

        def _is_finding(key, value):
            if not _is_non_default(value):
                return False
            defaults = _DOCKER_DEFAULTS.get(key)
            if defaults is not None and any(value == d for d in defaults):
                return False
            return True

        unknown_host = sorted(
            k for k, v in host.items()
            if _is_finding(k, v)
            and k not in self._HONORED_HOSTCONFIG
            and k not in self._SKIPPED_HOSTCONFIG
        )
        # …and the fields we skip KNOWINGLY are reported too, when they
        # are actually set. "Deliberately not carried" and "the user
        # knows it is not carried" are different things: DeviceRequests
        # sat in the skip list while the owner's GPU container failed
        # every update, and /audit — the command built to find exactly
        # such gaps — stayed silent about the one field that mattered.
        dropped_host = sorted(
            k for k, v in host.items()
            if _is_finding(k, v) and k in self._SKIPPED_HOSTCONFIG
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
            "host_dropped": dropped_host,
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
        old_inspect = self.backend.run(
            ["image", "inspect", "--format",
             '{{.Created}}||{{index .Config.Labels "org.opencontainers.image.version"}}', image])
        if old_inspect.returncode == 0:
            parts = old_inspect.stdout.strip().split("||")
            old_created = parts[0][:10]
            if len(parts) > 1:
                old_version = self._normalize_version_label(parts[1])

        # Pull new image
        result = self.backend.pull(image, timeout=1800)
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
        new_inspect = self.backend.run(
            ["image", "inspect", "--format",
             '{{.Created}}||{{.Size}}||{{index .Config.Labels "org.opencontainers.image.version"}}', image])
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

        layer_note = self._layer_farewell(name)

        # Recreate container: stop, rename old, create new with same config, start, remove old
        try:
            # Get full container config for recreation
            inspect_raw = self.backend.run(
                ["inspect", name])
            if inspect_raw.returncode != 0:
                # NOT a success. The image is on disk but the container
                # was never touched — it still runs the old version, and
                # calling that ✅ is how @famewolf read "pulled" as
                # "updated" while dockmox pulled images for containers
                # that live on another machine entirely (#2). The pull
                # is reported as what it is: half the job, stopped.
                return False, (f"Image pulled, but `{name}` could not be "
                               f"inspected — container NOT updated: "
                               f"{clip(getattr(inspect_raw, 'stderr', '') or 'no details')}")

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
                    cgroup_version=self._cgroup_version(self.backend),
                )
                run = self.backend.run(cmd[1:], timeout=120)
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
                               else "health check failed "
                                    f"({self._state_note(state, health)})")
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
                return True, f"OK ({detail}){suffix}{layer_note}"

            # Container still exists. If the stop itself failed, leave it
            # alone — same as before.
            if not stop_ok:
                return False, f"Couldn't stop container: {stop_detail}"

            # Rename old container
            old_name = f"{name}_old"
            # Clear any stale backup from a previous interrupted run FIRST.
            # Without this, a leftover `<name>_old` makes the rename fail;
            # the `docker run` then fails on the name conflict, and the
            # rollback — which trusts `<name>_old` to be this run's backup —
            # force-removes the healthy container and promotes the stale one
            # in its place. The user ends up running a previous generation
            # of their container and nothing says so.
            #
            # `recreate_dependent` has done this since it was written; the
            # main path never did. Two independent sweeps of Watchtower's
            # and Ouroboros's issue histories reproduced the same end state
            # here (watchtower#1101/#235, ouroboros#19/#20), which is what
            # sent me looking.
            self.backend.rm(old_name, force=True, timeout=self._lifecycle_timeout())
            # Journal the swap BEFORE it happens. The rollback that guards
            # every other failure lives in an `except` handler, and a
            # SIGKILL raises nothing — the process is simply gone, leaving
            # the user's container stopped, renamed, and with nobody
            # looking for it. @NotRetarded's Docksentry exited 137 during
            # an update and he only learned of it from a third-party
            # monitor (#2). Recorded rather than inferred from the `_old`
            # suffix, because a user may legitimately have a container
            # named that way and renaming theirs would be worse than the
            # bug.
            self._mark_inflight(name, old_name, image)
            rename = self.backend.rename(name, old_name, timeout=self._lifecycle_timeout())
            if getattr(rename, "returncode", 0) != 0:
                # Never proceed on an unverified backup: the recreate below
                # would run against a container that still holds the name,
                # and the rollback would restore something that is not this
                # run's backup. Stopping here leaves the container stopped
                # but intact and recoverable, which beats both.
                err = (getattr(rename, "stderr", "") or "").strip()[:200]
                msg = f"Couldn't back up the container before recreating: {err}"
                self._save_history(name, image, False, msg)
                return False, msg
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
                                       inherited=self._image_config(config.get("Image")),
                                       cgroup_version=self._cgroup_version(self.backend))
            self._debug(f"  Run cmd: docker run -d --name {name} ... {image}")

            # Create and start new container
            result = self.backend.run(cmd[1:], timeout=120)

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
                # And what the *probe* said, which is a different thing
                # from what the container said — and the one that
                # actually failed. Read here, before the rollback, or we
                # would be quoting the restored old container's health
                # log and calling it the reason the new one failed.
                #
                # The owner hit this on `ollama`: rolled back for
                # health=unhealthy, with ten lines of a textbook-clean
                # startup underneath and nothing to act on.
                probe = self._health_output(name)
                # Grab logs first (above), then roll back. _rollback_to_old
                # force-removes the broken new container and restores the
                # backup — safely (won't destroy `name` if no backup
                # exists). Replaces the old stop+rm+rename+start sequence
                # that silently failed when the new container wouldn't stop.
                self._rollback_to_old(name, old_name)
                # An empty health value renders as a bare `health=`, which
                # reads like a broken template rather than "this container
                # has no health probe" — which is what it means, and which
                # is the common case for the crash-loop path (#63, the
                # owner's tika rollback).
                if outcome == "crashloop":
                    msg = (f"Update produced a crash-restart loop "
                           f"({self._state_note(state, health)}) "
                           f"— rolled back")
                else:
                    msg = (f"Health check failed "
                           f"({self._state_note(state, health)}) "
                           f"— rolled back")
                if probe:
                    msg += f"\nHealth check said:\n```\n{probe}\n```"
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
                # force, like every other place that drops this backup.
                # Plain `rm` fails on a running container and its exit
                # code is not read here — measured: exit 1, "container is
                # running: stop the container before removing or force
                # remove". _rollback_to_old had exactly this bug and was
                # fixed; this call site was missed. The backup is stopped
                # in the ordinary case, so this mostly buys certainty
                # rather than a change in behaviour — but a silent rm
                # whose failure nobody notices is how `<name>_old`
                # containers pile up (#56, @LeeNX).
                self.backend.rm(old_name, force=True, timeout=30)
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

            # Remove old container — forced, like every other place that
            # drops this backup. Plain `rm` fails on a running container
            # and nobody reads the exit code here; measured, exit 1,
            # "container is running: stop the container before removing
            # or force remove". `_rollback_to_old` had the identical bug
            # and was fixed. This is the SUCCESS path, so it is the one
            # that runs every time an update works — and a silent `rm`
            # whose failure nobody notices is how `<name>_old` containers
            # accumulate until somebody concludes their containers are
            # not updating (#56, @LeeNX).
            self.backend.rm(old_name, force=True, timeout=30)
            self._debug(f"  Recreated successfully: {name} (health: {health or 'ok'})")

            detail = f"🗓️ {old_created} → {new_created}, 📦 {new_size}{getattr(self, '_version_arrow', '')}"
            self._save_history(name, image, True, detail)
            self._clear_inflight()
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
            self._clear_inflight()
            return False, f"Error: {str(e)[:200]}"
