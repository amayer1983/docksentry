#!/usr/bin/env python3
"""The managed-host registry (#7, multi-host).

Builds the registry from config and checks that each host's backend,
checker and store view agree with each other — and, most importantly,
that an unset DOCKER_HOSTS gives exactly the old single-host setup.

No Docker, no network: subprocess is stubbed and only argv is inspected.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import container_backend                       # noqa: E402
from container_store import ContainerStore, LOCAL_HOST   # noqa: E402
from config import parse_docker_hosts          # noqa: E402
from hosts import build_hosts, split_host_target, ALL_HOSTS   # noqa: E402

checks = {}


def _cfg(d, docker_hosts=""):
    names = ["pinned", "autoupdate", "update_windows", "ask_before_major",
             "trust_running", "cooldown", "protect_stop", "major_pending",
             "groups", "notes", "links"]
    cfg = types.SimpleNamespace(
        debug=False, container_cli="docker",
        docker_hosts=parse_docker_hosts(docker_hosts))
    for n in names:
        setattr(cfg, f"{n}_file", os.path.join(d, f"{n}.json"))
    return cfg


sent = []


class _CP:
    returncode = 0
    stdout = ""
    stderr = ""


real = container_backend.subprocess
container_backend.subprocess = types.SimpleNamespace(
    run=lambda argv, **kw: (sent.append(argv), _CP())[1],
    SubprocessError=real.SubprocessError, TimeoutExpired=real.TimeoutExpired,
    CalledProcessError=real.CalledProcessError,
    PIPE=real.PIPE, DEVNULL=real.DEVNULL)

try:
    with tempfile.TemporaryDirectory() as d:
        # ── unset DOCKER_HOSTS → exactly the old single-host setup ────
        cfg = _cfg(d)
        reg = build_hosts(cfg, ContainerStore(cfg))
        checks["no config → exactly one host"] = len(reg) == 1
        checks["that host is local"] = reg.local.name == LOCAL_HOST
        checks["single host is not 'multi'"] = reg.is_multi is False
        sent.clear()
        reg.local.checker._container_exists("nginx")
        checks["local host sends no endpoint flag"] = "-H" not in sent[-1]

        # ── with hosts configured ────────────────────────────────────
        cfg = _cfg(d, "pve1:ssh://root@pve1, nas:tcp://nas:2375")
        store = ContainerStore(cfg)
        reg = build_hosts(cfg, store)
        checks["local + configured hosts"] = reg.names == ["local", "pve1", "nas"]
        checks["local stays first"] = reg.hosts[0].is_local
        checks["is_multi once there are remotes"] = reg.is_multi is True

        pve1 = reg.get("pve1")
        checks["lookup by name"] = pve1 is not None and pve1.endpoint == "ssh://root@pve1"
        checks["lookup is case-insensitive"] = reg.get("PVE1") is pve1
        checks["unknown host → None"] = reg.get("nope") is None
        checks["empty name → None"] = reg.get("") is None
        checks["only local is flagged local"] = (
            [h.name for h in reg if h.is_local] == ["local"])

        # ── each host's checker talks to ITS host ────────────────────
        sent.clear()
        pve1.checker._container_exists("nginx")
        checks["remote checker carries its endpoint"] = (
            sent[-1][:3] == ["docker", "-H", "ssh://root@pve1"])
        sent.clear()
        reg.get("nas").checker._container_exists("nginx")
        checks["second remote uses its own endpoint"] = (
            sent[-1][:3] == ["docker", "-H", "tcp://nas:2375"])

        # ── each host's store view is keyed to it ────────────────────
        reg.local.store.pin("nginx")
        pve1.store.pin("nginx")
        checks["same name pinned on two hosts"] = (
            reg.local.store.is_pinned("nginx") and pve1.store.is_pinned("nginx"))
        checks["nas untouched by the other two"] = (
            not reg.get("nas").store.is_pinned("nginx"))
        checks["local key stays unprefixed on disk"] = (
            sorted(store.get_pinned()) == ["nginx", "pve1/nginx"])

        # ── podman propagates to remotes ─────────────────────────────
        cfg = _cfg(d, "nas:tcp://nas:2375")
        cfg.container_cli = "podman"
        reg = build_hosts(cfg, ContainerStore(cfg))
        sent.clear()
        reg.get("nas").checker._container_exists("x")
        checks["remote inherits the podman CLI"] = sent[-1][0] == "podman"
finally:
    container_backend.subprocess = real


# ── @host command targeting ──────────────────────────────────────────
# `/check @pve1` aims one command at one host; `@all` aims it at every
# host. The token may sit anywhere in the arguments because people write
# it both ways.
sh = split_host_target
checks["no @token → no target"] = sh("sonarr") == ("sonarr", None)
checks["empty input"] = sh("") == ("", None)
checks["@host alone"] = sh("@pve1") == ("", "pve1")
checks["@host after the name"] = sh("sonarr @nas") == ("sonarr", "nas")
checks["@host before the name"] = sh("@nas sonarr") == ("sonarr", "nas")
checks["@host in the middle"] = sh("a @nas b") == ("a b", "nas")
checks["@all is a distinct target"] = sh("@all") == ("", ALL_HOSTS)
checks["@all is not a host named 'all'"] = ALL_HOSTS != "all"
checks["host names are case-normalised"] = sh("@NAS")[1] == "nas"
# A bare "@" is a typo, not a target — leave it in the text rather than
# silently swallowing an argument.
checks["bare @ is left alone"] = sh("@") == ("@", None)
checks["only the first @token is consumed"] = sh("x @a @b") == ("x @b", "a")
# Telegram's own group addressing is `/check@botname` — no space before
# the @, handled elsewhere, and never reaches this function as a target.
checks["glob arguments survive"] = sh("ds_* @nas") == ("ds_*", "nas")


def main():
    ok = True
    for desc, passed in checks.items():
        print(f"  {'✅' if passed else '❌'} {desc}")
        ok = ok and passed
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
