#!/usr/bin/env python3
"""A fresh install stops claiming /data; an existing one keeps it.

`/data` is a busy name — Portainer keeps its stacks there, and our own
shipped compose file tried to mount them at `/data/compose`, straight over
our own state directory (#2). New installs get `/docksentry` instead.

The half that matters is the other one: an install that already has a
volume mounted at `/data` must keep using it. Moving the default out from
under it would leave it on an empty directory with its data still sitting
in the volume, and nothing would say so until someone noticed their
history was gone.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import config as cfg  # noqa: E402

checks = {}

_real_ismount = os.path.ismount


def _default_with(mounted):
    os.path.ismount = lambda p: p == cfg.LEGACY_DATA_DIR and mounted
    try:
        return cfg._default_data_dir()
    finally:
        os.path.ismount = _real_ismount


checks["a volume mounted at /data wins — that install keeps working"] = (
    _default_with(True) == "/data")
checks["nothing mounted there: the fresh default"] = (
    _default_with(False) == "/docksentry")
checks["the fresh default is not /data"] = (
    cfg.DEFAULT_DATA_DIR != cfg.LEGACY_DATA_DIR)


def _raises(_p):
    raise OSError("stat failed")


os.path.ismount = _raises
try:
    checks["a failing stat falls forward, it does not crash the boot"] = (
        cfg._default_data_dir() == "/docksentry")
finally:
    os.path.ismount = _real_ismount

# DATA_DIR still beats both, which is how anyone overrides this.
_env = os.environ.get("DATA_DIR")
os.environ["DATA_DIR"] = "/somewhere/else"
try:
    checks["DATA_DIR still overrides the default"] = (
        cfg._env("DATA_DIR", cfg._default_data_dir()) == "/somewhere/else")
finally:
    if _env is None:
        del os.environ["DATA_DIR"]
    else:
        os.environ["DATA_DIR"] = _env

# The image must not reserve /data any more, or the mount check above can
# never fire: Docker creates a mount for every VOLUME, so /data would
# always look deliberate.
_df = open(os.path.join(os.path.dirname(__file__), "..", "Dockerfile")).read()
checks["the image no longer reserves /data"] = ('VOLUME ["/data"]' not in _df)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
