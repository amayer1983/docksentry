#!/usr/bin/env python3
"""Self-update failure diagnostics (#43, @LeeNX).

A --rm helper container's stderr vanishes, so we've never seen WHY the podman
recreate fails. The helper now mounts /data and redirects its output there;
the next boot reads it and, if the recreate rolled back, reports the reason.

Covers:
1. _host_mount_source — resolve the host path of a container mount.
2. the boot-side rollback detection (mirrors main.py): report only when the
   log contains the 'rolling back' marker, stay silent on success.

Pure logic. Exits non-zero on any failure.
"""
import sys, os, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from telegram_bot import TelegramBot


def main():
    checks = {}

    # ── _host_mount_source ──
    hm = TelegramBot._host_mount_source
    cfg = {"Mounts": [
        {"Destination": "/var/run/docker.sock", "Source": "/run/user/1002/podman/podman.sock"},
        {"Destination": "/data", "Source": "/opt/podman.d/dockersentry/data"},
    ]}
    checks["mount source: resolves /data host path"] = hm(cfg, "/data") == "/opt/podman.d/dockersentry/data"
    checks["mount source: unknown dest → None"] = hm(cfg, "/nope") is None
    checks["mount source: no mounts → None"] = hm({}, "/data") is None

    # ── boot-side rollback detection (mirror of main.py logic) ──
    def surface(content):
        """Returns the reported detail tail, or None if silent. Mirrors the
        main.py block: report only on the 'rolling back' marker."""
        reported = {"detail": None}
        d = tempfile.mkdtemp()
        p = os.path.join(d, "selfupdate_helper.log")
        with open(p, "w") as f:
            f.write(content)
        if os.path.exists(p):
            with open(p) as f:
                c = f.read()
            os.unlink(p)
            if "rolling back" in c:
                reported["detail"] = c.strip()[-900:]
        return reported["detail"], (not os.path.exists(p))

    # failure: helper log shows the rollback marker + a podman error
    fail_log = ("+ docker stop docksentry\n+ docker rename docksentry docksentry_old\n"
                "+ docker run -d ... \nError: unknown flag: --foo\n"
                "Selfupdate recreate failed — rolling back\n")
    detail, consumed = surface(fail_log)
    checks["failure: reports on 'rolling back' marker"] = detail is not None and "unknown flag" in detail
    checks["failure: log consumed (unlinked)"] = consumed

    # success: no rollback marker → silent
    ok_log = "+ docker stop docksentry\n+ docker run -d ...\n+ docker rm docksentry_old\n"
    detail2, consumed2 = surface(ok_log)
    checks["success: no report (silent)"] = detail2 is None
    checks["success: log still consumed"] = consumed2

    for k, v in checks.items():
        print(("  ✅" if v else "  ❌"), k)
    if not all(checks.values()):
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
