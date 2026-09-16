#!/usr/bin/env python3
"""We are PID 1, so orphans land on us — and now we collect them.

Measured on a live four-host install: **12074 defunct `ssh` processes**,
9839 of them in one hour, until the container could not fork and every
host failed at once with `pthread_create failed: Resource temporarily
unavailable` — the `tcp://` ones included. The orphans are the ssh
masters `ControlPersist` backgrounds; the docker client that started
them exits, they are reparented to PID 1, and nobody waited for them.
Five days after the last restart it was at 12069 again.

An init in front of the entrypoint would be the textbook answer, and
2.17.10 shipped one. Changing ENTRYPOINT broke the self-update for every
container running older code — twice — so the entrypoint stays and the
leak is fixed from inside.

Two things have to hold, and only one of them is "the zombies go away".
The other is that `subprocess.run()` keeps every exit code it is owed:
a reaper calling `waitpid(-1)` races it for its own children and can
take a status out from under it, so an update would report a success it
never had. This collects only zombies whose command is `ssh` — a thing
Docksentry never starts — each by its own PID.

The behavioural half runs inside a freshly built image as PID 1, because
that is the only place the mechanism exists. Without Docker it skips.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import reaper                                              # noqa: E402

checks = {}
ROOT = os.path.join(os.path.dirname(__file__), "..")

import ast


def _code_only(path):
    """The source with every docstring removed — the rules below are
    about what the code DOES, and a docstring that explains why we do
    not call waitpid(-1) must not read as a call to it."""
    text = open(path, encoding="utf-8").read()
    tree = ast.parse(text)
    lines = text.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and node.body and isinstance(node.body[0], ast.Expr):
                d = node.body[0]
                for i in range(d.lineno - 1, d.end_lineno):
                    lines[i] = ""
    return "\n".join(lines)


# ── the rules, readable in the source ────────────────────────────────
src = _code_only(os.path.join(ROOT, "app", "reaper.py"))
checks["only commands we never start ourselves are collected"] = (
    reaper.FOREIGN == frozenset({"ssh"}))
checks["never waitpid(-1) — that races subprocess for its children"] = (
    re.search(r"waitpid\(\s*-1", src) is None)
checks["…each zombie is collected by its own PID"] = (
    "os.waitpid(pid, os.WNOHANG)" in src)
checks["only zombies are touched, never live processes"] = (
    'state == "Z"' in src)
checks["…and only children of PID 1"] = 'ppid == "1"' in src

# ── off duty when not PID 1 ──────────────────────────────────────────
# Under an init, or outside a container, orphans are somebody else's.
checks["does nothing when not PID 1"] = (
    os.getpid() != 1 and reaper.reap_once() == (0, 0))
checks["…and does not start a thread either"] = (
    os.getpid() != 1 and reaper.start() is False)

# ── wired in ─────────────────────────────────────────────────────────
main_src = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
checks["main starts it"] = "reaper.start()" in main_src
checks["…after the scheduler, never in the way of startup"] = (
    main_src.index("scheduler.start()") < main_src.index("reaper.start()"))

# ── the entrypoint stays what it was ─────────────────────────────────
# Fixing the leak from inside is the whole point: changing ENTRYPOINT
# broke the self-update for every container on older code, twice.
dockerfile = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
checks["the image still starts the app directly, no init in front"] = (
    'ENTRYPOINT ["python3", "/app/main.py"]' in dockerfile)

# ── the SIGCHLD ban still holds across the whole app ─────────────────
loose = []
for name in sorted(os.listdir(os.path.join(ROOT, "app"))):
    if name.endswith(".py"):
        code = _code_only(os.path.join(ROOT, "app", name))
        if "SIGCHLD" in code or re.search(r"waitpid\(\s*-1", code):
            loose.append(name)
checks["no home-made SIGCHLD reaper anywhere"] = loose == []

# ── the behaviour, as PID 1 in the real image ────────────────────────
have_docker = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
if not have_docker:
    print("SKIP: no Docker daemon available — behavioural half not run")
else:
    probe = r'''
import os, subprocess, sys, time, threading
sys.path.insert(0, "/app")
import reaper
os.makedirs("/tmp/fake", exist_ok=True)
# A binary called `ssh` that just sleeps; only the name matters.
open("/tmp/fake/ssh", "w").write("#!/bin/sh\nsleep \"$1\"\n")
os.chmod("/tmp/fake/ssh", 0o755)
def zombies():
    z = {}
    for p in os.listdir("/proc"):
        if not p.isdigit(): continue
        try: st = open(f"/proc/{p}/status").read()
        except OSError: continue
        if "\nState:\tZ" in st:
            z[st.split("\n")[0].split("\t")[1]] = z.get(st.split("\n")[0].split("\t")[1], 0) + 1
    return z
# A stranger of another name, mixed in: it must be left alone and counted.
open("/tmp/fake/other", "w").write("#!/bin/sh\nsleep \"$1\"\n")
os.chmod("/tmp/fake/other", 0o755)
for _ in range(120):
    subprocess.run(["sh", "-c", "( /tmp/fake/ssh 0.2 ) & exit 0"], check=False)
for _ in range(20):
    subprocess.run(["sh", "-c", "( /tmp/fake/other 0.2 ) & exit 0"], check=False)
time.sleep(1.5)
before = zombies().get("ssh", 0)
strangers_before = zombies().get("other", 0)
codes = []
def own():
    for i in range(40):
        codes.append(subprocess.run(["sh", "-c", f"exit {i % 7}"]).returncode)
t = threading.Thread(target=own); t.start()
reaped, left = reaper.reap_once()
t.join()
time.sleep(0.3)
after = zombies().get("ssh", 0)
strangers_after = zombies().get("other", 0)
ok_codes = codes == [i % 7 for i in range(40)]
print(f"RESULT pid={os.getpid()} before={before} reaped={reaped} left={left} after={after} codes_ok={ok_codes} strangers={strangers_before}/{strangers_after}")
'''
    build = subprocess.run(["docker", "build", "-q", "-t", "docksentry-reaper-test", ROOT],
                           capture_output=True, text=True, timeout=900)
    if build.returncode != 0:
        print("SKIP: image build failed —", build.stderr.strip()[-200:])
    else:
        run = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "python3",
             "docksentry-reaper-test", "-c", probe],
            capture_output=True, text=True, timeout=120)
        m = re.search(r"RESULT pid=(\d+) before=(\d+) reaped=(\d+) left=(\d+) "
                      r"after=(\d+) codes_ok=(\w+) strangers=(\d+)/(\d+)", run.stdout)
        if not m:
            print("probe output:", (run.stdout + run.stderr)[-400:])
        pid, before, reaped, left, after, codes_ok, s_before, s_after = (
            m.groups() if m else ("0",) * 5 + ("False", "0", "0"))
        checks["the probe ran as PID 1"] = pid == "1"
        checks["orphaned ssh processes did pile up without a reaper"] = int(before) >= 100
        checks["one pass collected every one of them"] = (
            int(reaped) == int(before) and int(after) == 0)
        # The boundary, measured: zombies of another name are not touched
        # — and they are counted, so a second leak would show, not hide.
        checks["a zombie of another name is left alone"] = (
            int(s_before) >= 15 and int(s_after) == int(s_before))
        checks["…and counted rather than hidden"] = int(left) == int(s_before)
        checks["subprocess.run kept all 40 of its own exit codes meanwhile"] = (
            codes_ok == "True")
        subprocess.run(["docker", "rmi", "-f", "docksentry-reaper-test"],
                       capture_output=True)

bad = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(f"  {'✅' if v else '❌'} {k}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
