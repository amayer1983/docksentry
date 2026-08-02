#!/usr/bin/env python3
"""Registries Docksentry could not reach at all, and Discord's field cap.

Three gaps that each made a whole class of setup silently unusable while
`docker pull` kept working — which is a confusing pair of facts to hand
someone.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import UpdateChecker, registry_scheme


def main():
    checks = {}

    # ── plain-HTTP registries ────────────────────────────────────
    # `https://` was hardcoded, so a local or internal HTTP-only registry
    # reported "unreachable / unauthorized" every cycle with no setting to
    # change it. Verified end to end against a real `registry:2` on
    # localhost: digest fetched and tags listed once the host is listed.
    checks["https by default"] = registry_scheme("ghcr.io", []) == "https"
    checks["http only when named"] = registry_scheme(
        "localhost:5000", ["localhost:5000"]) == "http"
    checks["an unnamed host stays https"] = registry_scheme(
        "ghcr.io", ["localhost:5000"]) == "https"
    # Wildcards, same matcher as the other list settings.
    checks["patterns work"] = registry_scheme(
        "reg.internal", ["*.internal"]) == "http"
    # Never guessed and never a fallback-on-failure: a tool that retries
    # over plain HTTP when TLS fails hands credentials to whoever answers.
    import inspect as _i
    src = _i.getsource(registry_scheme)
    checks["there is no automatic http fallback"] = "except" not in src

    # ── Basic-auth registries ────────────────────────────────────
    # The stock `registry:2` behind htpasswd answers `WWW-Authenticate:
    # Basic`. `_parse_www_authenticate` returned {} for anything not
    # Bearer, negotiation returned None, and the credentials already in
    # config.json were never sent.
    o = UpdateChecker.__new__(UpdateChecker)
    o._token_cache = {}
    o.debug = False
    o._debug = lambda m: None
    o._auth_kind = ""
    o._get_docker_credentials = lambda reg: "ZHM6cHc="
    hdr = UpdateChecker._negotiate_token(
        o, 'Basic realm="Registry Realm"', "reg.example.com", "x/y")
    checks["a Basic challenge is answered"] = hdr == "Basic ZHM6cHc="
    checks["the auth kind is recorded"] = o._auth_kind == "basic"

    # Without credentials there is nothing to send, and inventing an empty
    # header would only turn a clear 401 into a confusing one.
    o2 = UpdateChecker.__new__(UpdateChecker)
    o2._token_cache = {}
    o2.debug = False
    o2._debug = lambda m: None
    o2._auth_kind = ""
    o2._get_docker_credentials = lambda reg: None
    checks["no credentials, no header"] = UpdateChecker._negotiate_token(
        o2, 'Basic realm="R"', "reg", "x/y") is None

    # The return value is a complete Authorization header, not a bare
    # token — two schemes reach this now, and letting each call site
    # prepend "Bearer " is how one of them ends up sending Bearer in front
    # of Basic credentials.
    src2 = _i.getsource(UpdateChecker._negotiate_token)
    checks["the return value is a full header"] = 'f"Bearer {token}"' in src2

    # ── Discord's 25-field cap ───────────────────────────────────
    # Discord rejects an embed with more than 25 fields — with a 400, so
    # the whole notification was lost rather than truncated. Anyone back
    # from a holiday to 30 pending updates got silence.
    from notifiers.discord import DiscordNotifier
    posts = []
    n = DiscordNotifier.__new__(DiscordNotifier)
    n._bot_label = lambda: ""
    n._footer_text = lambda: "x"
    n.version_str = lambda u: ""
    n.post = lambda p: posts.append(p)
    for count, want_msgs in ((3, 1), (25, 1), (26, 2), (60, 3)):
        posts.clear()
        DiscordNotifier.send_updates_available(
            n, [{"name": f"c{i}", "image": "x:1"} for i in range(count)])
        sizes = [len(p["embeds"][0]["fields"]) for p in posts]
        checks[f"{count} updates -> {want_msgs} message(s)"] = len(posts) == want_msgs
        checks[f"{count} updates: no embed over 25"] = all(s <= 25 for s in sizes)
        checks[f"{count} updates: none lost"] = sum(sizes) == count
    # Part numbers only when there is more than one part.
    posts.clear()
    DiscordNotifier.send_updates_available(n, [{"name": "a", "image": "x:1"}])
    checks["a single message has no part number"] = "(1/1)" not in posts[0]["embeds"][0]["title"]

    # ── registry mirrors, for lookups only ───────────────────────
    # Checks go out over urllib, straight to the registry named in the
    # image reference, so they ignore the daemon's own `registry-mirrors`
    # entirely. On a network where only the mirror is reachable,
    # `docker pull` works and Docksentry reports "unreachable" forever
    # (#34, @LeeNX).
    from update_checker import mirror_host, parse_mirrors
    m = parse_mirrors(["docker.io=mirror.internal", "ghcr.io=ghcr.mirror"])
    checks["a pair is parsed"] = m == {"docker.io": "mirror.internal",
                                       "ghcr.io": "ghcr.mirror"}
    # A typo should lose that one line, not silently redirect lookups
    # somewhere the operator did not write.
    checks["a malformed entry is dropped"] = parse_mirrors(["nonsense"]) == {}
    checks["a half-empty entry is dropped"] = parse_mirrors(["a=", "=b"]) == {}

    # Docker Hub answers to several names; a mirror written for any of them
    # has to apply to the canonical one the lookup code actually uses.
    checks["hub alias reaches the canonical host"] = mirror_host(
        "registry-1.docker.io", m) == "mirror.internal"
    checks["an exact host matches"] = mirror_host("ghcr.io", m) == "ghcr.mirror"
    checks["an unmapped host is untouched"] = mirror_host("quay.io", m) == "quay.io"
    checks["no map, no change"] = mirror_host("ghcr.io", {}) == "ghcr.io"

    # Lookups only. Pulling still hands the container's own reference to
    # the daemon — pulling from elsewhere would rewrite that reference and
    # the container would stop matching its own compose file.
    src3 = _i.getsource(UpdateChecker._effective_host)
    checks["the pull side is explicitly out of scope"] = "daemon.json" in src3

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
