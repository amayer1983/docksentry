#!/usr/bin/env python3
"""Where /data really lives, and who gets told (#2, @famewolf).

He lost his settings on a recreate, then after a `compose down`/`up`,
then again — restored from backup, reconfigured three hosts, and wrote:
"I'm afraid to restart them." Every time, all we said was "possible data
loss", which named the symptom and nothing else.

The cause was one line of his compose file:

    - /mnt/dockerdata/docker/containers/docksentry/config:/app/data

We use `/data`. Nothing in this image has ever read `/app/data`. So his
bind mount held nothing, and the real `/data` fell to the anonymous
volume our `VOLUME ["/data"]` creates — a fresh one per container,
discarded with the old one. Which is exactly why it "worked all this
time" and lost everything on every recreate.

Both mistakes are visible from inside the container, in its own mounts.
So we look on startup and say which one it is, instead of describing the
loss afterwards.

The other half is that the old alert cried wolf. Measured on a fresh
env-only install over three boots: `/data` ends up holding
`version_state.json` and nothing else, because `save_persistent()` only
ever runs from a user action. Someone who configures everything through
the environment and never saves anything in the Web UI has no
`settings.json`, has lost nothing, and was told about "possible data
loss" on every single restart.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import storage_check  # noqa: E402

checks = {}

ANON = "a" * 64
SOCK = {"Type": "bind", "Source": "/var/run/docker.sock",
        "Destination": "/var/run/docker.sock"}
TZ = {"Type": "bind", "Source": "/etc/localtime",
      "Destination": "/etc/localtime"}


def named(dest="/data"):
    return {"Type": "volume", "Name": "docksentry_data",
            "Source": "/var/lib/docker/volumes/docksentry_data/_data",
            "Destination": dest}


def anon(dest="/data"):
    return {"Type": "volume", "Name": ANON,
            "Source": f"/var/lib/docker/volumes/{ANON}/_data",
            "Destination": dest}


def bind(src, dest):
    return {"Type": "bind", "Source": src, "Destination": dest}


def kinds(mounts):
    return [f["kind"] for f in storage_check.analyse(mounts)]


# ═══ his setup, exactly ══════════════════════════════════════════════
his = [SOCK,
       bind("/mnt/dockerdata/docker/containers/docksentry/config", "/app/data"),
       anon()]
found = storage_check.analyse(his)
checks["his own mounts are diagnosed"] = sorted(f["kind"] for f in found) == [
    "anonymous", "wrong_mount"]
checks["…and the cause is reported ahead of the effect"] = (
    storage_check.summary_key(found) == "storage_wrong_mount")

lines = " ".join(storage_check.describe(found))
checks["the log names the path he actually mounted"] = "/app/data" in lines
checks["…and the one we use"] = "/data" in lines
checks["…and gives him the corrected line"] = (
    "/mnt/dockerdata/docker/containers/docksentry/config:/data" in lines)
checks["…and says an anonymous volume dies on recreate"] = (
    "recreated" in lines and "anonymous" in lines)

# ═══ a healthy install says nothing ══════════════════════════════════
checks["a named volume is fine"] = kinds([SOCK, TZ, named()]) == []
checks["a bind mount at /data is fine"] = kinds(
    [SOCK, bind("/srv/docksentry", "/data")]) == []
# The documented compose mount lives *inside* /data and must not be
# mistaken for a stray one.
checks["/data/compose is not a stray mount"] = kinds(
    [named(), bind("/opt/stacks", "/data/compose")]) == []
# Nor may ordinary infrastructure mounts be nagged about.
checks["sockets and timezones are left alone"] = kinds(
    [SOCK, TZ, bind("/run/podman/podman.sock", "/run/podman/podman.sock"),
     bind("/etc/ssl/certs", "/etc/ssl/certs"), named()]) == []

# ═══ the two ways to lose everything ═════════════════════════════════
checks["an anonymous volume at /data is flagged"] = kinds(
    [SOCK, anon()]) == ["anonymous"]
checks["nothing mounted at /data is flagged"] = kinds([SOCK]) == ["unmounted"]

# Other spellings of the same mistake.
for wrong in ("/app/data", "/config", "/docksentry", "/opt/ds/data"):
    checks[f"a data directory mounted at {wrong} is flagged"] = (
        "wrong_mount" in kinds([bind("/host/dir", wrong), named()]))

# A custom DATA_DIR is respected — the check must follow the setting,
# not a hard-coded path.
checks["a custom DATA_DIR is honoured"] = [
    f["kind"] for f in storage_check.analyse(
        [bind("/srv/x", "/var/lib/docksentry")], data_dir="/var/lib/docksentry")
] == []

# ═══ "could not look" is not "nothing is wrong" ══════════════════════
checks["an inspect we could not run yields no findings"] = (
    storage_check.analyse(None) == [])
checks["…and is a different answer from an empty mount list"] = (
    storage_check.analyse([]) != storage_check.analyse(None)
    or storage_check.analyse([])[0]["kind"] == "unmounted")


class Backend:
    def __init__(self, out, rc=0):
        self.out, self.rc = out, rc

    def run(self, args, timeout=None):
        return types.SimpleNamespace(returncode=self.rc, stdout=self.out,
                                     stderr="")


import json  # noqa: E402

checks["mounts are read from our own container"] = (
    storage_check.read_mounts(Backend(json.dumps([named()])), "docksentry")
    == [named()])
checks["a failed inspect answers 'do not know'"] = (
    storage_check.read_mounts(Backend("", rc=1), "docksentry") is None)
checks["…as does output we cannot parse"] = (
    storage_check.read_mounts(Backend("not json"), "docksentry") is None)
checks["…and not knowing our own name stops the check"] = (
    storage_check.read_mounts(Backend("[]"), "") is None)


class Exploding:
    def run(self, *a, **k):
        raise OSError("daemon gone")


checks["a broken daemon does not take startup down"] = (
    storage_check.read_mounts(Exploding(), "docksentry") is None)

# ═══ every finding has a message in every language ═══════════════════
langs = sorted(f[:-5] for f in os.listdir(
    os.path.join(os.path.dirname(__file__), "..", "app", "lang")))
missing = []
for lang in langs:
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "app",
                                    "lang", f"{lang}.json"), encoding="utf-8"))
    for kind in ("wrong_mount", "anonymous", "unmounted"):
        key = "storage_" + kind
        if key not in d:
            missing.append(f"{lang}:{key}")
            continue
        try:
            d[key].format(source="/a", dest="/b", data_dir="/data")
        except (KeyError, IndexError):
            missing.append(f"{lang}:{key} placeholders")
checks["all 16 languages carry the three storage messages"] = not missing
checks["…and none of them has a placeholder we do not pass"] = not [
    m for m in missing if "placeholders" in m]

# ═══ the alert no longer cries wolf ══════════════════════════════════
# An env-only install never creates settings.json — measured on a fresh
# instance across three boots, /data held version_state.json alone. The
# startup path must tell that apart from a volume that lost everything.
main_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                             "main.py"), encoding="utf-8").read()
i = main_src.index("storage_msg = \"\"")
block = main_src[i:i + 1600]
checks["a known cause is reported instead of the generic warning"] = (
    "t(storage_key" in block)
checks["…the old warning survives for a volume that really lost it"] = (
    "settings_ever_saved" in block and "data_loss_alert" in block)
checks["…and a surviving volume with no settings.json is not an alarm"] = (
    "No settings.json yet" in block)
checks["loss is recorded, not inferred"] = (
    "config.settings_ever_saved()" in main_src)

# ═══ "was there ever one" is recorded, not guessed ═══════════════════
import tempfile  # noqa: E402

from config import Config  # noqa: E402


def cfg(tmp):
    c = Config.__new__(Config)
    c.data_dir = tmp
    c.settings_file = os.path.join(tmp, "settings.json")
    c.settings_seen_file = os.path.join(tmp, ".settings_seen")
    return c


with tempfile.TemporaryDirectory() as tmp:
    c = cfg(tmp)
    checks["a fresh volume has never held settings"] = (
        c.settings_ever_saved() is False)
    checks["…and asking does not create one"] = not os.path.exists(
        c.settings_file)

    open(c.settings_file, "w").write("{}")
    checks["an install that predates the marker is not mistaken for fresh"] = (
        c.settings_ever_saved() is True)
    checks["…and gets the marker stamped on the spot"] = os.path.exists(
        c.settings_seen_file)

    os.unlink(c.settings_file)
    checks["a settings.json that vanishes is now known to be a loss"] = (
        c.settings_ever_saved() is True)

with tempfile.TemporaryDirectory() as tmp:
    c = cfg(tmp)
    c.mark_settings_seen()
    first = open(c.settings_seen_file).read()
    c.mark_settings_seen()
    checks["marking twice is harmless"] = open(c.settings_seen_file).read() == first

# It must never become a setting: a value in the data directory that can
# outrank an environment variable is the #53 shape, and this file exists
# precisely because that shape has bitten this project repeatedly.
cfg_src = open(os.path.join(os.path.dirname(__file__), "..", "app",
                            "config.py"), encoding="utf-8").read()
checks["the marker is not a persistent setting"] = (
    "settings_seen" not in cfg_src.split("PERSISTENT_KEYS = [")[1].split("]")[0])
# Scoped to the function, not to a fixed window — the first version
# sliced 900 characters after the `def` and started failing the moment
# save_persistent grew a comment explaining what it writes.
_sp = cfg_src[cfg_src.index("    def save_persistent(self):"):]
_sp = _sp[:_sp.index("\n    def ", 10)]
checks["…and every save records it"] = "self.mark_settings_seen()" in _sp

# ═══ scan bookkeeping leaves the ordinary log ════════════════════════
# @NotRetarded, same thread: "Any idea what the skipped self in the logs
# are doing?" — four `Skipped (self): DockSentry` lines. Nothing is
# wrong; we exclude our own container from the regular update path
# because updating yourself through it kills PID 1 mid-swap (#16). But a
# line that makes someone ask what is broken is costing more attention
# than it is worth, and it was printed on every check, for everyone.
#
# The failure diagnostics stay unconditional, deliberately: @famewolf's
# `Stop …: effective_stop=60s, subprocess=90s` came from a debug-OFF log
# and is what made #2 readable at all.
import types as _t  # noqa: E402

from update_checker import UpdateChecker  # noqa: E402


def uc(debug):
    c = UpdateChecker.__new__(UpdateChecker)
    c.config = _t.SimpleNamespace(debug=debug)
    c.debug_log = []
    return c


import io  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402


def emitted(checker, method, msg):
    out = io.StringIO()
    with redirect_stdout(out):
        getattr(checker, method)(msg)
    return out.getvalue()


checks["routine scan lines are silent without debug"] = (
    emitted(uc(False), "_trace", "  Skipped (self): DockSentry") == "")
checks["…and present with it"] = (
    "Skipped (self)" in emitted(uc(True), "_trace", "  Skipped (self): x"))
checks["…and still reach the Web UI's debug view"] = (
    len(uc(True).debug_log) == 0)  # fresh checker starts empty
c = uc(True)
with redirect_stdout(io.StringIO()):
    c._trace("  Skipped (self): x")
checks["…recorded for the debug view too"] = c.debug_log == [
    "  Skipped (self): x"]

checks["failure diagnostics stay unconditional"] = (
    "Stop foo" in emitted(uc(False), "_debug", "  Stop foo: effective_stop=60s"))

src_uc = open(os.path.join(os.path.dirname(__file__), "..", "app",
                           "update_checker.py"), encoding="utf-8").read()
# Match on the emitting lines themselves rather than on a window of
# characters around the first hit — the docstrings above quote these
# strings, and a source grep that cannot tell prose from code proves
# nothing.
emit_lines = [ln.strip() for ln in src_uc.splitlines()
              if "self._trace(" in ln or "self._debug(" in ln]


def channel(marker):
    hits = [ln for ln in emit_lines if marker in ln]
    return {ln.split("self._", 1)[1].split("(")[0] for ln in hits} or {"none"}


for noisy in ("Skipped (self)", "Skipped (excluded)", "Skipped (pinned",
              "Skipped (image ID)", "Resolved image ID", "  Checking: "):
    checks[f"'{noisy.strip()}' goes through _trace"] = channel(noisy) == {"trace"}
for loud in ("Rollback: restored", "escalating to kill", "Stop failed",
             "Dependent recreate failed", "Registry error: HTTP"):
    checks[f"'{loud}' stays on _debug"] = channel(loud) == {"debug"}

# ═══ a backup names the machine it came from ═════════════════════════
# "I backup 3 hosts to my pc currently and end up with this: […] No clue
# what host they are from." Restoring the wrong one puts another
# machine's groups and pins on this one.
#
# The naming moved into backup.py when the Telegram `/backup` command
# and the automatic local copies needed the same bundle — one builder,
# three callers, rather than three that drift.
import backup as _bk  # noqa: E402

lbl = types.SimpleNamespace(bot_label="\U0001f5a5  dockmox.lan")
checks["the backup file is named after the instance"] = (
    _bk.filename(lbl).startswith("docksentry-backup-dockmox.lan-"))
checks["…sanitised, so a label with emoji cannot break the filename"] = (
    _bk.instance_slug(lbl) == "dockmox.lan")
checks["…and a hostile one cannot escape the directory"] = (
    "/" not in _bk.filename(
        types.SimpleNamespace(bot_label="../../etc/passwd")))
os.environ.pop("HOSTNAME", None)
checks["…and an unlabelled instance keeps the old name"] = (
    _bk.filename(types.SimpleNamespace(bot_label=""))
    == f"docksentry-backup-{_bk.datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
os.environ["HOSTNAME"] = "docknas"
checks["a hostname somebody chose stands in for a missing label"] = (
    _bk.instance_slug(types.SimpleNamespace(bot_label="")) == "docknas")
# Seen for real on this developer's instance the first time a file was
# written: HOSTNAME in a container is normally the container id, and
# `docksentry-backup-9cef9348bc8f-…` is worse than no name — it looks
# like it means something.
os.environ["HOSTNAME"] = "9cef9348bc8f"
checks["a container id is not mistaken for a machine name"] = (
    _bk.instance_slug(types.SimpleNamespace(bot_label="")) == "")
os.environ.pop("HOSTNAME", None)

# The Web UI export goes through that same builder rather than keeping
# its own copy of the format.
_web = open(os.path.join(os.path.dirname(__file__), "..", "app",
                         "web_ui.py"), encoding="utf-8").read()
checks["the Web UI export uses the shared builder"] = (
    "_backup.payload(config, store, VERSION)" in _web
    and "_backup.filename(config)" in _web)

# ═══ the local copy has to cover the state it is protecting ══════════
# Found against a real container, not reasoned about: a fresh install
# writes a copy on its first boot, when nothing is configured and
# `settings` is empty. Configure an hour later, lose settings.json,
# restart — and the copy being kept is the empty one from before you
# started. It restored nothing, correctly, and looked like the feature
# was broken.
import tempfile as _tf  # noqa: E402
import time as _time  # noqa: E402


class _Store:
    def get_pinned(self): return []
    def get_autoupdate(self): return []
    def get_ask_before_major(self): return []
    def get_groups(self): return {}
    def get_notes(self): return {}
    def get_links(self): return {}
    def get_update_windows(self): return {}


with _tf.TemporaryDirectory() as tmp:
    c = types.SimpleNamespace(data_dir=tmp, bot_label="docknas.lan",
                              settings_file=os.path.join(tmp, "settings.json"))
    st = _Store()
    first = _bk.write_local_if_stale(c, st, "2.9.3", min_gap_seconds=0)
    checks["a first boot writes a copy"] = bool(first)
    checks["…even with nothing saved yet"] = (
        json.loads(open(first).read())["settings"] == {})

    # Nothing changed → no second copy.
    checks["an unchanged instance does not write again"] = (
        _bk.write_local_if_stale(c, st, "2.9.3", min_gap_seconds=0) == "")

    # Settings appear → a copy that actually contains them must follow,
    # long before the twelve-hour age guard would allow one.
    open(c.settings_file, "w").write('{"cron_schedule": "0 5 * * *"}')
    os.utime(c.settings_file, (_time.time() + 2, _time.time() + 2))
    second = _bk.write_local_if_stale(c, st, "2.9.3", min_gap_seconds=0)
    checks["a change writes a copy that contains it"] = (
        bool(second) and json.loads(open(second).read())
        ["settings"]["cron_schedule"] == "0 5 * * *")

    # The burst guard still holds where it belongs — the request path.
    open(c.settings_file, "w").write('{"cron_schedule": "0 6 * * *"}')
    os.utime(c.settings_file, (_time.time() + 4, _time.time() + 4))
    checks["a burst of saves collapses into one file"] = (
        _bk.write_local_if_stale(c, st, "2.9.3", min_gap_seconds=300) == "")
    checks["…and the boot path is not held back by it"] = bool(
        _bk.write_local_if_stale(c, st, "2.9.3", min_gap_seconds=0))

    checks["state_mtime sees every file the bundle is built from"] = (
        "settings.json" in _bk.STATE_FILES and "groups.json" in _bk.STATE_FILES
        and len(_bk.STATE_FILES) == 8)

# And the boot path must not archive a loss it could not repair —
# five wipes would otherwise leave five backups of nothing.
checks["a boot that lost settings does not overwrite the good copies"] = (
    "if not (settings_missing and settings_ever_saved):" in main_src)

failed = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'PASS' if v else 'FAIL'} {k}")
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
