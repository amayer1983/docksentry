#!/usr/bin/env python3
"""Container repo / changelog link resolution (#52, @LeeNX).

Read-only, Telegram-agnostic string logic extracted from ``TelegramBot``
so both interactive channels (Telegram today, Discord in v2) and the Web
UI resolve a container's link through the *same* code path — nobody has
to reach into the bot's privates to answer "where's the changelog for
this container?".

The chain is: ``docksentry.link`` label → manually stored link → OCI
``image.source`` → OCI ``image.url`` → registry-overview heuristic. See
``LinkResolver.resolve_link_with_kind`` for the authoritative ordering.

Dependencies are passed in explicitly (``store`` for the manual override,
``config`` to lazily build a label reader) instead of hiding on ``self``
— that's what makes the module reusable outside the bot. A ``LinkResolver``
constructed with ``(store, config)`` is the shape both existing call sites
(the bot's notification paths and the Web UI's detail page) already need,
so it wins over free functions: the bot keeps one long-lived resolver,
the Web UI builds a throwaway one per request, and neither has to thread
``store``/``config`` through every call.

Zero third-party deps — stdlib ``subprocess`` and ``urllib.parse`` only,
exactly as when this lived in ``telegram_bot``.
"""

import subprocess
import urllib.parse


#: Hostname markers for Gitea / its fork Forgejo, whose URL layout
#: matches GitHub's closely enough that `/releases/latest` resolves
#: (@LeeNX showed the 303 from gitea.com). These are overwhelmingly
#: self-hosted, so there is no fixed host list to check against.
_FORGE_NAMES = ("gitea", "forgejo")
_FORGE_HOSTS = ("codeberg.org",)
#: The `git.example.com` convention. Matched on a whole DNS LABEL, not as
#: a substring: `"git." in host` also matches `digit.example.com` and
#: `legit.io`, which have nothing to do with it.
_FORGE_LABEL = "git"


def _looks_like_forge(host):
    """True when `host` looks like a Gitea/Forgejo instance.

    Deliberately a heuristic — guessing wrong costs a 404 on a link the
    user can ignore, while not guessing costs every self-hosted Gitea
    user the feature. But it matches on label boundaries so it stays a
    heuristic about names rather than about coincidental letters.
    """
    host = (host or "").lower().strip(".")
    if not host:
        return False
    if host in _FORGE_HOSTS or any(host.endswith("." + h) for h in _FORGE_HOSTS):
        return False if host not in _FORGE_HOSTS else True
    labels = host.split(".")
    if labels and labels[0] == _FORGE_LABEL:
        return True
    return any(n in labels_part for n in _FORGE_NAMES for labels_part in labels)


class LinkResolver:
    def __init__(self, store, config):
        # `store` supplies the manual /setlink override (get_link); `config`
        # is only needed to lazily build a label-reading UpdateChecker for
        # call sites that don't already have one in scope.
        self.store = store
        self.config = config
        self._cached_label_checker = None

    def label_checker(self):
        """Lazily built UpdateChecker used purely as a label reader for
        call sites that don't already have one in scope. Cheap to build
        (it only stores the config), cached so repeated notification
        passes don't churn objects. Returns None if construction fails
        so callers can treat it as "no labels available"."""
        ck = self._cached_label_checker
        if ck is None:
            try:
                from update_checker import UpdateChecker as _UC
                ck = _UC(self.config)
            except Exception:
                return None
            self._cached_label_checker = ck
        return ck

    def label_link(self, name, checker=None):
        """The `docksentry.link` container label, or "" when absent,
        unusable or unreadable (#52, @LeeNX).

        Read via `checker.get_container_labels()` — i.e. `docker inspect`
        — and deliberately NOT via the `docker ps --format {{.Labels}}`
        path: that one splits the label blob on commas, and commas are
        perfectly normal inside a query string, so a URL label would
        come back shredded.

        The value is validated with the shared `is_safe_link`. A value
        that fails is treated exactly like an unset label — we fall
        through to the next source rather than erroring out; a typo in a
        compose file must never break notifications. Note there is no
        `.lower()` here (unlike `_resolve_update_policy`): URL paths and
        query strings are case-sensitive.
        """
        from container_store import is_safe_link
        ck = checker if checker is not None else self.label_checker()
        if ck is None:
            return ""
        try:
            labels = ck.get_container_labels(name) or {}
            raw = labels.get("docksentry.link")
            if raw is None:
                return ""
            url = str(raw).strip()
            return url if is_safe_link(url) else ""
        except Exception:
            return ""

    def resolve_container_link(self, name, image="", checker=None, host=None):
        """Return the URL that should wrap `name` in update
        notifications, or empty string when no link is available.
        Thin wrapper around `resolve_link_with_kind` for the many call
        sites that only care about the URL itself."""
        return self.resolve_link_with_kind(name, image, checker, host)[0]

    def resolve_link_with_kind(self, name, image="", checker=None, host=None):
        """(url, kind) for a container's repo/changelog link.

        Priority order:
          0. `docksentry.link` container label (#52) — GitOps source of
             truth: the link travels with the compose file, no state in
             Docksentry needed.
          1. Manual override stored via Web UI / `/setlink`
             (`container_links.json`). Lets users point at the actual
             changelog of containers whose images don't ship OCI labels
             (redis, postgres, nginx-proxy-manager, …).
          2. `org.opencontainers.image.source` OCI label (gold standard).
          3. `org.opencontainers.image.url` OCI label (fallback).
          4. Registry overview heuristic (Hub / ghcr.io / quay.io /
             lscr.io → fleet.linuxserver.io) from the image reference.

        `kind` is one of "label", "manual", "source", "url", "registry",
        "none" — `/changelog <container>` uses it to pick how confident
        its wording should be. Everything else just wants the URL.

        Reuses the v1.18.3 `/changelog <container>` helpers so the
        notification-link feature gets the same coverage as that
        command (~67 % auto-detection rate without any user setup).

        `host` names the managed host the container runs on (#7). It only
        affects step 1: `container_links.json` is keyed per host, so the
        NAS's `nginx` gets the link you set for the NAS's `nginx`. Omitted
        — and on every single-host install — the key is the bare container
        name it has always been.
        """
        # 0. Container label
        labelled = self.label_link(name, checker)
        if labelled:
            return labelled, "label"
        # 1. Manual override
        from container_store import host_key
        manual = self.store.get_link(host_key(host, name))
        if manual:
            return manual, "manual"
        # 2 + 3. OCI labels
        url, kind = self.container_source_url(name)
        if url and kind in ("source", "url"):
            return url, kind
        # 4. Registry-overview heuristic — only when we have an image ref
        if image:
            guess = self.guess_registry_overview_url(image)
            if guess:
                return guess, "registry"
        return "", "none"

    def container_source_url(self, name):
        """Look up the upstream source URL for a container from its OCI
        labels. Returns (url, kind) where kind is:
          - "source": from `org.opencontainers.image.source` (the gold
                      standard — points at a real source repo)
          - "url":    fallback to `org.opencontainers.image.url`
                      (usually the product/landing page, less useful)
          - "none":   no usable label found

        Used by /changelog <container> to give the user a link to the
        upstream repo instead of trying (and frequently failing) to
        fetch + parse an arbitrary container's CHANGELOG file."""
        for label in ("org.opencontainers.image.source",
                      "org.opencontainers.image.url"):
            try:
                r = subprocess.run(
                    ["docker", "inspect", "--format",
                     "{{index .Config.Labels \"" + label + "\"}}", name],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    url = r.stdout.strip()
                    if url and url not in ("<no value>", "no value"):
                        kind = "source" if "source" in label else "url"
                        return self.prefer_release_url(url), kind
            except subprocess.SubprocessError:
                continue
        return "", "none"

    @staticmethod
    def prefer_release_url(url):
        """Point an auto-detected GitHub/GitLab *repo* link at its releases
        page instead of the bare homepage (#52, @LeeNX). A container's
        `org.opencontainers.image.source` almost always points at the
        project repo, and mid-upgrade the release notes are what you
        actually want — not the front page you then have to click through.

        Only a BARE repo URL is rewritten: exactly ``host/owner/repo``,
        with an optional trailing ``.git`` or ``/``. Anything deeper (a
        URL that already points at ``/releases``, a ``/tree/...`` path, a
        specific file) is left alone, as is any non-GitHub/GitLab host.
        The rewrite only ever runs on auto-detected OCI links; a link the
        user set by hand (``docksentry.link`` label or ``/setlink``) never
        reaches here, so their explicit choice is always respected. A repo
        with no releases will 404 on ``/releases/latest`` — that's the
        case the override exists for, and @LeeNX asked for `/latest`
        explicitly."""
        try:
            p = urllib.parse.urlparse(url)
        except ValueError:
            return url
        if p.scheme not in ("http", "https") or not p.netloc:
            return url
        host = p.netloc.lower()
        parts = [s for s in p.path.split("/") if s]
        if len(parts) != 2:
            return url
        owner, repo = parts[0], parts[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        if not owner or not repo:
            return url
        if host in ("github.com", "www.github.com"):
            return f"https://github.com/{owner}/{repo}/releases/latest"
        if host in ("gitlab.com", "www.gitlab.com"):
            return f"https://gitlab.com/{owner}/{repo}/-/releases"
        # Gitea and its fork Forgejo mimic GitHub's URL layout, including
        # the `/releases/latest` redirect (@LeeNX showed the 303 from
        # gitea.com). Self-hosted instances are the common case for both,
        # so this can't be a host allow-list — it keys off the hostname
        # looking like one, which is a guess, but a cheap and safe one: a
        # host that isn't Gitea simply 404s a link the user then ignores,
        # exactly as a repo with no releases already does.
        if _looks_like_forge(host):
            return f"https://{p.netloc}/{owner}/{repo}/releases/latest"
        return url

    @staticmethod
    def guess_registry_overview_url(image):
        """Heuristic for "where can the user look this up?" when the
        image has no OCI source label. Maps the image reference to its
        registry's overview page URL. Best-effort — at worst we say
        'check the registry's own page'."""
        # Strip tag
        ref = image.rsplit(":", 1)[0] if ":" in image else image
        # Docker Hub library/official ("redis" → docker.io/library/redis)
        if "/" not in ref:
            return f"https://hub.docker.com/_/{ref}"
        # GHCR
        if ref.startswith("ghcr.io/"):
            rest = ref[len("ghcr.io/"):]
            return f"https://github.com/{rest}/pkgs/container/{rest.split('/')[-1]}"
        # Quay
        if ref.startswith("quay.io/"):
            return f"https://quay.io/repository/{ref[len('quay.io/'):]}"
        # GitLab Container Registry (registry.gitlab.com / *.gitlab.io)
        if ref.startswith("registry.gitlab.com/"):
            return f"https://gitlab.com/{ref[len('registry.gitlab.com/'):]}"
        # LinuxServer (lscr.io) → fleet page
        if ref.startswith("lscr.io/"):
            return f"https://fleet.linuxserver.io/image?name={ref[len('lscr.io/'):]}"
        # Default: Docker Hub repo page (works for `user/image`)
        return f"https://hub.docker.com/r/{ref}"

    def enrich_with_source_url(self, updates):
        """Set u['source_url'] on each update from the link store + OCI
        labels + registry fallback. Idempotent — safe to call multiple
        times (it overwrites). Used by every notification path so the
        Telegram markdown link, Discord embed and webhook payload all
        share the same resolved URL.
        """
        for u in updates:
            u["source_url"] = self.resolve_container_link(
                u["name"], u.get("image", ""), host=u.get("host"))
