#!/usr/bin/env python3
"""Event watcher: parsing, evidence lifetime, burst sharing, reconnect.

The parsing half is the part most likely to rot. Docker and Podman agree on
almost nothing in this payload — different key for the action, different
key for the name, different type for the exit code, different clock format
— and every one of those was measured off a live daemon rather than read
off a doc page. Fixtures below are verbatim captures.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from event_watcher import (BURST_WINDOW, EventWatcher, MAX_EVIDENCE,
                           parse_event)

# Verbatim from `docker events --format '{{json .}}'`, 2026-08-01.
DOCKER_DIE = json.dumps({
    "Type": "container", "Action": "die",
    "Actor": {"ID": "a7d954", "Attributes": {
        "execDuration": "1", "exitCode": "137",
        "image": "alpine:latest", "name": "dsev"}},
    "scope": "local", "time": 1785612907,
    "timeNano": 1785612907255524400})

DOCKER_OOM = json.dumps({
    "Type": "container", "Action": "oom",
    "Actor": {"ID": "b1c2d3", "Attributes": {
        "image": "alpine:latest", "name": "dsoom"}},
    "scope": "local", "time": 1785612929,
    "timeNano": 1785612929506615376})

# Verbatim from `podman events --format '{{json .}}'`, same day. Note every
# single field differs from Docker's.
PODMAN_DIE = json.dumps({
    "ContainerExitCode": 3, "ID": "fcf2ab", "Image": "docker.io/library/alpine:latest",
    "Name": "dslat", "Status": "died",
    "Time": "2026-08-01T21:36:59.258167461+02:00",
    "Type": "container", "Attributes": None})

PODMAN_CLEAN = json.dumps({
    "ContainerExitCode": 0, "ID": "aaa", "Image": "alpine",
    "Name": "helper", "Status": "died", "Type": "container"})


class FakeBackend:
    """Emits fixed lines then ends the stream, like a daemon restart."""

    cli_binary = "fake"

    def __init__(self, lines):
        self.lines = lines
        self.starts = 0

    def build(self, args):
        self.starts += 1
        script = "".join(f"print({l!r}, flush=True)\n" for l in self.lines)
        return [sys.executable, "-c", script]


def main():
    checks = {}

    # ── parsing ───────────────────────────────────────────────────
    checks["docker die parses"] = parse_event(DOCKER_DIE) == ("die", "dsev", 137, False)
    checks["docker oom parses"] = parse_event(DOCKER_OOM) == ("oom", "dsoom", None, True)
    # Podman's exit code arrives as an int under a different key entirely.
    checks["podman died parses as die"] = parse_event(PODMAN_DIE) == ("die", "dslat", 3, False)
    checks["podman clean exit parses"] = parse_event(PODMAN_CLEAN) == ("die", "helper", 0, False)
    # Noise must never raise — one odd line cannot be allowed to end a
    # stream that is otherwise healthy.
    for junk in ("", "not json", "[]", "null", '{"Type":"image","Action":"pull"}',
                 '{"Action":"die"}', '{"Action":"start","Actor":{"Attributes":{"name":"x"}}}'):
        checks[f"noise ignored: {junk[:22]!r}"] = parse_event(junk) is None
    # Docker appends a scope to some actions; the head is what matters.
    checks["exec_die is not a container death"] = parse_event(json.dumps(
        {"Action": "exec_die: /bin/sh", "Actor": {"Attributes": {"name": "x"}}})) is None

    # ── evidence lifetime ─────────────────────────────────────────
    w = EventWatcher(FakeBackend([]), lambda: "SNAP")
    w._handle(DOCKER_DIE)
    checks["evidence recorded on a crash"] = w.evidence("dsev") == "SNAP"
    # Consumed: a second alert for the same death must not quote the first
    # one's picture as if it were freshly taken.
    checks["evidence is consumed"] = w.evidence("dsev") == ""

    w2 = EventWatcher(FakeBackend([]), lambda: "SNAP")
    w2._handle(DOCKER_DIE)
    checks["stale evidence is dropped"] = w2.evidence("dsev", ttl=-1) == ""

    # A clean exit is a normal ending; the poller stays quiet for those, so
    # snapshotting every `--rm` helper that finishes is pure cost.
    calls = []
    w3 = EventWatcher(FakeBackend([]), lambda: calls.append(1) or "SNAP")
    w3._handle(PODMAN_CLEAN)
    checks["clean exit records nothing"] = w3.evidence("helper") == ""
    checks["clean exit costs no stats call"] = len(calls) == 0

    # ── OOM ordering ──────────────────────────────────────────────
    # Docker sends `oom` ~100ms BEFORE `die`. The later event must not
    # erase the more informative flag the earlier one carried.
    w4 = EventWatcher(FakeBackend([]), lambda: "SNAP")
    w4._handle(DOCKER_OOM)
    w4._handle(json.dumps({"Action": "die", "Actor": {"Attributes": {
        "name": "dsoom", "exitCode": "137"}}}))
    checks["die does not erase the oom flag"] = w4.saw_oom("dsoom")
    checks["saw_oom peeks, does not consume"] = w4.evidence("dsoom") == "SNAP"

    # ── the exit code inspect cannot give ─────────────────────────
    # A crash-restarted container is RUNNING again when the poller looks,
    # and a running container reports ExitCode 0 — measured, and true of
    # the previous sweep too. The stream is the only source of the real
    # number, and that number is what tells "it crashed" from "I stopped
    # it".
    w9 = EventWatcher(FakeBackend([]), lambda: "SNAP")
    w9._handle(DOCKER_DIE)
    checks["exit code comes from the stream"] = w9.exit_code("dsev") == 137
    checks["exit code peeks, does not consume"] = w9.evidence("dsev") == "SNAP"
    checks["no event, no exit code"] = w9.exit_code("never-died") is None
    w10 = EventWatcher(FakeBackend([]), lambda: "SNAP")
    w10._handle(PODMAN_DIE)
    checks["podman exit code arrives as an int"] = w10.exit_code("dslat") == 3

    # ── burst sharing ─────────────────────────────────────────────
    calls = []
    w5 = EventWatcher(FakeBackend([]), lambda: calls.append(1) or "SNAP")
    for i in range(20):
        w5._handle(json.dumps({"Action": "die", "Actor": {"Attributes": {
            "name": f"c{i}", "exitCode": "1"}}}))
    checks["a stack collapsing costs one stats call"] = len(calls) == 1
    checks["every container in the burst has evidence"] = all(
        w5.evidence(f"c{i}") == "SNAP" for i in range(20))

    # ── bounded ───────────────────────────────────────────────────
    w6 = EventWatcher(FakeBackend([]), lambda: "SNAP")
    for i in range(MAX_EVIDENCE + 50):
        w6._handle(json.dumps({"Action": "die", "Actor": {"Attributes": {
            "name": f"c{i}", "exitCode": "1"}}}))
    checks["a crash-looping host cannot grow this forever"] = (
        len(w6._evidence) <= MAX_EVIDENCE)

    # ── a failing snapshot must not kill the stream ───────────────
    def boom():
        raise RuntimeError("stats refused")

    w7 = EventWatcher(FakeBackend([]), boom, log=lambda m: None)
    w7._handle(DOCKER_DIE)
    checks["a refused stats call still records the death"] = (
        w7.evidence("dsev") == "")
    checks["a refused stats call does not raise"] = True

    # ── the real thing: a stream that ends, then reconnects ───────
    b = FakeBackend([DOCKER_DIE])
    w8 = EventWatcher(b, lambda: "LIVE", log=lambda m: None)
    import event_watcher as EW
    orig = EW.RECONNECT_MIN
    EW.RECONNECT_MIN = 0.1
    try:
        w8.start()
        deadline = time.time() + 8
        while b.starts < 2 and time.time() < deadline:
            time.sleep(0.1)
        checks["a dropped stream is reconnected"] = b.starts >= 2
        checks["evidence survives the reconnect"] = w8.evidence("dsev") == "LIVE"
    finally:
        EW.RECONNECT_MIN = orig
        w8.stop()

    # ── argv goes through the backend seam ────────────────────────
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    import container_backend as cb
    argv = cb.RemoteBackend("ssh://nas", name="nas").build(
        ["events", "--filter", "event=die"])
    # The whole point of routing through build(): a remote watcher that
    # dropped the endpoint flag would silently watch the WRONG machine.
    checks["remote watcher keeps the endpoint flag"] = argv[:3] == [
        "docker", "-H", "ssh://nas"]
    argv_p = cb.RemoteBackend("tcp://box", name="b", cli_binary="podman").build(
        ["events"])
    checks["podman remote uses --url, not -H"] = argv_p[:3] == [
        "podman", "--url", "tcp://box"]

    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'} {k}")
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
