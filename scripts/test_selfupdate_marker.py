#!/usr/bin/env python3
"""Test for the self-update restart marker (#2, @famewolf).

A self-update recreate arrives as SIGTERM, which the signal handler records
as a generic external stop. _do_selfupdate writes a marker so the next boot
can tell "this SIGTERM was my own self-update" and NOT print the misleading
"restarted after an external stop signal" line.

This mirrors main.py's consume logic (which is inline in main()) against the
real atomic_write_json used to produce the marker. Fresh marker → suppress;
stale (>1h) → ignore; absent → not a self-update. Exits non-zero on failure.
"""
import sys, os, json, time, tempfile, types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from container_store import atomic_write_json
from telegram_bot import TelegramBot
from update_checker import UpdateChecker
from i18n import get_translator
from version import VERSION


def consume(path):
    """Mirror of main.py's self-update-marker consume logic."""
    selfupdate_restart = False
    if os.path.exists(path):
        with open(path) as f:
            m = json.load(f)
        if time.time() - float(m.get("ts", 0) or 0) < 3600:
            selfupdate_restart = True
        os.unlink(path)
    return selfupdate_restart


def main():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "selfupdate_restart.json")

    atomic_write_json(p, {"image": "repo:latest", "ts": time.time()})
    fresh = consume(p)
    consumed = not os.path.exists(p)

    atomic_write_json(p, {"image": "repo:latest", "ts": time.time() - 4000})
    stale = consume(p)

    absent = consume(p)

    # ── WRITE path: _do_selfupdate's marker write must land on
    # self.config.selfupdate_marker_file. Regression guard for the bug where
    # it read the path off the docker-inspect dict instead (AttributeError →
    # marker never written → every self-update mislabelled as external SIGTERM
    # since v1.26.2). The write must then round-trip through the consume logic.
    wp = os.path.join(d, "selfupdate_restart_write.json")
    bot = types.SimpleNamespace(config=types.SimpleNamespace(selfupdate_marker_file=wp))
    TelegramBot._write_selfupdate_marker(bot, "repo:latest")
    written = os.path.exists(wp)
    roundtrip = written and consume(wp) is True

    # ── version line in the self-update message (#41 follow-up) ──
    # Shows v_old → v_new (read from the target image's version label)
    # instead of opaque dates/hashes; omitted when the label is unreadable.
    vbot = types.SimpleNamespace(t=get_translator("en"))
    _orig_lbl = UpdateChecker.image_version_label
    try:
        UpdateChecker.image_version_label = staticmethod(lambda img: "9.9.9")
        line = TelegramBot._selfupdate_version_line(vbot, "img")
        line_ok = (f"v{VERSION}" in line) and ("v9.9.9" in line) and line.endswith("\n")
        UpdateChecker.image_version_label = staticmethod(lambda img: "")
        empty_ok = TelegramBot._selfupdate_version_line(vbot, "img") == ""
    finally:
        UpdateChecker.image_version_label = staticmethod(_orig_lbl)

    checks = {
        "fresh marker -> self-update (suppress external-signal line)": fresh is True,
        "marker consumed (deleted)": consumed,
        "stale marker (>1h) -> ignored": stale is False,
        "absent marker -> not a self-update": absent is False,
        "_write_selfupdate_marker writes to self.config path": written,
        "written marker round-trips through consume -> self-update": roundtrip,
        "version line shows v_old -> v_new when label readable": line_ok,
        "version line omitted when label unreadable": empty_ok,
    }
    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
