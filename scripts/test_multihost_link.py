#!/usr/bin/env python3
"""The repo/changelog link is read on the host the container runs on (#7, #52).

`LinkResolver.container_source_url` reads the OCI `image.source` /
`image.url` labels behind `/changelog <container>` and behind every
notification link. It built `["docker", "inspect", …]` by hand — the last
container read in the app that named a CLI itself — so it asked the local
machine no matter which host the container was on, and on a Podman install
with no `docker` alias it asked nothing at all.

What is asserted here is the intent, not the spelling: the lookup goes to
the CLI and the endpoint of the host the container actually runs on, the
other hosts are not asked, and a single-host install issues byte-for-byte
the argv it always did.

No Docker, no Podman, no network: `container_backend.subprocess` is stubbed
and answers per endpoint, so every argv the resolver caused is inspectable.
"""
import json
import os
import re
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import container_backend                                   # noqa: E402
from config import parse_docker_hosts                      # noqa: E402
from container_store import ContainerStore, LOCAL_HOST     # noqa: E402
from hosts import build_hosts                              # noqa: E402
from link_resolver import LinkResolver                     # noqa: E402

checks = {}

NAS_ENDPOINT = "ssh://nas"
LOCAL_SRC = "https://example.com/local/web"
NAS_SRC = "https://example.com/nas/web"

#: What each machine's `web` container says about itself. Same container
#: name on both hosts on purpose — that is the case where asking the wrong
#: machine doesn't fail, it answers, with the wrong repo.
LABELS = {
    LOCAL_HOST: {"org.opencontainers.image.source": LOCAL_SRC},
    "nas": {"org.opencontainers.image.source": NAS_SRC},
}

sent = []


class _CP:
    def __init__(self, stdout=""):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


def _host_of(argv):
    """Which machine an argv is aimed at, read off the endpoint flag."""
    for i, a in enumerate(argv):
        if a in ("-H", "--url") and i + 1 < len(argv):
            return "nas" if argv[i + 1] == NAS_ENDPOINT else argv[i + 1]
    return LOCAL_HOST


def _fake_run(argv, **kw):
    sent.append(list(argv))
    labels = LABELS.get(_host_of(argv), {})
    if "--format" in argv:
        fmt = argv[argv.index("--format") + 1]
        key = re.search(r'"([^"]+)"', fmt)
        if key:
            return _CP(labels.get(key.group(1), "<no value>"))
        if ".Config.Labels" in fmt:
            return _CP(json.dumps(labels))
    return _CP("")


def _cfg(d, docker_hosts="", cli="docker"):
    cfg = types.SimpleNamespace(
        debug=False, container_cli=cli,
        docker_hosts=parse_docker_hosts(docker_hosts))
    for n in ("pinned", "autoupdate", "update_windows", "ask_before_major",
              "trust_running", "cooldown", "protect_stop", "major_pending",
              "groups", "notes", "links"):
        setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
    return cfg


def oci_argv(host_flag=()):
    """The argv an OCI source read produces — the historical one, with
    whatever the backend puts between the binary and the subcommand."""
    return (["docker"] + list(host_flag) + [
        "inspect", "--format",
        '{{index .Config.Labels "org.opencontainers.image.source"}}', "web"])


real = container_backend.subprocess
container_backend.subprocess = types.SimpleNamespace(
    run=_fake_run,
    SubprocessError=real.SubprocessError, TimeoutExpired=real.TimeoutExpired,
    CalledProcessError=real.CalledProcessError,
    PIPE=real.PIPE, DEVNULL=real.DEVNULL, STDOUT=real.STDOUT)

try:
    with tempfile.TemporaryDirectory() as d:
        # ── two hosts, the same container name on both ───────────────
        cfg = _cfg(d, f"nas:{NAS_ENDPOINT}")
        store = ContainerStore(cfg)
        reg = build_hosts(cfg, store)
        lr = LinkResolver(store, cfg, hosts=reg)

        sent.clear()
        url = lr.resolve_container_link("web", "reg/web:1", host="nas")
        checks["a container on nas is read on nas"] = url == NAS_SRC
        checks["…and the local machine is never asked about it"] = (
            bool(sent) and all(_host_of(a) == "nas" for a in sent))
        checks["the OCI read carries that host's endpoint"] = (
            oci_argv(["-H", NAS_ENDPOINT]) in sent)
        checks["the docksentry.link read goes to the same machine"] = any(
            a[:3] == ["docker", "-H", NAS_ENDPOINT] and "--format" in a
            and ".Config.Labels" in a[a.index("--format") + 1]
            and '"' not in a[a.index("--format") + 1] for a in sent)

        sent.clear()
        checks["the local container keeps its own link"] = (
            lr.resolve_container_link("web", "reg/web:1", host=LOCAL_HOST)
            == LOCAL_SRC)
        checks["…read without any endpoint flag"] = oci_argv() in sent

        # A `kind` caller (/changelog) resolves through the same chain.
        checks["/changelog's (url, kind) pair is host-resolved too"] = (
            lr.resolve_link_with_kind("web", "reg/web:1", host="nas")
            == (NAS_SRC, "source"))

        # An explicit checker still wins — that is how the Web UI hands
        # the resolver the host whose page is open.
        checks["an explicit checker decides where to look"] = (
            lr.container_source_url("web", reg.get("nas").checker)
            == (NAS_SRC, "source"))

        # ── the host on an update entry reaches the lookup ───────────
        # This is the value that used to be dropped: `enrich_with_source_url`
        # passed `host` on, `resolve_link_with_kind` used it for the stored
        # override only, and the OCI read never saw it.
        ups = [{"name": "web", "image": "reg/web:1", "host": "nas"},
               {"name": "web", "image": "reg/web:1", "host": LOCAL_HOST}]
        lr.enrich_with_source_url(ups)
        checks["an update's host reaches the link lookup"] = (
            ups[0]["source_url"] == NAS_SRC)
        checks["…and each entry gets its own host's repo"] = (
            ups[1]["source_url"] == LOCAL_SRC)

        # A name the registry doesn't know (a host that has left
        # DOCKER_HOSTS) has no better machine to ask than this one.
        sent.clear()
        checks["an unknown host falls back to the local reader"] = (
            lr.resolve_container_link("web", "reg/web:1", host="typo")
            == LOCAL_SRC and all(_host_of(a) == LOCAL_HOST for a in sent))

        # ── single host: byte-for-byte the old argv ──────────────────
        cfg = _cfg(d)
        store = ContainerStore(cfg)
        reg = build_hosts(cfg, store)
        lr = LinkResolver(store, cfg, hosts=reg)
        sent.clear()
        checks["single host still resolves the link"] = (
            lr.resolve_container_link("web", "reg/web:1") == LOCAL_SRC)
        checks["single host: the OCI argv is unchanged"] = oci_argv() in sent
        checks["single host: nothing carries an endpoint flag"] = not any(
            "-H" in a or "--url" in a for a in sent)

        # ── Podman ───────────────────────────────────────────────────
        cfg = _cfg(d, cli="podman")
        store = ContainerStore(cfg)
        lr = LinkResolver(store, cfg, hosts=build_hosts(cfg, store))
        sent.clear()
        checks["a Podman install asks podman"] = (
            lr.resolve_container_link("web", "reg/web:1") == LOCAL_SRC
            and bool(sent) and all(a[0] == "podman" for a in sent))

        # …including a resolver nobody handed a registry (the Web UI
        # builds one per request), which is the shape that used to
        # hardcode `docker` regardless of the configured CLI.
        lr = LinkResolver(store, cfg)
        sent.clear()
        checks["…even without a host registry"] = (
            lr.resolve_container_link("web", "reg/web:1") == LOCAL_SRC
            and bool(sent) and all(a[0] == "podman" for a in sent))
finally:
    container_backend.subprocess = real

for k, v in checks.items():
    print(("  ✅" if v else "  ❌"), k)
if not all(checks.values()):
    print("FAIL")
    sys.exit(1)
print("PASS")
