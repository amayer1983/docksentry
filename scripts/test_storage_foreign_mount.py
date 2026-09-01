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

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
