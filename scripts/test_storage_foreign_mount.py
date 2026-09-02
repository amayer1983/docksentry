#!/usr/bin/env python3
"""A volume somebody else mounted is not our lost data directory.

The check looks for a mount whose name suggests it was *meant* to be the
data directory — `/app/data` and friends, from #2 where a bind mount held
nothing because nothing read it. That hint fired on its own, and once the
data directory moved off `/data` it started accusing whatever the user had
mounted there: somebody who deliberately mounted Portainer's volume at
`/data` (to let Docksentry read Compose files) was told to make it
Docksentry's database instead, which would have buried our state inside
another tool's volume.

The hint is only evidence when our own directory is actually in trouble.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import storage_check  # noqa: E402

checks = {}

HEALTHY = {"Type": "volume", "Name": "docksentry_data",
           "Source": "/var/lib/docker/volumes/ds/_data",
           "Destination": "/docksentry"}
FOREIGN = {"Type": "volume", "Name": "portainer_data",
           "Source": "/var/lib/docker/volumes/portainer_data/_data",
           "Destination": "/data"}
ANON = {"Type": "volume", "Name": "b" * 64, "Source": "/v",
        "Destination": "/docksentry"}


def kinds(mounts, data_dir="/docksentry"):
    return [f["kind"] for f in storage_check.analyse(mounts, data_dir)]


checks["a healthy data dir plus a stranger's volume says nothing"] = (
    kinds([HEALTHY, FOREIGN]) == [])
checks["a healthy data dir on its own says nothing"] = (
    kinds([HEALTHY]) == [])

# The warnings that must survive: this is what the check is for. The hint
# still fires for a path that only makes sense as an attempt to configure
# *us* — `/app/data` is the one from #2, @famewolf — just not for `/data`,
# which is everybody's.
MISTAKE = {"Type": "bind", "Source": "/mnt/tank/docksentry",
           "Destination": "/app/data"}

checks["no data mount at all is still reported"] = (
    "unmounted" in kinds([FOREIGN]))
checks["a path that only we would use is still flagged"] = (
    "wrong_mount" in kinds([MISTAKE, HEALTHY]))
checks["an anonymous volume is still reported"] = (
    "anonymous" in kinds([ANON, FOREIGN]))
checks["the old data path is not accused of being ours"] = (
    "wrong_mount" not in kinds([ANON, FOREIGN]))

# A mount inside the data directory is documented and never a finding.
INSIDE = dict(FOREIGN, Destination="/docksentry/compose")
checks["a mount inside the data dir is ours by definition"] = (
    kinds([HEALTHY, INSIDE]) == [])

checks["nothing to look at yields nothing, not a guess"] = (
    storage_check.analyse(None) == [])

# 2.17.6 shipped with the new default path on the suspect list, and the
# image declares a VOLUME there — so Docker created an anonymous volume at
# `/docksentry` on every install whose data sat at `/data`, and the check
# told all of them their data directory was wrong. Worse, the mount it
# offered was that anonymous volume's own path: the one place the data
# really would have been thrown away (#2, @famewolf, three hosts, hours
# after release).
ANON_NAME = "7" * 64
OURS_ANON = {"Type": "volume", "Name": ANON_NAME,
             "Source": "/var/lib/docker/volumes/7777/_data",
             "Destination": "/docksentry"}
THEIR_BIND = {"Type": "bind", "Source": "/mnt/dockerdata/config",
              "Destination": "/data"}

checks["the volume our own image creates is not an accusation"] = (
    kinds([THEIR_BIND, OURS_ANON], "/data") == [])
checks["an anonymous volume is never offered as the mount to use"] = (
    kinds([dict(OURS_ANON, Destination="/config"), HEALTHY]) == [])
checks["…while a real bind at the same place still is"] = (
    "wrong_mount" in kinds([{"Type": "bind", "Source": "/mnt/x",
                             "Destination": "/config"}, HEALTHY]))
checks["the current default path is exempt like the old one"] = (
    kinds([THEIR_BIND, dict(OURS_ANON, Name="named_vol")], "/data") == [])

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
