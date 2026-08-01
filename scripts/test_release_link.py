#!/usr/bin/env python3
"""Auto-detected GitHub/GitLab repo links point at the releases page (#52, @LeeNX).

`LinkResolver.prefer_release_url` rewrites a BARE `host/owner/repo` URL
to its releases page — but only that shape, only on github.com/gitlab.com,
and never anything deeper or any other host. Pure function, no Docker, no
network.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from link_resolver import LinkResolver   # noqa: E402

f = LinkResolver.prefer_release_url
checks = {}

# ── GitHub: bare repo → releases/latest ──────────────────────────────
GH = "https://github.com/amayer1983/docksentry/releases/latest"
checks["github bare repo → releases/latest"] = f("https://github.com/amayer1983/docksentry") == GH
checks["github trailing slash"] = f("https://github.com/amayer1983/docksentry/") == GH
checks["github .git suffix"] = f("https://github.com/amayer1983/docksentry.git") == GH
checks["github http (not https) still rewritten"] = f("http://github.com/amayer1983/docksentry") == GH
checks["github www. host"] = f("https://www.github.com/amayer1983/docksentry") == GH

# ── GitLab: bare repo → /-/releases ──────────────────────────────────
checks["gitlab bare repo → /-/releases"] = (
    f("https://gitlab.com/me/app") == "https://gitlab.com/me/app/-/releases")

# ── Left ALONE: anything deeper, or already a releases page ──────────
for u in (
    "https://github.com/amayer1983/docksentry/releases/latest",   # already there
    "https://github.com/amayer1983/docksentry/releases",          # releases list
    "https://github.com/amayer1983/docksentry/tree/main",         # a subpath
    "https://github.com/amayer1983/docksentry/blob/main/README.md",
    "https://github.com/amayer1983",                              # org page, 1 segment
    "https://gitlab.com/group/subgroup/app",                      # 3 segments (subgroup)
):
    checks[f"deeper/1-seg left alone: {u[:48]}"] = f(u) == u

# ── Left ALONE: other hosts, product homepages, junk ────────────────
# NOTE: `gitea.example.com` used to live in this list. It moved to the
# rewritten set when Gitea/Forgejo support landed — that host IS a
# forge, and treating it as "some other host" was the old behaviour,
# not a rule worth keeping.
for u in (
    "https://vaultwarden.example.com",
    "https://hub.docker.com/_/redis",
    "https://example.com/owner/repo",         # 2 segments but not github/gitlab
    "ftp://github.com/owner/repo",            # wrong scheme
    "not a url",
    "",
):
    checks[f"other/invalid left alone: {u[:40]!r}"] = f(u) == u

# github.com/owner/repo where the "already releases/latest" idempotency holds
checks["idempotent on an already-rewritten url"] = f(GH) == GH


# ── Gitea / Forgejo (#52, @LeeNX) ────────────────────────────────────
# They mimic GitHub's layout, including the /releases/latest redirect —
# @LeeNX showed the 303 from gitea.com. Mostly self-hosted, so the match
# is a hostname heuristic rather than a fixed list.
checks["gitea.com bare repo -> releases/latest"] = (
    f("https://gitea.com/gitea/runner")
    == "https://gitea.com/gitea/runner/releases/latest")
checks["codeberg (forgejo) rewritten"] = (
    f("https://codeberg.org/o/r") == "https://codeberg.org/o/r/releases/latest")
checks["self-hosted git.example.com rewritten"] = (
    f("https://git.example.com/o/r")
    == "https://git.example.com/o/r/releases/latest")
checks["a host merely containing 'gitea' rewritten"] = (
    f("https://mygitea.net/o/r") == "https://mygitea.net/o/r/releases/latest")
# The heuristic matches DNS LABELS, not letters that happen to line up.
# `"git." in host` would have caught both of these.
for host in ("digit.example.com", "legit.io"):
    u = f"https://{host}/o/r"
    checks[f"not a forge, left alone: {host}"] = f(u) == u
# …and a deeper path is still left alone on a forge too.
deep = "https://gitea.com/o/r/releases/tag/v1.2.3"
checks["gitea deep link left alone"] = f(deep) == deep


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
