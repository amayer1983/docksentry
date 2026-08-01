#!/usr/bin/env python3
"""Live watcher on the container runtime's event stream.

**Why this exists.** The monitor polls every 60 seconds. When it finds a
container dead it takes a memory snapshot to name whoever was eating the
RAM — but by then the culprit may have released it, been restarted, or be
the thing the operator killed in a panic. Evidence up to a minute stale.
@NotRetarded put it plainly in #2: how do you track *another* container
being the culprit? You cannot, from a poll.

`docker events` is a live stream. Measured on 2026-08-01: the `die` event
reached this process **0 ms** after the daemon stamped it, and a real
cgroup OOM produced an `oom` event 103 ms *before* the `die`. So a listener
can take the snapshot while the culprit still holds the memory.

**What this deliberately does not do: alert.** Alerting stays entirely with
the poller, which already handles debouncing, cooldowns and the persistent
event log. This watcher only *records evidence* that the poller picks up
when it builds its message. Two alerting paths would mean duplicate
messages and two sets of rules to keep in sync, which is the drift that
made the Web UI link to a different page than Telegram.

**Runtime differences, measured rather than assumed** (the `-H`/`--url`
lesson). Docker and Podman disagree on nearly every field:

    Docker : {"Action":"die","Actor":{"Attributes":{"name":…,"exitCode":"137"}},
              "timeNano":1785612907255524400}
    Podman : {"Status":"died","Name":…,"ContainerExitCode":137,
              "Time":"2026-08-01T21:36:59.258167461+02:00"}

Both accept `--filter event=die`, so the filter is common ground even
though Podman then reports the action as `died`. One more measured
difference worth knowing: rootless Podman emitted **no `oom` event at all**
for a container killed at its 20 MB ceiling, and `OOMKilled` was false in
`inspect` too — the same death Docker reported as an OOM twice over. That
is precisely why the memory snapshot hangs off every death rather than off
the OOM flag.
"""

import atexit
import json
import subprocess
import threading
import time

#: Deaths within this many seconds of each other share one snapshot. A host
#: losing a whole stack produces a burst, and `docker stats` costs ~2s —
#: taken per container that would serialise into a minute of work, by which
#: time the snapshot is exactly the stale thing this class exists to avoid.
BURST_WINDOW = 2.0

#: How long a recorded snapshot stays usable. The poller runs every 60s, so
#: it needs to still find evidence from a death that happened just after
#: the previous sweep. Beyond this it takes its own, which is the old
#: behaviour and still correct, only less precise.
EVIDENCE_TTL = 180.0

#: Bound on remembered deaths, so a crash-looping host cannot grow this
#: without limit. Far above any plausible burst.
MAX_EVIDENCE = 200

#: Backoff between reconnects. A daemon restart drops the stream; so does
#: `systemctl restart docker` during an upgrade. Neither is an error worth
#: shouting about, but reconnecting in a tight loop against a daemon that
#: is genuinely gone would spin a core.
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0


def parse_event(raw):
    """One event line → `(action, name, exit_code, oom)`, or None.

    Normalises Docker's and Podman's shapes; see the module docstring for
    the two payloads side by side. Returns None for anything unparseable
    rather than raising, because a single odd line must not take down a
    stream that is otherwise healthy.
    """
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None

    # Docker says Action, Podman says Status.
    action = d.get("Action") or d.get("status") or d.get("Status") or ""
    # Podman spells the death `died`; Docker `die`. Docker also appends a
    # scope to some actions (`exec_die: …`), so compare on the head.
    action = str(action).split(":")[0].strip()
    if action == "died":
        action = "die"
    if action not in ("die", "oom"):
        return None

    attrs = (d.get("Actor") or {}).get("Attributes") or {}
    name = attrs.get("name") or d.get("Name") or ""
    if not name:
        return None

    code = attrs.get("exitCode")
    if code is None:
        code = d.get("ContainerExitCode")
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None

    return action, name, code, action == "oom"


class EventWatcher:
    """Watches one host's event stream on a background thread.

    `snapshot_fn()` is called to capture the memory picture at the instant
    of a death; it is whatever the monitor already uses, so the two produce
    identical text.
    """

    def __init__(self, backend, snapshot_fn, *, log=print):
        self.backend = backend
        self.snapshot_fn = snapshot_fn
        self.log = log
        self.running = False
        self._thread = None
        self._proc = None
        self._lock = threading.Lock()
        #: name -> (recorded_at, snapshot, exit_code, oom)
        self._evidence = {}
        #: (taken_at, snapshot) — shared across a burst of simultaneous
        #: deaths so one stack collapsing costs one `stats`, not twenty.
        self._recent_snap = (0.0, "")

    # ── the half the monitor talks to ─────────────────────────────
    def evidence(self, name, *, ttl=EVIDENCE_TTL):
        """The snapshot recorded at this container's death, or "" if there
        is none fresh enough. Consumed — a second alert for the same death
        would otherwise quote a picture belonging to the first."""
        with self._lock:
            rec = self._evidence.get(name)
            if not rec:
                return ""
            recorded_at, snap, _code, _oom = rec
            if time.monotonic() - recorded_at > ttl:
                del self._evidence[name]
                return ""
            del self._evidence[name]
            return snap

    def saw_oom(self, name, *, ttl=EVIDENCE_TTL):
        """Whether the stream reported an OOM for this container.

        Worth asking separately because `inspect`'s `OOMKilled` is not
        reliable: measured false on rootless Podman for a container the
        kernel killed at its own 20 MB ceiling. Peeks rather than consumes.
        """
        with self._lock:
            rec = self._evidence.get(name)
            if not rec:
                return False
            recorded_at, _snap, _code, oom = rec
            return oom and (time.monotonic() - recorded_at <= ttl)

    # ── the thread ────────────────────────────────────────────────
    def start(self):
        if self.running:
            return
        self.running = True
        # A subprocess is NOT reaped when its parent dies, so a Docksentry
        # that exits without this leaves a `docker events` behind — once
        # per restart, and self-update restarts on every release. Inside
        # the container this is masked (PID 1 going down takes everything
        # with it); outside it, it accumulates.
        atexit.register(self.stop)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="event-watcher")
        self._thread.start()

    def stop(self):
        self.running = False
        proc = self._proc
        if proc:
            try:
                proc.terminate()
            except OSError:
                pass

    def _run(self):
        backoff = RECONNECT_MIN
        while self.running:
            try:
                ended_cleanly = self._stream_once()
            except (OSError, subprocess.SubprocessError) as e:
                self.log(f"Event watcher: stream failed ({e})")
                ended_cleanly = False
            if not self.running:
                break
            if ended_cleanly:
                # The daemon closed the stream on us — normal on a restart.
                # Reconnect promptly; this is the case the watcher exists
                # to survive.
                backoff = RECONNECT_MIN
            else:
                backoff = min(backoff * 2, RECONNECT_MAX)
            time.sleep(backoff)

    def _stream_once(self):
        """Run the CLI until the stream ends. True if it ended by itself."""
        args = ["events", "--filter", "event=die", "--filter", "event=oom",
                "--format", "{{json .}}"]
        cmd = self.backend.build(args) if hasattr(self.backend, "build") else args
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        try:
            for line in self._proc.stdout:
                if not self.running:
                    return True
                self._handle(line.strip())
        finally:
            proc, self._proc = self._proc, None
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        return True

    def _handle(self, raw):
        parsed = parse_event(raw)
        if not parsed:
            return
        action, name, code, oom = parsed
        # A clean exit is a normal ending — the poller stays quiet for those,
        # so capturing a snapshot for every `--rm` helper that finishes would
        # be pure cost. An OOM always counts, since Podman reports no exit
        # code alongside it.
        if action == "die" and not oom and (code is None or code == 0):
            return

        snap = self._snapshot()
        with self._lock:
            prev = self._evidence.get(name)
            # The `oom` event arrives ~100ms BEFORE the `die` on Docker.
            # Keep the OOM flag when the die overwrites it, or the more
            # informative of the two facts is lost to the later event.
            was_oom = bool(prev and prev[3])
            if len(self._evidence) >= MAX_EVIDENCE and name not in self._evidence:
                oldest = min(self._evidence, key=lambda k: self._evidence[k][0])
                del self._evidence[oldest]
            self._evidence[name] = (time.monotonic(), snap,
                                    code if code is not None else
                                    (prev[2] if prev else None),
                                    oom or was_oom)

    def _snapshot(self):
        """The memory picture, shared across a burst of deaths."""
        now = time.monotonic()
        with self._lock:
            taken_at, snap = self._recent_snap
            if now - taken_at < BURST_WINDOW:
                return snap
        try:
            snap = self.snapshot_fn() or ""
        except Exception as e:                     # never kill the stream
            self.log(f"Event watcher: snapshot failed ({e})")
            snap = ""
        with self._lock:
            self._recent_snap = (time.monotonic(), snap)
        return snap
