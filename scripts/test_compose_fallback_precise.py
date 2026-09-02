#!/usr/bin/env python3
"""The Compose-fallback note names what was lost, or stays quiet (#65).

When Docksentry cannot read a container's Compose file it rebuilds the
container from its inspect data with `docker run` and, until now, always
appended the same note: "this loses Compose metadata". Two people said
the same thing from opposite sides. @LeeNX in #65: "Maybe just throw the
warning when there are healthchecks, else the container is rebuilt fine,
is it not?" And the owner, about his own machine: "Ich bekomme bei jedem
compose Container die Warnung … die ist zwar richtig, nervt aber."

They were both right. Measured on this host with the new rule: of 18
Compose containers that got the note, 3 lose anything at all.

So the note is now a function of the container in front of us, and the
intent this file guards is that function's three halves:

  * a container with none of the risky fields set produces NO note;
  * a container with one produces a note that NAMES it;
  * the name is the one the reader will find in their compose file
    (`blkio_config`), not Docker's API spelling (`BlkioDeviceReadBps`).

The healthcheck cases carry their own weight, because "has a
healthcheck" is exactly the over-broad rule this replaces. A CMD-SHELL
healthcheck round-trips through `--health-cmd` byte for byte, and one
identical to the image's own is dropped on purpose so the new image can
supply it. Neither is a loss. Exec form and sub-second timings are.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from update_checker import compose_fallback_losses as L  # noqa: E402

checks = {}

NO_IMAGE_HC = {"Healthcheck": {}}


def C(host=None, cfg=None, mounts=None):
    return {"HostConfig": host or {}, "Config": cfg or {},
            "Mounts": mounts or []}


# ── nothing set: nothing to say ──────────────────────────────────────
# The plain case, and the one the change exists for: a web app with
# ports, an env file and two volumes comes back out of `docker run`
# exactly as it went in.
plain = C(host={"NetworkMode": "bridge", "RestartPolicy": {"Name": "always"},
                "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]},
                "Memory": 536870912, "Privileged": False},
          cfg={"Env": ["TZ=Europe/Berlin"], "Labels": {"a": "b"}},
          mounts=[{"Type": "bind", "Source": "/srv/x", "Destination": "/data",
                   "RW": True},
                  {"Type": "volume", "Name": "db", "Destination": "/var/lib",
                   "RW": True}])
checks["an ordinary container produces no note"] = L(plain, {}) == []
checks["…and an empty inspect dict does not blow up"] = L({}, {}) == []
checks["…nor does None"] = L(None, None) == []

# ── healthchecks: only the two forms that really degrade ─────────────
sh = {"Test": ["CMD-SHELL", "pg_isready -U app"], "Interval": 5_000_000_000,
      "Timeout": 3_000_000_000, "Retries": 5}
checks["a CMD-SHELL healthcheck round-trips, so no note"] = (
    L(C(cfg={"Healthcheck": sh}), NO_IMAGE_HC) == [])

exec_form = {"Test": ["CMD", "python", "-c", "import x; x.ping()"],
             "Interval": 10_000_000_000}
checks["an exec-form healthcheck is reported"] = (
    L(C(cfg={"Healthcheck": exec_form}), NO_IMAGE_HC) == ["healthcheck"])
# The whole reason it is a loss: `--health-cmd` can only produce
# CMD-SHELL, so the tokens get shell-joined and the check now needs
# /bin/sh in the image.
checks["…and it is the only thing reported"] = (
    len(L(C(cfg={"Healthcheck": exec_form}), NO_IMAGE_HC)) == 1)

# An image that ships its own HEALTHCHECK is the common case — reporting
# it would put the note straight back on nearly every container. It is
# dropped on recreate deliberately, so the new image supplies its own.
checks["a healthcheck inherited from the image is not a loss"] = (
    L(C(cfg={"Healthcheck": exec_form}), {"Healthcheck": exec_form}) == [])
# Same shape, one field different: now it IS the user's.
overridden = dict(exec_form, Interval=99_000_000_000)
checks["…but the same check with a changed interval is"] = (
    L(C(cfg={"Healthcheck": overridden}), {"Healthcheck": exec_form})
    == ["healthcheck"])

# `interval: 500ms` is 500000000ns, and whole-second integer division
# turns that into `--health-interval 0s`, which Docker reads as unset.
sub_second = {"Test": ["CMD-SHELL", "true"], "Interval": 500_000_000}
checks["a sub-second interval is reported"] = (
    L(C(cfg={"Healthcheck": sub_second}), NO_IMAGE_HC) == ["healthcheck"])
checks["…and a whole-second one is not"] = (
    L(C(cfg={"Healthcheck": {"Test": ["CMD-SHELL", "true"],
                             "Interval": 1_000_000_000}}), NO_IMAGE_HC) == [])

# Timings with nothing to hang them on: _build_run_args emits the
# intervals only inside `if hc_test:`.
checks["timings with no test are reported"] = (
    L(C(cfg={"Healthcheck": {"Interval": 30_000_000_000, "Retries": 3}}),
      NO_IMAGE_HC) == ["healthcheck"])

# `test: ["NONE"]` becomes --no-healthcheck, which is exact.
checks["an explicit NONE is carried, so no note"] = (
    L(C(cfg={"Healthcheck": {"Test": ["NONE"]}}), NO_IMAGE_HC) == [])

# Without the old image's config we cannot tell an override from an
# inherited healthcheck. Silence beats a false alarm on everything.
checks["no image config means the healthcheck is left alone"] = (
    L(C(cfg={"Healthcheck": exec_form}), None) == [])

# ── the HostConfig fields the recreate has no flag for ───────────────
checks["blkio_config is reported under its compose name"] = (
    L(C(host={"BlkioDeviceReadBps": [{"Path": "/dev/sda", "Rate": 1048576}]}),
      {}) == ["blkio_config"])
checks["…and so is the IOps half of it"] = (
    L(C(host={"BlkioDeviceWriteIOps": [{"Path": "/dev/sda", "Rate": 100}]}),
      {}) == ["blkio_config"])
# BlkioWeight on its own IS carried (--blkio-weight) — it must not fire.
checks["a plain blkio_weight is carried and stays quiet"] = (
    L(C(host={"BlkioWeight": 300}), {}) == [])
checks["cgroup_parent is reported"] = (
    L(C(host={"CgroupParent": "/docker-limited"}), {}) == ["cgroup_parent"])
checks["device_cgroup_rules is reported"] = (
    L(C(host={"DeviceCgroupRules": ["c 189:* rmw"]}), {})
    == ["device_cgroup_rules"])
checks["storage_opt is reported"] = (
    L(C(host={"StorageOpt": {"size": "20G"}}), {}) == ["storage_opt"])
# -P asks for a fresh random port each start; the rebuild reads the
# concrete bindings and nails last start's ports down forever.
checks["publish-all is reported"] = (
    L(C(host={"PublishAllPorts": True}), {}) == ["publish all ports (-P)"])
checks["…and PublishAllPorts=false is not"] = (
    L(C(host={"PublishAllPorts": False}), {}) == [])

# ── long-form mounts the bind/volume loop cannot see ─────────────────
checks["a long-form tmpfs mount is reported as tmpfs"] = (
    L(C(mounts=[{"Type": "tmpfs", "Destination": "/run"}]), {}) == ["tmpfs"])
# The short `tmpfs:` form lands in HostConfig.Tmpfs, which IS carried
# as --tmpfs. Only the long form is lost, and confusing the two would
# reintroduce the noise.
checks["the short tmpfs form is carried and stays quiet"] = (
    L(C(host={"Tmpfs": {"/run": "rw,size=64m"}}), {}) == [])
checks["an unknown mount type names itself"] = (
    L(C(mounts=[{"Type": "npipe", "Destination": "/x"}]), {})
    == ["volumes (type: npipe)"])

# ── ExposedPorts is deliberately NOT a finding ───────────────────────
# It is inert metadata that `docker run` re-derives from the new image's
# EXPOSE plus our -p flags, and nearly every image sets it. Reporting it
# would fire on almost everything — the exact noise being removed.
checks["ExposedPorts alone produces no note"] = (
    L(C(cfg={"ExposedPorts": {"8080/tcp": {}}}), {}) == [])

# ── several at once: named, deduplicated, in a fixed order ───────────
many = C(host={"CgroupParent": "/x", "StorageOpt": {"size": "1G"},
               "BlkioDeviceReadBps": [{"Path": "/dev/sda", "Rate": 1}]},
         cfg={"Healthcheck": exec_form},
         mounts=[{"Type": "tmpfs", "Destination": "/run"},
                 {"Type": "tmpfs", "Destination": "/tmp"},
                 {"Type": "bind", "Source": "/a", "Destination": "/b"}])
got = L(many, NO_IMAGE_HC)
checks["every set field is named, each once"] = got == [
    "healthcheck", "tmpfs", "blkio_config", "cgroup_parent", "storage_opt"]
checks["the order is fixed, so the message reads the same twice"] = (
    got == L(many, NO_IMAGE_HC))

# ── the wiring: the note only exists when there is something to say ──
import inspect as _inspect  # noqa: E402
from update_checker import UpdateChecker as U  # noqa: E402

src = _inspect.getsource(U._update_compose)
checks["the unreachable-file note is gated on a real loss"] = (
    "lost = self._fallback_loss_note()" in src
    and "if not lost:" in src)
checks["the remote-host fallback got the same note"] = (
    "compose_fallback_remote" in src)
checks["a container with no config_files label got it too"] = (
    "compose_fallback_nolabel" in _inspect.getsource(U.update_container))

note = _inspect.getsource(U._fallback_loss_note)
checks["no losses means an empty note, not a message"] = (
    'return ""' in note)

# The text goes through the translations like every other user-facing
# line, and the key has to exist in all sixteen.
import json  # noqa: E402
import glob  # noqa: E402
_lang = os.path.join(os.path.dirname(__file__), "..", "app", "lang")
_files = sorted(glob.glob(os.path.join(_lang, "*.json")))
_have = [f for f in _files
         if "compose_fallback_lost" in json.load(open(f, encoding="utf-8"))]
checks["the new keys exist in every language"] = (
    len(_files) == 16 and len(_have) == 16)

from i18n import get_translator  # noqa: E402
_rendered = get_translator("en")("compose_fallback_lost",
                                 fields="`healthcheck`, `tmpfs`")
checks["the rendered note names the fields"] = (
    "healthcheck" in _rendered and "tmpfs" in _rendered
    and "{" not in _rendered)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
