#!/usr/bin/env python3
"""`<name>_old` containers piled up, and nothing ever removed them.

Every update renames the running container to `<name>_old` before creating
its replacement, and drops that backup once the new one is healthy. A run
whose process died in between left one behind — and then it stayed, because
*nothing in Docksentry has ever removed a leftover backup container*.

`cleanup_images` prunes images. `_prune_old_backups`, despite the name,
deletes backup DIRECTORIES on disk. `recovery.py` walked past them on
purpose, on the stated grounds that "`<name>_old` is then an ordinary
backup, which the cleanup grace period owns" — a comment describing
something that does not exist.

@LeeNX found three of them and reasonably concluded his containers were not
updating (#56). They were updating fine. What he was looking at was the
debris.

Three changes, and this asserts all three:

**The successful-update path forced its removal.** One call site used
`rm(old_name, timeout=30)` where every other used `force=True`, and its
exit code is not read. Measured against a real container:

    docker rm <running>   -> exit 1, "container is running: stop the
                             container before removing or force remove"
    docker rm <stopped>   -> exit 0

so in the ordinary case it worked and the missing `force` bought nothing —
but a silent `rm` whose failure nobody notices is precisely how debris
accumulates, and `_rollback_to_old` had the identical bug and was fixed.

**Recovery removes its own backup.** Only when the live container is
present — that is the proof the swap finished — and only the name from its
own in-flight note. It never goes looking for `*_old` containers in
general: one somebody else named that way is theirs.

**The debris already out there is reported, not deleted.** It is visible in
the container table with nothing saying what it is. The Status page names
them and offers the command. Removing containers this process did not
create is the operator's call, which is why there is no button.
"""

import os
import re
import sys

import srcslice

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

APP = os.path.join(os.path.dirname(__file__), "..", "app")


def src(name):
    return open(os.path.join(APP, name), encoding="utf-8").read()


def main():
    checks = {}

    # ── 1. every backup removal forces ───────────────────────────
    uc = src("update_checker.py")
    unforced = re.findall(r"\.rm\(old_name,(?![^)]*force=True)[^)]*\)", uc)
    checks["no backup removal is left unforced"] = not unforced
    if unforced:
        print(f"    unforced: {unforced}")
    checks["and there are several of them, all forced"] = (
        uc.count("rm(old_name, force=True") >= 3)

    # ── 2. recovery clears the backup it knows is stale ──────────
    rec = src("recovery.py")
    branch = srcslice.section(rec, "if live:", "if not backup:",
                              label="recovery.py: the live branch")
    checks["recovery removes the backup once the swap is proven done"] = (
        branch is not None
        and "backend.rm(old_name" in branch and "force=True" in branch)
    # …and only then. Removing it in the other branches would destroy the
    # copy the recovery itself is about to restore.
    checks["…and only in that branch"] = rec.count("backend.rm(old_name") == 1
    checks["…guarded on the backup actually existing"] = (
        branch is not None and "if backup:" in branch)

    # ── 3. the debris already out there is named, not deleted ────
    web = src("web_ui.py")
    # Bounded by the end of the block, not by a character count: a fixed
    # window silently stops covering the code as soon as someone adds a
    # comment, and the assertion then fails for a reason that has nothing
    # to do with what it is about.
    seg = srcslice.section(web, "leftovers = []",
                           "leftovers = sorted(set(leftovers))",
                           label="web_ui.py: the leftover loop")
    checks["the page lists leftovers by name"] = (
        seg is not None and '_old"' in seg)
    # Only where the live container is present too — otherwise a
    # container someone deliberately named `foo_old` gets accused of
    # being our debris.
    # Per host: a `foo_old` on one machine and a `foo` on another are
    # unrelated, and pairing them would accuse an innocent container of
    # being our debris.
    checks["…only when the live container is there as well"] = (
        seg is not None and "n[:-4] in _live" in seg)
    # And it looks at ALL containers, not the running ones: a leftover
    # backup is stopped, so the first version of this searched the table
    # above and could never have found one. Measured on the demo — the
    # notice stayed invisible with a leftover sitting right there.
    checks["…found among all containers, not just the running ones"] = (
        seg is not None and "ps(all=True" in seg)
    checks["…matched within one host, not across them"] = (
        seg is not None and "for _v in views" in seg)
    checks["…and offers a command rather than removing anything"] = (
        seg is not None and "docker rm" in seg)
    # The Web UI must not grow a quiet delete for this. Removing a
    # container this process did not create is the operator's call.
    checks["nothing in the Web UI removes them for you"] = (
        seg is not None
        and ("_old" not in web.split("leftovers = []")[0]
             or "rm(" not in seg))

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
