#!/usr/bin/env python3
"""A recreate never separates log options from their log driver (#2).

@famewolf's llama-server: created with json-file and `max-size`, on a
host whose daemon.json makes journald the default driver. The recreate
skipped `--log-driver json-file` ("that's the default") but kept the
`--log-opt max-size=…` — so the option landed on the daemon's ACTUAL
default, journald, which refuses it:

    docker: Error response from daemon: unknown log opt 'max-size'
    for journald log driver

Measured both sides before fixing (docker 27.x):
  * `docker create --log-driver journald --log-opt max-size=5m` fails at
    CREATE time with exactly that error — which closes the chain: the
    old container existed, so its inspect can never say journald+max-size;
    the journald in the error can only be the daemon default filling the
    gap WE left.
  * a json-file container with max-size inspects as
    `{"Type":"json-file","Config":{"max-file":"3","max-size":"5m"}}` —
    the shape the builder gets.

The rule: options only mean anything next to their driver, so emitting
any `--log-opt` forces the `--log-driver` — json-file included. A bare
json-file with no options still emits nothing: inspect cannot tell "the
user chose json-file" from "the factory default applied", and saying
nothing is the only answer that is right in both readings.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from update_checker import UpdateChecker  # noqa: E402

checks = {}


def build(log_config):
    cfg = {
        "Id": "c" * 64,
        "Image": "sha256:" + "d" * 64,
        "Config": {"Image": "nginx:latest", "Cmd": [], "Labels": {},
                   "Env": []},
        "HostConfig": {"NetworkMode": "bridge",
                       "RestartPolicy": {"Name": "no"},
                       "LogConfig": log_config},
        "NetworkSettings": {"Networks": {}},
    }
    uc = UpdateChecker.__new__(UpdateChecker)
    return uc._build_run_args(cfg, "alpine:3.19", "c1", None)


def pairs(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


# The famewolf case: json-file WITH options → the driver must be said,
# or the options drift onto whatever the daemon default happens to be.
args = build({"Type": "json-file",
              "Config": {"max-size": "5m", "max-file": "3"}})
checks["json-file with options names its driver"] = (
    pairs(args, "--log-driver") == ["json-file"])
checks["…and carries the options"] = (
    sorted(pairs(args, "--log-opt")) == ["max-file=3", "max-size=5m"])

# Bare json-file: still silent — cannot be told apart from the factory
# default, and there is nothing that could drift.
args = build({"Type": "json-file", "Config": {}})
checks["bare json-file stays silent"] = (
    "--log-driver" not in args and "--log-opt" not in args)

# A non-default driver is named, options or not (unchanged behaviour).
args = build({"Type": "journald", "Config": {}})
checks["a chosen driver is always named"] = (
    pairs(args, "--log-driver") == ["journald"])

args = build({"Type": "syslog", "Config": {"syslog-address": "udp://1.2.3.4:514"}})
checks["…with its options next to it"] = (
    pairs(args, "--log-driver") == ["syslog"]
    and pairs(args, "--log-opt") == ["syslog-address=udp://1.2.3.4:514"])

# No LogConfig at all (old inspects, some podman versions): nothing said.
args = build({})
checks["a missing LogConfig emits nothing"] = (
    "--log-driver" not in args and "--log-opt" not in args)

# The invariant itself, stated once: options never appear without the
# driver that owns them.
for lc in ({"Type": "json-file", "Config": {"max-size": "1m"}},
           {"Type": "journald", "Config": {"tag": "x"}},
           {"Type": "local", "Config": {"max-size": "2m"}}):
    args = build(lc)
    if "--log-opt" in args:
        checks[f"opts imply the driver ({lc['Type']})"] = (
            "--log-driver" in args
            and args.index("--log-driver") < args.index("--log-opt"))

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
