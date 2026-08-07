#!/usr/bin/env python3
"""A container that has written nothing must not close the connection.

**What broke.** v1.73.0 merged a container's two output streams at the
pipe (`stderr=subprocess.STDOUT`) so `/logs` stopped showing half the
output — the fix for #2. What it did not account for is that subprocess
leaves `.stderr` as `None` when it was redirected away: there is no
second pipe, so nothing was captured on one. Every caller in this
codebase reads

    output = result.stdout or result.stderr

which was correct for years, and for a container with no output at all
now evaluates to `None`. The `.strip()` on the next line raised, the
handler died mid-response, and the Web UI answered with a closed
connection and no body — not a 500, nothing.

Measured against a `sleep`-only container before the fix:

    curl "http://localhost:9091/logs?container=redis-cache"
      -> exit 52, empty reply from server, 0 bytes
    docker logs docksentry-demo
      -> AttributeError: 'NoneType' object has no attribute 'strip'

and after it, HTTP 200 with "No logs found".

**Where the fix belongs.** Not at the call sites. `container_backend`'s
own docstring says the merge lives in `logs()` "so a fifth call site
cannot get it wrong" — and then three call sites got it wrong anyway, by
building `["logs", …]` by hand and never reaching the method. So the
normalisation goes at the seam, and this file also asserts that nobody
hand-builds that argv any more.

`""` rather than `None` is not a white lie: to subprocess the two mean
"no pipe" and "an empty pipe", but to a caller they mean the same thing
here — there is no separate stderr content, because all of it is in
stdout.
"""

import os
import re
import subprocess
import sys

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)

from container_backend import ContainerBackend


class PythonBackend(ContainerBackend):
    """A backend whose "CLI" is this interpreter.

    The suite runs without a container runtime for all but six
    procedures, and this check is about `run()`'s stream handling, not
    about docker. Pointing `cli_binary` at `sys.executable` exercises
    the real method — argv construction, merge branch and all — against
    a program that is guaranteed to exist.
    """
    cli_binary = sys.executable


def main():
    checks = {}
    b = PythonBackend()

    # ── the crash, in the shape the Web UI hit it ────────────────
    # A program that writes nothing, exactly like `docker logs` on a
    # container that has written nothing.
    r = b.run(["-c", "pass"], merge_stderr=True)
    checks["a silent command leaves stderr not-None"] = r.stderr is not None
    checks["…and it is the empty string"] = r.stderr == ""
    try:
        (r.stdout or r.stderr).strip()
        survived = True
    except AttributeError:
        survived = False
    checks["the call site's own idiom no longer raises"] = survived

    # ── the merge itself still works ─────────────────────────────
    prog = ("import sys\n"
            "sys.stdout.write('out\\n'); sys.stdout.flush()\n"
            "sys.stderr.write('err\\n'); sys.stderr.flush()\n")
    r = b.run(["-c", prog], merge_stderr=True)
    merged = r.stdout or r.stderr
    checks["stdout survives the merge"] = "out" in merged
    checks["stderr survives the merge"] = "err" in merged

    # ── and the unmerged path is untouched ───────────────────────
    # Everything that is not `logs()` still gets two separate streams;
    # normalising the merge branch must not have leaked into it.
    r = b.run(["-c", prog])
    checks["an unmerged call still separates the streams"] = (
        r.stdout.strip() == "out" and r.stderr.strip() == "err")

    # ── nobody hand-builds the logs argv any more ────────────────
    # This is the check that would have caught it: three front ends
    # each assembled `["logs", …]` themselves and so never reached the
    # method that does the merging. `container_backend` is where that
    # list is allowed to exist.
    offenders = []
    for name in sorted(os.listdir(APP)):
        if not name.endswith(".py") or name == "container_backend.py":
            continue
        src = open(os.path.join(APP, name), encoding="utf-8").read()
        # Strip comments so the explanation above a fixed call site is
        # not read as the call site itself.
        code = "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())
        for m in re.finditer(r'\[\s*"logs"\s*,', code):
            line = code[:m.start()].count("\n") + 1
            offenders.append(f"{name}:{line}")
    checks["no front end builds its own logs argv"] = not offenders
    if offenders:
        print("  hand-built logs argv still at: " + ", ".join(offenders))

    # And the method they should call does ask for the merge.
    src = open(os.path.join(APP, "container_backend.py"), encoding="utf-8").read()
    seg = src[src.index("def logs(self"):]
    seg = seg[:seg.index("\n    def ", 1)]
    checks["logs() asks for the merged pipe"] = "merge_stderr=True" in seg

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
